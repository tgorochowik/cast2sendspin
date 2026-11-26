"""Discover eligible MA outputs. Emit only allowlisted, nonsecret device data."""
import asyncio
import json
import logging
import os
import sys

import aiohttp

from app_config import load_config
from ma import MA
from routing import supports_power, validate_target


async def main():
    config = load_config()
    async with aiohttp.ClientSession() as session:
        ma = MA(session, config["ma_url"])
        try:
            if "--connection-only" in sys.argv:
                await ma.client.connect()
                print(json.dumps({"players": []}))
                return
            # Include underlying protocol players for provenance validation, but
            # do not offer them as duplicate targets in the UI.
            players = await ma.call(
                "players/all", return_unavailable=True, return_disabled=True,
                return_protocol_players=True,
            )
            visible = await ma.call(
                "players/all", return_unavailable=True, return_disabled=True,
                return_protocol_players=False,
            )
            visible_ids = {p["player_id"] for p in visible}
            result = []
            for player in players:
                if player["player_id"] not in visible_ids or player.get("protocol_parent_id"):
                    continue
                reason = None
                try:
                    validate_target(player, players)
                except RuntimeError as error:
                    reason = str(error)  # Only our fixed local validation messages.
                result.append({
                    "player_id": player["player_id"],
                    "name": player.get("display_name") or player.get("name") or player["player_id"],
                    "provider": player.get("provider", ""),
                    "available": bool(player.get("available")),
                    "eligible": reason is None,
                    "reason": reason,
                    "power": supports_power(player),
                    "model": (player.get("device_info") or {}).get("model") or "",
                })
            print(json.dumps({"players": result}))
        finally:
            await ma.client.disconnect()


if __name__ == "__main__":
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as error:
        # No raw upstream errors: they may contain credentials or response data.
        print(json.dumps({"error": "MA discovery failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
