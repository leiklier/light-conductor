"""Unit tests for the natural-light estimator (ENGINE_SPEC §3, §4.5).

These exercise the estimator functions directly (filter, write-blank, night
prior, band-fill, deadband/sustain, online gain) with a fixed clock; the
closed-loop *system* behaviour against a synthetic plant lives in
``test_closed_loop.py``.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.light_conductor.core import estimator
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EstimatorState,
)
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import at

TUN = Tunables()
DAY = 20.0
NIGHT = -10.0


class _Photo:
    """A tiny PhotometricModel: square-law curve, per-channel gains."""

    def __init__(self, gains: dict[str, float]) -> None:
        self._g = gains

    def flux(self, cid: str, b: float) -> float:
        return b * b

    def command_for_flux(self, cid: str, f: float) -> float:
        return f**0.5

    def gain(self, cid: str) -> float:
        return self._g[cid]


# --- §3.2 write blanking -------------------------------------------------


def test_write_blank_excludes_own_transient() -> None:
    """§3.2a: a lux sample within write_blank of an own command is excluded."""
    est = EstimatorState()
    estimator.ingest_lux(est, 40.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    assert est.l_filt == 40.0
    # An own command opens the blank window; a wild sample 2 s later is ignored.
    estimator.note_own_command(est, at(1, 20, 0, 2))
    estimator.ingest_lux(est, 500.0, at(1, 20, 0, 3), DAY, 0.0, TUN)
    assert est.l_filt == 40.0  # transient excluded (rule 3.2a)
    # Past the window (write_blank 5 s) the filter tracks again.
    estimator.ingest_lux(est, 60.0, at(1, 20, 0, 10), DAY, 0.0, TUN)
    assert est.l_filt > 40.0


def test_low_pass_is_asymmetric_and_slow() -> None:
    """§3.2: rise/fall use tau_lux_up/down — seconds barely move a minutes filter."""
    est = EstimatorState()
    estimator.ingest_lux(est, 10.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.ingest_lux(est, 110.0, at(1, 20, 0, 10), DAY, 0.0, TUN)  # +10 s, tau_up 30
    # After 10 s of a 30 s rise constant, ~28 % of the 100 lx jump landed.
    assert 30.0 < est.l_filt < 45.0


def test_a_filt_tracks_artificial_so_residual_is_stable() -> None:
    """§3.2: filtering Â with the same low-pass keeps N̂ steady through a step.

    N is a constant 30 lx; the room's own light steps up. Because L and Â lag
    together, N̂ never spikes (the bug that made the loop over-command)."""
    est = EstimatorState()
    # Settle with lights off: N̂ == N.
    for k in range(5):
        estimator.ingest_lux(est, 30.0, at(1, 20, 0, k * 2), DAY, 0.0, TUN)
    assert abs(est.n_hat - 30.0) < 0.01
    # Lights jump to full (200 lx). Feed rising lux + matching Â each sample.
    for k in range(30):
        t = at(1, 20, 1, k * 2)
        a = 200.0  # Â at full (gain 200, flux 1)
        estimator.ingest_lux(est, 30.0 + 200.0, t, DAY, a, TUN)
        assert est.n_hat < 45.0  # never spikes toward the 230 lx total


# --- §3.3 night prior ----------------------------------------------------


def test_night_prior_lets_real_source_survive() -> None:
    """§3.3: a persistent TV-glow-style 3 lx source survives the night prior."""
    est = EstimatorState()
    t = at(1, 23, 0, 0)
    for _k in range(1200):  # 40 min at 2 s
        estimator.ingest_lux(est, 3.0, t, NIGHT, 0.0, TUN)
        t = t + timedelta(seconds=2)
    assert est.n_hat > 2.0  # pulled a little, never clamped to 0 (rule 3.3)


def test_night_prior_pulls_spurious_estimate_to_zero() -> None:
    """§3.3: with no real source the estimate relaxes toward 0."""
    est = EstimatorState()
    # Seed an elevated estimate, then feed genuine darkness (0 lx, lights off).
    est.l_filt = 20.0
    est.a_filt = 0.0
    est.n_hat = 20.0
    est.last_filt_at = at(1, 23, 0, 0)
    t = at(1, 23, 0, 2)
    for _k in range(1800):  # 1 h
        estimator.ingest_lux(est, 0.0, t, NIGHT, 0.0, TUN)
        t = t + timedelta(seconds=2)
    assert est.n_hat < 0.5  # relaxed toward 0 (rule 3.3)


def test_day_has_no_prior() -> None:
    """§3.3: above night_prior_deg N̂ is the raw residual, no relaxation."""
    est = EstimatorState()
    for k in range(10):
        estimator.ingest_lux(est, 50.0, at(1, 12, 0, k * 2), DAY, 0.0, TUN)
    assert abs(est.n_hat - 50.0) < 0.01


# --- §3.5 staleness ------------------------------------------------------


def test_staleness_trips_after_lux_stale() -> None:
    est = EstimatorState()
    assert estimator.is_stale(est, at(1, 20, 0), TUN)  # no sample yet
    estimator.ingest_lux(est, 10.0, at(1, 20, 0), DAY, 0.0, TUN)
    assert not estimator.is_stale(est, at(1, 20, 4), TUN)  # 240 s < lux_stale (300 s)
    assert estimator.is_stale(est, at(1, 20, 6), TUN)  # > 300 s later


# --- §3.6 deadband + sustain --------------------------------------------


def test_deadband_holds_and_sustain_gates() -> None:
    """§3.6: inside the deadband no action; outside, the error must sustain."""
    est = EstimatorState()
    # Small error inside max(5, 0.15*100)=15 lx: no action.
    correct, _ = estimator.should_correct(est, 10.0, 15.0, at(1, 20, 0), False, TUN)
    assert not correct and est.error_sustain_until is None
    # Big error: arms the 20 s sustain, still no action.
    correct, review = estimator.should_correct(est, 40.0, 15.0, at(1, 20, 0, 0), False, TUN)
    assert not correct and review == at(1, 20, 0, 20)
    # Before the window elapses: still holding.
    correct, _ = estimator.should_correct(est, 40.0, 15.0, at(1, 20, 0, 10), False, TUN)
    assert not correct
    # After 20 s: corrects once, clock resets.
    correct, _ = estimator.should_correct(est, 40.0, 15.0, at(1, 20, 0, 20), False, TUN)
    assert correct and est.error_sustain_until is None


def test_control_deadband_scales_with_capacity() -> None:
    """§3.6: the absolute deadband component is capped at a fraction of C.

    A sofakrok-like low-capacity room (C≈8.8) gets a deadband well below the
    fixed 5 lx, so its ~3.8-lx daytime deficit clears it and the loop corrects;
    a high-capacity room (C≥25) is unchanged at 5.0.
    """
    # Low capacity: min(5, 0.2·8.8=1.76) wins over floor/rel → 1.76 lx.
    low = estimator.control_deadband(TUN, capacity=8.8, t_prime=5.3)
    assert abs(low - 1.76) < 1e-9
    assert low < 5.3 - 1.5  # the 3.8-lx auto-day deficit now corrects
    # High capacity: min(5, 0.2·200=40) → 5.0; a small target keeps rel below it.
    high = estimator.control_deadband(TUN, capacity=200.0, t_prime=10.0)
    assert high == TUN.deadband_abs == 5.0
    # C ≥ 25 already pins the fixed 5-lx deadband (0.2·25 = 5).
    assert estimator.control_deadband(TUN, capacity=25.0, t_prime=1.0) == 5.0


def test_control_deadband_never_below_floor() -> None:
    """§3.6: even a near-zero capacity/target never drops below deadband_floor."""
    db = estimator.control_deadband(TUN, capacity=0.1, t_prime=0.0)
    assert db == TUN.deadband_floor == 0.5


def test_control_deadband_rel_dominates_for_large_targets() -> None:
    """§3.6: deadband_rel·T' still dominates once the target is large."""
    db = estimator.control_deadband(TUN, capacity=8.8, t_prime=100.0)
    assert abs(db - TUN.deadband_rel * 100.0) < 1e-9  # 15 lx, above both other terms


