"""§2: illuminance targets & circadian shaping (open-loop tables)."""

from __future__ import annotations

import itertools

from custom_components.light_conductor.core import targets
from custom_components.light_conductor.core.model import Band, Profile, Role
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()


def _profile() -> Profile:
    return Profile(
        out_active_day={Band.PRIMARY: 0.8},
        out_active_evening={Band.PRIMARY: 0.3},
        out_background={Band.PRIMARY: 0.06},
        evening_output_cap=0.3,
    )


def test_active_interpolates_day_to_evening() -> None:
    """§2.1: ACTIVE interpolates day<->evening by the circadian factor E."""
    p = _profile()
    assert targets.role_outputs(p, Role.ACTIVE, 0.0, TUN)[Band.PRIMARY] == 0.8
    assert targets.role_outputs(p, Role.ACTIVE, 1.0, TUN)[Band.PRIMARY] == 0.3
    assert abs(targets.role_outputs(p, Role.ACTIVE, 0.5, TUN)[Band.PRIMARY] - 0.55) < 1e-9


def test_adjacent_is_fraction_of_active() -> None:
    """§1.5: ADJACENT is adjacent_fraction of the would-be ACTIVE target."""
    p = _profile()
    assert targets.role_outputs(p, Role.ADJACENT, 0.0, TUN)[Band.PRIMARY] == 0.4  # 0.8 * 0.5


def test_background_uses_open_loop_table() -> None:
    """§4.6: BACKGROUND reads the profile's out_background table."""
    p = _profile()
    assert targets.role_outputs(p, Role.BACKGROUND, 0.0, TUN)[Band.PRIMARY] == 0.06


def test_off_and_mode_roles_are_zero() -> None:
    p = _profile()
    assert targets.role_outputs(p, Role.OFF, 0.5, TUN) == dict.fromkeys(
        (Band.ACCENT, Band.PRIMARY, Band.BOOST), 0.0
    )


def test_evening_cap_is_continuous_in_w() -> None:
    """§2.4/D33: cap(W) = 1 - W·(1 - evening_output_cap) — non-binding at W = 0,
    the profile cap at W = 1, linear between; no step anywhere."""
    p = _profile()  # evening_output_cap 0.3
    hot = {Band.PRIMARY: 0.8}
    assert targets.apply_evening_cap(hot, 0.0, p, TUN) == {Band.PRIMARY: 0.8}
    assert targets.apply_evening_cap(hot, 1.0, p, TUN) == {Band.PRIMARY: 0.3}
    assert abs(targets.evening_cap(0.5, p) - 0.65) < 1e-12
    caps = [targets.evening_cap(k / 100, p) for k in range(101)]
    assert all(b <= a for a, b in itertools.pairwise(caps))
    assert max(a - b for a, b in itertools.pairwise(caps)) <= 0.7 / 100 + 1e-12


def test_cap_never_dims_an_interpolation_that_ends_at_or_below_it() -> None:
    """D33: for evening tier <= cap (every live room) the ACTIVE interpolation
    is always at or under cap(W) — the cap only trims what rides above it."""
    p = Profile(
        out_active_day={Band.PRIMARY: 1.0},
        out_active_evening={Band.PRIMARY: 0.25},
        evening_output_cap=0.25,
    )
    for k in range(101):
        w = k / 100
        tier = targets.role_outputs(p, Role.ACTIVE, w, TUN)[Band.PRIMARY]
        assert tier <= targets.evening_cap(w, p) + 1e-12


def test_peak_output() -> None:
    assert targets.peak_output({Band.ACCENT: 0.2, Band.PRIMARY: 0.5}) == 0.5
    assert targets.peak_output({}) == 0.0


# --- §4.5 evening boost output (D26) --------------------------------------


def _boost_profile(value: float | None) -> Profile:
    return Profile(out_active_day={Band.BOOST: 0.6}, boost_evening_output=value)


