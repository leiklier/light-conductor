"""Monotone-band fade corridor unit tests for the echo ledger (§8.4, D25)."""

from __future__ import annotations

import custom_components.light_conductor.controller as ctrl


def _ledger(monkeypatch, t0: float = 1000.0):
    clock = [t0]
    monkeypatch.setattr(ctrl, "_monotonic", lambda: clock[0])
    return ctrl.EchoLedger(ttl=10.0), clock


def test_corridor_accepts_advancing_values_on_the_band(monkeypatch) -> None:
    """D25: while the corridor lives, a level between frm and to that does not
    regress is an echo — the fork's deadline-paced ramp skips levels and the
    mesh reorders/coalesces reports, so the timing carries no information."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)  # start = 1000
    for dt, level in ((0.0, 0.0), (1.0, 0.1), (2.5, 0.33), (4.0, 0.55), (5.0, 0.6)):
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


def test_envelope_records_no_ct_wildcard(monkeypatch) -> None:
    """A corridor write records NO CT echo: a CT-only echo has no level test, so
    for the whole (long) corridor TTL it would consume any report carrying that
    kelvin — and a CT-capable lamp repeats its CT on every state change, so a
    wall dial to full would be swallowed."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.4, 2.0)
    clock[0] = 1008.0  # past the corridor deadline (1000+2+5), inside the echo TTL
    assert led.consume("x", 1.0, 2700) is False  # dial to full, our CT ⇒ foreign
    assert led.consume("x", 0.4, 2700) is True  # the final-value echo still works


def test_corridor_high_water_mark_catches_a_mid_fade_back_dial(monkeypatch) -> None:
    """S1: a fade only ever approaches its goal. A report materially FURTHER
    from the goal than the best progress so far is a hand on the dial."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)
    clock[0] = 1002.0
    assert led.consume("x", 0.4, None) is True  # progress: |0.4-0.6| = 0.2
    clock[0] = 1003.0
    assert led.consume("x", 0.2, None) is False  # back-dial: 0.4 > 0.2 ⇒ foreign
    # ...and the accepted progress is unaffected by the rejected report.
    clock[0] = 1004.0
    assert led.consume("x", 0.58, None) is True


def test_corridor_does_not_absorb_a_dial_after_the_fade_completed(monkeypatch) -> None:
    """S1: the band stays live for the overshoot margin AFTER the lamp arrived;
    without the high-water mark a dial-down in those seconds was consumed — and
    permanently, since an absorbed value is already the entity's HA state and
    the ~3 min poll re-reports it as state_reported, never state_changed."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 4.0)  # deadline 1009
    clock[0] = 1004.0
    assert led.consume("x", 0.6, None) is True  # arrived: best = 0.0
    clock[0] = 1006.0  # corridor still live (margin)
    assert led.consume("x", 0.3, None) is False  # the dial ⇒ foreign


def test_retarget_mid_fade_unions_the_band(monkeypatch) -> None:
    """S1: re-targeting mid-fade (a TV resolution landing during a turn-on)
    reads ``frm`` from HA's LAGGING state, so the new segment alone excludes
    where the lamp actually is. The band is the union of both."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.0, 0.6, 5.0)
    clock[0] = 1002.0
    assert led.consume("x", 0.25, None) is True
    # HA still reports 0.05 while the lamp is really near 0.45; re-target to 0.2.
    clock[0] = 1002.5
    led.record_envelope("x", 0.05, 0.2, 3.0)
    env = led.corridor("x")
    assert (env.lo, env.hi) == (0.0, 0.6)  # union, not [0.05, 0.2]
    assert env.best is None  # progress is measured toward the NEW goal
    clock[0] = 1003.0
    assert led.consume("x", 0.45, None) is True  # the lamp's real position
    clock[0] = 1004.0
    assert led.consume("x", 0.2, None) is True  # and on down to the new goal


def test_corridor_exposes_itself_for_logging(monkeypatch) -> None:
    """The foreign-change log line reports the live corridor; an expired one is
    reported as absent."""
    led, clock = _ledger(monkeypatch)
    led.record_envelope("x", 0.49, 0.0, 4.0)
    env = led.corridor("x")
    assert env is not None and (env.frm, env.to) == (0.49, 0.0)
    clock[0] = 1100.0
    assert led.corridor("x") is None
