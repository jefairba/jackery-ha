"""A command only counts as done when the device confirms the new value."""

from __future__ import annotations

import importlib.util
import unittest
import unittest.mock
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


def make_api(session, readbacks=None):
    """API with a fake MQTT session and fake state read-backs (no network)."""
    client = api.JackeryAPI("user@example.com", "pw", android_id="feedfacecafebeef")
    client._mqtt_session = session
    reads = list(readbacks or [])

    def fake_detail(device_id):
        item = reads.pop(0) if len(reads) > 1 else reads[0]
        if isinstance(item, Exception):
            raise item
        return {"code": 0, "data": {"properties": item}}

    client.get_device_detail = fake_detail
    return client


TS_ACK = {"deviceSn": "ts-sn", "actionId": 3, "body": {"cmd": 5, "messageId": 123}}


class CommandConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def no_wait(_seconds):
            return None

        patcher = unittest.mock.patch.object(api.asyncio, "sleep", no_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def send(self, session, body=FORCE_CHARGE, readbacks=None, **kw):
        return await make_api(session, readbacks).async_send_device_command(
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

    async def test_transfer_switch_ack_confirmed_by_reading_state_back(self):
        """The TS only acks ({cmd, messageId}); success = state shows the value."""
        await self.send(FakeSession(reply=TS_ACK), readbacks=[{"rc": 0}, {"rc": 1}])

    async def test_ack_but_state_never_changes_is_rejected(self):
        """What really happened on 2026-09-26: acked, rc stayed 0."""
        with self.assertRaises(api.JackeryCommandRejected) as ctx:
            await self.send(FakeSession(reply=TS_ACK), readbacks=[{"rc": 0}])
        message = str(ctx.exception)
        self.assertIn("acknowledged the command but did not apply it", message)
        self.assertIn("rc: sent 1, device still reports 0", message)

    async def test_readback_failure_is_unconfirmed(self):
        with self.assertRaises(api.JackeryCommandUnconfirmed) as ctx:
            await self.send(FakeSession(reply=TS_ACK), readbacks=[ConnectionError("x")])
        self.assertIn("could not be read back", str(ctx.exception))

    async def test_readback_without_the_key_is_unconfirmed(self):
        with self.assertRaises(api.JackeryCommandUnconfirmed) as ctx:
            await self.send(FakeSession(reply=TS_ACK), readbacks=[{"en": 0}])
        self.assertIn("does not report rc", str(ctx.exception))

    async def test_readback_is_bounded(self):
        """Read-back waits at most ~6 s on top of the 10 s reply wait."""
        self.assertLessEqual(sum(api.READBACK_DELAYS_SEC), 6.0)
        self.assertEqual(len(api.READBACK_DELAYS_SEC), 3)

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
