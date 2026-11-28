"""Shared per-receiver configuration without changing working directory."""
import json
import os
from pathlib import Path


def load_config():
    return json.loads(Path(os.environ.get("BRIDGE_CONFIG", "config.json")).read_text())


def stop_receiver_on_seek(config):
    """Positive public setting; accept the old experimental inverse as fallback."""
    value = config.get("stop_receiver_on_seek", config.get("seek_without_output_stop") is not True)
    if type(value) is not bool:
        raise ValueError("Seek stop setting must be boolean")
    return value
