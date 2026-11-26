"""tools/screenshot.json keeps up with the server it is laid over. The picture itself needs a browser and isn't taken here."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from osiris import ui

TOOLS = Path(__file__).resolve().parent.parent / "tools"
spec = importlib.util.spec_from_file_location("screenshot", TOOLS / "screenshot.py")
shot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shot)
DATA = json.loads((TOOLS / "screenshot.json").read_text(encoding="utf-8"))


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(shot, "warn", said.append)
    return said


def with_page(out: Path, data: dict, check):
    async def main():
        app = ui.make_app(out)
        app.middlewares.append(shot.overlay(data))
        async with TestClient(TestServer(app)) as client:
            await shot.save_config(str(client.make_url("/")), data["config"])
            return await check(client)
    return asyncio.run(main())


def test_the_server_knows_every_name_in_the_screenshot_data(tmp_path: Path, warnings: list[str]):
    async def load(client):
        return [await (await client.get(path)).json() for path in ("/api/config", "/api/state", "/api/cleanup")]

    config, state, cleanup = with_page(tmp_path, DATA, load)
    assert warnings == []
    # the settings are the server's own with the data's over them, so a new setting shows its default in the picture
    assert config["options"]["cookies"] == "/home/baran/Downloads/cookies.txt" and config["options"]["lanes"] == ["twitter"]
    assert set(ui.DEFAULT_CONFIG["options"]) <= set(config["options"])
    assert config["defaults"]["workers"] == 8 and config["defaults"]["concurrency"] == ui.DEFAULT_CONFIG["options"]["concurrency"]
    assert (state["documents"], state["failures"]["total"], state["job"]["state"]) == (229970, 885, "done")
    assert sum(lane.get("pending", 0) for lane in state["lanes"].values()) == 4600
    assert cleanup["kept"] == 1779


def test_a_name_the_server_has_dropped_is_said(tmp_path: Path, warnings: list[str]):
    data = json.loads(json.dumps(DATA))
    data["config"]["options"]["no_such_setting"] = True
    data["state"]["no_such_number"] = 1

    async def load(client):
        await client.get("/api/state")

    with_page(tmp_path, data, load)
    assert len(warnings) == 2 and "no_such_setting" in warnings[0] and "no_such_number" in warnings[1]


def test_the_page_is_sent_the_log_and_the_failures_once(tmp_path: Path, warnings: list[str]):
    async def poll(client):
        first = await (await client.get("/api/state?since=0&failures=")).json()
        again = await (await client.get(f"/api/state?since={first['job']['count']}&failures={first['failures']['version']}")).json()
        return first, again

    first, again = with_page(tmp_path, DATA, poll)
    assert first["job"]["lines"] == DATA["log"] and first["job"]["count"] == len(DATA["log"])
    assert again["job"]["lines"] == [] and again["failures"] is None


def test_folders_in_the_data_get_its_summaries_and_others_the_server_s(tmp_path: Path, warnings: list[str]):
    async def scan(client):
        shown = await (await client.get("/api/scan", params={"kind": "local", "path": "/home/baran/git/moewe-saved"})).json()
        real = await (await client.get("/api/scan", params={"kind": "local", "path": str(tmp_path / "none")})).json()
        return shown, real

    shown, real = with_page(tmp_path, DATA, scan)
    assert shown == {"ok": True, "summary": "130 readable (130 html)"}
    assert real["ok"] is False
