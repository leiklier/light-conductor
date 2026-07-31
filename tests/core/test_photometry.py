"""§4: photometry curves and open-loop allocation (minus §4.4 calibration)."""

from __future__ import annotations

from math import isclose

from custom_components.light_conductor.core.model import Band, ChannelConfig, Profile, RoomConfig
from custom_components.light_conductor.core.photometry import Curve, RoomPhotometry, allocate
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()


def test_default_curve_is_square_law() -> None:
    """§4.2: uncalibrated channels default to b**2, invertible for slew (4.3)."""
    c = Curve(None)
    assert c.flux(0.5) == 0.25
    assert c.flux(1.0) == 1.0
    assert isclose(c.command(0.25), 0.5)


def test_piecewise_curve_interpolates_both_ways() -> None:
    """§4.2: a config curve replaces the default with measured points."""
    c = Curve(((0.0, 0.0), (0.5, 0.1), (1.0, 1.0)))
    assert c.flux(0.5) == 0.1
    assert isclose(c.flux(0.75), 0.55)  # halfway 0.1..1.0
    assert isclose(c.command(0.1), 0.5)


def test_photometry_starts_uncalibrated() -> None:
    """§4.4 seam: rooms run the default curve, calibrated is False until the sweep."""
    room = RoomConfig("r", (ChannelConfig("c"),), Profile())
    photo = RoomPhotometry(room)
    assert photo.calibrated is False
    assert photo.flux("c", 0.5) == 0.25
    assert isclose(photo.command_for_flux("c", 0.25), 0.5)


def test_piecewise_curve_clamps_out_of_range() -> None:
    """§4.2: the curve saturates outside [b0, b1] instead of extrapolating."""
    c = Curve(((0.2, 0.1), (0.8, 0.9)))
    assert c.flux(0.0) == 0.1  # below first point
    assert c.flux(1.0) == 0.9  # above last point
    assert c.command(0.0) == 0.2  # inverse also clamps


def test_photometry_reports_channel_gain() -> None:
    """§3.1 seam: per-channel gain is exposed (1.0 by default)."""
    room = RoomConfig("r", (ChannelConfig("c", gain=1.5),), Profile())
    assert RoomPhotometry(room).gain("c") == 1.5


def _channels() -> tuple[ChannelConfig, ...]:
    return (
        ChannelConfig("acc", band=Band.ACCENT),
        ChannelConfig("prim", band=Band.PRIMARY),
        ChannelConfig("boost", band=Band.BOOST),
    )


def test_allocation_maps_bands_to_channels() -> None:
    """§4.6: each channel in a band takes that band's tier output."""
    out = allocate(_channels(), {Band.ACCENT: 0.4, Band.PRIMARY: 0.5, Band.BOOST: 0.6}, 0.0, TUN)
    assert out == {"acc": 0.4, "prim": 0.5, "boost": 0.6}


def test_boost_band_evening_lockout() -> None:
    """§4.5: boost is gated off once E >= boost_evening_max (benke evening off)."""
    bands = {Band.ACCENT: 0.4, Band.PRIMARY: 0.5, Band.BOOST: 0.6}
    lit = allocate(_channels(), bands, 0.49, TUN)
    assert lit["boost"] == 0.6  # just below the gate
    off = allocate(_channels(), bands, 0.5, TUN)
    assert off["boost"] == 0.0  # gated; accent + primary survive
    assert off["acc"] == 0.4 and off["prim"] == 0.5


def test_allocation_clamps_to_unit() -> None:
    out = allocate(_channels(), {Band.ACCENT: 1.5}, 0.0, TUN)
    assert out["acc"] == 1.0


def test_within_band_weight_sharing() -> None:
    """§4.5: within a band, channels share by weight (relative to the heaviest),
    never by sensor gain — equal weights leave every channel at the band output."""
    channels = (
        ChannelConfig("a", band=Band.ACCENT, weight=1.0, gain=50.0),  # huge gain, equal weight
        ChannelConfig("b", band=Band.ACCENT, weight=0.5, gain=1.0),
    )
    out = allocate(channels, {Band.ACCENT: 0.6}, 0.0, TUN)
    assert out["a"] == 0.6  # heaviest -> full band output (gain is ignored)
    assert out["b"] == 0.3  # half the weight -> half


