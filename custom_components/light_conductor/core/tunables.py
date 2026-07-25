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
    lux_stale: float = 120.0  # 3.5
    deadband_abs: float = 5.0  # 3.6
    deadband_rel: float = 0.15  # 3.6
    error_sustain: float = 20.0  # 3.6
    error_sustain_fast: float = 2.0  # 3.6

    # --- §4 photometry / allocation ---------------------------------------
    calibration_levels: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 1.0)  # 4.4
    calibration_dwell: float = 4.0  # 4.4
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


def gain_multiplier(pct: float, stops: float) -> float:
    """Master-gain multiplier G for a slider percentage (rule 7.1).

    ``G = 2 ** ((pct - 50) / 50 * stops)`` — 50 % neutral, 100 % gives
    2^stops, 0 % gives 2^-stops.
    """
    return 2.0 ** ((pct - 50.0) / 50.0 * stops)


DEFAULT_DIM_FLOOR = 0.02
