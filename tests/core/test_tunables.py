"""§12: the tunables dataclass and the doc defaults table must agree."""

from __future__ import annotations

from pathlib import Path

from custom_components.light_conductor.core.tunables import Tunables, gain_multiplier

SPEC = Path(__file__).resolve().parents[2] / "docs" / "ENGINE_SPEC.md"

# Every §12 doc row -> (dataclass field(s), expected default). Rows that name
# two knobs ("a / b") map to a tuple of fields. Rows whose default is
# "profile"/per-room carry the tunable default used when config omits it.
DOC_ROWS: dict[str, tuple[tuple[str, ...], tuple[object, ...]]] = {
    "hold_seconds": (("hold_seconds",), (120.0,)),
    "hold_passing_scale / hold_settled_scale": (
        ("hold_passing_scale", "hold_settled_scale"),
        (0.3, 4.0),
    ),
    "adjacent_fraction / adjacent_cap": (("adjacent_fraction", "adjacent_cap"), (0.5, 30.0)),
    "background_fraction / background_cap": (
        ("background_fraction", "background_cap"),
        (0.25, 15.0),
    ),
    "living_memory": (("living_memory",), (900.0,)),
    "trigger_hold / door_close_hold": (("trigger_hold", "door_close_hold"), (300.0, 15.0)),
    "presence_blind_hold": (("presence_blind_hold",), (120.0,)),
    "sun_high_deg / sun_low_deg": (("sun_high_deg", "sun_low_deg"), (10.0, -4.0)),
    "evening_start / evening_full": (
        ("evening_start_min", "evening_full_min"),
        (20 * 60, 22 * 60 + 30),
    ),
    "morning_start / morning_full": (
        ("morning_start_min", "morning_full_min"),
        (6 * 60, 7 * 60 + 30),
    ),
    "circadian_tick": (("circadian_tick",), (300.0,)),
    "evening_output_cap": (("evening_output_cap",), (0.3,)),
    "write_blank": (("write_blank",), (5.0,)),
    "tau_lux_up / tau_lux_down": (("tau_lux_up", "tau_lux_down"), (30.0, 60.0)),
    "night_prior_deg / tau_night_prior": (("night_prior_deg", "tau_night_prior"), (-6.0, 600.0)),
    "gain_learn_rate": (("gain_learn_rate",), (0.1,)),
    "lux_stale": (("lux_stale",), (120.0,)),
    "deadband_abs / deadband_rel": (("deadband_abs", "deadband_rel"), (5.0, 0.15)),
    "error_sustain / error_sustain_fast": (("error_sustain", "error_sustain_fast"), (20.0, 2.0)),
    "calibration_levels / calibration_dwell": (
        ("calibration_levels", "calibration_dwell"),
        ((0.10, 0.25, 0.50, 0.75, 1.0), 4.0),
    ),
    "band_overlap / boost_evening_max": (("band_overlap", "boost_evening_max"), (0.15, 0.5)),
    "ct_day / ct_evening / ct_min_evening": (
        ("ct_day", "ct_evening", "ct_min_evening"),
        (3300, 2400, 2200),
    ),
    "blend_threshold / blend_delta": (("blend_threshold", "blend_delta"), (0.1, 300)),
    "warm_dim_output": (("warm_dim_output",), (0.3,)),
    "ct_min_delta": (("ct_min_delta",), (100,)),
    "sleep_fade / night_hold / night_fade": (
        ("sleep_fade", "night_hold", "night_fade"),
        (4.0, 600.0, 10.0),
    ),
    "outdoor_on_threshold": (("outdoor_on_threshold",), (0.7,)),
    "gain_range_stops / gain_reset": (("gain_range_stops", "gain_reset"), (1.0, True)),
    "slew_step / slew_interval / slew_step_empty": (
        ("slew_step", "slew_interval", "slew_step_empty"),
        (0.1, 1.0, 0.25),
    ),
    "min_delta / min_write_interval / max_inflight": (
        ("min_delta", "min_write_interval", "max_inflight"),
        (0.03, 1.0, 3),
    ),
    "echo_window": (("echo_window",), (10.0,)),
    "override_timeout": (("override_timeout",), (4 * 3600.0,)),
    "startup_grace": (("startup_grace",), (30.0,)),
}


def test_dataclass_matches_doc_defaults() -> None:
    """Every §12 row maps to a field whose default equals the tabled value."""
    tun = Tunables()
    for row, (fields, defaults) in DOC_ROWS.items():
        for field, default in zip(fields, defaults, strict=True):
            assert hasattr(tun, field), f"{row}: missing field {field}"
            assert getattr(tun, field) == default, f"{row}: {field} default mismatch"


def test_every_field_is_documented() -> None:
    """No stray tunable exists that §12 does not describe (evening_cap_threshold
    is the one deliberate addition, tabled in the same PR)."""
    documented = {f for _, (fields, _d) in DOC_ROWS.items() for f in fields}
    documented.add("evening_cap_threshold")  # §2.4 gap, added to the table
    assert set(Tunables().__dataclass_fields__) == documented


def test_doc_table_lists_each_tunable() -> None:
    """Each documented knob name appears verbatim in the §12 table."""
    text = SPEC.read_text(encoding="utf-8")
    table = text.split("## 12. Tunables")[1]
    for row in DOC_ROWS:
        first = row.split(" / ")[0]
        assert first in table, f"{first} missing from §12 table"
    assert "evening_cap_threshold" in text  # tabled addition (§2.4/§12)


def test_validation_rejects_bad_values() -> None:
    """__post_init__ guards the invariants the engine relies on."""
    from dataclasses import replace

    import pytest

    base = Tunables()
    with pytest.raises(ValueError):
        replace(base, slew_step=0.0)
    with pytest.raises(ValueError):
        replace(base, sun_high_deg=-10.0)  # not above sun_low_deg
    with pytest.raises(ValueError):
        replace(base, morning_full_min=5 * 60)  # out of order vs morning_start


def test_gain_multiplier_curve() -> None:
    """Rule 7.1: 50 % neutral, 100 % -> x2, 0 % -> x0.5 at 1.0 stops."""
    assert gain_multiplier(50.0, 1.0) == 1.0
    assert gain_multiplier(100.0, 1.0) == 2.0
    assert gain_multiplier(0.0, 1.0) == 0.5
