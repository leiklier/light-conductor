"""Natural-light estimator & closed-loop control (ENGINE_SPEC §3, §4.5).

The lux sensor measures natural + our own artificial light. This module
separates them (``N̂ = clamp(L_filt - Â, 0, ∞)``, rule 3.2) so the controller
regulates *natural shortfall* — never chasing its own output (the legacy
"cutting in and out", D2). It owns:

- the write-blanking window and asymmetric low-pass on measured lux (§3.2),
- the night prior that pulls ``N̂`` toward 0 without clamping (§3.3),
- the online per-room scalar gain multiplier refined on own-step ΔL when the
  room is quiet, bounded [0.5, 2.0] (§3.4),
- sensor staleness ⇒ open-loop fallback at the same tier (§3.5),
- feed-forward closed-loop control with deadband + sustain (§3.6),
- lux band-fill demand → per-channel command with a crossfade so band
  engagement is never a pop (§4.5).

The estimator is a pure feature module: it imports only :mod:`model` and
:mod:`tunables`, and consumes the room's photometry through the
:class:`~.model.PhotometricModel` protocol (never importing :mod:`photometry`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import exp

from .model import (
    BAND_ORDER,
    Band,
    ChannelConfig,
    EstimatorState,
    PhotometricModel,
    Role,
    RoomState,
)
from .tunables import Tunables

#: Online gain-multiplier bounds relative to the calibrated gains (rule 3.4).
GAIN_MIN = 0.5
GAIN_MAX = 2.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Artificial estimate Â (rule 3.1)
# ---------------------------------------------------------------------------


def a_hat(rs: RoomState, photo: PhotometricModel, gain_mult: float | None = None) -> float:
    """Predicted lux ``Â`` from the room's currently commanded outputs (rule 3.1).

    ``Â = Σ_i g_i · f_i(b_i) · m`` where ``m`` is the online scalar multiplier
    (rule 3.4). ``gain_mult`` overrides ``m`` (the calibration/learning code
    passes ``1.0`` to reason about the calibrated gains alone).
    """
    m = rs.est.gain_mult if gain_mult is None else gain_mult
    total = 0.0
    for cid, cs in rs.channels.items():
        if cs.commanded_b > 0.0:
            total += photo.gain(cid) * photo.flux(cid, cs.commanded_b) * m
    return total


# ---------------------------------------------------------------------------
# Sample folding: staleness, write-blank, low-pass, night prior, gain learning
# ---------------------------------------------------------------------------


def _settle_seconds(tun: Tunables) -> float:
    """How long after a step the plant+sensor are settled enough to learn (3.4).

    The physical light moves fast; the sensor filter lags, so we wait a few
    filter time-constants before reading the observed ΔL.
    """
    return tun.write_blank + 3.0 * max(tun.tau_lux_up, tun.tau_lux_down)


def is_stale(est: EstimatorState, now: datetime, tun: Tunables) -> bool:
    """Whether the lux sensor is unavailable/stale (rule 3.5)."""
    return est.last_report_at is None or (now - est.last_report_at).total_seconds() > tun.lux_stale


def note_own_command(est: EstimatorState, now: datetime) -> None:
    """Record an own channel command — opens the write-blank window (rule 3.2a)."""
    est.last_own_command_at = now


def invalidate_pending(est: EstimatorState) -> None:
    """Drop any pending gain observation — the window was not quiet (rule 3.4)."""
    est.pending_valid = False
    est.pending_l_before = None
    est.pending_base_delta = None
    est.pending_settle_at = None


def record_step(
    est: EstimatorState, l_before: float | None, base_delta: float, now: datetime, tun: Tunables
) -> None:
    """Arm an online-gain observation for a just-emitted feed-forward step (3.4).

    ``base_delta`` is the predicted ΔL of the step at ``gain_mult == 1``. A
    second step (or a foreign command) inside the settle window overwrites /
    invalidates this, so only a *quiet* single step ever updates the gain.
    """
    if l_before is None or abs(base_delta) < tun.deadband_abs:
        # Too small to learn from without amplifying sensor noise.
        invalidate_pending(est)
        return
    est.pending_l_before = l_before
    est.pending_base_delta = base_delta
    est.pending_settle_at = now + timedelta(seconds=_settle_seconds(tun))
    est.pending_valid = True


def ingest_lux(
    est: EstimatorState,
    lux: float | None,
    now: datetime,
    sun_elevation: float | None,
    a_now: float,
    tun: Tunables,
) -> None:
    """Fold one :class:`~.events.LuxReport` into the estimator (rules 3.2-3.4).

    ``a_now`` is ``Â`` evaluated for the room's current commanded outputs.
    A ``None`` lux (sensor unavailable) is ignored — staleness (rule 3.5) then
    trips on ``last_report_at`` ageing out.
    """
    if lux is None:
        return
    lux = max(0.0, lux)
    est.last_report_at = now

    # (a) Write blanking (rule 3.2a): a sample inside write_blank of an own
    # command carries our own switching transient — exclude it from the filter
    # (but it still proves the sensor is alive, so last_report_at stands).
    if (
        est.last_own_command_at is not None
        and (now - est.last_own_command_at).total_seconds() < tun.write_blank
    ):
        return

    # (b) Asymmetric low-pass (rule 3.2): clouds are minutes, not seconds.
    # Â is filtered with the *same* alpha, so the residual L_filt - Â_filt
    # stays consistent through our own switching transient (no residual spike).
    if est.l_filt is None or est.last_filt_at is None:
        est.l_filt = lux
        est.a_filt = a_now
        dt = 0.0
    else:
        dt = max(0.0, (now - est.last_filt_at).total_seconds())
        tau = tun.tau_lux_up if lux > est.l_filt else tun.tau_lux_down
        alpha = 1.0 - exp(-dt / tau) if tau > 0 else 1.0
        est.l_filt += alpha * (lux - est.l_filt)
        est.a_filt += alpha * (a_now - est.a_filt)
    est.last_filt_at = now

    # Natural estimate N̂ = clamp(L_filt - Â_filt, 0, ∞) with the night prior.
    _update_n_hat(est, dt, sun_elevation, tun)

    # (c) Online gain refinement (rule 3.4): a quiet, settled single step.
    _maybe_learn_gain(est, now, tun)


def _update_n_hat(
    est: EstimatorState,
    dt: float,
    sun_elevation: float | None,
    tun: Tunables,
) -> None:
    """Advance ``N̂`` from ``L_filt - Â_filt`` under the night prior (rules 3.2/3.3).

    ``dt`` is the interval since the previous folded sample (0 on the first).
    """
    assert est.l_filt is not None
    n_meas = max(0.0, est.l_filt - est.a_filt)

    night = sun_elevation is not None and sun_elevation < tun.night_prior_deg
    if not night or dt <= 0.0:
        est.n_hat = n_meas
        return

    # Night prior (rule 3.3): N̂ is state that relaxes toward the measurement
    # AND toward 0 with tau_night_prior — the prior only *pulls*. A persistent
    # source (street/TV glow) keeps re-feeding n_meas so it survives; a
    # spurious positive with no source decays to 0. Equilibrium sits at
    # n_meas · tau_night_prior / (tau_lux_down + tau_night_prior) ≈ 0.9·n_meas.
    a_track = 1.0 - exp(-dt / tun.tau_lux_down) if tun.tau_lux_down > 0 else 1.0
    a_prior = 1.0 - exp(-dt / tun.tau_night_prior) if tun.tau_night_prior > 0 else 1.0
    est.n_hat += a_track * (n_meas - est.n_hat) - a_prior * est.n_hat
    est.n_hat = max(0.0, est.n_hat)


def _maybe_learn_gain(est: EstimatorState, now: datetime, tun: Tunables) -> None:
    if not est.pending_valid or est.pending_settle_at is None:
        return
    if now < est.pending_settle_at:
        return
    if (
        est.l_filt is None
        or est.pending_l_before is None
        or est.pending_base_delta is None
        or abs(est.pending_base_delta) < tun.deadband_abs
    ):
        invalidate_pending(est)
        return
    obs_delta = est.l_filt - est.pending_l_before
    obs_mult = obs_delta / est.pending_base_delta
    # EMA toward the observed multiplier, bounded relative to the calibration.
    est.gain_mult = _clamp(
        est.gain_mult + tun.gain_learn_rate * (obs_mult - est.gain_mult),
        GAIN_MIN,
        GAIN_MAX,
    )
    invalidate_pending(est)


# ---------------------------------------------------------------------------
# Closed-loop targets (§2.1) and demand
# ---------------------------------------------------------------------------


def target_lux(rs: RoomState, role: Role, profile, e: float, g: float, tun: Tunables) -> float:
    """Sensor-relative lux target ``T'`` for a role tier (rules 2.1, 2.5, 1.5).

    ACTIVE interpolates ``lux_active_day``↔``lux_active_evening`` by E;
    ADJACENT scales it by ``adjacent_fraction`` (capped ``adjacent_cap``);
    BACKGROUND by ``background_fraction`` (capped ``background_cap``); OFF is 0.
    The master gain scales the target (rule 2.5), clamped to ``lux_max``.
    """
    if role is Role.OFF:
        return 0.0
    active = profile.lux_active_day * (1.0 - e) + profile.lux_active_evening * e
    if role is Role.ACTIVE:
        t = active
    elif role is Role.ADJACENT:
        t = min(active * tun.adjacent_fraction, tun.adjacent_cap)
    elif role is Role.BACKGROUND:
        t = min(active * tun.background_fraction, tun.background_cap)
    else:  # NIGHT_PATH / TV are mode-driven (band_outputs), never lux tiers.
        return 0.0
    return min(t * g, profile.lux_max)


# ---------------------------------------------------------------------------
# Lux band-fill (§4.5)
# ---------------------------------------------------------------------------


def _band_allotments(
    demand: float, capacities: dict[Band, float], overlap: float
) -> dict[Band, float]:
    """Split ``demand`` lux across bands accent→primary→boost (rule 4.5).

    Each band fills before the next, but with a ``band_overlap`` crossfade: a
    band begins engaging while the previous is still ``overlap`` short of full,
    so no channel snaps on at a hard boundary. Allotments are rescaled to sum
    to ``min(demand, total capacity)`` exactly, keeping the fill continuous in
    demand (feed-forward convergence) and never a step.
    """
    total = sum(max(0.0, capacities.get(b, 0.0)) for b in BAND_ORDER)
    demand = _clamp(demand, 0.0, total)
    if total <= 0.0 or demand <= 0.0:
        return dict.fromkeys(BAND_ORDER, 0.0)

    cumulative = 0.0
    raw: dict[Band, float] = {}
    for b in BAND_ORDER:
        cap = max(0.0, capacities.get(b, 0.0))
        if cap <= 0.0:
            raw[b] = 0.0
            continue
        # Engage a touch early (overlap into the previous band) so the handoff
        # crossfades rather than pops; saturate at the cumulative capacity.
        lo = cumulative - overlap * cap
        hi = cumulative + cap
        raw[b] = _clamp((demand - lo) / (hi - lo), 0.0, 1.0) * cap
        cumulative = hi
    scale = demand / sum(raw.values()) if sum(raw.values()) > 0.0 else 0.0
    return {b: raw[b] * scale for b in BAND_ORDER}


def channel_outputs_for_demand(
    channels: tuple[ChannelConfig, ...],
    demand: float,
    e: float,
    photo: PhotometricModel,
    gain_mult: float,
    tun: Tunables,
) -> dict[str, float]:
    """Invert demand lux ``D`` to per-channel normalized commands (rules 4.5/3.6).

    Per-channel lux capacity ``c_i = g_i · f_i(1) · m`` orders the band fill;
    within a band the allotted lux is shared by aesthetic ``weight`` (never the
    sensor gain, §3.1), then inverted through the flux curve to a command. The
    boost band is gated off once ``E ≥ boost_evening_max`` (rule 4.5).
    """
    caps: dict[Band, float] = dict.fromkeys(BAND_ORDER, 0.0)
    cap_i: dict[str, float] = {}
    for ch in channels:
        gated = ch.band is Band.BOOST and e >= tun.boost_evening_max
        c = 0.0 if gated else photo.gain(ch.channel_id) * photo.flux(ch.channel_id, 1.0) * gain_mult
        cap_i[ch.channel_id] = c
        caps[ch.band] += c

    allot = _band_allotments(demand, caps, tun.band_overlap)

    weight_sum: dict[Band, float] = dict.fromkeys(BAND_ORDER, 0.0)
    for ch in channels:
        if cap_i[ch.channel_id] > 0.0:
            weight_sum[ch.band] += ch.weight

    out: dict[str, float] = {}
    for ch in channels:
        cid = ch.channel_id
        if cap_i[cid] <= 0.0 or weight_sum[ch.band] <= 0.0:
            out[cid] = 0.0
            continue
        lux_i = allot[ch.band] * ch.weight / weight_sum[ch.band]
        g_eff = photo.gain(cid) * gain_mult
        f_max = photo.flux(cid, 1.0)
        f_i = _clamp(lux_i / g_eff, 0.0, f_max) if g_eff > 0.0 else f_max
        out[cid] = _clamp(photo.command_for_flux(cid, f_i), 0.0, 1.0)
    return out


# ---------------------------------------------------------------------------
# Feed-forward control decision (§3.6)
# ---------------------------------------------------------------------------


def should_correct(
    est: EstimatorState,
    error: float,
    deadband: float,
    now: datetime,
    fast_edge: bool,
    tun: Tunables,
) -> tuple[bool, datetime | None]:
    """Deadband + sustain gate on the closed loop (rule 3.6).

    Returns ``(correct_now, review_at)``. No action while ``|error| <
    deadband``; once outside, the error must persist for ``error_sustain``
    (shortened to ``error_sustain_fast`` on a role/mode edge) before a single
    feed-forward write lands. ``review_at`` asks the engine to re-evaluate when
    the sustain window would elapse, so a steady error is acted on unprompted.
    """
    if abs(error) < deadband:
        est.error_sustain_until = None
        return False, None
    if fast_edge:
        # A role/mode edge (re-)bases the clock to the shortened window.
        est.error_sustain_until = now + timedelta(seconds=tun.error_sustain_fast)
    elif est.error_sustain_until is None:
        est.error_sustain_until = now + timedelta(seconds=tun.error_sustain)
    if now >= est.error_sustain_until:
        est.error_sustain_until = None
        return True, None
    return False, est.error_sustain_until
