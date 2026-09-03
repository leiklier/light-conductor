"""Monotone-band fade corridor unit tests for the echo ledger (§8.4, D25)."""

from __future__ import annotations

import custom_components.light_conductor.controller as ctrl


def _ledger(monkeypatch, t0: float = 1000.0):
    clock = [t0]
    monkeypatch.setattr(ctrl, "_monotonic", lambda: clock[0])
    return ctrl.EchoLedger(ttl=10.0), clock


def test_corridor_accepts_any_value_on_the_band(monkeypatch) -> None:
    """D25: while the corridor lives, any level between frm and to is an echo —
    the fork's deadline-paced ramp skips levels and the mesh reorders/coalesces
    reports, so only the VALUE is trustworthy."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)  # start = 1000
    for dt, level in ((0.0, 0.0), (1.0, 0.55), (2.5, 0.1), (4.0, 0.33), (5.0, 0.6)):
        clock[0] = 1000.0 + dt
        assert led.consume("x", level, None) is True


def test_late_intermediate_echo_is_accepted(monkeypatch) -> None:
    """The kjøkken incident: benkebelysning's FIRST-step echo (0.016) arrived
    ~2 s late — outside the old moving-front band [0.033, 0.093] — and latched
    the whole room 2 s after its own turn-on. On the band ⇒ echo."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("kj", 0.0, 0.3, 3.0)
    clock[0] = 1002.0  # a first-step value arriving seconds late
    assert led.consume("kj", 0.016, None) is True


def test_value_off_the_band_latches_during_the_corridor(monkeypatch) -> None:
    """A dial that leaves the segment between start and goal is foreign at once."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.8, 0.2, 5.0)
    clock[0] = 1002.5
    assert led.consume("x", 0.95, None) is False  # above the down-fade → foreign
    led2, clock2 = _ledger(monkeypatch, t0=2000.0)
    led2.record_envelope("y", 0.0, 0.6, 5.0)
    clock2[0] = 2002.0
    assert led2.consume("y", 0.9, None) is False  # beyond the goal → foreign


def test_corridor_fade_to_off(monkeypatch) -> None:
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.5, 0.0, 5.0)
    for dt, level in ((0.0, 0.5), (2.5, 0.4), (5.0, 0.0)):
        clock[0] = 1000.0 + dt
        assert led.consume("x", level, None) is True


def test_post_deadline_foreign_value_latches(monkeypatch) -> None:
    """After the deadline only the final-value echo (± tol) matches — the first
    post-deadline report at a foreign value latches (D25)."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)  # deadline 1010 (margin 5); echo TTL 1020
    clock[0] = 1011.0
    assert led.consume("x", 0.6, None) is True  # late completion near target → echo
    assert led.consume("x", 0.3, None) is False  # mid-band, but the corridor is dead


def test_envelope_margin_covers_a_congested_mesh_tail(monkeypatch) -> None:
    """D25: the margin floor is 5 s — a room transition serializes several
    channels' writes behind one another at ~7 writes/s."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.5, 0.0, 4.0)  # sleep_fade; deadline = 1000+4+5
    clock[0] = 1008.5
    assert led.consume("x", 0.25, None) is True  # still inside the corridor


def test_long_fade_final_echo_outlives_corridor(monkeypatch) -> None:
    """A sleep_fade/night_fade-length ramp (4-10 s) must not expire its
    final-value echo before the corridor deadline: a completion report just
    past the deadline is an echo, not a spurious override."""
    clock = [1000.0]
    monkeypatch.setattr(ctrl, "_monotonic", lambda: clock[0])
    led = ctrl.EchoLedger(ttl=3.0)  # the PRODUCTION TTL, not the test default
    led.record_envelope("x", 0.5, 0.0, 8.0)  # deadline = 1000 + 8 + 5 = 1013
    clock[0] = 1013.5  # corridor expired; plain 3 s TTL would have died at 1003
    assert led.consume("x", 0.0, None) is True


def test_envelope_records_a_ct_echo_for_a_combined_write(monkeypatch) -> None:
    """D25: an off lamp takes CT + brightness in one call, so the corridor write
    records the CT echo too."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.4, 2.0, ct=2700)
    clock[0] = 1009.0  # past the corridor, inside the echo TTL
    assert led.consume("x", None, 2700) is True


def test_corridor_exposes_itself_for_logging(monkeypatch) -> None:
    """The foreign-change log line reports the live corridor; an expired one is
    reported as absent."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.49, 0.0, 4.0)
    env = led.corridor("x")
    assert env is not None and (env.frm, env.to) == (0.49, 0.0)
    clock[0] = 1100.0
    assert led.corridor("x") is None
