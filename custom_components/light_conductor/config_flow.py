"""Config flow (scaffold).

A single-step flow that creates one hub entry. ``entry.data`` stays empty
per the house convention (see const.py) — every user-facing setting will
live in ``entry.options``, populated by the options flow that lands with a
later bring-up PR.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector

from .const import DOMAIN

CONF_NAME = "name"


class LightConductorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of a Light Conductor hub."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=user_input[CONF_NAME], data={}, options={})

        schema = vol.Schema({vol.Required(CONF_NAME): TextSelector()})
        return self.async_show_form(step_id="user", data_schema=schema)