def test_fast_edge_shortens_sustain() -> None:
    """§3.6: a role/mode edge uses error_sustain_fast (2 s)."""
    est = EstimatorState()
    correct, review = estimator.should_correct(est, 40.0, 15.0, at(1, 20, 0, 0), True, TUN)
    assert not correct and review == at(1, 20, 0, 2)
    correct, _ = estimator.should_correct(est, 40.0, 15.0, at(1, 20, 0, 2), False, TUN)
    assert correct


# --- §4.5 lux band-fill --------------------------------------------------


def test_band_fill_orders_accent_primary_boost() -> None:
    """§4.5: demand fills accent, then primary, then boost."""
    chans = (
        ChannelConfig("acc", band=Band.ACCENT, fixed_ct=2700, gain=100.0),
        ChannelConfig("pri", band=Band.PRIMARY, fixed_ct=2700, gain=100.0),
        ChannelConfig("bst", band=Band.BOOST, fixed_ct=2700, gain=100.0),
    )
    photo = _Photo({"acc": 100.0, "pri": 100.0, "bst": 100.0})
    # Low demand: only accent engages.
    low = estimator.channel_outputs_for_demand(chans, 40.0, 0.0, photo, 1.0, TUN)
    assert low["acc"] > 0.0 and low["pri"] < 0.05 and low["bst"] == 0.0
    # High demand: all three engage.
    high = estimator.channel_outputs_for_demand(chans, 270.0, 0.0, photo, 1.0, TUN)
    assert high["acc"] > 0.5 and high["pri"] > 0.5 and high["bst"] > 0.0


