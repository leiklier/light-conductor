"""Fade-front corridor unit tests for the echo ledger (§8.4, F1)."""

from __future__ import annotations

import custom_components.light_conductor.controller as ctrl


def _ledger(monkeypatch, t0: float = 1000.0):
    clock = [t0]
    monkeypatch.setattr(ctrl, "_monotonic", lambda: clock[0])
    return ctrl.EchoLedger(ttl=10.0), clock


def test_corridor_rising_front_tracking(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)  # start = 1000
    for dt in (0.0, 1.0, 2.5, 4.0, 5.0):
        clock[0] = 1000.0 + dt
        front = 0.6 * min(1.0, dt / 5.0)
        assert led.consume("x", front, None) is True  # tracks the front → echo


def test_corridor_full_range_stuck_dial_latches(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)
    # A dial to 0.3 that STICKS: an echo while the front is near it...
    clock[0] = 1002.5  # front == 0.3
    assert led.consume("x", 0.3, None) is True
    # ...but foreign once the full-range front has swept past it.
    clock[0] = 1005.0  # front == 0.6, front(t-slack) == 0.42 > 0.3
    assert led.consume("x", 0.3, None) is False


def test_corridor_downward_fade(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.8, 0.2, 5.0)
    for dt in (0.0, 2.5, 5.0):
        clock[0] = 1000.0 + dt
        front = 0.8 + (0.2 - 0.8) * min(1.0, dt / 5.0)
        assert led.consume("x", front, None) is True
    clock[0] = 1002.5
    assert led.consume("x", 0.95, None) is False  # above the down-fade → foreign


def test_corridor_fade_to_off(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.5, 0.0, 5.0)
    for dt in (0.0, 2.5, 5.0):
        clock[0] = 1000.0 + dt
        front = 0.5 * (1.0 - min(1.0, dt / 5.0))
        assert led.consume("x", front, None) is True


def test_corridor_late_tail_near_target_echo_far_foreign(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)  # corridor deadline 1007.5; final echo TTL 1010
    clock[0] = 1008.0  # past the corridor, within the final-value echo TTL
    assert led.consume("x", 0.6, None) is True  # late completion near target → echo
    assert led.consume("x", 0.1, None) is False  # far from target → foreign
