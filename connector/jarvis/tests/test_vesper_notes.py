"""VESPER dashboard Notes API: one JSON file per note under HERMES_HOME/vesper-notes.

Covers the HTTP contract (auth, CRUD, validation, ordering) and the on-disk contract the agent's
own file tools rely on (plain JSON, private permissions, stray files tolerated).
"""

import asyncio
import json
import re
import stat
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from jarvis import vesper as api_server_vesper
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}
JSON_HEADERS = {**AUTH, "Content-Type": "application/json"}
NOTES = "/vesper/notes"
NOTE_FIELDS = {"id", "title", "body", "created_at", "updated_at"}
ID_RE = re.compile(r"[0-9a-f]{32}")
TIMESTAMP_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    # Same body ceiling the gateway's own app gets; aiohttp's 1 MiB default would reject a
    # maximum-size note whose non-ASCII characters are \uXXXX-escaped in the request JSON.
    application = web.Application(client_max_size=api_server.MAX_REQUEST_BYTES)
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    """The tests/conftest.py autouse fixture already points HERMES_HOME at a per-test tempdir;
    pinning it here as well keeps this file hermetic when run without that conftest."""
    home = tmp_path / "notes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def notes_dir(hermes_home):
    return hermes_home / "vesper-notes"


@pytest.fixture
def clock(monkeypatch):
    """Deterministic, strictly increasing timestamps (one second apart), so ordering tests do
    not depend on two requests landing in different milliseconds."""
    start = datetime(2026, 10, 11, 23, 5, 12, tzinfo=timezone.utc)
    ticks = iter(range(10_000))
    monkeypatch.setattr(
        api_server_vesper, "_notes_timestamp",
        lambda: (start + timedelta(seconds=next(ticks))).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"))


@pytest_asyncio.fixture
async def client():
    async with TestClient(TestServer(app())) as test_client:
        yield test_client


def slow_note_writes(monkeypatch):
    """Make every note write take a while, to hold open the read-modify-write window an unlocked
    PATCH would lose. The returned event is set as soon as a write is under way."""
    entered = threading.Event()
    write_note_file = api_server_vesper._write_note_file

    def slow_write(path, note):
        entered.set()
        time.sleep(0.15)
        write_note_file(path, note)

    monkeypatch.setattr(api_server_vesper, "_write_note_file", slow_write)
    return entered


async def create(client, **fields):
    resp = await client.post(NOTES, json=fields, headers=AUTH)
    assert resp.status == 201, await resp.text()
    return (await resp.json())["note"]


async def list_ids(client):
    resp = await client.get(NOTES, headers=AUTH)
    assert resp.status == 200
    return [note["id"] for note in (await resp.json())["notes"]]


async def error_code(resp):
    return (await resp.json())["error"]["code"]


class TestAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}],
                             ids=["no_key", "wrong_key"])
    async def test_every_route_rejects_unauthorized_requests_without_side_effects(
            self, client, headers):
        note = await create(client, title="keep", body="me")
        note_url = f"{NOTES}/{note['id']}"
        attempts = [
            ("GET", NOTES, None),
            ("POST", NOTES, {"title": "intruder"}),
            ("GET", note_url, None),
            ("PATCH", note_url, {"title": "hijacked"}),
            ("DELETE", note_url, None),
        ]
        for method, url, payload in attempts:
            resp = await client.request(method, url, json=payload, headers=headers)
            assert resp.status == 401, (method, url)
        assert await list_ids(client) == [note["id"]]
        stored = await (await client.get(note_url, headers=AUTH)).json()
        assert stored["note"] == note


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_create_list_get_patch_delete(self, client):
        resp = await client.post(NOTES, json={"title": "Groceries", "body": "milk\neggs"}, headers=AUTH)
        assert resp.status == 201
        created = (await resp.json())["note"]
        assert set(created) == NOTE_FIELDS
        assert ID_RE.fullmatch(created["id"])
        assert (created["title"], created["body"]) == ("Groceries", "milk\neggs")
        assert created["created_at"] == created["updated_at"]

        listed = await (await client.get(NOTES, headers=AUTH)).json()
        assert listed == {"notes": [created]}

        resp = await client.get(f"{NOTES}/{created['id']}", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json()) == {"note": created}

        resp = await client.patch(f"{NOTES}/{created['id']}", json={"body": "milk"}, headers=AUTH)
        assert resp.status == 200
        patched = (await resp.json())["note"]
        assert patched["body"] == "milk"
        assert (await (await client.get(f"{NOTES}/{created['id']}", headers=AUTH)).json()) == {
            "note": patched}

        resp = await client.delete(f"{NOTES}/{created['id']}", headers=AUTH)
        assert resp.status == 200
        assert await resp.json() == {"deleted": True}
        assert (await client.get(f"{NOTES}/{created['id']}", headers=AUTH)).status == 404
        assert await list_ids(client) == []

    @pytest.mark.asyncio
    async def test_missing_fields_default_to_empty_and_unknown_keys_are_ignored(self, client):
        assert (await create(client))["title"] == ""
        note = await create(client, title="t", id="f" * 32, created_at="1999-01-01T00:00:00.000Z",
                            updated_at="1999-01-01T00:00:00.000Z", pinned=True)
        assert note["body"] == ""
        assert note["id"] != "f" * 32
        assert not note["created_at"].startswith("1999")
        assert "pinned" not in note

    @pytest.mark.asyncio
    async def test_unknown_and_deleted_notes_are_404(self, client):
        absent = "0" * 32
        for resp in (await client.get(f"{NOTES}/{absent}", headers=AUTH),
                     await client.patch(f"{NOTES}/{absent}", json={"title": "x"}, headers=AUTH),
                     await client.delete(f"{NOTES}/{absent}", headers=AUTH)):
            assert resp.status == 404
            assert await error_code(resp) == "note_not_found"

    @pytest.mark.asyncio
    async def test_timestamps_are_utc_iso_with_millisecond_precision(self, client):
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        note = await create(client, title="t")
        patched = (await (await client.patch(
            f"{NOTES}/{note['id']}", json={"body": "b"}, headers=AUTH)).json())["note"]
        for stamp in (note["created_at"], patched["updated_at"]):
            assert TIMESTAMP_RE.fullmatch(stamp)
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            assert before <= parsed <= datetime.now(timezone.utc) + timedelta(seconds=1)


