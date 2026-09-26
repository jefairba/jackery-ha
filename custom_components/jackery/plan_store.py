"""Keep HA's view of Transfer Switch plans in step with the device.

No Home Assistant imports, so both the integration setup and the plan
entities can use it.
"""

from __future__ import annotations

from .const import DOMAIN


def publish_plans(hass, device_sn: str, plans: list[dict]) -> None:
    """Make a confirmed plan list the one HA shows and keeps.

    Updates both the per-device plan cache the coordinator re-injects on
    every poll and the coordinator's current data. Updating only the latter
    (as upstream did) was undone by the next poll.
    """
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        cache = entry_data.get("plan_caches", {}).get(device_sn)
        if cache is not None:
            cache["plans"] = plans
        for device in entry_data.get("devices", []):
            if device.get("devSn") != device_sn:
                continue
            coordinator = entry_data.get("coordinators", {}).get(device.get("devId"))
            if coordinator is not None and coordinator.data is not None:
                coordinator.async_set_updated_data({**coordinator.data, "_plans": plans})
