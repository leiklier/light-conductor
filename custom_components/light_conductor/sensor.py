"""Per-room diagnostic sensors (§10) with a fresh publish gate.

Recorder discipline (presence-conductor lesson, D12): the measurement sensors
(natural lux, target output) publish through a *quantize + rate-limit* gate — a
value only reaches the state machine when its bucket changes AND at least
``MIN_PUBLISH_INTERVAL`` has passed. Estimator wiggle inside a bucket produces
no state write at all. Availability transitions bypass the gate. Volatile
internals never appear as recorded attributes — they live in ``diagnostics.py``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX, MATCH_ALL, PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LUX_SENSOR, CONF_NAME, CONF_ROOM_ID, CONF_ROOMS, DOMAIN
from .controller import Controller, _monotonic
from .core.model import Role
from .entity import LightConductorEntity, room_device_info

#: Lux gate: 5-lx buckets, ≥10 s min interval (spec §10). The core already
#: rounds these to 0.1 lx; the gate is the recorder-discipline layer on top.
LUX_BUCKET = 5.0
MIN_PUBLISH_INTERVAL = 10.0

ROLE_OPTIONS = [r.value for r in Role]


class PublishGate:
    """Quantize + rate-limit a measurement before it reaches the recorder."""

    def __init__(self, bucket: float, min_interval: float) -> None:
        self._bucket = bucket
        self._min_interval = min_interval
        self._published: float | None = None
        self._last = 0.0
        self._available = False

    def evaluate(self, raw: float | None) -> tuple[float | None, bool, bool]:
        """Return ``(value, available, changed)`` for a raw reading."""
        if raw is None:
            changed = self._available
            self._available = False
            return None, False, changed
        q = round(raw / self._bucket) * self._bucket
        now = _monotonic()
        if not self._available:  # availability recovery bypasses the interval
            self._available = True
            self._published = q
            self._last = now
            return q, True, True
        if q == self._published:
            return self._published, True, False
        if now - self._last >= self._min_interval:
            self._published = q
            self._last = now
            return q, True, True
        return self._published, True, False


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller: Controller = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for room in entry.options.get(CONF_ROOMS, ()):
        rid = room[CONF_ROOM_ID]
        name = room.get(CONF_NAME, rid)
        entities.append(RoleSensor(controller, rid, name))
        # Per-channel commanded outputs — a disabled-by-default debug sensor (§10).
        entities.append(ChannelsSensor(controller, rid, name))
        # Natural- and target-lux are closed-loop quantities: only meaningful
        # for a room with a lux sensor (§10), like the calibration button.
        if room.get(CONF_LUX_SENSOR):
            entities.append(NaturalLuxSensor(controller, rid, name))
            entities.append(TargetLuxSensor(controller, rid, name))
    async_add_entities(entities)


class _RoomSensor(LightConductorEntity, SensorEntity):
    def __init__(self, controller: Controller, room_id: str, name: str, suffix: str) -> None:
        super().__init__(controller, f"{room_id}_{suffix}")
        self._room_id = room_id
        self._attr_device_info = room_device_info(controller.entry, room_id, name)

    def _diag(self):
        return self.controller.diagnostics.get(self._room_id)


class RoleSensor(_RoomSensor):
    """Current room role (enum, §1)."""

    _attr_translation_key = "role"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ROLE_OPTIONS

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, room_id, name, "role")

    @property
    def native_value(self) -> str | None:
        diag = self._diag()
        return diag.role.value if diag is not None else None


class ChannelsSensor(_RoomSensor):
    """Per-channel commanded outputs for debugging (§10, recorder-safe).

    Disabled by default (registry opt-in): the operator enables it only while
    debugging. State is the room's peak commanded output as a whole percent;
    attributes carry one entry per channel — ``{output_pct, ct, on}`` — sourced
    from the engine's per-channel commanded state (the same data diagnostics.py
    exposes). ALL attributes are unrecorded (``MATCH_ALL``) so even when enabled
    the per-channel churn never reaches the recorder, and pushes are gated to a
    changed commanded value AND a ≥ ``MIN_PUBLISH_INTERVAL`` interval — it
    piggybacks the engine publish signal (no new timers), preserving the
    zero-write recorder discipline the gated lux sensors uphold.
    """

    _attr_translation_key = "channels"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, room_id, name, "channels")
        self._state: int | None = None
        self._attrs: dict[str, dict[str, Any]] = {}
        self._sig: tuple[Any, ...] | None = None
        self._last = 0.0

    def _compute(self) -> tuple[int, dict[str, dict[str, Any]]]:
        try:
            channels = self.controller.engine.room_state(self._room_id).channels
        except KeyError:  # room not seeded yet (defensive)
            return 0, {}
        attrs: dict[str, dict[str, Any]] = {}
        peak = 0.0
        for cid, cs in channels.items():
            peak = max(peak, cs.commanded_b)
            attrs[cid] = {
                "output_pct": round(cs.commanded_b * 100.0),
                "ct": None if cs.commanded_ct is None else round(cs.commanded_ct),
                "on": cs.on,
            }
        return round(peak * 100.0), attrs

    @staticmethod
    def _signature(state: int, attrs: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
        # Quantized identity: sub-percent / sub-kelvin wiggle is not a change.
        return (
            state,
            tuple(sorted((cid, a["output_pct"], a["ct"], a["on"]) for cid, a in attrs.items())),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._state, self._attrs = self._compute()
        self._sig = self._signature(self._state, self._attrs)
        # Leave ``_last`` at 0.0 so the FIRST commanded change after startup
        # publishes promptly (the controller builds a snapshot-less engine before
        # async_start rebuilds + reconciles it — the real value lands after add);
        # the interval only throttles *subsequent* churn.

    @callback
    def _on_engine_update(self) -> None:
        state, attrs = self._compute()
        sig = self._signature(state, attrs)
        if sig == self._sig:
            return  # no commanded value changed — nothing to record
        if _monotonic() - self._last < MIN_PUBLISH_INTERVAL:
            return  # rate-limit churn (recorder discipline; a later tick pushes)
        self._state, self._attrs, self._sig = state, attrs, sig
        self._last = _monotonic()
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs


class _GatedSensor(_RoomSensor):
    """A measurement sensor writing state only when its gate opens."""

    _bucket = LUX_BUCKET

    def __init__(self, controller: Controller, room_id: str, name: str, suffix: str) -> None:
        super().__init__(controller, room_id, name, suffix)
        self._gate = PublishGate(self._bucket, MIN_PUBLISH_INTERVAL)
        self._value: float | None = None
        self._available = False

    def _raw(self) -> float | None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh(write=False)

    @callback
    def _on_engine_update(self) -> None:
        # Run the gate first; only touch the state machine when it opens.
        self._refresh(write=True)

    def _refresh(self, write: bool = True) -> None:
        value, available, changed = self._gate.evaluate(self._raw())
        self._value = value
        self._available = available
        if changed and write:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def native_value(self) -> float | None:
        return self._value


class NaturalLuxSensor(_GatedSensor):
    """Estimated natural light at the room's sensor (§3), gated 5-lx buckets."""

    _attr_translation_key = "natural_lux"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT
    _bucket = LUX_BUCKET

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, room_id, name, "natural_lux")

    def _raw(self) -> float | None:
        diag = self._diag()
        return diag.natural_lux if diag is not None else None


class TargetLuxSensor(_GatedSensor):
    """Target illuminance the closed loop is regulating toward (§2/§10), gated.

    ``None`` (open-loop / stale sensor) renders the sensor unavailable.
    """

    _attr_translation_key = "target_lux"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT
    _bucket = LUX_BUCKET

    def __init__(self, controller: Controller, room_id: str, name: str) -> None:
        super().__init__(controller, room_id, name, "target_lux")

    def _raw(self) -> float | None:
        diag = self._diag()
        return diag.target_lux if diag is not None else None
