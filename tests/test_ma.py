import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from music_assistant_client.exceptions import ConnectionClosed

from ma import MA


class MAConnectionTests(unittest.IsolatedAsyncioTestCase):
    def client(self, connected=True):
        with patch("ma.Path.read_text", return_value="test-token"), patch(
            "ma.MusicAssistantClient"
        ) as factory:
            client = factory.return_value
            client.connection.connected = connected
            client.connect = AsyncMock()
            client.disconnect = AsyncMock()
            client.send_command = AsyncMock(return_value={"ok": True})
            ma = MA(Mock(), "http://example.test")
        return ma, client

    async def test_connected_calls_preserve_connection_for_setup_flows(self):
        ma, client = self.client()
        self.assertEqual(await ma.call("players/all"), {"ok": True})
        client.disconnect.assert_not_awaited()

    async def test_closed_connection_is_reset_before_reconnect(self):
        ma, client = self.client(connected=False)
        calls = []
        client.disconnect.side_effect = lambda: calls.append("disconnect")
        client.connect.side_effect = lambda: calls.append("connect")
        await ma.call("players/all")
        self.assertEqual(calls, ["disconnect", "connect"])

    async def test_connection_failure_resets_without_replaying_command(self):
        ma, client = self.client()
        client.send_command.side_effect = ConnectionClosed("closed")
        with self.assertRaises(ConnectionClosed):
            await ma.call("players/cmd/play")
        client.disconnect.assert_awaited_once()
        client.send_command.assert_awaited_once()
        client.send_command.side_effect = None
        self.assertEqual(await ma.call("players/all"), {"ok": True})

    async def test_cancelled_request_resets_connection(self):
        ma, client = self.client()
        client.send_command.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await ma.call("players/all")
        client.disconnect.assert_awaited_once()

    async def test_timeout_resets_connection(self):
        ma, client = self.client()
        client.send_command.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await ma.call("players/all")
        client.disconnect.assert_awaited_once()

    async def test_application_error_does_not_interrupt_setup_flow(self):
        ma, client = self.client()
        client.send_command.side_effect = ValueError("invalid command arguments")
        with self.assertRaises(ValueError):
            await ma.call("config/flows/submit")
        client.disconnect.assert_not_awaited()
