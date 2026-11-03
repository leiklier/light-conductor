"""The live house's room options (diagnostics, 2026-11-01) — test fixture.

Not collected by pytest. Used by the property tests that must hold for the
configuration the conductor actually runs (D27/D33), not just the synthetic
apartment in ``helpers``.
"""

from __future__ import annotations

from typing import Any

#: sofakrok's committed calibration (diagnostics 2026-11-01).
SOFAKROK_GAIN = 8.89166628476232
SOFAKROK_CURVE = (
    (0.0, 0.0),
    (0.1, 0.12558575979686565),
    (0.25, 0.3402061965551038),
    (0.5, 0.7441424873953103),
    (0.75, 1.0),
    (1.0, 1.0),
)

LIVE_ROOMS: list[dict[str, Any]] = [
    {
        "activity_sensor": "sensor.presence_conductor_sofakrok_room_activity",
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.sofakrok_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            }
        ],
        "daylight_full": 150.0,
        "daylight_reference": "spisebord",
        "living_group": True,
        "lux_sensor": "sensor.apollo_msr_2_f79794_ltr390_light",
        "name": "Sofakrok",
        "neighbours": ["spisebord", "kjokken", "kontor", "balkong"],
        "occupancy_fallback": ["binary_sensor.sofakrok_occupancy"],
        "presence_primary": "binary_sensor.presence_conductor_sofakrok_room_occupancy",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.08,
            "evening_output_cap": 0.3,
            "lux_active_day": 8.0,
            "lux_active_evening": 3.5,
            "night_output": 0.04,
            "tv_output": 0.05,
            "tv_output_empty": 0.0,
            "tv_output_paused": 0.2,
            "tv_output_paused_empty": 0.1,
            "vacancy": "dim",
        },
        "room_id": "sofakrok",
        "shape": "presence",
        "sleep_keeps_override": False,
        "tv_mode": True,
        "wall_event_entities": ["event.sofakrok_taklys_3"],
    },
    {
        "activity_sensor": "sensor.presence_conductor_spisebord_room_activity",
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.spisebord_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            }
        ],
        "daylight_full": 150.0,
        "living_group": True,
        "lux_sensor": "sensor.apollo_msr_2_fadea8_ltr390_light",
        "name": "Spisebord",
        "neighbours": ["sofakrok", "kjokken", "kontor", "balkong"],
        "occupancy_fallback": ["binary_sensor.spisebord_occupancy"],
        "presence_primary": "binary_sensor.presence_conductor_spisebord_room_occupancy",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.08,
            "evening_output_cap": 0.3,
            "night_output": 0.02,
            "tv_output": 0.15,
            "tv_output_empty": 0.05,
            "tv_output_paused": 0.22,
            "tv_output_paused_empty": 0.1,
            "vacancy": "dim",
        },
        "room_id": "spisebord",
        "shape": "presence",
        "sleep_keeps_override": False,
        "tv_mode": True,
    },
    {
        "activity_sensor": "sensor.presence_conductor_kjokken_room_activity",
        "channels": [
            {
                "band": "accent",
                "dim_floor": 0.02,
                "entity": "light.kjokken_downlights",
                "fixed_ct": None,
                "weight": 1.0,
            },
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.kjokken_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            },
            {
                "band": "boost",
                "dim_floor": 0.02,
                "entity": "light.kjokken_benkebelysning",
                "fixed_ct": 2700,
                "response_offset": -0.5,
                "response_slope": 0.8,
                "weight": 1.0,
            },
        ],
        "daylight_full": 80.0,
        "living_group": True,
        "lux_sensor": "sensor.apollo_msr_2_29abc4_ltr390_light",
        "name": "Kjøkken",
        "neighbours": ["spisebord", "sofakrok"],
        "occupancy_fallback": ["binary_sensor.kjokken_occupancy"],
        "presence_primary": "binary_sensor.presence_conductor_kjokken_room_occupancy",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.25,
            "background_output": 0.08,
            "boost_evening_output": 0.85,
            "evening_output_cap": 0.25,
            "night_output": 0.2,
            "tv_output": 0.15,
            "tv_output_empty": 0.0,
            "tv_output_paused": 0.3,
            "tv_output_paused_empty": 0.15,
            "vacancy": "dim",
        },
        "room_id": "kjokken",
        "shape": "presence",
        "sleep_keeps_override": False,
        "tv_mode": False,
    },
    {
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.gang_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            }
        ],
        "daylight_full": 100.0,
        "daylight_reference": "spisebord",
        "living_group": False,
        "name": "Gang",
        "neighbours": ["kjokken", "spisebord", "sofakrok", "kontor", "soverom"],
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.08,
            "evening_output_cap": 0.3,
            "night_output": 0.05,
            "tv_output": 0.05,
            "tv_output_empty": 0.05,
            "tv_output_paused": 0.1,
            "tv_output_paused_empty": 0.1,
            "vacancy": "dim",
        },
        "room_id": "gang",
        "shape": "corridor",
        "sleep_keeps_override": False,
        "tv_mode": True,
        "wall_event_entities": ["event.gang_taklys_3"],
    },
    {
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.balkong_taklys",
                "fixed_ct": None,
                "weight": 1.0,
            }
        ],
        "living_group": True,
        "lux_sensor": "sensor.apollo_msr_2_fadea8_ltr390_light",
        "name": "Balkong",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.15,
            "evening_output_cap": 0.3,
            "night_output": 0.05,
            "tv_output": 0.15,
            "tv_output_empty": 0.0,
            "vacancy": "dim",
        },
        "room_id": "balkong",
        "shape": "outdoor",
        "tv_mode": False,
    },
    {
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.soverom_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            }
        ],
        "living_group": False,
        "name": "Soverom",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.08,
            "evening_output_cap": 0.3,
            "night_output": 0.05,
            "tv_output": 0.15,
            "tv_output_empty": 0.0,
            "tv_output_paused": 0.3,
            "tv_output_paused_empty": 0.15,
            "vacancy": "off",
        },
        "room_id": "soverom",
        "shape": "door",
        "sleep_keeps_override": True,
        "trigger_entities": ["binary_sensor.soverom_dor"],
        "tv_mode": False,
        "wall_event_entities": ["event.soverom_taklys_3"],
    },
    {
        "activity_sensor": "sensor.presence_conductor_kontor_room_activity",
        "channels": [
            {
                "band": "primary",
                "dim_floor": 0.02,
                "entity": "light.kontor_taklys",
                "fixed_ct": 2700,
                "weight": 1.0,
            }
        ],
        "living_group": True,
        "lux_sensor": "sensor.apollo_msr_2_f77c08_ltr390_light",
        "name": "Kontor",
        "occupancy_fallback": ["binary_sensor.kontor_occupancy", "binary_sensor.kontor_pir_sensor"],
        "presence_primary": "binary_sensor.presence_conductor_kontor_room_occupancy",
        "profile": {
            "active_day_output": 1.0,
            "active_evening_output": 0.3,
            "background_output": 0.08,
            "evening_output_cap": 0.3,
            "night_output": 0.05,
            "tv_output": 0.15,
            "tv_output_empty": 0.0,
            "tv_output_paused": 0.3,
            "tv_output_paused_empty": 0.15,
            "vacancy": "off",
        },
        "room_id": "kontor",
        "shape": "presence",
        "sleep_keeps_override": False,
        "tv_mode": False,
        "wall_event_entities": ["event.kontor_taklys_3"],
    },
]


LIVE_OPTIONS: dict[str, Any] = {
    "rooms": LIVE_ROOMS,
    "night_path_rooms": ["sofakrok", "gang", "spisebord", "kjokken"],
    "night_trigger_entities": ["binary_sensor.soverom_dor"],
    "sleep_entity": "binary_sensor.household_sleep_mode",
    "anyone_home_entity": "binary_sensor.presence_conductor_anyone_home",
}
