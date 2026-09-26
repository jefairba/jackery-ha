"""A 240V circuit must never be left switched on one leg only."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "custom_components" / "jackery"
TEST_PACKAGE = "jackery_circuit_legs_test"
_MISSING = object()


class HomeAssistantError(Exception):
    pass


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def async_write_ha_state(self):
        pass


def _load(stubbed):
    def install(name, module):
        stubbed.setdefault(name, sys.modules.get(name, _MISSING))
        sys.modules[name] = module

    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        install(name, module)

    enum = types.SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic")
    stub("homeassistant")
    stub("homeassistant.components")
    stub(
        "homeassistant.components.sensor",
        SensorDeviceClass=types.SimpleNamespace(POWER="power"),
        SensorEntity=object,
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    stub("homeassistant.components.switch", SwitchEntity=object)
    stub("homeassistant.const", EntityCategory=enum, UnitOfPower=types.SimpleNamespace(WATT="W"))
    stub("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    stub("homeassistant.helpers")
    stub("homeassistant.helpers.entity", DeviceInfo=dict)
    stub(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=object,
    )
    pkg = types.ModuleType(TEST_PACKAGE)
    pkg.__path__ = [str(PACKAGE_ROOT)]
    install(TEST_PACKAGE, pkg)
    stub(f"{TEST_PACKAGE}.api", JackeryAPI=object)
    stub(f"{TEST_PACKAGE}.const", DOMAIN="jackery")
    spec = importlib.util.spec_from_file_location(
        f"{TEST_PACKAGE}.circuit", PACKAGE_ROOT / "circuit.py"
    )
    module = importlib.util.module_from_spec(spec)
    install(spec.name, module)
    spec.loader.exec_module(module)
    return module


class FakeAPI:
    """Records circuit commands; fails the ones listed in ``fail``."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def async_set_circuit_switch(self, device_id, device_sn, idx, on):
        self.calls.append((idx, on))
        if (idx, on) in self.fail:
            raise TimeoutError(f"leg {idx} did not confirm")


class CircuitLegTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._stubbed = {}
        cls.circuit = _load(cls._stubbed)

    @classmethod
    def tearDownClass(cls):
        for name, previous in reversed(list(cls._stubbed.items())):
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def make_switch(self, api, indices, states):
        coordinator = types.SimpleNamespace(
            data={"_circuits": [{"idx": i, "sw": s} for i, s in zip(indices, states)]},
            async_request_refresh=AsyncMock(),
        )
        logical = {
            "primary_idx": indices[0],
            "partner_idx": indices[1] if len(indices) > 1 else None,
            "indices": list(indices),
            "name": "Dryer",
        }
        switch = self.circuit.JackeryCircuitSwitch(
            api, coordinator, {"devId": "ts-1", "devSn": "ts-sn"}, logical
        )
        return switch, coordinator

    async def test_both_legs_switch_when_both_confirm(self):
        api = FakeAPI()
        switch, coordinator = self.make_switch(api, [3, 4], [1, 1])
        await switch.async_turn_off()
        self.assertEqual(api.calls, [(3, False), (4, False)])
        self.assertEqual([c["sw"] for c in coordinator.data["_circuits"]], [0, 0])

    async def test_second_leg_failure_restores_both_legs(self):
        api = FakeAPI(fail={(4, False)})
        switch, coordinator = self.make_switch(api, [3, 4], [1, 1])
        with self.assertRaises(HomeAssistantError) as ctx:
            await switch.async_turn_off()
        # leg 3 switched, leg 4 failed (may have applied) -> both sent back on
        self.assertEqual(api.calls, [(3, False), (4, False), (3, True), (4, True)])
        self.assertIn("restored leg 3, leg 4", str(ctx.exception))
        self.assertNotIn("WARNING", str(ctx.exception))
        # state is not faked: the entity asks the device instead
        self.assertEqual([c["sw"] for c in coordinator.data["_circuits"]], [1, 1])
        coordinator.async_request_refresh.assert_awaited()

    async def test_failed_restore_warns_about_split_circuit(self):
        api = FakeAPI(fail={(4, False), (3, True)})
        switch, _ = self.make_switch(api, [3, 4], [1, 1])
        with self.assertRaises(HomeAssistantError) as ctx:
            with self.assertLogs(self.circuit._LOGGER, level="ERROR"):
                await switch.async_turn_off()
        message = str(ctx.exception)
        self.assertIn("WARNING: 240V circuit may be on only one leg", message)
        self.assertIn("leg 3", message)
        self.assertIn("restored leg 4", message)

    async def test_first_leg_failure_still_restores_it(self):
        api = FakeAPI(fail={(3, True)})
        switch, _ = self.make_switch(api, [3, 4], [0, 0])
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_on()
        # leg 4 never sent; leg 3 (may have applied) put back off
        self.assertEqual(api.calls, [(3, True), (3, False)])

    async def test_single_leg_failure_does_not_send_extra_commands(self):
        api = FakeAPI(fail={(5, False)})
        switch, _ = self.make_switch(api, [5], [1])
        with self.assertRaises(HomeAssistantError):
            await switch.async_turn_off()
        self.assertEqual(api.calls, [(5, False)])


if __name__ == "__main__":
    unittest.main()
