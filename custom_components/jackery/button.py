"""Button platform for Jackery."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .session import JackeryReclaimButton


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Jackery button entities."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            JackeryReclaimButton(
                entry_data["api"], config_entry.entry_id, entry_data["coordinators"]
            )
        ]
    )
