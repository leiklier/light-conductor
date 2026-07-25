"""Light Conductor — adaptive whole-home lighting for Home Assistant.

Scaffold entry point: it stores a per-entry placeholder and forwards no
platforms yet. The controller, platforms, and options flow arrive in the
stacked bring-up PRs (see docs/ENGINE_SPEC.md).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Light Conductor config entry (placeholder wiring)."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = None
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a Light Conductor config entry."""
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
