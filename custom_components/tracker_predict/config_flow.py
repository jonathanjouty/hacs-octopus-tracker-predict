"""Config flow for Tracker Predict integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)

from .const import (
    CONF_AGILE_PRODUCT_CODE,
    CONF_CALIBRATION_DAYS,
    CONF_CALIBRATION_INTERVAL,
    CONF_CHEAP_THRESHOLD_PERCENTILE,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_TRACKER_PRODUCT_CODE,
    DEFAULT_CALIBRATION_DAYS,
    DEFAULT_CALIBRATION_INTERVAL,
    DEFAULT_CHEAP_THRESHOLD_PERCENTILE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DOMAIN,
    KNOWN_TRACKER_PRODUCTS,
    REGIONS,
)


_TRACKER_PRODUCT_OPTIONS = {"": "Auto-detect"} | {c: c for c in KNOWN_TRACKER_PRODUCTS}


class TrackerPredictConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tracker Predict."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return TrackerPredictOptionsFlow()

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


class TrackerPredictOptionsFlow(OptionsFlow):
    """Handle options for Tracker Predict."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POLL_INTERVAL,
                        default=options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=1)),
                    vol.Optional(
                        CONF_CALIBRATION_DAYS,
                        default=options.get(
                            CONF_CALIBRATION_DAYS, DEFAULT_CALIBRATION_DAYS
                        ),
                    ): vol.All(int, vol.Range(min=1)),
                    vol.Optional(
                        CONF_CALIBRATION_INTERVAL,
                        default=options.get(
                            CONF_CALIBRATION_INTERVAL, DEFAULT_CALIBRATION_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=1)),
                    vol.Optional(
                        CONF_CHEAP_THRESHOLD_PERCENTILE,
                        default=options.get(
                            CONF_CHEAP_THRESHOLD_PERCENTILE,
                            DEFAULT_CHEAP_THRESHOLD_PERCENTILE,
                        ),
                    ): vol.All(int, vol.Range(min=1, max=99)),
                    vol.Optional(
                        CONF_AGILE_PRODUCT_CODE,
                        default=options.get(CONF_AGILE_PRODUCT_CODE, ""),
                    ): str,
                    vol.Optional(
                        CONF_TRACKER_PRODUCT_CODE,
                        default=options.get(CONF_TRACKER_PRODUCT_CODE, ""),
                    ): vol.In(_TRACKER_PRODUCT_OPTIONS),
                }
            ),
        )