class TestOrderingAndPatchSemantics:
    @pytest.mark.asyncio
    async def test_list_puts_the_most_recently_updated_note_first(self, client, clock):
        first = await create(client, title="first")
        second = await create(client, title="second")
        third = await create(client, title="third")
        assert await list_ids(client) == [third["id"], second["id"], first["id"]]

        await client.patch(f"{NOTES}/{first['id']}", json={"body": "edited"}, headers=AUTH)
        assert await list_ids(client) == [first["id"], third["id"], second["id"]]

        await client.patch(f"{NOTES}/{second['id']}", json={"title": "renamed"}, headers=AUTH)
        assert await list_ids(client) == [second["id"], first["id"], third["id"]]

    @pytest.mark.asyncio
    async def test_patch_changes_only_the_provided_fields(self, client, clock):
        note = await create(client, title="Title", body="  verbatim\n\nbody  \n")
        url = f"{NOTES}/{note['id']}"

        retitled = (await (await client.patch(url, json={"title": "New"}, headers=AUTH)).json())["note"]
        assert retitled["title"] == "New"
        assert retitled["body"] == note["body"]
        assert retitled["id"] == note["id"]
        assert retitled["created_at"] == note["created_at"]
        assert retitled["updated_at"] > note["updated_at"]

        rebodied = (await (await client.patch(url, json={"body": "other"}, headers=AUTH)).json())["note"]
        assert rebodied["body"] == "other"
        assert rebodied["title"] == "New"
        assert rebodied["created_at"] == note["created_at"]
        assert rebodied["updated_at"] > retitled["updated_at"]

    @pytest.mark.asyncio
    async def test_concurrent_patches_of_different_fields_both_survive(self, client, monkeypatch):
        note = await create(client, title="old", body="old")
        slow_note_writes(monkeypatch)
        url = f"{NOTES}/{note['id']}"
        responses = await asyncio.gather(
            client.patch(url, json={"title": "new title"}, headers=AUTH),
            client.patch(url, json={"body": "new body"}, headers=AUTH))
        assert [resp.status for resp in responses] == [200, 200]

        stored = (await (await client.get(url, headers=AUTH)).json())["note"]
        assert (stored["title"], stored["body"]) == ("new title", "new body")

    @pytest.mark.asyncio
    async def test_patch_racing_a_delete_cannot_resurrect_the_note(self, client, monkeypatch):
        note = await create(client, title="doomed")
        write_started = slow_note_writes(monkeypatch)
        url = f"{NOTES}/{note['id']}"

        async def patch():
            return await client.patch(url, json={"title": "late"}, headers=AUTH)

        patching = asyncio.create_task(patch())
        assert await asyncio.to_thread(write_started.wait, 10)
        deleted = await client.delete(url, headers=AUTH)
        assert (await patching).status == 200
        assert deleted.status == 200
        assert (await client.get(url, headers=AUTH)).status == 404
        assert await list_ids(client) == []


class TestValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw, normalized", [
        ("  Hello   World  ", "Hello World"),
        ("line one\nline two\n\nline three", "line one line two line three"),
        ("tabs\t\tand\r\nCRLF", "tabs and CRLF"),
        ("\n\n", ""),
    ])
    async def test_title_is_collapsed_to_a_single_trimmed_line(self, client, raw, normalized):
        note = await create(client, title=raw)
        assert note["title"] == normalized
        patched = await client.patch(f"{NOTES}/{note['id']}", json={"title": f" {raw} "}, headers=AUTH)
        assert (await patched.json())["note"]["title"] == normalized

    @pytest.mark.asyncio
    async def test_body_is_stored_verbatim(self, client):
        body = "\n  leading, trailing and\r\n\ttabbed  \n\n"
        assert (await create(client, body=body))["body"] == body

    @pytest.mark.asyncio
    async def test_title_limit_applies_after_normalization(self, client):
        limit = api_server_vesper._NOTE_TITLE_MAX_LEN
        assert len((await create(client, title="a" * limit))["title"]) == limit
        # Raw length far over the limit, but one short line once whitespace is collapsed.
        assert (await create(client, title="a" + " \n" * limit + "b"))["title"] == "a b"

        resp = await client.post(NOTES, json={"title": "a" * (limit + 1)}, headers=AUTH)
        assert resp.status == 400
        assert await error_code(resp) == "note_title_too_long"

        note = await create(client, title="keep")
        resp = await client.patch(
            f"{NOTES}/{note['id']}", json={"title": "a" * (limit + 1)}, headers=AUTH)
        assert resp.status == 400
        assert await error_code(resp) == "note_title_too_long"
        stored = (await (await client.get(f"{NOTES}/{note['id']}", headers=AUTH)).json())["note"]
        assert stored == note

    @pytest.mark.asyncio
    async def test_body_limit_counts_characters_not_bytes(self, client):
        limit = api_server_vesper._NOTE_BODY_MAX_LEN
        accepted = await create(client, body="é" * limit)  # twice the limit in UTF-8 bytes
        assert accepted["body"] == "é" * limit

        resp = await client.post(NOTES, json={"body": "x" * (limit + 1)}, headers=AUTH)
        assert resp.status == 400
        assert await error_code(resp) == "note_body_too_long"
        resp = await client.patch(
            f"{NOTES}/{accepted['id']}", json={"body": "x" * (limit + 1)}, headers=AUTH)
        assert resp.status == 400
        assert await error_code(resp) == "note_body_too_long"
        assert len(await list_ids(client)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "patch"])
    @pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe"], ids=["garbage", "empty", "bad_utf8"])
    async def test_unparseable_json_is_invalid_json(self, client, method, raw):
        note = await create(client, title="target")
        url = NOTES if method == "post" else f"{NOTES}/{note['id']}"
        resp = await getattr(client, method)(url, data=raw, headers=JSON_HEADERS)
        assert resp.status == 400
        assert await error_code(resp) == "invalid_json"

    @pytest.mark.asyncio
    async def test_unknown_charset_is_invalid_json_not_a_server_error(self, client):
        resp = await client.post(NOTES, data=b'{"title": "x"}', headers={
            **AUTH, "Content-Type": "application/json; charset=no-such-charset"})
        assert resp.status == 400
        assert await error_code(resp) == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "patch"])
    @pytest.mark.parametrize("raw", [
        b"[]", b'"a string"', b"null", b"42",
        b'{"title": 5}', b'{"title": null}', b'{"body": ["x"]}', b'{"body": {"a": 1}}',
        b'{"body": "\\ud800"}',  # lone surrogate: valid JSON, but not storable as UTF-8
    ])
    async def test_non_object_or_wrongly_typed_fields_are_invalid_note(self, client, method, raw):
        note = await create(client, title="target", body="intact")
        url = NOTES if method == "post" else f"{NOTES}/{note['id']}"
        resp = await getattr(client, method)(url, data=raw, headers=JSON_HEADERS)
        assert resp.status == 400
        assert await error_code(resp) == "invalid_note"
        assert await list_ids(client) == [note["id"]]
        stored = (await (await client.get(f"{NOTES}/{note['id']}", headers=AUTH)).json())["note"]
        assert stored == note