def test_boost_evening_value_is_active_only_opt_in_and_gain_scaled() -> None:
    """§4.5/D26: only an ACTIVE room whose profile sets one gets an evening
    value; it is scaled by the master gain (0 with the master off, §7.2)."""
    assert targets.boost_evening_value(Role.ACTIVE, _boost_profile(0.35), 1.0) == 0.35
    assert abs(targets.boost_evening_value(Role.ACTIVE, _boost_profile(0.35), 0.5) - 0.175) < 1e-9
    assert targets.boost_evening_value(Role.ACTIVE, _boost_profile(0.35), 0.0) == 0.0
    for role in (Role.ADJACENT, Role.BACKGROUND, Role.TV, Role.NIGHT_PATH):
        assert targets.boost_evening_value(role, _boost_profile(0.35), 1.0) is None
    assert targets.boost_evening_value(Role.ACTIVE, _boost_profile(None), 1.0) is None


def test_boost_weight_fades_across_a_window_ending_at_boost_evening_max() -> None:
    """§4.5/D33: β is 1 below boost_evening_max - BOOST_FADE, 0 at and above
    boost_evening_max (exactly beta.19's lockout there), linear between."""
    top = TUN.boost_evening_max
    lo = top - targets.BOOST_FADE
    assert targets.boost_weight(0.0, TUN) == 1.0
    assert targets.boost_weight(lo, TUN) == 1.0
    assert abs(targets.boost_weight((lo + top) / 2, TUN) - 0.5) < 1e-12
    assert targets.boost_weight(top, TUN) == 0.0
    assert targets.boost_weight(1.0, TUN) == 0.0


# --- §4.7 daylight-aware open-loop ---------------------------------------


def test_daylight_factor_scales_with_natural_light() -> None:
    """§4.7: D = clamp(1 - N̂/daylight_full, min, 1). Default daylight_full=200."""
    assert targets.daylight_factor(0.0, TUN) == 1.0  # dark → full output
    assert abs(targets.daylight_factor(150.0, TUN) - 0.25) < 1e-9  # 1 - 150/200
    assert targets.daylight_factor(100.0, TUN) == 0.5


def test_daylight_factor_floors_at_min_and_clamps_high() -> None:
    """§4.7: N̂ ≥ daylight_full floors at daylight_min_factor; clamps to [min, 1]."""
    from dataclasses import replace

    assert targets.daylight_factor(200.0, TUN) == 0.0  # floor (default min 0.0)
    assert targets.daylight_factor(500.0, TUN) == 0.0  # never negative
    floored = replace(TUN, daylight_min_factor=0.2)
    assert targets.daylight_factor(1000.0, floored) == 0.2  # honours the floor


def test_apply_daylight_scales_every_band() -> None:
    d = targets.apply_daylight({Band.PRIMARY: 0.8, Band.ACCENT: 0.4}, 150.0, TUN)
    assert abs(d[Band.PRIMARY] - 0.2) < 1e-9  # 0.8 * 0.25
    assert abs(d[Band.ACCENT] - 0.1) < 1e-9  # 0.4 * 0.25


def test_daylight_disabled_when_full_nonpositive() -> None:
    from dataclasses import replace

    off = replace(TUN, daylight_full=0.0)
    assert targets.daylight_factor(150.0, off) == 1.0  # guarded, no zero-division


def test_per_room_daylight_full_overrides_the_global() -> None:
    """§4.7/D26: a room whose sensor reads 40-60 lx at noon (kjøkken) cannot
    share a 200 lx reference with one reading 300-450 lx (spisebord)."""
    assert abs(targets.daylight_factor(30.0, TUN, 60.0) - 0.5) < 1e-9  # 1 - 30/60
    assert targets.daylight_factor(60.0, TUN, 60.0) == 0.0  # at the room's full
    # Blank / zero falls back to the global (200 lx).
    assert abs(targets.daylight_factor(100.0, TUN, None) - 0.5) < 1e-9
    assert abs(targets.daylight_factor(100.0, TUN, 0.0) - 0.5) < 1e-9
    scaled = targets.apply_daylight({Band.PRIMARY: 0.8}, 30.0, TUN, 60.0)
    assert abs(scaled[Band.PRIMARY] - 0.4) < 1e-9
