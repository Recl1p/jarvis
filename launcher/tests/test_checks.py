"""Test connection: each check, in order, and the hint that says what to do."""

from jarvis_launcher import checks, hermes_setup

from fake_hermes import KEY, make_app


def by_id(result):
    return {c["id"]: c for c in result["checks"]}


async def test_all_green(aiohttp_server):
    server = await aiohttp_server(make_app(connector=hermes_setup.bundled_connector_version()))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is True
    found = by_id(result)
    for cid in ("reach", "hermes", "auth", "connector", "connector_version", "isolation", "dashboard"):
        assert found[cid]["ok"] is True, cid
    assert found["model"]["ok"] is None and "mock-model" in found["model"]["detail"]
    assert result["hermes_version"] == "0.22.5"


async def test_nothing_listening():
    result = await checks.run("http://127.0.0.1:9", KEY, timeout=3)
    assert result["ok"] is False
    assert result["checks"][0]["id"] == "reach" and "0.0.0.0" in result["checks"][0]["hint"]


async def test_not_hermes(aiohttp_server):
    server = await aiohttp_server(make_app(platform="something-else"))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is False and by_id(result)["hermes"]["ok"] is False and "auth" not in by_id(result)


async def test_too_old(aiohttp_server):
    server = await aiohttp_server(make_app(version="0.21.0"))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is False and by_id(result)["version"]["ok"] is False


async def test_wrong_key(aiohttp_server):
    server = await aiohttp_server(make_app())
    result = await checks.run(str(server.make_url("")), "wrong")
    assert result["ok"] is False and "API_SERVER_KEY" in by_id(result)["auth"]["hint"]


async def test_no_connector_points_at_the_prompt(aiohttp_server):
    server = await aiohttp_server(make_app(connector=None))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is False and "connection prompt" in by_id(result)["connector"]["hint"]


async def test_older_connector_is_a_warning_not_a_failure(aiohttp_server):
    server = await aiohttp_server(make_app(connector="1.0.0"))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is True and by_id(result)["connector_version"]["ok"] is None


async def test_plugin_host_isolation_fails_with_the_fix(aiohttp_server):
    server = await aiohttp_server(make_app(isolation="host"))
    result = await checks.run(str(server.make_url("")), KEY)
    assert result["ok"] is False and "in_process" in by_id(result)["isolation"]["hint"]
