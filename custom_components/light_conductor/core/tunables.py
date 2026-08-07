"""Engine tunables (ENGINE_SPEC §12).

Every field maps to a row of the §12 defaults table; ``tests/core/
test_tunables.py`` asserts the doc table and this dataclass agree exactly.
Per-room / per-profile rows (``hold_seconds``, ``evening_output_cap``)
appear here as the *default* used when a room or profile does not override
them in config.

The §3 estimator and §4.4 calibration tunables are present (so the table
correspondence is exact and the closed-loop path has its constants ready)
but unused while every room runs open-loop.
"""

from __future__ import annotations

from dataclasses import dataclass

HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class Tunables:
    """Validated engine settings; adapter option keys land with config-flow."""

    # --- §1 activity FSM ---------------------------------------------------
    hold_seconds: float = 120.0  # 1.3 (kontor 90 via room config)
    hold_passing_scale: float = 0.3  # 1.3
    hold_settled_scale: float = 4.0  # 1.3
    adjacent_fraction: float = 0.5  # 1.5
    adjacent_cap: float = 30.0  # 1.5 (lux; closed-loop only)
    background_fraction: float = 0.25  # 2.1
    background_cap: float = 15.0  # 2.1 (lux; closed-loop only)
    #: Capacity-fraction defaults for an UNSET (0) closed-loop lux tier (§2.1):
    #: the tier falls back to this fraction of the room's calibrated capacity
    #: C = Σ_i g_i·f_i(1)·m. Engineering constants — not room-editable; the
    #: operator surface is the per-room tiers themselves (an explicit tier wins).
    lux_day_frac: float = 0.6  # 2.1
    lux_evening_frac: float = 0.2  # 2.1
    lux_background_frac: float = 0.05  # 2.1
    living_memory: float = 900.0  # 1.6
    trigger_hold: float = 300.0  # 1.7, 1.9
    door_close_hold: float = 15.0  # 1.9
    presence_blind_hold: float = 120.0  # 1.1, 1.8

    # --- §2 circadian ------------------------------------------------------
    sun_high_deg: float = 10.0  # 2.3
    sun_low_deg: float = -4.0  # 2.3
    evening_start_min: int = 20 * 60  # 2.3 (20:00, minutes past local midnight)
    evening_full_min: int = 22 * 60 + 30  # 2.3 (22:30)
    morning_start_min: int = 6 * 60  # 2.3 (06:00)
    morning_full_min: int = 7 * 60 + 30  # 2.3 (07:30)
    circadian_tick: float = 300.0  # 2.3
    evening_output_cap: float = 0.3  # 2.4 (profile default: 0.3 living)
    #: Circadian threshold at/above which the evening cap and the corridor
    #: "evening" gate engage (rule 2.4/1.7). Not tabled in the original
    #: §12; added there in the same PR (docs-only commit).
    evening_cap_threshold: float = 0.5

    # --- §3 estimator (deferred; closed-loop) -----------------------------
    write_blank: float = 5.0  # 3.2
    tau_lux_up: float = 30.0  # 3.2
    tau_lux_down: float = 60.0  # 3.2
    night_prior_deg: float = -6.0  # 3.3
    tau_night_prior: float = 600.0  # 3.3
    gain_learn_rate: float = 0.1  # 3.4
    #: First-night bootstrap (§3.5/§4.4): an uncalibrated lux-sensor room runs
    #: open-loop while the estimator learns a conservative room-scalar gain from
    #: >= bootstrap_min_obs own-step observations, over-modelled by
    #: bootstrap_margin so the resulting loop gain is < 1 (stable undershoot).
    bootstrap_min_obs: int = 3  # 3.5/4.4
    bootstrap_margin: float = 1.5  # 3.5/4.4
    #: Bootstrap arming dispersion guard (§3.5): before committing a bootstrap
    #: gain the collected own-step ratios must agree — with m = median(ratios),
    #: ``max(r) <= bootstrap_dispersion_max·m`` AND ``min(r) >= m /
    #: bootstrap_dispersion_max``. Genuine own-light observations cluster
    #: tightly; ambient contamination (clouds swinging daylight while lights
    #: happen to toggle) scatters. A failing set is dropped so a later quiet
    #: period can bootstrap cleanly (the kjøkken false-promotion incident).
    bootstrap_dispersion_max: float = 3.0  # 3.5
    lux_stale: float = 300.0  # 3.5 (60 s publish cadence with dedup: 120 s was flappy)
    #: Lux-sensor "wedge" warning threshold (§3.5): a configured lux sensor whose
    #: entity stays AVAILABLE but has produced no state update for this many
    #: seconds raises a (non-fixable) HA repairs issue suggesting its ESP reboot
    #: button. Much longer than ``lux_stale`` (open-loop fallback) — a wedge is a
    #: hardware quirk needing operator action, not routine unavailability.
    lux_wedge_warn: float = 1800.0  # 3.5
    deadband_abs: float = 5.0  # 3.6
    deadband_rel: float = 0.15  # 3.6
    #: Capacity-scaled control deadband (§3.6): the absolute deadband component is
    #: itself capped at ``deadband_capacity_frac·C`` (room calibrated capacity)
    #: so a low-capacity room (e.g. sofakrok, C≈8.8 lx) can reach targets that sit
    #: below the fixed 5-lx floor, then floored at ``deadband_floor`` (sensor-noise
    #: floor). A high-capacity room (C≥25) is unchanged (min picks deadband_abs).
    deadband_capacity_frac: float = 0.2  # 3.6
    deadband_floor: float = 0.5  # 3.6 (lx, sensor-noise floor)
    error_sustain: float = 20.0  # 3.6
    error_sustain_fast: float = 2.0  # 3.6
    #: Closed-loop capacity gate (§4.5/§4.7): a room enters closed loop only when
    #: its capacity C ≥ this floor. Below it (e.g. kjøkken, C≈2 lx) the room runs
    #: the daylight-aware open-loop path even once calibrated — servoing ~1 lx
    #: targets against ~1 lx sensor quantization would never visibly light.
    min_closed_loop_capacity: float = 4.0  # 4.5/4.7 (lx)

    # --- §4 photometry / allocation ---------------------------------------
    calibration_levels: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 1.0)  # 4.4
    calibration_dwell: float = 4.0  # 4.4
    #: Daylight-aware open-loop (rule 4.7): an untrusted lux-sensor room scales
    #: its open-loop tables by D = clamp(1 - N̂/daylight_full, min_factor, 1.0),
    #: replicating the legacy 100 - 0.5·lux daytime damping for sensors that read
    #: daylight well but their own lamps barely.
    daylight_full: float = 200.0  # 4.7
    daylight_min_factor: float = 0.0  # 4.7
    band_overlap: float = 0.15  # 4.5
    boost_evening_max: float = 0.5  # 4.5

    # --- §5 colour temperature --------------------------------------------
    ct_day: int = 3300  # 5.1
    ct_evening: int = 2400  # 5.1
    ct_min_evening: int = 2200  # 5.3
    blend_threshold: float = 0.1  # 5.2
    blend_delta: int = 300  # 5.2
    warm_dim_output: float = 0.3  # 5.3
    ct_min_delta: int = 100  # 5.4

    # --- §6 modes ----------------------------------------------------------
    sleep_fade: float = 4.0  # 6.1
    night_hold: float = 600.0  # 6.2
    night_fade: float = 10.0  # 6.2
    outdoor_on_threshold: float = 0.7  # 6.5
    #: Measured-dusk window for an outdoor room that has a lux sensor (§6.5a).
    #: Its dusk factor ramps 0 -> 1 as N̂ falls from ``outdoor_on_lux`` to
    #: ``outdoor_full_lux``; the E gate above stays as a union backstop.
    outdoor_on_lux: float = 15.0  # 6.5a
    outdoor_full_lux: float = 2.0  # 6.5a
    #: How deep the dusk ramp must be before an outdoor room's occupational
    #: switch counts as presence for its neighbours (§1.10) — the interior must
    #: not follow the balcony into ADJACENT while it is still bright indoors.
    outdoor_presence_factor: float = 0.5  # 1.10
    #: A foreign ZERO on an outdoor room within this many seconds of the
    #: engine's own last write to the room is treated as a suspect stale
    #: report (Plejd gateway re-delivery), not an off-press declaration
    #: (§6.5b). Must stay well below the ~180 s true-state poll, which is the
    #: legitimate corrector for genuinely lost writes.
    outdoor_stale_zero_window: float = 45.0  # 6.5b

    # --- §7 master gain ----------------------------------------------------
    gain_range_stops: float = 1.0  # 7.1
    gain_reset: bool = True  # 7.3

    # --- §8 write governor -------------------------------------------------
    slew_step: float = 0.1  # 8.2
    slew_interval: float = 1.0  # 8.2
    slew_step_empty: float = 0.25  # 8.2
    min_delta: float = 0.03  # 8.3
    min_write_interval: float = 1.0  # 8.3
    max_inflight: int = 3  # 8.3
    echo_window: float = 10.0  # 8.4

    # --- §9 override / §11 startup ----------------------------------------
    override_timeout: float = 4 * HOUR  # 9.2
    startup_grace: float = 30.0  # 11.1

    def __post_init__(self) -> None:
        for name in ("slew_step", "slew_interval", "slew_step_empty", "min_delta"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if not self.sun_low_deg < self.sun_high_deg:
            raise ValueError("sun_low_deg must be below sun_high_deg")
        if not self.morning_start_min < self.morning_full_min < self.evening_start_min:
            raise ValueError("circadian clock boundaries out of order")
        if self.bootstrap_dispersion_max < 1.0:
            raise ValueError("bootstrap_dispersion_max must be >= 1.0")
        if not 0.0 <= self.outdoor_full_lux < self.outdoor_on_lux:
            raise ValueError("outdoor_full_lux must be >= 0 and below outdoor_on_lux")
        if not 0.0 < self.outdoor_presence_factor <= 1.0:
            raise ValueError("outdoor_presence_factor must be in (0, 1]")
        if not 0.0 <= self.outdoor_stale_zero_window < 175.0:
            # The ~180 s true-state poll is the legitimate corrector for a
            # genuinely lost write; a window reaching it would swallow that
            # correction and strand sessions (§6.5b).
            raise ValueError("outdoor_stale_zero_window must be in [0, 175)")


def gain_multiplier(pct: float, stops: float) -> float:
    """Master-gain multiplier G for a slider percentage (rule 7.1).

    ``G = 2 ** ((pct - 50) / 50 * stops)`` — 50 % neutral, 100 % gives
    2^stops, 0 % gives 2^-stops.
    """
    return 2.0 ** ((pct - 50.0) / 50.0 * stops)


DEFAULT_DIM_FLOOR = 0.02