def test_response_mapping_benke_like_channel() -> None:
    """§4.5: an affine response mapping (slope 0.8, offset -0.5) reshapes a lone
    boost channel - the normalized legacy benke curve 0.8*base - 0.5."""
    ch = (ChannelConfig("benke", band=Band.BOOST, response_slope=0.8, response_offset=-0.5),)
    assert isclose(allocate(ch, {Band.BOOST: 1.0}, 0.0, TUN)["benke"], 0.3)
    assert allocate(ch, {Band.BOOST: 0.6}, 0.0, TUN)["benke"] == 0.0  # 0.8*0.6-0.5<0 clamps
    assert allocate(ch, {Band.BOOST: 0.45}, 0.0, TUN)["benke"] == 0.0
    assert allocate(ch, {Band.BOOST: 0.0}, 0.0, TUN)["benke"] == 0.0  # zero stays zero


def test_response_mapping_positive_offset_never_lights_off_band() -> None:
    """§4.5: a positive offset lifts a lit channel but a zero band output stays 0
    (the mapping applies only when out > 0), and an overshoot clamps to 1.

    (slope 0.8, offset +0.2 gives the brief's 0.28 at band 0.1; slope 1 there
    would give 0.3 - the brief's "slope 1" label is an arithmetic slip.)"""
    ch = (ChannelConfig("c", band=Band.PRIMARY, response_slope=0.8, response_offset=0.2),)
    assert allocate(ch, {Band.PRIMARY: 0.0}, 0.0, TUN)["c"] == 0.0  # zero stays zero
    assert isclose(allocate(ch, {Band.PRIMARY: 0.1}, 0.0, TUN)["c"], 0.28)
    assert isclose(allocate(ch, {Band.PRIMARY: 1.0}, 0.0, TUN)["c"], 1.0)  # 0.8+0.2
    # An affine mapping that overshoots unit is clamped to 1.
    hot = (ChannelConfig("h", band=Band.PRIMARY, response_slope=1.0, response_offset=0.5),)
    assert allocate(hot, {Band.PRIMARY: 0.8}, 0.0, TUN)["h"] == 1.0  # 1.3 clamps


def test_response_mapping_defaults_are_a_no_op() -> None:
    """§4.5: default slope/offset (1.0/0.0) yield a byte-identical result on a
    mixed 3-band room — with and without the fields explicitly set."""
    plain = (
        ChannelConfig("acc", band=Band.ACCENT, weight=0.5),
        ChannelConfig("prim", band=Band.PRIMARY),
        ChannelConfig("boost", band=Band.BOOST),
    )
    explicit = tuple(
        ChannelConfig(
            c.channel_id, band=c.band, weight=c.weight, response_slope=1.0, response_offset=0.0
        )
        for c in plain
    )
    bands = {Band.ACCENT: 0.6, Band.PRIMARY: 0.5, Band.BOOST: 0.7}
    assert allocate(plain, bands, 0.0, TUN) == allocate(explicit, bands, 0.0, TUN)


def test_response_mapping_after_evening_lockout() -> None:
    """§4.5: the evening lockout zeroes a boost channel BEFORE the mapping runs,
    so even a positive offset cannot resurrect it past boost_evening_max."""
    ch = (ChannelConfig("boost", band=Band.BOOST, response_slope=1.0, response_offset=0.5),)
    lit = allocate(ch, {Band.BOOST: 0.6}, 0.0, TUN)
    assert isclose(lit["boost"], 1.0)  # 0.6+0.5 clamps high while lit
    off = allocate(ch, {Band.BOOST: 0.6}, TUN.boost_evening_max, TUN)
    assert off["boost"] == 0.0  # locked out -> out=0 -> mapping skipped