BAD_IDS = [
    "..%2Fsecret",              # <home>/secret.json if a server ever decoded the slash
    "%2e%2e%2Fsecret",
    "..%5Csecret",
    "a" * 31, "a" * 33,
    "A" * 32,                   # hex, but not lowercase
    "a" * 32 + "%0A",           # '$' anchoring would let a trailing newline through
    "a" * 32 + ".json",
    "not-an-id",
]


class TestIdGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", BAD_IDS)
    async def test_malformed_ids_are_404_even_when_the_file_they_name_exists(
            self, client, hermes_home, notes_dir, bad_id):
        """Plant a well-formed note at the path each bad id would name if it were joined onto the
        notes directory unchecked: the API must still 404 and leave every file untouched."""
        notes_dir.mkdir()
        planted = notes_dir / (urllib.parse.unquote(bad_id) + ".json")
        planted.write_text(json.dumps({
            "id": "planted", "title": "t", "body": "private",
            "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z"}),
            encoding="utf-8")
        before = {p: p.read_bytes() for p in hermes_home.rglob("*") if p.is_file()}
        assert planted.resolve() in {p.resolve() for p in before}

        url = f"{NOTES}/{bad_id}"
        responses = [await client.get(url, headers=AUTH),
                     await client.patch(url, json={"title": "pwned"}, headers=AUTH),
                     await client.delete(url, headers=AUTH)]
        for resp in responses:
            assert resp.status == 404
            assert await error_code(resp) == "note_not_found"
        assert {p: p.read_bytes() for p in hermes_home.rglob("*") if p.is_file()} == before

    @pytest.mark.asyncio
    async def test_rejected_ids_do_not_create_the_notes_directory(self, client, notes_dir):
        for bad_id in BAD_IDS + ["0" * 32]:
            for method in ("GET", "PATCH", "DELETE"):
                resp = await client.request(
                    method, f"{NOTES}/{bad_id}", json={"title": "x"}, headers=AUTH)
                assert resp.status == 404
        assert not notes_dir.exists()


