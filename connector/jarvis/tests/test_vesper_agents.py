"""Exercise VESPER HTTP controls against real isolated Hermes cron persistence."""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cron.jobs import get_job, list_jobs, save_job_output
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


AUTH = {"Authorization": "Bearer test-vesper-key"}
FIELDS = {"name": "Research tracker", "prompt": "Summarize the supplied research notes.",
          "schedule": "every 6h", "model": "test-model", "provider": "ollama",
          "skills": [], "enabled_toolsets": ["web"]}


@pytest.mark.asyncio
async def test_agent_controls_persist_settings_and_share_scheduler_state(tmp_path):
    async with TestClient(TestServer(app())) as client:
        response = await client.post("/vesper/agents", json=FIELDS, headers=AUTH)
        assert response.status == 201, await response.text()
        job = (await response.json())["job"]
        jid = job["id"]
        saved = get_job(jid)
        assert saved["model"] == FIELDS["model"]
        assert saved["enabled_toolsets"] == FIELDS["enabled_toolsets"]
        assert saved["deliver"] == "local"
        assert saved["next_run_at"]
        assert jid in [j["id"] for j in list_jobs()]

        response = await client.post(f"/api/jobs/{jid}/pause", headers=AUTH)
        assert response.status == 200
        assert get_job(jid)["state"] == "paused"
        changed = {**FIELDS, "name": "Updated tracker", "schedule": "every 12h", "model": "second-model"}
        response = await client.patch(f"/vesper/agents/{jid}", json=changed, headers=AUTH)
        assert response.status == 200, await response.text()
        saved = get_job(jid)
        assert saved["state"] == "paused"
        assert saved["schedule"]["minutes"] == 720
        assert saved["model"] == changed["model"]
        response = await client.post(f"/api/jobs/{jid}/resume", headers=AUTH)
        assert response.status == 200
        assert get_job(jid)["state"] == "scheduled"
        response = await client.post(f"/api/jobs/{jid}/run", headers=AUTH)
        assert response.status == 200, await response.text()

        # A scheduler result round-trips as text; no LLM or live job is invoked.
        output = "# Research result\n\n<script>alert('not HTML')</script>"
        saved_file = save_job_output(jid, output)
        response = await client.get(f"/vesper/agents/{jid}/output", headers=AUTH)
        assert response.status == 200
        data = await response.json()
        assert data["content"] == output
        assert data["selected"] == saved_file.name
        response = await client.delete(f"/api/jobs/{jid}", headers=AUTH)
        assert response.status == 200
        assert get_job(jid) is None
        assert not saved_file.exists()


@pytest.mark.asyncio
async def test_agent_auth_validation_and_output_boundaries(tmp_path):
    async with TestClient(TestServer(app())) as client:
        response = await client.post("/vesper/agents", json=FIELDS)
        assert response.status == 401
        assert list_jobs(include_disabled=True) == []
        for invalid in ([], {**FIELDS, "name": ""}, {**FIELDS, "prompt": 3},
                        {**FIELDS, "schedule": "not a schedule"},
                        {**FIELDS, "enabled_toolsets": []}, {**FIELDS, "skills": "web"}):
            response = await client.post("/vesper/agents", json=invalid, headers=AUTH)
            assert response.status == 400, await response.text()
        assert list_jobs(include_disabled=True) == []
        response = await client.post("/vesper/agents", json={**FIELDS, "script": "/ignored", "no_agent": True}, headers=AUTH)
        assert response.status == 201, await response.text()
        jid = (await response.json())["job"]["id"]
        assert not get_job(jid)["script"]
        assert get_job(jid)["no_agent"] is False
        response = await client.patch(f"/vesper/agents/{jid}", json=FIELDS)
        assert response.status == 401
        response = await client.get(f"/vesper/agents/{jid}/output")
        assert response.status == 401
        response = await client.get(f"/vesper/agents/{jid}/output?file=../../config.yaml", headers=AUTH)
        assert response.status == 404
        saved = save_job_output(jid, "x" * 300_000)
        response = await client.get(f"/vesper/agents/{jid}/output", headers=AUTH)
        data = await response.json()
        assert data["truncated"] and len(data["content"]) == 256_000
        outside = tmp_path / "private.md"
        outside.write_text("private content", encoding="utf-8")
        saved.unlink()
        saved.symlink_to(outside)
        response = await client.get(f"/vesper/agents/{jid}/output", headers=AUTH)
        data = await response.json()
        assert data["files"] == [] and data["content"] == ""
