"""Diagnostics platform — full engine + estimator state on demand (§10, D12).

Volatile internals never live in recorded entity attributes; they surface here
so support / debugging has the whole picture without polluting the recorder.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .controller import Controller


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    engine = controller.engine
    s = engine.state

    rooms: dict[str, Any] = {}
    for room in engine.config.rooms:
        rs = engine.room_state(room.room_id)
        rooms[room.room_id] = {
            "role": rs.role.value,
            "overridden": rs.overridden,
            "override_since": _plain(rs.override_since),
            "activity": _plain(rs.activity),
            "episode_peak": _plain(rs.episode_peak),
            "self_active": rs.self_active,
            "vacancy_hold_until": _plain(rs.vacancy_hold_until),
            "trigger_hold_until": _plain(rs.trigger_hold_until),
            "door_lighting": rs.door_lighting,
            "occupational": rs.occupational,
            "channels": {
                cid: {"commanded_b": cs.commanded_b, "commanded_ct": cs.commanded_ct, "on": cs.on}
                for cid, cs in rs.channels.items()
            },
            "estimator": _plain(rs.est),
            "calibration": _plain(engine.calibration_of(room.room_id).to_dict()),
        }

    return {
        "options": {k: v for k, v in entry.options.items() if k != "calibrations"},
        "engine": {
            "enabled": s.enabled,
            "sun_elevation": s.sun_elevation,
            "sleep": s.sleep,
            "anyone_home": s.anyone_home,
            "vacation": s.vacation,
            "away_lighting": s.away_lighting,
            "tv": _plain(s.tv),
            "tv_hold_until": _plain(s.tv_hold_until),
            "night_active": s.night_active,
            "night_hold_until": _plain(s.night_hold_until),
            "master_on": s.master_on,
            "master_pct": s.master_pct,
            "last_e": s.last_e,
        },
        "rooms": rooms,
    }