class TestStorage:
    @pytest.mark.asyncio
    async def test_note_is_a_private_plain_json_file_the_agent_can_read(self, client, notes_dir):
        note = await create(client, title="ملاحظة", body="مرحبا 👋\nsecond line")
        patched = (await (await client.patch(
            f"{NOTES}/{note['id']}", json={"body": "مرحبا 👋 edited"}, headers=AUTH)).json())["note"]

        path = notes_dir / f"{note['id']}.json"
        raw = path.read_text(encoding="utf-8")
        assert json.loads(raw) == patched
        assert "مرحبا 👋 edited" in raw  # stored as readable text, not \u escapes
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(notes_dir.stat().st_mode) == 0o700
        assert [p.name for p in notes_dir.iterdir()] == [path.name]  # no temp files left behind

    @pytest.mark.asyncio
    async def test_a_note_written_by_hand_in_the_documented_shape_is_served(self, client, notes_dir):
        """The agent (or the user) may add a note file directly; the API serves it."""
        notes_dir.mkdir()
        note_id = "b" * 32
        stored = {"id": note_id, "title": "From the agent", "body": "details",
                  "created_at": "2026-10-11T10:00:00.000Z", "updated_at": "2026-10-11T10:00:00.000Z"}
        (notes_dir / f"{note_id}.json").write_text(json.dumps(stored), encoding="utf-8")

        resp = await client.get(f"{NOTES}/{note_id}", headers=AUTH)
        assert (await resp.json()) == {"note": stored}
        assert await list_ids(client) == [note_id]

    @pytest.mark.asyncio
    async def test_list_skips_stray_and_malformed_files(self, client, notes_dir):
        kept = [await create(client, title="one"), await create(client, title="two")]
        valid_shape = json.dumps({"id": "x", "title": "t", "body": "b",
                                  "created_at": "2026-01-01T00:00:00.000Z",
                                  "updated_at": "2026-01-01T00:00:00.000Z"})
        strays = {
            ".tmp-abc123.json": valid_shape,             # in-flight temp file
            "README.txt": "not a note",
            "notes.json": valid_shape,                   # not a 32-hex name
            f"{'C' * 32}.json": valid_shape,             # uppercase hex
            f"{'1' * 32}.json.bak": valid_shape,
            f"{'2' * 32}.json": "{truncated",            # malformed JSON
            f"{'3' * 32}.json": "[]",                    # JSON, but not an object
            f"{'4' * 32}.json": json.dumps({"title": "no body or timestamps"}),
            f"{'5' * 32}.json": json.dumps({"title": 1, "body": 2, "created_at": 3, "updated_at": 4}),
        }
        for name, content in strays.items():
            (notes_dir / name).write_text(content, encoding="utf-8")
        (notes_dir / f"{'6' * 32}.json").mkdir()         # a directory with a note-like name
        (notes_dir / "subdir").mkdir()

        resp = await client.get(NOTES, headers=AUTH)
        assert resp.status == 200
        listed = (await resp.json())["notes"]
        assert sorted(n["id"] for n in listed) == sorted(n["id"] for n in kept)

        # A corrupt file is "not found" rather than a 500 on the id routes too.
        corrupt = f"{NOTES}/{'2' * 32}"
        assert (await client.get(corrupt, headers=AUTH)).status == 404
        assert (await client.patch(corrupt, json={"title": "x"}, headers=AUTH)).status == 404

    @pytest.mark.asyncio
    async def test_listing_before_any_note_exists_is_empty_and_creates_nothing(self, client, notes_dir):
        resp = await client.get(NOTES, headers=AUTH)
        assert resp.status == 200
        assert await resp.json() == {"notes": []}
        assert not notes_dir.exists()


class TestDashboardScripts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/vesper/agents.js", "/vesper/cloudflare-speedtest.js"])
    async def test_scripts_are_served_without_auth_and_never_cached(self, client, path):
        resp = await client.get(path)
        assert resp.status == 200
        assert resp.content_type == "application/javascript"
        assert resp.headers["Cache-Control"] == "no-cache"
        assert await resp.text()
