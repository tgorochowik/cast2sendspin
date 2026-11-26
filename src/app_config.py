"""Shared per-receiver configuration without changing working directory."""
import json
import os
from pathlib import Path


def load_config():
    return json.loads(Path(os.environ.get("BRIDGE_CONFIG", "config.json")).read_text())