def test_band_fill_boost_evening_lockout() -> None:
    """§4.5: the boost band is gated off once E >= boost_evening_max."""
    chans = (
        ChannelConfig("pri", band=Band.PRIMARY, fixed_ct=2700, gain=100.0),
        ChannelConfig("bst", band=Band.BOOST, fixed_ct=2700, gain=100.0),
    )
    photo = _Photo({"pri": 100.0, "bst": 100.0})
    out = estimator.channel_outputs_for_demand(chans, 300.0, 0.9, photo, 1.0, TUN)
    assert out["bst"] == 0.0  # locked out in the evening
    assert out["pri"] > 0.9  # primary carries what it can


def test_band_fill_within_band_by_weight_not_gain() -> None:
    """§4.5/§3.1: two same-band channels split demand by weight, never by gain.

    The heavy-gain channel (sensor next to it) must not dominate: equal weights
    share the *lux* equally, so its command is smaller than the low-gain one."""
    chans = (
        ChannelConfig("near", band=Band.PRIMARY, fixed_ct=2700, gain=200.0, weight=1.0),
        ChannelConfig("far", band=Band.PRIMARY, fixed_ct=2700, gain=20.0, weight=1.0),
    )
    photo = _Photo({"near": 200.0, "far": 20.0})
    out = estimator.channel_outputs_for_demand(chans, 40.0, 0.0, photo, 1.0, TUN)
    # Each should produce ~20 lx; the near (high-gain) channel needs less command.
    assert out["near"] < out["far"]
    assert abs(200.0 * out["near"] ** 2 - 20.0 * out["far"] ** 2) < 1.0


# --- §3.4 online gain refinement ----------------------------------------


