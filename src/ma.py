"""Small MA RPC client. Credentials never appear in logs or command arguments."""
from pathlib import Path
import asyncio
import os

import aiohttp
from music_assistant_client import MusicAssistantClient


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
            async with asyncio.timeout(40):
                await self.client.connect()
                return await self.client.send_command(command, **args)

    async def player(self, player_id):
        return await self.call("players/get", player_id=player_id)
