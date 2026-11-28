"""Summarize manual seek trials from sanitized per-receiver bridge JSON logs."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import median


def samples(events):
    rows, current = [], None
    requested, pending, by_request = {}, [], {}
    for event in events:
        kind = event.get("event")
        if kind == "COMMAND_STARTED" and event.get("method") == "seek":
            requested[event["request_id"]] = event.get("requested_position")
        elif kind == "COMMAND_RECEIVED" and event.get("method") == "seek":
            pending.append((event["request_id"], event["time"]))
        elif kind == "SEEK_STARTED":
            match = next((entry for entry in pending
                          if requested.get(entry[0]) in (None, event["requested_position"])), None)
            if match:
                pending.remove(match)
            received = match[1] if match else event["time"]
            queue_ms = max(0, round((
                datetime.fromisoformat(event["time"]) - datetime.fromisoformat(received)
            ).total_seconds() * 1000))
            rapid = bool(current and 0 <= (
                datetime.fromisoformat(received)
                - datetime.fromisoformat(current["received"])
            ).total_seconds() <= 1.25)
            if current and current["audio_ms"] is None:
                current["status"] = "superseded/no audio"
            if current and rapid:
                current["rapid"] = True
            current = {
                "time": event["time"], "target": event["requested_position"],
                "mode": event["mode"], "delay": event.get("prefetch_delay_ms", 0),
                "complete": event.get("buffer_complete", False),
                "hits": [], "command_ms": None, "audio_ms": None, "status": "pending",
                "rapid": rapid,
                "output": event.get("seek_output_mode", "interrupt"),
                "received": received, "queue_ms": queue_ms, "handling_ms": None,
            }
            rows.append(current)
            if match:
                by_request[match[0]] = current
        elif kind == "COMMAND_APPLIED" and event.get("method") == "seek":
            row = by_request.get(event.get("request_id"), current)
            if row:
                row["command_ms"] = event["duration_ms"]
        elif kind in ("READY", "SHUTDOWN", "PLAYING"):
            current = None
            requested, pending, by_request = {}, [], {}
        elif current:
            if kind == "BUFFER_READ" and current["audio_ms"] is None:
                current["hits"].append(event["cache_hit"])
            elif (kind == "SEEK_AUDIO"
                  and event["requested_position"] == current["target"]
                  and current["audio_ms"] is None):
                current["handling_ms"] = event["duration_ms"]
                current["audio_ms"] = event["duration_ms"] + current["queue_ms"]
                current["status"] = "audio"
            elif (kind == "PLAYBACK_ERROR"
                  or kind in ("COMMAND_FAILED", "COMMAND_TIMEOUT") and event.get("method") == "seek"):
                current["status"] = "error"
    return rows


def category(row):
    if row["mode"] == "direct":
        return "direct"
    if row["complete"]:
        return "buffered/full"
    if False in row["hits"]:
        return "buffered/miss"
    return "buffered/partial-hit" if row["hits"] else "buffered/unverified"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--since", default="", help="UTC ISO timestamp, e.g. 2026-11-28T12:30:00")
    args = parser.parse_args()
    events = []
    with args.log.open() as log:
        for line in log:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("time", "") >= args.since:
                events.append(event)
    rows = samples(events)
    print("| UTC | target (s) | path | output | trial | BG delay (ms) | queued (ms) | command (ms) | first PCM (ms) | status |")
    print("|---|---:|---|---|---|---:|---:|---:|---:|---|")
    groups = {}
    for row in rows:
        path = category(row)
        trial = "rapid" if row["rapid"] else "regular"
        command = row["command_ms"] if row["command_ms"] is not None else "—"
        audio = row["audio_ms"] if row["audio_ms"] is not None else "—"
        print(f'| {row["time"]} | {row["target"]:g} | {path} | {row["output"]} | {trial} | {row["delay"]} | '
              f'{row["queue_ms"]} | {command} | {audio} | {row["status"]} |')
        if row["audio_ms"] is not None and row["status"] == "audio":
            groups.setdefault((path, row["output"], trial, row["delay"]), []).append(row["audio_ms"])
    for (path, output, trial, delay), values in groups.items():
        print(f"\n{path}, {output}, {trial}, BG delay {delay} ms: n={len(values)}, "
              f"first PCM median={median(values):g} ms, min={min(values)}, max={max(values)}.")
    print("\nFirst PCM includes estimated worker queue wait plus handling through feeding Sendspin, "
          "not phone gesture to speaker-audible sound. Pending/superseded seeks are not latency samples.")
    print("Rapid pairs (seek starts at most 1.25 seconds apart) are summarized separately.")


if __name__ == "__main__":
    main()
