"""Room calibration sweep (ENGINE_SPEC §4.4).

An engine-driven state machine that measures each channel's lux gain ``g_i``
and relative-flux curve ``f_i`` at the sensor, then commits transactionally
(all-or-nothing, like presence-conductor 3.3). A :class:`~.events.StartCalibration`
is rejected unless sun elevation < ``night_prior_deg`` and the room's lux is
stable. The sweep turns all channels off, settles, then drives each channel
alone through ``calibration_levels`` dwelling ``calibration_dwell`` per level,
collecting :class:`~.events.LuxReport` samples.

Room control is suspended while its own sweep runs (other rooms unaffected).
Any abort trigger — a foreign change, a sleep/away hard-off, the sensor going
stale, or a level yielding no samples — restores the prior calibration AND the
prior light state exactly. The engine owns the photometry, so :func:`step`
returns an :class:`Outcome` and the engine applies/rolls back the calibration
and re-lights the room.

Feature-module discipline: imports only :mod:`model`, :mod:`tunables`,
:mod:`plan`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import (
    CalibrationSession,
    CalPhase,
    EngineState,
    RoomCalibration,
    RoomConfig,
    RoomState,
)
from .plan import Plan
from .tunables import Tunables


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one :func:`step` (engine acts on ``done``)."""

    done: bool
    ok: bool = False
    reason: str = ""
    calibration: RoomCalibration | None = None
    coverage: tuple[tuple[str, float], ...] = ()
    #: Restore the prior light state (set on any abort, rule 4.4 transaction).
    restore_light: bool = False


_ONGOING = Outcome(done=False)

#: A level completes as soon as this many post-blank samples have settled (rule
#: 4.4 early advance) — a fast sensor sweeps quickly; a slow, delta-filtered one
#: still runs to ``calibration_dwell``.
CAL_MIN_SAMPLES_PER_LEVEL = 1

#: A channel commits from its sampled points once at least this many of its
#: levels were captured (rule 4.4 partial coverage); the room rejects
#: ``missing_samples`` if any channel falls short.
CAL_MIN_POINTS = 3

_CAL_EPS = 1e-9


# ---------------------------------------------------------------------------
# Start gating (rule 4.4)
# ---------------------------------------------------------------------------


def can_start(
    rs: RoomState, room: RoomConfig, state: EngineState, now: datetime, tun: Tunables
) -> str:
    """Empty string if a sweep may start, else the rejection reason (rule 4.4)."""
    if not room.has_lux_sensor:
        return "no_lux_sensor"
    if rs.cal is not None:
        return "already_calibrating"
    if state.sleep or state.vacation or state.anyone_home is False:
        return "mode_off"
    if state.sun_elevation is None or state.sun_elevation >= tun.night_prior_deg:
        return "sun_too_high"
    est = rs.est
    if est.last_report_at is None or (now - est.last_report_at).total_seconds() > tun.lux_stale:
        return "sensor_stale"
    if est.l_filt is None:
        return "lux_unstable"
    return ""


def begin(
    rs: RoomState,
    room: RoomConfig,
    prior_cal: RoomCalibration,
    now: datetime,
    plan: Plan,
    tun: Tunables,
) -> None:
    """Open a sweep: snapshot the world, turn everything off, settle (rule 4.4)."""
    prior_light = {cid: (cs.commanded_b, cs.commanded_ct, cs.on) for cid, cs in rs.channels.items()}
    rs.cal = CalibrationSession(
        channel_order=tuple(c.channel_id for c in room.channels),
        phase=CalPhase.SETTLE_OFF,
        deadline=now + timedelta(seconds=tun.calibration_dwell),
        prior_cal=prior_cal,
        prior_light=prior_light,
    )
    _drive(rs, plan, {}, now)  # all channels off


# ---------------------------------------------------------------------------
# Sample collection
# ---------------------------------------------------------------------------


def ingest_lux(rs: RoomState, lux: float | None, now: datetime, tun: Tunables) -> None:
    """Collect a lux sample for the current dwell (rule 4.4).

    Samples within ``write_blank`` of entering the level carry the switching
    transient and are skipped; the settled tail is what calibration reads.
    """
    cal = rs.cal
    if cal is None or lux is None or cal.deadline is None:
        return
    rs.est.last_report_at = now  # keep staleness tracking alive through the sweep
    phase_start = cal.deadline - timedelta(seconds=tun.calibration_dwell)
    if (now - phase_start).total_seconds() < min(tun.write_blank, tun.calibration_dwell / 2.0):
        return
    cal.samples.append(max(0.0, lux))


# ---------------------------------------------------------------------------
# Sweep stepping (rule 4.4)
# ---------------------------------------------------------------------------


