"""Small MA RPC client. Credentials never appear in logs or command arguments."""
from pathlib import Path
import asyncio
import os

import aiohttp
from music_assistant_client import MusicAssistantClient
from music_assistant_client.exceptions import (
    CannotConnect, ConnectionClosed, ConnectionFailed, InvalidState, NotConnected,
)


class MAError(RuntimeError):
    pass


class MA:
    def __init__(self, session: aiohttp.ClientSession, base: str):
        self.session = session
        self.base = base.rstrip("/")
        self._token = Path(os.environ.get("MA_TOKEN_FILE", ".ma_token")).read_text().strip()
        self.client = MusicAssistantClient(self.base, session, token=self._token)
        self.lock = asyncio.Lock()

    async def call(self, command, **args):
        # Setup flows are bound to the connection: use a persistent authenticated
        # WebSocket rather than separate short-lived HTTP requests.
        async with self.lock:
            try:
                async with asyncio.timeout(40):
                    if not self.client.connection.connected:
                        # The upstream client retains a closed WebSocket until
                        # disconnect() is called, otherwise connect() rejects it.
                        await self.client.disconnect()
                    await self.client.connect()
                    return await self.client.send_command(command, **args)
            except (
                CannotConnect, ConnectionClosed, ConnectionFailed, InvalidState,
                NotConnected, OSError, TimeoutError, asyncio.CancelledError,
            ):
                # A timed-out/cancelled request may leave an unread response.
                # Reset for the next call, but never replay a command: it may
                # already have changed playback on the server.
                await self.client.disconnect()
                raise

    async def player(self, player_id):
        return await self.call("players/get", player_id=player_id)
