"""The Yielding sensor and Reclaim button reflect and control the API's yield."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "custom_components" / "jackery"
TEST_PACKAGE = "jackery_session_entities_test"
_MISSING = object()


def _load(stubbed):
    def install(name, module):
        stubbed.setdefault(name, sys.modules.get(name, _MISSING))
        sys.modules[name] = module

    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        install(name, module)

    stub("homeassistant")
    stub("homeassistant.components")
    stub("homeassistant.components.binary_sensor", BinarySensorEntity=object)
    stub("homeassistant.components.button", ButtonEntity=object)
    pkg = types.ModuleType(TEST_PACKAGE)
    pkg.__path__ = [str(PACKAGE_ROOT)]
    install(TEST_PACKAGE, pkg)
    stub(f"{TEST_PACKAGE}.api", JackeryAPI=object)
    stub(f"{TEST_PACKAGE}.const", DOMAIN="jackery")
    spec = importlib.util.spec_from_file_location(
        f"{TEST_PACKAGE}.session", PACKAGE_ROOT / "session.py"
    )
    module = importlib.util.module_from_spec(spec)
    install(spec.name, module)
    spec.loader.exec_module(module)
    return module


class SessionEntityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._stubbed = {}
        cls.session = _load(cls._stubbed)

    @classmethod
    def tearDownClass(cls):
        for name, previous in reversed(list(cls._stubbed.items())):
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def make_api(self, yielding, remaining=0.0):
        return types.SimpleNamespace(
            is_yielding=lambda: yielding,
            yield_remaining=lambda: remaining,
            yield_seconds=900,
            reclaim_session=Mock(),
        )

    def test_sensor_reports_yield_and_time_left(self):
        sensor = self.session.JackeryYieldingSensor(self.make_api(True, 450), "e1")
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.extra_state_attributes["minutes_remaining"], 7.5)
        self.assertEqual(sensor.extra_state_attributes["yield_minutes"], 15)

    def test_sensor_off_when_not_yielding(self):
        sensor = self.session.JackeryYieldingSensor(self.make_api(False), "e1")
        self.assertFalse(sensor.is_on)

    async def test_reclaim_ends_yield_and_refreshes_every_device(self):
        api = self.make_api(True, 300)
        coordinators = {
            "a": types.SimpleNamespace(async_request_refresh=AsyncMock()),
            "b": types.SimpleNamespace(async_request_refresh=AsyncMock()),
        }
        button = self.session.JackeryReclaimButton(api, "e1", coordinators)
        await button.async_press()
        api.reclaim_session.assert_called_once()
        for coordinator in coordinators.values():
            coordinator.async_request_refresh.assert_awaited_once()

    def test_both_entities_share_one_session_device(self):
        api = self.make_api(False)
        sensor = self.session.JackeryYieldingSensor(api, "e1")
        button = self.session.JackeryReclaimButton(api, "e1", {})
        self.assertEqual(sensor._attr_device_info, button._attr_device_info)
        self.assertNotEqual(sensor._attr_unique_id, button._attr_unique_id)


if __name__ == "__main__":
    unittest.main()
