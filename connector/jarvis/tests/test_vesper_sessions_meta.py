"""Context-window figures for the dashboard's sessions drawer (GET /vesper/sessions/meta and /{id}/context)."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from jarvis import sessions_meta as meta
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB

AUTH = {"Authorization": "Bearer test-vesper-key"}
WINDOW = {"model": "test-model", "context_length": 100_000, "compress_at": 75_000}


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def fixed_window(monkeypatch):
    """No provider probing, no tool registry: one known window and a 1,000-token tool schema."""
    monkeypatch.setattr(meta, "_window_for", lambda model: dict(WINDOW))
    monkeypatch.setattr(meta, "_tool_schema_tokens", lambda: 1000)
    meta._usage_cache.clear()


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def app(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    adapter._session_db = session_db
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


def chat(session_db, session_id, texts, system_prompt=None, **kwargs):
    session_db.create_session(session_id, "api_server", system_prompt=system_prompt, **kwargs)
    session_db.replace_messages(session_id, [
        {"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(texts)])


@pytest.mark.asyncio
async def test_routes_require_auth(session_db):
    async with TestClient(TestServer(app(session_db))) as client:
        for path in ("/vesper/sessions/meta?ids=a", "/vesper/sessions/a/context"):
            assert (await client.get(path)).status == 401, path


@pytest.mark.asyncio
async def test_context_estimates_system_prompt_messages_and_tools(session_db):
    # 400 + 4000 + 8000 characters at about four per token, plus the 1,000-token tool schema (and a few tokens
    # of per-message overhead in Hermes's own estimator).
    chat(session_db, "s1", ["x" * 4000, "y" * 8000], system_prompt="s" * 400)
    async with TestClient(TestServer(app(session_db))) as client:
        resp = await client.get("/vesper/sessions/s1/context", headers=AUTH)
        assert resp.status == 200
        data = await resp.json()
    assert data["session_id"] == data["id"] == "s1"
    assert data["used_tokens"] == pytest.approx(100 + 1000 + 2000 + 1000, abs=60)
    assert data["context_length"] == 100_000
    assert data["pct"] == pytest.approx(4.1, abs=0.3)
    assert data["compress_at"] == 75_000
    assert data["model"] == "test-model"          # the session has none of its own: the gateway's model
    assert data["estimated"] is True
    assert data["lineage_ids"] == ["s1"]


@pytest.mark.asyncio
async def test_context_pct_is_a_percentage_capped_at_100(session_db):
    chat(session_db, "big", ["z" * 600_000])
    async with TestClient(TestServer(app(session_db))) as client:
        data = await (await client.get("/vesper/sessions/big/context", headers=AUTH)).json()
    assert data["used_tokens"] > 100_000
    assert data["pct"] == 100.0


@pytest.mark.asyncio
async def test_empty_chat_has_zero_usage_and_session_model_is_kept(session_db):
    session_db.create_session("fresh", "api_server", model="my-model")
    async with TestClient(TestServer(app(session_db))) as client:
        data = await (await client.get("/vesper/sessions/fresh/context", headers=AUTH)).json()
    assert data["used_tokens"] == 0 and data["pct"] == 0.0
    assert data["model"] == "my-model"


@pytest.mark.asyncio
async def test_unknown_session_is_404_on_context_and_null_in_meta(session_db):
    chat(session_db, "known", ["hi"])
    async with TestClient(TestServer(app(session_db))) as client:
        resp = await client.get("/vesper/sessions/nope/context", headers=AUTH)
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == "session_not_found"
        data = await (await client.get("/vesper/sessions/meta?ids=known,nope", headers=AUTH)).json()
    assert data["data"]["nope"] is None
    assert data["data"]["known"]["id"] == "known"


@pytest.mark.asyncio
async def test_meta_batches_and_validates_ids(session_db):
    for name in ("a", "b"):
        chat(session_db, name, ["hello " * 100])
    async with TestClient(TestServer(app(session_db))) as client:
        data = await (await client.get("/vesper/sessions/meta?ids=a,b,a", headers=AUTH)).json()
        assert set(data["data"]) == {"a", "b"}
        assert data["data"]["a"]["used_tokens"] > 1000
        for bad in ("", "?ids=", "?ids=" + ",".join(f"s{i}" for i in range(meta._MAX_IDS + 1)),
                    "?ids=" + "x" * 201):
            resp = await client.get("/vesper/sessions/meta" + (bad if bad.startswith("?") else ""), headers=AUTH)
            assert resp.status == 400, bad
            assert (await resp.json())["error"]["code"] == "invalid_ids"


@pytest.mark.asyncio
async def test_compressed_chat_follows_the_tip_and_reports_its_lineage(session_db):
    """After a compaction the chat continues in a child session; an id from before it resolves to the live tip."""
    session_db.create_session("root", "api_server")
    session_db.end_session("root", "compression")
    chat(session_db, "tip", ["after the compaction " * 50], parent_session_id="root")
    async with TestClient(TestServer(app(session_db))) as client:
        data = await (await client.get("/vesper/sessions/meta?ids=root,tip", headers=AUTH)).json()
    for asked in ("root", "tip"):
        entry = data["data"][asked]
        assert entry["id"] == "tip"
        assert entry["lineage_ids"] == ["root", "tip"]
        assert entry["used_tokens"] > 1000


@pytest.mark.asyncio
async def test_usage_follows_new_messages(session_db):
    chat(session_db, "grow", ["short"])
    async with TestClient(TestServer(app(session_db))) as client:
        before = (await (await client.get("/vesper/sessions/grow/context", headers=AUTH)).json())["used_tokens"]
        session_db.append_message("grow", "assistant", "long " * 4000)
        after = (await (await client.get("/vesper/sessions/grow/context", headers=AUTH)).json())["used_tokens"]
    assert after > before + 4000


@pytest.mark.asyncio
async def test_unresolvable_window_still_reports_lineage(session_db, monkeypatch):
    def broken(model):
        raise RuntimeError("no provider")
    monkeypatch.setattr(meta, "_window_for", broken)
    chat(session_db, "s1", ["hi"])
    async with TestClient(TestServer(app(session_db))) as client:
        data = await (await client.get("/vesper/sessions/s1/context", headers=AUTH)).json()
    assert data["id"] == "s1"
    assert data["used_tokens"] is None and data["context_length"] is None and data["pct"] is None


@pytest.mark.asyncio
async def test_dashboard_sessions_are_stored_as_api_server(session_db):
    """The dashboard creates its session with source "vesper-dashboard"; the API server only knows a fixed set of
    sources and stores anything else as api_server, which is what the drawer's list has to ask for."""
    async with TestClient(TestServer(app(session_db))) as client:
        resp = await client.post("/api/sessions", json={"source": "vesper-dashboard"}, headers=AUTH)
        assert resp.status == 201
        created = (await resp.json())["session"]
        assert created["source"] == "api_server"
        listed = await (await client.get("/api/sessions?source=api_server&limit=100", headers=AUTH)).json()
    assert [s["id"] for s in listed["data"]] == [created["id"]]


def test_compress_at_follows_the_agents_threshold_rules(monkeypatch):
    import gateway.run as gateway_run
    config = {}
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: config)
    assert meta._compress_at("m", 200_000) == 150_000      # windows under 512K are floored at 75%
    assert meta._compress_at("m", 1_000_000) == 500_000    # the default 50%
    config["compression"] = {"threshold": 0.4, "model_thresholds": {"big": 0.3}, "threshold_tokens": 120_000}
    assert meta._compress_at("m", 1_000_000) == 120_000    # the absolute cap wins when lower
    assert meta._compress_at("big-1", 300_000) == 120_000  # floored to 75% = 225K, then capped
    config["compression"] = {"enabled": False}
    assert meta._compress_at("m", 200_000) is None
