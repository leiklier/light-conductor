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
    assert not estimator.is_stale(est, at(1, 20, 1), TUN)
    assert estimator.is_stale(est, at(1, 20, 3), TUN)  # > 120 s later


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


def test_band_fill_zero_demand_is_all_off() -> None:
    """§4.5: zero demand (natural light already sufficient) leaves all bands 0."""
    chans = (ChannelConfig("c", band=Band.PRIMARY, fixed_ct=2700, gain=100.0),)
    photo = _Photo({"c": 100.0})
    out = estimator.channel_outputs_for_demand(chans, 0.0, 0.0, photo, 1.0, TUN)
    assert out == {"c": 0.0}
