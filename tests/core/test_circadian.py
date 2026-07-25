"""§2.3: the circadian factor E = max(E_sun, E_clock)."""

from __future__ import annotations

from custom_components.light_conductor.core import circadian
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import at

TUN = Tunables()


def test_sun_ramp_bounds() -> None:
    """§2.3: E_sun ramps 0->1 as elevation falls +10deg -> -4deg."""
    assert circadian.e_sun(10.0, TUN) == 0.0
    assert circadian.e_sun(-4.0, TUN) == 1.0
    assert circadian.e_sun(20.0, TUN) == 0.0  # clamped above sun_high
    assert circadian.e_sun(3.0, TUN) == 0.5  # midpoint of +10..-4
    assert circadian.e_sun(None, TUN) == 0.0  # unknown fails safe to day


def test_clock_ramp_day_and_evening() -> None:
    """§2.3: clock term is 0 by day, ramps 20:00->22:30, 1 overnight."""
    assert circadian.e_clock(at(1, 12, 0), TUN) == 0.0  # full day
    assert circadian.e_clock(at(1, 20, 0), TUN) == 0.0  # evening start
    assert circadian.e_clock(at(1, 21, 15), TUN) == 0.5  # halfway to full
    assert circadian.e_clock(at(1, 22, 30), TUN) == 1.0  # full evening
    assert circadian.e_clock(at(1, 1, 0), TUN) == 1.0  # small hours, still evening


def test_clock_morning_ramp_down() -> None:
    """§2.3: clock term ramps back to 0 between 06:00 and 07:30."""
    assert circadian.e_clock(at(1, 6, 0), TUN) == 1.0
    assert circadian.e_clock(at(1, 6, 45), TUN) == 0.5
    assert circadian.e_clock(at(1, 7, 30), TUN) == 0.0


def test_factor_is_max_of_terms() -> None:
    """§2.3: Nordic-summer late sun still dims via the clock term."""
    # Sun still up (E_sun 0) but 22:30 clock -> E = 1.
    assert circadian.factor(15.0, at(1, 22, 30), TUN) == 1.0
    # Winter afternoon: sun low (E_sun high), midday clock 0.
    assert circadian.factor(-4.0, at(1, 15, 0), TUN) == 1.0


def test_ramp_degenerate_bounds() -> None:
    """A zero-width ramp is a hard step (defensive)."""
    assert circadian._ramp(5.0, 3.0, 3.0) == 1.0
    assert circadian._ramp(2.0, 3.0, 3.0) == 0.0


def test_is_evening_threshold() -> None:
    """§2.4/1.7: evening gate at evening_cap_threshold."""
    assert not circadian.is_evening(20.0, at(1, 12, 0), TUN)
    assert circadian.is_evening(None, at(1, 21, 15), TUN)  # E_clock 0.5 == threshold
