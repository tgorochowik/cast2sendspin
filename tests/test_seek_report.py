import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "seek_report", Path(__file__).resolve().parents[1] / "scripts/seek_report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class SeekReportTests(unittest.TestCase):
    def test_output_mode_is_preserved_and_legacy_samples_default_to_interruption(self):
        def start(output=None):
            event = {"event": "SEEK_STARTED", "time": "2026-11-28T15:00:00Z",
                     "requested_position": 100, "mode": "direct"}
            if output:
                event["seek_output_mode"] = output
            return event

        self.assertEqual(report.samples([start()])[0]["output"], "interrupt")
        self.assertEqual(report.samples([start("continuous")])[0]["output"], "continuous")

    def test_queued_seek_includes_wait_behind_previous_command(self):
        rows = report.samples([
            {"event": "COMMAND_STARTED", "method": "seek", "request_id": 8, "requested_position": 403},
            {"event": "COMMAND_RECEIVED", "method": "seek", "request_id": 8,
             "time": "2026-11-28T14:15:28.302Z"},
            {"event": "SEEK_STARTED", "time": "2026-11-28T14:15:28.601Z",
             "requested_position": 403, "mode": "buffered"},
            {"event": "COMMAND_APPLIED", "method": "seek", "request_id": 8, "duration_ms": 335},
            {"event": "SEEK_AUDIO", "requested_position": 403, "duration_ms": 125},
        ])
        self.assertEqual(rows[0]["queue_ms"], 299)
        self.assertEqual(rows[0]["handling_ms"], 125)
        self.assertEqual(rows[0]["audio_ms"], 424)
        self.assertEqual(rows[0]["command_ms"], 335)

    def test_rapid_pair_is_not_mixed_with_regular_seek_summary(self):
        def start(time, target):
            return {"event": "SEEK_STARTED", "time": time,
                    "requested_position": target, "mode": "direct"}

        rows = report.samples([
            start("2026-11-28T13:00:00Z", 10),
            {"event": "SEEK_AUDIO", "requested_position": 10, "duration_ms": 700},
            start("2026-11-28T13:00:05Z", 75),
            start("2026-11-28T13:00:05.6Z", 25),
            {"event": "SEEK_AUDIO", "requested_position": 25, "duration_ms": 150},
        ])
        self.assertEqual([row["rapid"] for row in rows], [False, True, True])

    def test_separates_complete_buffer_misses_and_superseded_seeks(self):
        def start(target, **extra):
            return {"event": "SEEK_STARTED", "time": "2026-11-28T13:00:00Z",
                    "requested_position": target, "mode": "buffered", **extra}

        rows = report.samples([
            start(180, buffer_complete=True),
            {"event": "COMMAND_APPLIED", "method": "seek", "duration_ms": 100},
            {"event": "SEEK_AUDIO", "requested_position": 180, "duration_ms": 110},
            start(30, prefetch_delay_ms=5000),
            {"event": "BUFFER_READ", "cache_hit": False},
            {"event": "SEEK_AUDIO", "requested_position": 30, "duration_ms": 220},
            start(90),
            start(10),
            {"event": "SEEK_AUDIO", "requested_position": 90, "duration_ms": 300},
            {"event": "SEEK_AUDIO", "requested_position": 10, "duration_ms": 150},
        ])
        self.assertEqual(report.category(rows[0]), "buffered/full")
        self.assertEqual(report.category(rows[1]), "buffered/miss")
        self.assertEqual(rows[2]["status"], "superseded/no audio")
        self.assertIsNone(rows[2]["audio_ms"])
        self.assertEqual(rows[3]["audio_ms"], 150)
