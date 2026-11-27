"""MA target validation and ownership-aware power/route lifecycle."""
import asyncio


def validate_target(player, players):
    """Reject Cast outputs even when hidden behind universal/group players."""
    by_id = {p["player_id"]: p for p in players}
    visited = set()

    def visit(p):
        if p["player_id"] in visited:
            return
        visited.add(p["player_id"])
        identifiers = (p.get("device_info") or {}).get("identifiers") or {}
        if p.get("provider") == "chromecast" or identifiers.get("cast_uuid"):
            raise RuntimeError("Chromecast-backed targets are not allowed")
        for protocol in p.get("output_protocols", []):
            if protocol.get("protocol_domain") == "chromecast":
                raise RuntimeError("Chromecast-backed targets are not allowed")
            if protocol.get("derived_from"):
                origin = by_id.get(protocol["derived_from"])
                if origin is None:
                    raise RuntimeError("Cannot establish output protocol provenance")
                visit(origin)
        for member in p.get("group_members", []):
            if member == p["player_id"]:
                continue
            if member not in by_id:
                raise RuntimeError("Unknown group member")
            visit(by_id[member])

    visit(player)
    if player.get("type", "player") != "player":
        raise RuntimeError("Only individual output players are supported for now")
    if player.get("protocol_parent_id"):
        raise RuntimeError("Use the parent player, not its internal protocol output")
    if not player.get("available"):
        raise RuntimeError("Configured target is unavailable")
    # First slice supports only the explicitly selected single player.
    if player.get("group_members"):
        raise RuntimeError("Group routing is not implemented in this first slice")


def owns_source(player, uri):
    # MA can retain old current_media after a source switch. An explicit
    # active_source wins; otherwise a stale input URI could mask a takeover.
    if player.get("active_source") is not None:
        return player["active_source"] == uri
    return (player.get("current_media") or {}).get("uri") == uri


def supports_power(player):
    return "power" in player.get("supported_features", []) or player.get(
        "power_control", "none"
    ) not in (None, "none")


class SessionRouter:
    def __init__(self, ma, target, uri):
        self.ma, self.target, self.uri = ma, target, uri
        self.active = False
        self.suspended = False
        self.closing = False
        self.power_owned = False
        self.lock = asyncio.Lock()

    async def claim(self):
        players = await self.ma.call("players/all", return_protocol_players=True)
        p = next((p for p in players if p["player_id"] == self.target), None)
        if p is None:
            raise RuntimeError("Configured target was not found")
        validate_target(p, players)
        if supports_power(p):
            await self.ma.call("players/cmd/power", player_id=self.target, powered=True)
            self.power_owned = True
        self.active = True
        self.suspended = False
        try:
            await self.ma.call("players/cmd/select_source", player_id=self.target, source=self.uri)
        except BaseException:
            # Only power down if no other playback has taken over.
            await self.finish(rollback=True)
            raise

    async def require_ownership(self):
        p = await self.ma.player(self.target)
        if not self.active or not p.get("available") or not owns_source(p, self.uri):
            self.yield_to_external()
            raise RuntimeError("Cast no longer owns this destination")
        return p

    async def suspend(self):
        """Interrupt the renderer without releasing the selected casting source."""
        await self.require_ownership()
        if self.suspended:
            return
        # Set before the RPC: its source-stop callback can precede the response.
        self.suspended = True
        await self.ma.call("players/cmd/stop", player_id=self.target)
        async with asyncio.timeout(5):
            while True:
                p = await self.require_ownership()
                if p.get("playback_state") == "idle":
                    break
                await asyncio.sleep(0.05)

    async def resume(self):
        """Restart only a source we still own; never reclaim after a takeover."""
        await self.require_ownership()
        if not self.suspended:
            return
        await self.ma.call("players/cmd/select_source", player_id=self.target, source=self.uri)
        self.suspended = False

    async def finish(self, rollback=False):
        self.closing = True
        try:
            p = await self.ma.player(self.target)
            owned = self.active and owns_source(p, self.uri)
            if owned:
                await self.ma.call("players/cmd/stop", player_id=self.target)
                p = await self.ma.player(self.target)
                # Some providers acknowledge Stop before their state changes.
                # Wait briefly, but never power off newly started playback.
                for _ in range(20):
                    if not owns_source(p, self.uri) or p.get("playback_state") not in ("playing", "paused"):
                        break
                    await asyncio.sleep(0.1)
                    p = await self.ma.player(self.target)
                if owns_source(p, self.uri) and p.get("playback_state") == "idle":
                    # Selecting the MA queue releases the live-input session
                    # without clearing its items or resuming playback.
                    await self.ma.call(
                        "players/cmd/select_source", player_id=self.target,
                        source=self.target,
                    )
                    p = await self.ma.player(self.target)
            # No power-off after a different source has taken over. A failed
            # startup may roll back power only while the target remains idle.
            safe_to_power_off = (
                owned and p.get("active_source") in (self.uri, self.target, None)
                and p.get("playback_state") not in ("playing", "paused")
            ) or (
                rollback and self.active and not owned
                and p.get("playback_state") == "idle"
            )
            if self.power_owned and safe_to_power_off:
                await self.ma.call("players/cmd/power", player_id=self.target, powered=False)
        finally:
            self.active = False
            self.suspended = False
            self.power_owned = False
            self.closing = False

    def yield_to_external(self):
        self.active = False
        self.suspended = False
        self.power_owned = False


class ManualSession:
    """A casting session with no authority over MA destinations or their power."""
    def __init__(self, uri):
        self.uri = uri
        self.active = False
        self.closing = False

    async def claim(self):
        self.active = True

    async def finish(self):
        self.active = False

    def yield_to_external(self):
        self.active = False