def test_gain_learns_on_quiet_step_and_is_bounded() -> None:
    """§3.4: a quiet settled step nudges gain_mult toward the observed ratio."""
    est = EstimatorState()
    # Settle at L_filt=0 with lights off.
    estimator.ingest_lux(est, 0.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    # Emit a step whose model predicts +10 lx (base_delta 10) but truly +15.
    estimator.record_step(est, est.l_filt, 10.0, at(1, 20, 0, 2), TUN)
    assert est.pending_valid
    # Settle window elapses; observe L_filt risen to 15 (obs_mult 1.5).
    t = at(1, 20, 0, 2)
    for _k in range(200):  # let the filter settle to 15 over > settle window
        estimator.ingest_lux(est, 15.0, t, DAY, 15.0, TUN)  # Â matched -> N̂~0
        t = t + timedelta(seconds=3)
    assert 1.0 < est.gain_mult <= 2.0  # moved toward 1.5, bounded [0.5, 2.0]


def test_gain_never_learns_in_non_quiet_window() -> None:
    """§3.4: a foreign command inside the settle window voids the observation."""
    est = EstimatorState()
    estimator.ingest_lux(est, 0.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.record_step(est, est.l_filt, 10.0, at(1, 20, 0, 2), TUN)
    estimator.invalidate_pending(est)  # a foreign change lands (non-quiet)
    before = est.gain_mult
    t = at(1, 20, 0, 2)
    for _k in range(200):
        estimator.ingest_lux(est, 15.0, t, DAY, 15.0, TUN)
        t = t + timedelta(seconds=3)
    assert est.gain_mult == before  # never updated (rule 3.4)


def test_tiny_step_does_not_learn() -> None:
    """§3.4: a sub-deadband predicted step is not learnable (noise guard)."""
    est = EstimatorState()
    estimator.ingest_lux(est, 0.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.record_step(est, est.l_filt, 1.0, at(1, 20, 0, 2), TUN)  # < deadband_abs
    assert not est.pending_valid


def test_gain_attribution_window_rejects_cloud_drift() -> None:
    """§3.4 (F5): a cloud drifting through the settle window (ΔL of the wrong
    sign / magnitude vs the model) must not move gain_mult."""
    # Wrong sign: predicted a rise, but lux fell (a cloud dimmed the room).
    est = EstimatorState()
    estimator.ingest_lux(est, 100.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.record_step(est, est.l_filt, 20.0, at(1, 20, 0, 2), TUN)  # predicts +20
    before = est.gain_mult
    t = at(1, 20, 0, 2)
    for _k in range(200):
        estimator.ingest_lux(est, 40.0, t, DAY, 0.0, TUN)  # lux FELL (cloud)
        t = t + timedelta(seconds=3)
    assert est.gain_mult == before  # sign disagreement -> discarded

    # Wrong magnitude: predicted +20 but a cloud added +200 (10x, outside window).
    est2 = EstimatorState()
    estimator.ingest_lux(est2, 100.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.record_step(est2, est2.l_filt, 20.0, at(1, 20, 0, 2), TUN)
    before2 = est2.gain_mult
    t = at(1, 20, 0, 2)
    for _k in range(200):
        estimator.ingest_lux(est2, 320.0, t, DAY, 0.0, TUN)  # +220, > 3x predicted
        t = t + timedelta(seconds=3)
    assert est2.gain_mult == before2  # magnitude outside window -> discarded


def test_none_lux_is_ignored_but_ages_toward_stale() -> None:
    """§3.5: a None lux (sensor unavailable) is ignored; the estimate stands."""
    est = EstimatorState()
    estimator.ingest_lux(est, 42.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    estimator.ingest_lux(est, None, at(1, 20, 0, 2), DAY, 0.0, TUN)
    assert est.l_filt == 42.0  # None left the filter untouched


def test_stale_pending_observation_is_dropped() -> None:
    """§3.4: a pending step whose base_delta is no longer valid is voided."""
    est = EstimatorState()
    estimator.ingest_lux(est, 0.0, at(1, 20, 0, 0), DAY, 0.0, TUN)
    est.pending_valid = True  # force an invalid pending past its settle
    est.pending_l_before = 0.0
    est.pending_base_delta = 0.0  # below deadband -> dropped at observe time
    est.pending_settle_at = at(1, 20, 0, 1)
    estimator.ingest_lux(est, 5.0, at(1, 20, 5, 0), DAY, 0.0, TUN)
    assert not est.pending_valid  # dropped, no NaN division


# --- §2.1 closed-loop lux targets ---------------------------------------


def test_target_lux_per_role_tier() -> None:
    """§2.1/§1.5: OFF=0; ADJACENT/BACKGROUND scale + cap the ACTIVE target."""
    from custom_components.light_conductor.core.model import Profile, Role, RoomState

    prof = Profile(lux_active_day=200.0, lux_active_evening=80.0, lux_max=1000.0)
    rs = RoomState()
    assert estimator.target_lux(rs, Role.OFF, prof, 0.0, 1.0, TUN) == 0.0
    assert estimator.target_lux(rs, Role.ACTIVE, prof, 0.0, 1.0, TUN) == 200.0
    # ADJACENT: 200*0.5=100 but capped at adjacent_cap (30).
    assert estimator.target_lux(rs, Role.ADJACENT, prof, 0.0, 1.0, TUN) == 30.0
    # BACKGROUND: 200*0.25=50 capped at background_cap (15).
    assert estimator.target_lux(rs, Role.BACKGROUND, prof, 0.0, 1.0, TUN) == 15.0
    # Modes (NIGHT_PATH/TV) are not lux-tiered here.
    assert estimator.target_lux(rs, Role.NIGHT_PATH, prof, 0.0, 1.0, TUN) == 0.0
    # Master gain scales, clamped to lux_max.
    prof2 = Profile(lux_active_day=800.0, lux_max=1000.0)
    assert estimator.target_lux(rs, Role.ACTIVE, prof2, 0.0, 2.0, TUN) == 1000.0


def test_target_lux_capacity_fraction_defaults() -> None:
    """§2.1: an UNSET (0) lux tier falls back to a capacity fraction; an explicit
    tier overrides the auto default; the background floor uses lux_background_frac."""
    from custom_components.light_conductor.core.model import Profile, Role, RoomState

    rs = RoomState()
    c = 200.0
    auto = Profile()  # every tier 0 (UNSET), lux_max default 1000
    # ACTIVE, day (E=0) -> lux_day_frac·C; evening (E=1) -> lux_evening_frac·C.
    assert estimator.target_lux(rs, Role.ACTIVE, auto, 0.0, 1.0, TUN, c) == TUN.lux_day_frac * c
    assert estimator.target_lux(rs, Role.ACTIVE, auto, 1.0, 1.0, TUN, c) == TUN.lux_evening_frac * c
    # E midway interpolates the two capacity fractions.
    mid = estimator.target_lux(rs, Role.ACTIVE, auto, 0.5, 1.0, TUN, c)
    assert abs(mid - 0.5 * (TUN.lux_day_frac + TUN.lux_evening_frac) * c) < 1e-9
    # An explicit tier always wins over the auto default.
    explicit = Profile(lux_active_day=30.0)
    assert estimator.target_lux(rs, Role.ACTIVE, explicit, 0.0, 1.0, TUN, c) == 30.0
    # BACKGROUND fraction path (small C): min(0.6·40·0.25, cap 15)=6, floor 0.05·40=2.
    assert estimator.target_lux(rs, Role.BACKGROUND, auto, 0.0, 1.0, TUN, 40.0) == 6.0
    # The AUTO background floor respects background_cap even on a very bright
    # room (C=2000 -> 0.05·C=100, capped to 15): idle brightness must not
    # silently outgrow the design; only an explicit floor may exceed the cap.
    assert estimator.target_lux(rs, Role.BACKGROUND, auto, 0.0, 1.0, TUN, 2000.0) == 15.0
    # Background never exceeds the ACTIVE target, even with an explicit floor.
    dim = Profile(lux_active_day=20.0, lux_background=100.0)
    assert estimator.target_lux(rs, Role.BACKGROUND, dim, 0.0, 1.0, TUN, 2000.0) == 20.0
    # An explicit lux_background is a floor under BACKGROUND (still capacity-fraction
    # ACTIVE): min(0.6·200·0.25,15)=15 floored by 50 -> 50.
    bg = Profile(lux_background=50.0)
    assert estimator.target_lux(rs, Role.BACKGROUND, bg, 0.0, 1.0, TUN, c) == 50.0
    # No capacity + unset tiers -> 0 (an uncalibrated/no-capacity room is untouched).
    assert estimator.target_lux(rs, Role.ACTIVE, auto, 0.0, 1.0, TUN, 0.0) == 0.0


def test_record_step_ignores_no_flux_move() -> None:
    """§3.4: a step with no baseline or no flux change never arms an observation."""
    est = EstimatorState()
    estimator.record_step(est, None, 10.0, at(1, 20, 0), TUN)
    assert not est.pending_valid
    estimator.record_step(est, 5.0, 0.0, at(1, 20, 0), TUN)  # zero flux move
    assert not est.pending_valid


def _settled_obs(
    est: EstimatorState,
    *,
    l_before: float,
    l_now: float,
    base: float,
    shadow: bool,
    calibrated: bool,
) -> None:
    """Craft a settled pending observation and fold one sample to consume it."""
    est.l_filt = l_now
    est.last_filt_at = at(1, 20, 0, 0)
    est.pending_l_before = l_before
    est.pending_base_delta = base
    est.pending_settle_at = at(1, 20, 0, 1)
    est.pending_valid = True
    est.pending_shadow = shadow
    # Fold a sample equal to l_now (no filter move) well past the settle window.
    estimator.ingest_lux(est, l_now, at(1, 20, 5, 0), DAY, 0.0, TUN, calibrated=calibrated)


def test_refine_skips_subdeadband_base() -> None:
    """§3.4: the refine path ignores a predicted move below the deadband."""
    est = EstimatorState()
    before = est.gain_mult
    _settled_obs(est, l_before=0.0, l_now=100.0, base=2.0, shadow=False, calibrated=True)
    assert est.gain_mult == before  # base 2 < deadband_abs 5 -> no refine


def test_settled_pending_with_missing_field_is_dropped() -> None:
    """§3.4: a settled pending whose captured fields are incomplete is voided."""
    est = EstimatorState()
    est.l_filt = 10.0
    est.last_filt_at = at(1, 20, 0, 0)
    est.pending_valid = True
    est.pending_l_before = None  # incomplete capture
    est.pending_base_delta = 10.0
    est.pending_settle_at = at(1, 20, 0, 1)
    estimator.ingest_lux(est, 10.0, at(1, 20, 5, 0), DAY, 0.0, TUN)
    assert not est.pending_valid  # dropped without a division


def test_bootstrap_skips_tiny_or_wrong_sign_observation() -> None:
    """§3.5/4.4: a sub-deadband or wrong-sign observed ΔL adds no bootstrap ratio."""
    est = EstimatorState()
    _settled_obs(est, l_before=8.0, l_now=10.0, base=0.1, shadow=True, calibrated=False)
    assert est.bootstrap_ratios == []  # observed ΔL 2 < deadband_abs 5
    est2 = EstimatorState()
    _settled_obs(est2, l_before=20.0, l_now=10.0, base=0.1, shadow=True, calibrated=False)
    assert est2.bootstrap_ratios == []  # observed ΔL -10 < 0 -> sign disagreement


def test_band_fill_zero_demand_is_all_off() -> None:
    """§4.5: zero demand (natural light already sufficient) leaves all bands 0."""
    chans = (ChannelConfig("c", band=Band.PRIMARY, fixed_ct=2700, gain=100.0),)
    photo = _Photo({"c": 100.0})
    out = estimator.channel_outputs_for_demand(chans, 0.0, 0.0, photo, 1.0, TUN)
    assert out == {"c": 0.0}
