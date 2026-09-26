"""Tests that credentials and network identifiers never reach the log."""

from __future__ import annotations

import importlib.util
import logging
import unittest
from pathlib import Path
from unittest import mock

import requests

API_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "jackery" / "api.py"
)


def load_api():
    spec = importlib.util.spec_from_file_location("jackery_api_redaction_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = load_api()

LOGIN_RESPONSE = {
    "code": 0,
    "token": "secret-session-token",
    "data": {"userId": "1234567", "mqttPassWord": "c2VjcmV0LW1xdHQ="},
}


class RedactTests(unittest.TestCase):
    def test_masks_nested_sensitive_keys(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "properties": {"rb": 86, "wname": "HomeWiFi", "wip": "192.168.4.50"},
                "devices": [{"devSn": "SN1", "mac": "aa:bb:cc:dd:ee:ff"}],
            },
        }
        redacted = api._redact(payload)
        self.assertEqual(redacted["data"]["properties"]["rb"], 86)
        self.assertEqual(redacted["data"]["devices"][0]["devSn"], "SN1")
        for value in ("HomeWiFi", "192.168.4.50", "aa:bb:cc:dd:ee:ff"):
            self.assertNotIn(value, repr(redacted))

    def test_does_not_mutate_input(self) -> None:
        payload = {"token": "t"}
        api._redact(payload)
        self.assertEqual(payload, {"token": "t"})


class LoginLoggingTests(unittest.TestCase):
    def _api(self):
        return api.JackeryAPI(account="me@example.com", password="hunter2")

    def test_debug_log_of_login_response_is_redacted(self) -> None:
        response = mock.Mock(status_code=200)
        response.json.return_value = LOGIN_RESPONSE
        with mock.patch.object(api.requests, "post", return_value=response):
            with self.assertLogs(api._LOGGER, level=logging.DEBUG) as logs:
                self.assertTrue(self._api().login())
        output = "\n".join(logs.output)
        for secret in ("secret-session-token", "c2VjcmV0LW1xdHQ=", "1234567"):
            self.assertNotIn(secret, output)

    def test_http_error_does_not_leak_encrypted_password_url(self) -> None:
        leaked_url = "https://iot.jackeryapp.com/v1/auth/login?aesEncryptData=ENCRYPTED"
        error = requests.HTTPError(
            f"500 Server Error for url: {leaked_url}",
            response=mock.Mock(status_code=500),
        )
        response = mock.Mock(status_code=500)
        response.raise_for_status.side_effect = error
        with mock.patch.object(api.requests, "post", return_value=response):
            with self.assertLogs(api._LOGGER, level=logging.DEBUG) as logs:
                with self.assertRaises(api.JackeryConnectionError) as ctx:
                    self._api().login()
        self.assertNotIn("ENCRYPTED", "\n".join(logs.output))
        self.assertNotIn("ENCRYPTED", str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_network_outage_during_login_is_not_an_auth_failure(self) -> None:
        """No internet (e.g. HA up before the router after a power cut) must
        be retried, not treated as a bad password that parks the entry in
        reauth."""
        with mock.patch.object(
            api.requests, "post", side_effect=requests.ConnectionError("no route")
        ):
            with self.assertRaises(api.JackeryConnectionError):
                self._api().login()
        self.assertFalse(
            issubclass(api.JackeryConnectionError, api.JackeryAuthenticationError)
        )

    def test_rejected_credentials_are_still_an_auth_failure(self) -> None:
        response = mock.Mock(status_code=200)
        response.json.return_value = {"code": 10001, "msg": "wrong password"}
        with mock.patch.object(api.requests, "post", return_value=response):
            with self.assertRaises(api.JackeryAuthenticationError):
                self._api().login()


class DeviceIdentityTests(unittest.TestCase):
    def test_no_shared_default_identity(self) -> None:
        a = api.JackeryAPI(account="a", password="p")
        b = api.JackeryAPI(account="a", password="p")
        self.assertNotEqual(a.android_id, "abcd1234567890ef")
        self.assertNotEqual(a.android_id, b.android_id)
        self.assertRegex(a.android_id, r"^[0-9a-f]{16}$")

    def test_stable_id_gives_stable_mac_id(self) -> None:
        a = api.JackeryAPI(account="a", password="p", android_id="feedfacecafebeef")
        b = api.JackeryAPI(account="a", password="p", android_id="feedfacecafebeef")
        self.assertEqual(a._mac_id, b._mac_id)
        self.assertEqual(a._mac_id, a._generate_udid())


if __name__ == "__main__":
    unittest.main()
