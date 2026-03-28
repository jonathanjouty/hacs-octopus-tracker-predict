"""Config flow for Tracker Predict integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_REGION, DEFAULT_REGION, DOMAIN, REGIONS


class TrackerPredictConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tracker Predict."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            region = user_input[CONF_REGION]

            # Prevent duplicate entries for the same region
            await self.async_set_unique_id(f"tracker_predict_{region}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Tracker Predict ({REGIONS.get(region, region)})",
                data={CONF_REGION: region},
            )

        region_options = {code: f"{code} - {name}" for code, name in REGIONS.items()}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(
                        region_options
                    ),
                }
            ),
        )
