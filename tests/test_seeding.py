"""Seeding: boot with a light already on must not flash it during grace (§11.1)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.light_conductor.controller import Controller

from .adapter import options, room, set_light, setup_entry


async def test_no_commands_during_startup_grace(hass: HomeAssistant, monkeypatch) -> None:
    # Count every write the controller would dispatch, regardless of the light
    # service (the real `light` component loads during setup).
    writes: list[object] = []

    def _spy(self: Controller, coro: object) -> None:
        writes.append(coro)
        getattr(coro, "close", lambda: None)()  # never actually run it

    monkeypatch.setattr(Controller, "async_run_write", _spy)

    # A light is already ON at boot; presence is off (engine would want OFF).
    set_light(hass, "light.k", "on", brightness=100, transition=True)
    hass.states.async_set("binary_sensor.pk", "off")

    # Real grace window (default 30 s) — do NOT set startup_grace=0 here.
    await setup_entry(
        hass,
        options([room("k", ["light.k"], presence="binary_sensor.pk")], instant=False),
    )

    # Within the grace window the engine adopts the on-light as its baseline and
    # emits nothing — no boot flash.
    assert writes == []
