"""A command only counts as done when the device confirms the new value."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

API_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "jackery" / "api.py"
)
spec = importlib.util.spec_from_file_location("jackery_api_confirm_test", API_PATH)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

FORCE_CHARGE = {"cmd": 5, "rc": 1}


class FakeSession:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.sent, self.timeout = reply, error, [], None

    async def publish_and_wait(self, payload, matcher, timeout):
        self.sent.append(payload)
        self.timeout = timeout
        if self.error:
            raise self.error
        return self.reply


def make_api(session):
    client = api.JackeryAPI("user@example.com", "pw", android_id="feedfacecafebeef")
    client._mqtt_session = session
    return client


class CommandConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, session, body=FORCE_CHARGE, **kw):
        return await make_api(session).async_send_device_command(
            "dev-1", "ts-sn", 3, dict(body), **kw
        )

    async def test_confirmed_when_device_reports_commanded_value(self):
        reply = {"deviceSn": "ts-sn", "actionId": 3, "body": {"cmd": 5, "rc": 1}}
        session = FakeSession(reply=reply)
        self.assertEqual(await self.send(session), reply)
        self.assertEqual(session.timeout, 10.0)

    async def test_string_and_int_values_compare_equal(self):
        session = FakeSession(reply={"body": {"rc": "1"}})
        await self.send(session)

    async def test_rejected_when_device_keeps_old_value(self):
        session = FakeSession(reply={"body": {"cmd": 5, "rc": 0}})
        with self.assertRaises(api.JackeryCommandRejected) as ctx:
            await self.send(session)
        self.assertIn("rc: sent 1, device reports 0", str(ctx.exception))

    async def test_reply_without_the_value_is_unconfirmed_not_success(self):
        session = FakeSession(reply={"body": {"cmd": 5}})
        with self.assertRaises(api.JackeryCommandUnconfirmed):
            await self.send(session)

    async def test_timeout_is_unconfirmed(self):
        session = FakeSession(error=TimeoutError())
        with self.assertRaises(api.JackeryCommandUnconfirmed) as ctx:
            await self.send(session)
        self.assertIn("may or may not have applied", str(ctx.exception))

    async def test_send_failure_is_an_error_not_silence(self):
        session = FakeSession(error=RuntimeError("MQTT session disconnected"))
        with self.assertRaises(api.JackeryCommandError) as ctx:
            await self.send(session)
        self.assertNotIsInstance(ctx.exception, api.JackeryCommandUnconfirmed)

    async def test_unverified_commands_accept_any_reply(self):
        session = FakeSession(reply={"body": {"cir": []}})
        await self.send(session, {"cmd": 12, "idx": 2, "sw": 0}, verify=False)

    async def test_unverified_commands_still_fail_on_timeout(self):
        with self.assertRaises(api.JackeryCommandUnconfirmed):
            await self.send(
                FakeSession(error=TimeoutError()),
                {"cmd": 12, "idx": 2, "sw": 0},
                verify=False,
            )

    async def test_errors_share_a_base_class(self):
        for cls in (api.JackeryCommandUnconfirmed, api.JackeryCommandRejected):
            self.assertTrue(issubclass(cls, api.JackeryCommandError))


if __name__ == "__main__":
    unittest.main()
