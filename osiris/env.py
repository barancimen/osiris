"""API keys come from one file: .env in the osiris folder. See .env.example."""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
# Tells child processes it's been read. The page takes the paid Twitter key away from runs that
# haven't allowed it, and a run reading the file again would bring it back.
LOADED = "OSIRIS_ENV_LOADED"


def load_env() -> None:
    """Put the file's keys in the environment; a key in the file beats one set in the shell."""
    if os.environ.get(LOADED):
        return
    os.environ[LOADED] = "1"
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip().strip("'\"")
        if sep and value and not key.startswith("#"):
            os.environ[key.strip()] = value