def abort(rs: RoomState, plan: Plan, reason: str, now: datetime, tun: Tunables) -> Outcome:
    """Tear down the sweep and restore prior light state (rule 4.4 transaction)."""
    cal = rs.cal
    assert cal is not None
    coverage = _coverage(cal, tun.calibration_levels)
    _restore_light(rs, plan, now)
    rs.cal = None
    return Outcome(done=True, ok=False, reason=reason, coverage=coverage, restore_light=True)


def step(rs: RoomState, state: EngineState, now: datetime, plan: Plan, tun: Tunables) -> Outcome:
    """Advance the sweep; returns :data:`_ONGOING` or a terminal :class:`Outcome`."""
    cal = rs.cal
    assert cal is not None and cal.deadline is not None

    # Abort triggers (rule 4.4): a foreign change, a mode hard-off, or the
    # sensor going stale.
    if cal.foreign:
        return abort(rs, plan, "foreign_change", now, tun)
    if state.sleep or state.vacation or state.anyone_home is False:
        return abort(rs, plan, "mode_off", now, tun)
    est = rs.est
    if est.last_report_at is None or (now - est.last_report_at).total_seconds() > tun.lux_stale:
        return abort(rs, plan, "sensor_stale", now, tun)

    # Early advance (rule 4.4): a level completes as soon as
    # CAL_MIN_SAMPLES_PER_LEVEL post-blank samples have settled (the blank
    # window in ingest_lux already guarantees the minimum settle), otherwise at
    # the full calibration_dwell for a slow, delta-filtered sensor.
    if now < cal.deadline and len(cal.samples) < CAL_MIN_SAMPLES_PER_LEVEL:
        plan.review_at(cal.deadline)
        return _ONGOING

    settled = cal.samples[-1] if cal.samples else None

    if cal.phase is CalPhase.SETTLE_OFF:
        # The off baseline is essential (every contribution subtracts it); a
        # settle window that yields no dark sample still aborts (rule 4.4).
        if settled is None:
            return abort(rs, plan, "missing_samples", now, tun)
        cal.off_baseline = settled
        return _enter_level(rs, 0, 0, now, plan, tun)

    # DWELL: record this (channel, level) if a sample landed; a level whose
    # window elapsed with no sample is simply skipped (partial coverage, 4.4).
    cid = cal.channel_order[cal.channel_index]
    level = tun.calibration_levels[cal.level_index]
    if settled is not None:
        cal.measurements.setdefault(cid, {})[level] = settled

    nxt_level = cal.level_index + 1
    if nxt_level < len(tun.calibration_levels):
        return _enter_level(rs, cal.channel_index, nxt_level, now, plan, tun)
    nxt_channel = cal.channel_index + 1
    if nxt_channel < len(cal.channel_order):
        return _enter_level(rs, nxt_channel, 0, now, plan, tun)
    return _commit(rs, plan, now, tun)


def _enter_level(
    rs: RoomState, channel_index: int, level_index: int, now: datetime, plan: Plan, tun: Tunables
) -> Outcome:
    cal = rs.cal
    assert cal is not None
    cal.phase = CalPhase.DWELL
    cal.channel_index = channel_index
    cal.level_index = level_index
    cal.samples = []
    cal.deadline = now + timedelta(seconds=tun.calibration_dwell)
    cid = cal.channel_order[channel_index]
    _drive(rs, plan, {cid: tun.calibration_levels[level_index]}, now)
    plan.review_at(cal.deadline)
    return _ONGOING


def _commit(rs: RoomState, plan: Plan, now: datetime, tun: Tunables) -> Outcome:
    """All channels swept: build the calibration and hand it back (rule 4.4).

    Transactional: the room commits only if EVERY channel reached
    ``CAL_MIN_POINTS`` sampled levels; otherwise it rejects ``missing_samples``
    with the per-channel coverage map and restores the prior light state.
    """
    cal = rs.cal
    assert cal is not None
    coverage = _coverage(cal, tun.calibration_levels)
    if any(len(cal.measurements.get(cid, {})) < CAL_MIN_POINTS for cid in cal.channel_order):
        _restore_light(rs, plan, now)
        rs.cal = None
        return Outcome(
            done=True, ok=False, reason="missing_samples", coverage=coverage, restore_light=True
        )
    base = cal.off_baseline or 0.0
    gains: dict[str, float] = {}
    curves: dict[str, tuple[tuple[float, float], ...]] = {}
    for cid in cal.channel_order:
        meas = cal.measurements.get(cid, {})
        gains[cid], curves[cid] = _fit_channel(base, meas, tun.calibration_levels)
    if any(g <= 0.0 for g in gains.values()):
        # A channel whose sampled levels never rose above the off baseline
        # yields gain 0 — RoomCalibration.validate would reject it on the next
        # load anyway, silently reverting the room after a restart. Reject at
        # commit time instead so the operator sees it.
        _restore_light(rs, plan, now)
        rs.cal = None
        return Outcome(
            done=True, ok=False, reason="dark_channel", coverage=coverage, restore_light=True
        )
    calibration = RoomCalibration(room_id="", gains=gains, curves=curves)
    rs.cal = None
    return Outcome(done=True, ok=True, reason="ok", calibration=calibration, coverage=coverage)


