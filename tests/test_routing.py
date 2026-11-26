import copy
import unittest

from routing import SessionRouter, owns_source, validate_target

URI = "sendspin_source://audio_source/test"


class FakeMA:
    def __init__(self, power=True):
        self.p = {
            "player_id": "speaker", "available": True, "provider": "sendspin",
            "supported_features": ["power"] if power else [],
            "power_control": "native" if power else "none",
            "playback_state": "idle", "current_media": {}, "powered": False,
            "active_source": "speaker",
        }
        self.calls = []
        self.fail_route = False

    async def player(self, _):
        return copy.deepcopy(self.p)

    async def call(self, command, **args):
        self.calls.append((command, args))
        if command == "players/all":
            return [copy.deepcopy(self.p)]
        if command == "players/cmd/power":
            self.p["powered"] = args["powered"]
        if command == "players/cmd/select_source":
            if self.fail_route:
                raise RuntimeError("failed")
            self.p["active_source"] = args["source"]
            if args["source"] == "speaker":
                self.p["playback_state"] = "idle"
            else:
                self.p.update(current_media={"uri": args["source"]}, playback_state="playing")
        if command == "players/cmd/stop":
            self.p["playback_state"] = "idle"


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_power_lifecycle(self):
        ma = FakeMA()
        router = SessionRouter(ma, "speaker", URI)
        await router.claim()
        await router.finish()
        commands = [c for c, _ in ma.calls if c != "players/all"]
        self.assertEqual(commands, [
            "players/cmd/power", "players/cmd/select_source",
            "players/cmd/stop", "players/cmd/select_source", "players/cmd/power",
        ])
        self.assertFalse(ma.p["powered"])
        self.assertEqual(ma.p["active_source"], "speaker")
        self.assertEqual(ma.p["playback_state"], "idle")

    async def test_unsupported_power_is_never_called(self):
        ma = FakeMA(power=False)
        router = SessionRouter(ma, "speaker", URI)
        await router.claim()
        await router.finish()
        self.assertNotIn("players/cmd/power", [c for c, _ in ma.calls])

    async def test_takeover_is_not_stopped_or_powered_off(self):
        for state in ("playing", "paused", "idle"):
            ma = FakeMA()
            router = SessionRouter(ma, "speaker", URI)
            await router.claim()
            ma.calls.clear()
            ma.p.update(active_source="other://track", current_media={"uri": "other://track"},
                        playback_state=state)
            await router.finish()
            self.assertEqual(ma.calls, [])
            self.assertTrue(ma.p["powered"])

    async def test_failed_route_rolls_back_power(self):
        ma = FakeMA()
        ma.fail_route = True
        router = SessionRouter(ma, "speaker", URI)
        with self.assertRaises(RuntimeError):
            await router.claim()
        self.assertFalse(ma.p["powered"])
        self.assertFalse(router.active)

    async def test_yield_never_touches_destination(self):
        ma = FakeMA()
        router = SessionRouter(ma, "speaker", URI)
        await router.claim()
        ma.calls.clear()
        router.yield_to_external()
        await router.finish()
        self.assertEqual(ma.calls, [])

    def test_cast_protocol_wrapped_by_universal_player_is_rejected(self):
        p = FakeMA().p
        p["provider"] = "universal_player"
        p["output_protocols"] = [{"protocol_domain": "chromecast"}]
        with self.assertRaises(RuntimeError):
            validate_target(p, [p])

    def test_cast_device_identity_is_rejected(self):
        p = FakeMA().p
        p["device_info"] = {"identifiers": {"cast_uuid": "cast-device"}}
        with self.assertRaises(RuntimeError):
            validate_target(p, [p])

    def test_noncast_sendspin_is_allowed(self):
        p = FakeMA().p
        validate_target(p, [p])

    def test_source_only_client_is_not_an_output(self):
        p = FakeMA().p
        p["type"] = "source"
        with self.assertRaises(RuntimeError):
            validate_target(p, [p])

    def test_cast_derived_output_is_rejected(self):
        p = FakeMA().p
        p["output_protocols"] = [{"protocol_domain": "sendspin", "derived_from": "cast"}]
        cast = {"player_id": "cast", "provider": "chromecast"}
        with self.assertRaises(RuntimeError):
            validate_target(p, [p, cast])

    async def test_stale_media_never_overrides_takeover(self):
        ma = FakeMA()
        router = SessionRouter(ma, "speaker", URI)
        await router.claim()
        ma.p.update(active_source="another-source", playback_state="idle")
        # MA may retain the old input in current_media.
        self.assertEqual(ma.p["current_media"]["uri"], URI)
        self.assertFalse(owns_source(ma.p, URI))
        ma.calls.clear()
        await router.finish()
        self.assertEqual(ma.calls, [])

    async def test_cleanup_restores_queue_without_clearing_or_playing(self):
        ma = FakeMA(power=False)
        router = SessionRouter(ma, "speaker", URI)
        await router.claim()
        ma.calls.clear()
        await router.finish()
        self.assertEqual(ma.calls, [
            ("players/cmd/stop", {"player_id": "speaker"}),
            ("players/cmd/select_source", {"player_id": "speaker", "source": "speaker"}),
        ])
        self.assertEqual(ma.p["active_source"], "speaker")
        ma.calls.clear()
        await router.finish()
        self.assertEqual(ma.calls, [])


if __name__ == "__main__":
    unittest.main()
