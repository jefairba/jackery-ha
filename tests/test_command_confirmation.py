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


class PlanConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """Plan commands are only acknowledged; confirm them from the plan list."""

    def setUp(self):
        async def no_wait(_seconds):
            return None

        patcher = unittest.mock.patch.object(api.asyncio, "sleep", no_wait)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, plan_lists):
        client = api.JackeryAPI("u", "p", android_id="feedfacecafebeef")
        client.sent = []

        async def send(device_id, device_sn, action_id, body, message_type="", *, verify=True):
            client.sent.append((action_id, body, verify))
            return {"body": {"cmd": body["cmd"], "messageId": 1}}

        reads = list(plan_lists)

        async def query(device_sn):
            return reads.pop(0) if len(reads) > 1 else reads[0]

        client.async_send_device_command = send
        client.async_query_transfer_switch_plans = query
        return client

    P1 = {"pid": 1750886921, "tt": 1, "st": "15:00", "et": "18:00", "sw": 1, "lps": "1111111"}
    P2 = {"pid": 1750886968, "tt": 0, "st": "00:00", "et": "05:00", "sw": 1, "lps": "1111111"}

    async def test_delete_confirmed_when_plan_disappears(self):
        client = self.make([[self.P1, self.P2], [self.P2]])
        plans = await client.async_delete_transfer_switch_plan("d", "ts", "1750886921")
        self.assertEqual(plans, [self.P2])

    async def test_deleting_the_last_plan_confirms_with_empty_list(self):
        client = self.make([[]])
        self.assertEqual(await client.async_delete_transfer_switch_plan("d", "ts", "1750886968"), [])

    async def test_delete_that_does_not_stick_is_rejected(self):
        client = self.make([[self.P1, self.P2]])
        with self.assertRaises(api.JackeryCommandRejected) as ctx:
            await client.async_delete_transfer_switch_plan("d", "ts", "1750886921")
        self.assertIn("plan list did not change", str(ctx.exception))

    async def test_no_plan_list_answer_is_unconfirmed(self):
        client = self.make([None])
        with self.assertRaises(api.JackeryCommandUnconfirmed):
            await client.async_delete_transfer_switch_plan("d", "ts", "1750886921")

    async def test_create_confirmed_when_new_matching_plan_appears(self):
        new = {"pid": "1790000000", "tt": 0, "st": "01:00", "et": "06:00", "sw": 1, "lps": 127}
        client = self.make([[], [], [dict(new)]])
        plans = await client.async_create_transfer_switch_plan("d", "ts", new)
        self.assertEqual(len(plans), 1)

    async def test_update_confirmed_when_fields_match(self):
        updated = {**self.P2, "sw": 0}
        client = self.make([[self.P2], [updated]])
        plans = await client.async_update_transfer_switch_plan("d", "ts", updated)
        self.assertEqual(plans[0]["sw"], 0)


if __name__ == "__main__":
    unittest.main()
