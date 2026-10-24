"""Weather location for the dashboard: a place saved server-side, used when the browser can't locate
itself (plain-http LAN origins have no navigator.geolocation), plus the agenda's absolute timestamps."""

import json
import os
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


class FakeHTTP:
    """Stands in for aiohttp.ClientSession: records each GET and answers with canned JSON."""

    calls = []

    def __init__(self, payload):
        self.payload = payload

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        FakeHTTP.calls.append((url, dict(params or {})))
        return self

    @property
    def status(self):
        return 200

    async def json(self):
        return self.payload


def fake_upstream(monkeypatch, payload):
    import aiohttp
    FakeHTTP.calls = []
    monkeypatch.setattr(aiohttp, "ClientSession", FakeHTTP(payload))


FORECAST = {"current": {"temperature_2m": 11.6, "weather_code": 3, "wind_speed_10m": 9.4,
                        "wind_direction_10m": 270, "time": "2026-10-12T20:00"},
            "hourly": {"time": [], "precipitation_probability": []}}


@pytest.mark.asyncio
async def test_location_routes_require_auth():
    async with TestClient(TestServer(app())) as client:
        for method, path in (("GET", "/vesper/location"), ("PUT", "/vesper/location"),
                             ("DELETE", "/vesper/location"), ("GET", "/vesper/geocode?q=Toronto")):
            resp = await client.request(method, path, json={"name": "X", "lat": 1, "lon": 1})
            assert resp.status == 401, (method, path)


@pytest.mark.asyncio
async def test_saved_location_round_trip(hermes_home):
    async with TestClient(TestServer(app())) as client:
        assert (await (await client.get("/vesper/location", headers=AUTH)).json()) == {"location": None}
        resp = await client.put("/vesper/location", headers=AUTH,
                                json={"name": "  Toronto,   Ontario ", "lat": 43.7, "lon": -79.4})
        assert resp.status == 200
        saved = (await resp.json())["location"]
        assert saved == {"name": "Toronto, Ontario", "lat": 43.7, "lon": -79.4}
        assert (await (await client.get("/vesper/location", headers=AUTH)).json())["location"] == saved
        path = hermes_home / "vesper-location.json"
        assert json.loads(path.read_text()) == saved
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert (await client.delete("/vesper/location", headers=AUTH)).status == 200
        assert (await (await client.get("/vesper/location", headers=AUTH)).json()) == {"location": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"name": "Nowhere", "lat": 91, "lon": 0}, {"name": "Nowhere", "lat": 0, "lon": -181},
    {"name": "Nowhere", "lat": "43", "lon": 0}, {"name": "Nowhere", "lat": True, "lon": 0},
    {"name": "", "lat": 0, "lon": 0}, {"name": "x" * 121, "lat": 0, "lon": 0}, ["not", "an", "object"]])
async def test_invalid_location_rejected(body, hermes_home):
    async with TestClient(TestServer(app())) as client:
        resp = await client.put("/vesper/location", headers=AUTH, json=body)
        assert resp.status == 400
        assert (await resp.json())["error"]["code"] == "invalid_location"
    assert not (hermes_home / "vesper-location.json").exists()


@pytest.mark.asyncio
async def test_weather_without_coordinates_uses_saved_place(monkeypatch):
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/weather")
        assert resp.status == 404
        assert (await resp.json())["error"]["code"] == "location_not_set"

        await client.put("/vesper/location", headers=AUTH, json={"name": "Toronto", "lat": 43.7, "lon": -79.4})
        fake_upstream(monkeypatch, FORECAST)
        data = await (await client.get("/vesper/weather")).json()
        assert data["location"] == "Toronto" and data["temp_c"] == 12 and data["wind_dir"] == "W"
        url, _ = FakeHTTP.calls[-1]
        assert "latitude=43.7" in url and "longitude=-79.4" in url

        # Coordinates from a browser that CAN geolocate still win, and aren't labelled with the saved name.
        data = await (await client.get("/vesper/weather?lat=51.5&lon=-0.12")).json()
        assert data["location"] is None
        assert "latitude=51.5" in FakeHTTP.calls[-1][0]


@pytest.mark.asyncio
async def test_geocode_returns_clean_labels(monkeypatch):
    fake_upstream(monkeypatch, {"results": [
        {"name": "Singapore", "admin1": "Singapore", "country": "Singapore", "latitude": 1.29, "longitude": 103.85},
        {"name": "Broken", "latitude": None, "longitude": 3}]})
    async with TestClient(TestServer(app())) as client:
        resp = await client.get("/vesper/geocode?q=Singapore", headers=AUTH)
        assert resp.status == 200
        assert (await resp.json())["results"] == [{"name": "Singapore", "lat": 1.29, "lon": 103.85}]
        assert FakeHTTP.calls[-1][1]["name"] == "Singapore"
        bad = await client.get("/vesper/geocode?q=x", headers=AUTH)
        assert bad.status == 400 and (await bad.json())["error"]["code"] == "invalid_query"


@pytest.mark.asyncio
async def test_agenda_carries_absolute_time(monkeypatch):
    monkeypatch.setattr(api_server, "_CRON_AVAILABLE", True)
    monkeypatch.setattr(api_server, "_cron_list", lambda include_disabled=False: [
        {"id": "a1", "name": "Weekly report", "next_run_at": "2026-10-18T15:00:00+00:00"}])
    async with TestClient(TestServer(app())) as client:
        agenda = (await (await client.get("/vesper/status")).json())["agenda"]
    assert agenda == [{"time": "15:00", "at": "2026-10-18T15:00:00+00:00", "label": "Weekly report"}]
