"""Tests for the reauth step of the config flow."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "custom_components" / "jackery"
TEST_PACKAGE = "jackery_config_flow_test"
_MISSING = object()


class ConfigFlow:
    """Minimal stand-in for homeassistant.config_entries.ConfigFlow."""

    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def _get_reauth_entry(self):
        return self.reauth_entry

    def async_update_reload_and_abort(self, entry, *, data):
        self.updated = (entry, data)
        return {"type": "abort", "reason": "reauth_successful"}


class JackeryAuthenticationError(Exception):
    pass


class FakeAPI:
    accepted_password = "new-password"
    calls: list[dict] = []

    def __init__(self, account, password, android_id):
        self.kwargs = {"account": account, "password": password, "android_id": android_id}
        type(self).calls.append(self.kwargs)

    def login(self):
        if self.kwargs["password"] != self.accepted_password:
            raise JackeryAuthenticationError("bad password")
        return True


def _load(stubbed: dict):
    def install(name, module):
        stubbed.setdefault(name, sys.modules.get(name, _MISSING))
        sys.modules[name] = module

    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        install(name, module)

    stub("homeassistant")
    stub("homeassistant.config_entries", ConfigFlow=ConfigFlow)
    stub("homeassistant.const", CONF_PASSWORD="password", CONF_USERNAME="username")
    stub("homeassistant.core", HomeAssistant=object)
    pkg = types.ModuleType(TEST_PACKAGE)
    pkg.__path__ = [str(PACKAGE_ROOT)]
    install(TEST_PACKAGE, pkg)
    stub(
        f"{TEST_PACKAGE}.api",
        JackeryAPI=FakeAPI,
        JackeryAuthenticationError=JackeryAuthenticationError,
        new_android_id=lambda: "0123456789abcdef",
    )
    stub(f"{TEST_PACKAGE}.const", DOMAIN="jackery", CONF_ANDROID_ID="android_id")

    spec = importlib.util.spec_from_file_location(
        f"{TEST_PACKAGE}.config_flow", PACKAGE_ROOT / "config_flow.py"
    )
    module = importlib.util.module_from_spec(spec)
    install(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _restore(stubbed: dict):
    for name, previous in reversed(list(stubbed.items())):
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class ReauthFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._stubbed = {}
        cls.flow_module = _load(cls._stubbed)

    @classmethod
    def tearDownClass(cls):
        _restore(cls._stubbed)

    def setUp(self):
        FakeAPI.calls = []
        self.entry = types.SimpleNamespace(
            data={
                "username": "me@example.com",
                "password": "old-password",
                "android_id": "feedfacecafebeef",
            }
        )
        self.flow = self.flow_module.JackeryConfigFlow()
        self.flow.reauth_entry = self.entry

        async def run_in_executor(func, *args):
            return func(*args)

        self.flow.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)

    async def test_reauth_shows_password_form_for_account(self):
        result = await self.flow.async_step_reauth(self.entry.data)
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(result["description_placeholders"], {"username": "me@example.com"})

    async def test_good_password_updates_entry_and_keeps_identity(self):
        result = await self.flow.async_step_reauth_confirm({"password": "new-password"})
        self.assertEqual(result, {"type": "abort", "reason": "reauth_successful"})
        entry, data = self.flow.updated
        self.assertIs(entry, self.entry)
        self.assertEqual(data["password"], "new-password")
        self.assertEqual(data["username"], "me@example.com")
        self.assertEqual(data["android_id"], "feedfacecafebeef")
        self.assertEqual(FakeAPI.calls[-1]["android_id"], "feedfacecafebeef")

    async def test_bad_password_shows_error_and_changes_nothing(self):
        result = await self.flow.async_step_reauth_confirm({"password": "wrong"})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertFalse(hasattr(self.flow, "updated"))
        self.assertEqual(self.entry.data["password"], "old-password")


class TranslationTests(unittest.TestCase):
    def test_reauth_strings_exist(self):
        strings = json.loads((PACKAGE_ROOT / "translations" / "en.json").read_text())
        self.assertIn("reauth_confirm", strings["config"]["step"])
        self.assertIn("{username}", strings["config"]["step"]["reauth_confirm"]["description"])
        self.assertIn("reauth_successful", strings["config"]["abort"])


if __name__ == "__main__":
    unittest.main()