def _fit_channel(
    base: float, meas: dict[float, float], levels: tuple[float, ...]
) -> tuple[float, tuple[tuple[float, float], ...]]:
    """Gain + relative-flux points for one channel from its SAMPLED levels (4.4).

    ``meas`` holds only the levels a sample landed on (partial coverage). ``g_i``
    is the full-output (b=1) contribution above the off baseline, square-law
    extrapolated from the highest sampled level (``g = contrib_top / top**2`` —
    exact when the top level is 1.0). ``f_i`` is the measured contribution
    normalized by ``g``, enforced monotone; below the lowest sampled level the
    curve follows a square-law arc scaled to meet that point, and above the top
    sampled level it follows ``b**2`` to (1, 1). A channel that produced no
    measurable light keeps a zero gain and the default square-law curve
    (bounded influence, §3.4/4.2).
    """
    sampled = sorted(meas)
    contribs = [(lv, max(0.0, meas[lv] - base)) for lv in sampled]
    top_level, top_contrib = contribs[-1] if contribs else (0.0, 0.0)
    if top_contrib <= 0.0 or top_level <= 0.0:
        return 0.0, tuple((b, b * b) for b in (0.0, *levels))

    gain = top_contrib / (top_level * top_level)  # square-law extrapolation to b=1
    lowest = sampled[0]
    rel: dict[float, float] = {}
    running = 0.0
    for lv, c in contribs:
        running = max(running, c / gain)  # enforce monotone in flux
        rel[lv] = min(1.0, running)
    f_low = rel[lowest]

    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    for lv in levels:
        if lv <= 0.0:
            continue
        if lv < lowest - _CAL_EPS:  # square-law arc below the lowest sample
            pts.append((lv, f_low * (lv / lowest) ** 2))
        elif lv in rel:  # a measured point
            pts.append((lv, rel[lv]))
        elif lv > top_level + _CAL_EPS:  # square-law above the top sample to (1, 1)
            pts.append((lv, min(1.0, lv * lv)))
    if pts[-1][0] < 1.0 - _CAL_EPS:
        pts.append((1.0, 1.0))

    # Final monotone + span guarantee (validate(): spans b=0..1, flux 0..1).
    out: list[tuple[float, float]] = []
    run = 0.0
    for b, f in pts:
        run = max(run, min(1.0, f))
        out.append((b, run))
    if out[-1][1] < 1.0 - _CAL_EPS:
        out[-1] = (out[-1][0], 1.0)
    return gain, tuple(out)


def _coverage(cal: CalibrationSession, levels: tuple[float, ...]) -> tuple[tuple[str, float], ...]:
    """Per-channel measured-levels / total-levels fraction (rule 4.4, §10)."""
    n = len(levels)
    return tuple((cid, len(cal.measurements.get(cid, {})) / n) for cid in cal.channel_order)


# ---------------------------------------------------------------------------
# Command emission (single writer for the room while calibrating)
# ---------------------------------------------------------------------------


def _drive(rs: RoomState, plan: Plan, levels: dict[str, float], now: datetime) -> None:
    """Set the room's channels to exact ``levels`` (off if absent), immediately.

    Calibration needs precise dwell levels, so it bypasses the slew shaping of
    the §8 governor — it is still the *only* writer for the room while active.
    The ledger is updated so post-sweep control and the write-blank window
    (§3.2a) stay consistent.
    """
    rs.est.last_own_command_at = now
    for cid, cs in rs.channels.items():
        target = levels.get(cid, 0.0)
        if target <= 0.0:
            if cs.on:
                plan.turn_off(cid, 0.0)
            cs.on = False
            cs.commanded_b = 0.0
        else:
            plan.set_channel(cid, target, None, 0.0)
            cs.on = True
            cs.commanded_b = target


def _restore_light(rs: RoomState, plan: Plan, now: datetime) -> None:
    """Restore the exact pre-sweep light state (rule 4.4 abort transaction)."""
    cal = rs.cal
    assert cal is not None
    rs.est.last_own_command_at = now
    for cid, (b, ct, on) in cal.prior_light.items():
        cs = rs.channels[cid]
        if on and b > 0.0:
            plan.set_channel(cid, b, ct, 0.0)
            cs.on = True
            cs.commanded_b = b
            cs.commanded_ct = ct
        else:
            if cs.on:
                plan.turn_off(cid, 0.0)
            cs.on = False
            cs.commanded_b = 0.0
