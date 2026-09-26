"""Config flow for Jackery integration."""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import JackeryAPI, JackeryAuthenticationError, new_android_id
from .const import CONF_ANDROID_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict) -> dict[str, str]:
    """Validate the user input allows us to connect."""
    api = JackeryAPI(
        account=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        android_id=data[CONF_ANDROID_ID],
    )

    # The login method is synchronous, so we run it in an executor
    if not await hass.async_add_executor_job(api.login):
        raise JackeryAuthenticationError("Login returned false")

    # Return info we want to store in the config entry.
    return {"title": data[CONF_USERNAME]}


class JackeryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Jackery."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # One device identity per install, kept for the entry's lifetime.
            user_input = {**user_input, CONF_ANDROID_ID: new_android_id()}
            try:
                info = await validate_input(self.hass, user_input)
                # Set unique ID to prevent multiple configs for the same account
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=info["title"], data=user_input)
            except JackeryAuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )
