"""HA backs off when the phone app takes the one-login-per-account session."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

API_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "jackery" / "api.py"
)
spec = importlib.util.spec_from_file_location("jackery_api_yield_test", API_PATH)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def response(payload):
    resp = mock.Mock(status_code=200)
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    return resp


DISPLACED = {"code": 10403, "msg": "Account logged in elsewhere"}
OK = {"code": 0, "data": {"rb": 86}}


def make_api():
    client = api.JackeryAPI("me@example.com", "pw", android_id="feedfacecafebeef")
    client._token = "t"
    client.login = mock.Mock(side_effect=lambda: setattr(client, "_token", "t2") or True)
    return client


class YieldTests(unittest.TestCase):
    def test_default_yield_is_15_minutes(self):
        self.assertEqual(make_api().yield_seconds, 900)

    def test_displacement_yields_instead_of_logging_back_in(self):
        client = make_api()
        with mock.patch.object(api.requests, "get", return_value=response(DISPLACED)):
            with self.assertLogs(api._LOGGER, "WARNING"):
                with self.assertRaises(api.JackerySessionYielded):
                    client._get_request("/v1/device/property")
        client.login.assert_not_called()
        self.assertTrue(client.is_yielding())
        self.assertIsNone(client._token)

    def test_no_network_calls_while_yielding(self):
        client = make_api()
        client._start_yield()
        with mock.patch.object(api.requests, "get") as get:
            with self.assertRaises(api.JackerySessionYielded) as ctx:
                client._get_request("/v1/device/property")
        get.assert_not_called()
        client.login.assert_not_called()
        self.assertIn("Reclaim Jackery Session", str(ctx.exception))

    def test_yield_expires(self):
        client = make_api()
        client.yield_seconds = 60
        with mock.patch.object(api.time, "monotonic", return_value=1000.0):
            client._start_yield()
        with mock.patch.object(api.time, "monotonic", return_value=1059.0):
            self.assertTrue(client.is_yielding())
        with mock.patch.object(api.time, "monotonic", return_value=1061.0):
            self.assertFalse(client.is_yielding())

    def test_reclaim_logs_back_in_on_next_request(self):
        client = make_api()
        client._start_yield()
        client.reclaim_session()
        self.assertFalse(client.is_yielding())
        with mock.patch.object(api.requests, "get", return_value=response(OK)):
            self.assertEqual(client._get_request("/v1/device/property"), OK)
        client.login.assert_called_once()

    def test_commands_fail_fast_while_yielding(self):
        client = make_api()
        session = mock.Mock()
        session.publish_and_wait = mock.AsyncMock()
        client._mqtt_session = session
        client._start_yield()
        with self.assertRaises(api.JackerySessionYielded):
            asyncio.run(client.async_send_device_command("d", "sn", 3, {"cmd": 5, "rc": 1}))
        session.publish_and_wait.assert_not_awaited()

    def test_yielded_is_a_command_error(self):
        self.assertTrue(issubclass(api.JackerySessionYielded, api.JackeryCommandError))


class MqttYieldTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_waits_until_yield_is_over(self):
        client = make_api()
        client.yield_seconds = 60
        client._start_yield()
        mqtt = api.JackeryMqttSession(client)
        mqtt._running = True
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            client.reclaim_session()  # user presses Reclaim

        with mock.patch.object(api.asyncio, "sleep", fake_sleep):
            await mqtt._wait_out_yield()
        self.assertEqual(len(sleeps), 1)
        self.assertLessEqual(sleeps[0], 5.0)
        self.assertFalse(client.is_yielding())


class SharedDeviceTests(unittest.TestCase):
    def test_shared_devices_are_added_and_marked(self):
        client = make_api()
        owned = {"code": 0, "data": [{"devId": "1", "devSn": "OWNED"}]}
        shared_accounts = {
            "code": 0,
            "data": {"receive": [{"bindUserId": 42, "level": 1, "userName": "owner"}]},
        }
        shared_devices = {
            "code": 0,
            "data": [
                {"devId": "2", "devSn": "TS", "devNickname": "Transfer Switch"},
                {"devId": "1", "devSn": "OWNED"},
            ],
        }

        def fake_get(url, **kw):
            return response(owned if url.endswith("/bind/list") else shared_accounts)

        with mock.patch.object(api.requests, "get", side_effect=fake_get), mock.patch.object(
            api.requests, "post", return_value=response(shared_devices)
        ) as post:
            result = client.get_device_list()

        sns = [d["devSn"] for d in result["data"]]
        self.assertEqual(sns, ["OWNED", "TS"])
        ts = result["data"][1]
        self.assertTrue(ts["shared"])
        self.assertEqual(ts["shareLevel"], 1)
        self.assertEqual(ts["devName"], "Transfer Switch")
        self.assertEqual(post.call_args.kwargs["data"], {"bindUserId": "42", "level": "1"})

    def test_shared_lookup_failure_keeps_owned_devices(self):
        client = make_api()
        owned = {"code": 0, "data": [{"devId": "1", "devSn": "OWNED"}]}

        def fake_get(url, **kw):
            if url.endswith("/bind/list"):
                return response(owned)
            return response({"code": 500, "msg": "boom"})

        with mock.patch.object(api.requests, "get", side_effect=fake_get):
            with self.assertLogs(api._LOGGER, "WARNING"):
                result = client.get_device_list()
        self.assertEqual([d["devSn"] for d in result["data"]], ["OWNED"])


if __name__ == "__main__":
    unittest.main()
