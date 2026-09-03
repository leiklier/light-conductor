"""§2: illuminance targets & circadian shaping (open-loop tables)."""

from __future__ import annotations

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


def test_evening_cap_clamps_high_e() -> None:
    """§2.4: E >= threshold clamps normalized output to evening_output_cap."""
    p = _profile()
    hot = {Band.PRIMARY: 0.8}
    assert targets.apply_evening_cap(hot, 0.4, p, TUN) == {Band.PRIMARY: 0.8}  # below threshold
    assert targets.apply_evening_cap(hot, 0.6, p, TUN) == {Band.PRIMARY: 0.3}  # capped


def test_peak_output() -> None:
    assert targets.peak_output({Band.ACCENT: 0.2, Band.PRIMARY: 0.5}) == 0.5
    assert targets.peak_output({}) == 0.0


# --- §4.5 evening boost output (D26) --------------------------------------


def _boost_profile(value: float | None) -> Profile:
    return Profile(out_active_day={Band.BOOST: 0.6}, boost_evening_output=value)


def test_boost_evening_output_drives_an_active_room_in_the_lockout() -> None:
    """§4.5/D26: past boost_evening_max an ACTIVE room takes the explicit value
    instead of the lockout (the bench strip the user turned on at 22:04)."""
    outputs, unlocked = targets.apply_boost_evening_output(
        {Band.PRIMARY: 0.3, Band.BOOST: 0.6}, 1.0, Role.ACTIVE, _boost_profile(0.35), TUN
    )
    assert unlocked and outputs[Band.BOOST] == 0.35
    assert outputs[Band.PRIMARY] == 0.3  # other bands untouched


def test_boost_evening_output_only_inside_the_lockout_window() -> None:
    """Below boost_evening_max the band is not locked out at all — nothing to do."""
    outputs, unlocked = targets.apply_boost_evening_output(
        {Band.BOOST: 0.6}, 0.4, Role.ACTIVE, _boost_profile(0.35), TUN
    )
    assert not unlocked and outputs[Band.BOOST] == 0.6


def test_boost_evening_output_is_active_only_and_opt_in() -> None:
    """§4.5/D26: ADJACENT/BACKGROUND stay locked out, and an unset profile keeps
    the plain lockout (D6/Q4 — the default does not change)."""
    for role in (Role.ADJACENT, Role.BACKGROUND):
        _out, unlocked = targets.apply_boost_evening_output(
            {Band.BOOST: 0.6}, 1.0, role, _boost_profile(0.35), TUN
        )
        assert not unlocked
    _out, unlocked = targets.apply_boost_evening_output(
        {Band.BOOST: 0.6}, 1.0, Role.ACTIVE, _boost_profile(None), TUN
    )
    assert not unlocked


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
