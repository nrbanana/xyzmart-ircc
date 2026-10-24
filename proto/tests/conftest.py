"""Test setup: a throwaway SQLite database, the scripted model, and no network."""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

PROTO = Path(__file__).resolve().parents[1]
DB_PATH = Path(tempfile.mkdtemp()) / "test.db"
os.environ.update(DB_PATH=str(DB_PATH), LLM_MODE="mock", RUN_MODE="local")
sys.path[:0] = [str(PROTO), str(PROTO / "tool_service"), str(PROTO / "agent")]


def load(name: str, path: Path):
    """Both services have a main.py, so load each under its own name."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tool_main = load("tool_main", PROTO / "tool_service" / "main.py")
agent_main = load("agent_main", PROTO / "agent" / "main.py")


@pytest.fixture(autouse=True)
def fresh_db():
    import store
    DB_PATH.unlink(missing_ok=True)
    store.init_db()


@pytest.fixture(autouse=True)
def published(monkeypatch):
    """Capture messages instead of delivering them. Returns a list of (topic, message)."""
    from shared import cloud
    sent = []
    monkeypatch.setattr(cloud, "publish", lambda topic, message: sent.append((topic, message)))
    return sent


@pytest.fixture
def supplier(monkeypatch):
    """Fake supplier. Set supplier['down'] = True to make it fail."""
    state = {"down": False, "calls": 0}

    def send(order):
        state["calls"] += 1
        return ("QUEUED", None) if state["down"] else ("ORDERED", "SUP-TEST")

    monkeypatch.setattr(tool_main, "_send_to_supplier", send)
    return state


def push(message: dict) -> dict:
    """Wrap a message the way a Pub/Sub push subscription delivers it."""
    import base64
    import json
    return {"message": {"data": base64.b64encode(json.dumps(message).encode()).decode(), "messageId": "1"}}
