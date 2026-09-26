"""Entities for the account-wide Jackery cloud session.

Jackery allows one login per account, so HA and the phone app take turns.
These entities show when HA has stepped aside and let the user take the
session back. They belong to the account, not a device, so they live on a
separate "Jackery Cloud Session" device.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity

from .api import JackeryAPI
from .const import DOMAIN

# Poll the in-memory yield state often enough that the dashboard follows
# the phone app taking over and giving back the session.
SCAN_INTERVAL = timedelta(seconds=15)


def session_device_info(entry_id: str) -> dict:
    return {
        "identifiers": {(DOMAIN, f"{entry_id}_session")},
        "name": "Jackery Cloud Session",
        "manufacturer": "Jackery",
        "model": "Cloud account",
    }


class JackeryYieldingSensor(BinarySensorEntity):
    """On while HA is signed out so the Jackery app can use the account."""

    _attr_name = "Yielding to Jackery App"
    _attr_icon = "mdi:cellphone-arrow-down"
    _attr_should_poll = True

    def __init__(self, api: JackeryAPI, entry_id: str) -> None:
        self._api = api
        self._attr_unique_id = f"{entry_id}_session_yielding"
        self._attr_device_info = session_device_info(entry_id)

    @property
    def is_on(self) -> bool:
        return self._api.is_yielding()

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "minutes_remaining": round(self._api.yield_remaining() / 60, 1),
            "yield_minutes": round(self._api.yield_seconds / 60),
            "description": (
                "On when the Jackery app signed in and HA stepped aside instead "
                "of signing it out. Data goes stale and commands are refused "
                "until the timer ends or you press Reclaim Jackery Session."
            ),
        }


class JackeryReclaimButton(ButtonEntity):
    """End a yield now; HA signs back in (which signs the phone app out)."""

    _attr_name = "Reclaim Jackery Session"
    _attr_icon = "mdi:account-arrow-left"

    def __init__(self, api: JackeryAPI, entry_id: str, coordinators: dict) -> None:
        self._api = api
        self._coordinators = coordinators
        self._attr_unique_id = f"{entry_id}_session_reclaim"
        self._attr_device_info = session_device_info(entry_id)

    async def async_press(self) -> None:
        self._api.reclaim_session()
        for coordinator in self._coordinators.values():
            await coordinator.async_request_refresh()
