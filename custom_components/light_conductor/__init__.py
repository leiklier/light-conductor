"""Light Conductor — adaptive whole-home lighting for Home Assistant.

Setup ordering follows the family "restore before enforce" lesson
(grow-conductor): the controller and its engine are built eagerly so entities
can read state, platforms are forwarded FIRST so restorable entities re-hydrate
and enqueue their restored values, then the controller seeds an
:class:`InitialSnapshot` from restored + live world state and only *then* arms
subscriptions and the actor. The startup grace (rule §11.1) means no command is
written until the world has settled — no boot flash.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS, RUNTIME_OPTION_KEYS
from .controller import Controller

_LOGGER = logging.getLogger(__name__)

#: Per-entry baseline of the user-facing options; a runtime options write
#: (calibration commit) that leaves this unchanged must NOT reload the entry.
DATA_RELOAD_BASELINE = "reload_baseline"


def _reload_baseline(entry: ConfigEntry) -> dict:
    return {k: v for k, v in entry.options.items() if k not in RUNTIME_OPTION_KEYS}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Light Conductor config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    controller = Controller(hass, entry)
    domain_data[entry.entry_id] = controller
    hass.data.setdefault(f"{DOMAIN}_{DATA_RELOAD_BASELINE}", {})[entry.entry_id] = _reload_baseline(
        entry
    )

    # Platforms first: restorable entities re-hydrate and enqueue restore events
    # while the actor is still parked (grow-conductor restore-before-enforce).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Seed from restored + live state, then arm the actor (no boot flash, §11.1).
    snapshot = controller.build_snapshot()
    await controller.async_start(snapshot)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a Light Conductor config entry."""
    controller: Controller | None = hass.data[DOMAIN].get(entry.entry_id)
    if controller is not None:
        await controller.async_stop()  # cancel subscriptions/timers before platforms
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data.get(f"{DOMAIN}_{DATA_RELOAD_BASELINE}", {}).pop(entry.entry_id, None)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when user-facing options changed (skip runtime writes)."""
    baselines = hass.data.setdefault(f"{DOMAIN}_{DATA_RELOAD_BASELINE}", {})
    new_baseline = _reload_baseline(entry)
    if baselines.get(entry.entry_id) == new_baseline:
        # Only a runtime key (e.g. a calibration commit) moved — no reload.
        return
    baselines[entry.entry_id] = new_baseline
    await hass.config_entries.async_reload(entry.entry_id)
