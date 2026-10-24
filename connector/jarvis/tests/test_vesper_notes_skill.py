"""The jarvis-notes skill's helper script writes notes the dashboard's Notes panel reads back unchanged."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

import jarvis

SCRIPT = Path(jarvis.__file__).resolve().parent / "skills" / "jarvis-notes" / "scripts" / "jarvis_note.py"
AUTH = {"Authorization": "Bearer test-vesper-key"}


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def note_cli(*args, stdin=None, ok=True):
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                          env={**os.environ}, timeout=30)
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


@pytest.mark.asyncio
async def test_script_notes_show_up_in_the_dashboard(hermes_home):
    made = note_cli("add", "--title", "Article: Why sleep matters", stdin="- REM consolidates memory\n- 7-9 hours\n")
    assert made["created"] and made["title"] == "Article: Why sleep matters"
    path = hermes_home / "vesper-notes" / f"{made['id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    async with TestClient(TestServer(app())) as client:
        notes = (await (await client.get("/vesper/notes", headers=AUTH)).json())["notes"]
    assert [(n["id"], n["title"], n["body"]) for n in notes] == [
        (made["id"], "Article: Why sleep matters", "- REM consolidates memory\n- 7-9 hours")]
    assert notes[0]["created_at"].endswith("Z")


def test_append_keeps_a_list_going_and_matches_titles_loosely():
    note_cli("add", "--title", "Groceries", "--body", "- eggs")
    out = note_cli("append", "--title", "grocery list", "--body", "oat milk")
    assert out["appended"] and out["title"] == "Groceries"
    assert note_cli("show", "--title", "groceries")["body"] == "- eggs\n- oat milk"


def test_append_to_a_missing_note_needs_create():
    assert "no note matches" in note_cli("append", "--title", "Reading list", "--body", "Dune", ok=False)["error"]
    assert note_cli("append", "--title", "Reading list", "--create", "--body", "- Dune")["created"]
    assert [n["title"] for n in note_cli("list")] == ["Reading list"]


def test_rejects_empty_and_oversized_notes():
    assert "title or a body" in note_cli("add", "--body", "  ", ok=False)["error"]
    assert "longer than 200" in note_cli("add", "--title", "x" * 201, "--body", "y", ok=False)["error"]


def test_bad_id_is_not_found():
    assert "no note matches" in note_cli("show", "--id", "../../etc/passwd", ok=False)["error"]


def test_import_moves_a_markdown_file_into_the_panel(tmp_path):
    md = tmp_path / "workspace" / "local-llm-notes.md"
    md.parent.mkdir()
    md.write_text("# Running AI Locally: RTX 5070 vs RTX 3070\n\n## Hardware\n- 12 GB VRAM\n", encoding="utf-8")
    out = note_cli("import", "--file", str(md))
    assert out["created"] and out["title"] == "Running AI Locally: RTX 5070 vs RTX 3070"
    assert note_cli("show", "--id", out["id"])["body"] == "## Hardware\n- 12 GB VRAM"
    assert md.exists()   # the original file is left alone
    plain = tmp_path / "workspace" / "packing_list.txt"
    plain.write_text("charger\nsocks\n", encoding="utf-8")
    assert note_cli("import", "--file", str(plain))["title"] == "Packing list"


def test_import_twice_reuses_the_panel_note(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# GPU notes\n- 12 GB\n", encoding="utf-8")
    first = note_cli("import", "--file", str(md))
    again = note_cli("import", "--file", str(md))
    assert again["already_imported"] and again["id"] == first["id"]
    assert len(note_cli("list")) == 1


def test_set_edits_a_note_in_place():
    made = note_cli("add", "--title", "Zeta rollout plan", "--body", "- Step 1 (done)\n- Step 2: book the venue (next)")
    out = note_cli("set", "--title", "zeta rollout", "--body", "- Step 1 (done)\n- Step 2: book the venue (done)\n- Step 3: order the cake")
    assert out["updated"] and out["id"] == made["id"]
    shown = note_cli("show", "--id", made["id"])
    assert shown["title"] == "Zeta rollout plan" and shown["body"].endswith("order the cake")
    assert len(note_cli("list")) == 1                      # edited, not duplicated
    renamed = note_cli("set", "--id", made["id"], "--new-title", "Zeta plan", "--body", shown["body"])
    assert renamed["title"] == "Zeta plan" and renamed["id"] == made["id"]
    assert "set needs" in note_cli("set", "--id", made["id"], ok=False, stdin="")["error"]


def test_add_does_not_take_an_id():
    proc = subprocess.run([sys.executable, str(SCRIPT), "add", "--id", "0" * 32, "--title", "x", "--body", "y"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and "unrecognized arguments: --id" in proc.stderr
