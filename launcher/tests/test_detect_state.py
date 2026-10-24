"""Finding a Hermes (one row per computer) and the launcher's saved state."""

from jarvis_launcher import config, detect, state


async def test_the_docker_host_shows_once_under_its_lan_address(monkeypatch):
    hits = {"http://172.18.0.1:8642", "http://host.docker.internal:8642", "http://192.168.1.103:8642", "http://192.168.1.50:8642"}

    async def probe(session, url):
        return {"url": url, "hermes_version": "0.22.5", "connector_version": ""} if url in hits else None

    monkeypatch.setattr(detect, "_probe", probe)
    monkeypatch.setattr(detect, "_default_gateway", lambda: "172.18.0.1")
    monkeypatch.setattr(detect, "_resolves", lambda name: True)
    monkeypatch.setattr(detect, "_ip_of", lambda name: "172.18.0.1")
    monkeypatch.setattr(config, "SCAN_SUBNETS", ["192.168.1.0/24"])
    found = await detect.find(client_ip="192.168.1.20", host_ip="192.168.1.103")
    urls = [f["url"] for f in found]
    assert urls.count("http://192.168.1.103:8642") == 1
    assert "http://172.18.0.1:8642" not in urls and "http://host.docker.internal:8642" not in urls
    assert "http://192.168.1.50:8642" in urls
    this = next(f for f in found if f["url"] == "http://192.168.1.103:8642")
    assert this["source"] == "this computer"


async def test_a_mounted_hermes_home_gives_its_key(monkeypatch):
    home = config.HOST_HERMES_HOME
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("platforms:\n  api_server:\n    enabled: true\n    port: 8650\n")
    (home / ".env").write_text("API_SERVER_KEY=" + "a" * 64 + "\n")

    async def probe(session, url):
        return {"url": url, "hermes_version": "0.22.5", "connector_version": "2.0.0"} if url.endswith(":8650") else None

    monkeypatch.setattr(detect, "_probe", probe)
    monkeypatch.setattr(detect, "_resolves", lambda name: False)
    monkeypatch.setattr(detect, "_default_gateway", lambda: "")
    found = await detect.find()
    assert found and found[0]["url"] == "http://host.docker.internal:8650" and found[0]["key_known"] is True
    assert detect.mounted_key("http://host.docker.internal:8650/") == "a" * 64


async def test_a_mounted_hermes_that_does_not_answer_says_why(monkeypatch):
    home = config.HOST_HERMES_HOME
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model:\n  default: x\n")

    async def probe(session, url):
        return None

    monkeypatch.setattr(detect, "_probe", probe)
    monkeypatch.setattr(detect, "_resolves", lambda name: False)
    monkeypatch.setattr(detect, "_default_gateway", lambda: "")
    found = await detect.find()
    assert found[0]["problem"] == "its API server is not enabled"


def test_state_defaults_and_private_file():
    data = state.load()
    assert data["mode"] is None and data["updates"]["auto"] is True and data["updates"]["hour"] == 4
    state.update(mode="managed")
    path = config.STATE_DIR / "state.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert state.load()["mode"] == "managed"


def test_update_history_keeps_the_last_twenty():
    for i in range(25):
        state.record_update({"ok": True, "to": str(i)})
    data = state.load()["updates"]
    assert len(data["history"]) == 20 and data["history"][-1]["to"] == "24" and data["last_result"]["to"] == "24"
