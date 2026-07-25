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
