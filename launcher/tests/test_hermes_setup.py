"""Setting a Hermes up for JARVIS: reading config, the API key, the connector, the persona, and old JARVIS volumes."""

import os
import stat

from jarvis_launcher import config, hermes_setup


def home():
    return config.HERMES_HOME


def test_config_get_reads_scalars_from_the_file():
    (home() / "config.yaml").write_text(
        "platforms:\n  api_server:\n    enabled: true\n    port: 8643\nbrowser:\n  backend: 'off'\n  headed: false\n")
    assert hermes_setup.config_get("platforms.api_server.enabled") == "true"
    assert hermes_setup.config_get("platforms.api_server.port") == "8643"
    assert hermes_setup.config_get("browser.backend") == "off"
    assert hermes_setup.config_get("browser.headed") == "false"
    assert hermes_setup.config_get("browser.cdp_url") == ""
    assert hermes_setup.config_get("nothing.here") == ""


def test_config_get_without_a_config_file_is_empty():
    assert hermes_setup.config_get("model.default") == ""


def test_config_set_skips_values_already_set(monkeypatch):
    (home() / "config.yaml").write_text("browser:\n  engine: chrome\n")
    calls = []
    monkeypatch.setattr(hermes_setup, "hermes", lambda *a, **k: (calls.append(a), (0, ""))[1])
    hermes_setup.config_set("browser.engine", "chrome", lambda m: None)
    assert calls == []
    hermes_setup.config_set("browser.headed", "true", lambda m: None)
    assert calls == [("config", "set", "browser.headed", "true")]


def test_browser_tools_are_enabled_where_missing(monkeypatch):
    (home() / "config.yaml").write_text(
        "platform_toolsets:\n  cli: [web, browser]\n  api_server: [web]\n  telegram: [hermes-telegram]\n")
    calls = []
    monkeypatch.setattr(hermes_setup, "hermes", lambda *a, **k: (calls.append(a), (0, ""))[1])
    hermes_setup.enable_browser_tools(lambda m: None)
    assert calls == [("tools", "enable", "--platform", p, "browser") for p in ("api_server", "telegram", "cron")]


def test_api_key_is_created_once_and_kept_private():
    log = []
    key = hermes_setup.ensure_api_key(log.append)
    assert len(key) == 64
    env = home() / ".env"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert hermes_setup.ensure_api_key(log.append) == key
    assert env.read_text().count("API_SERVER_KEY=") == 1


def test_a_short_api_key_is_replaced_and_other_lines_kept():
    (home() / ".env").write_text("OPENROUTER_API_KEY=abc\nAPI_SERVER_KEY=short\n")
    key = hermes_setup.ensure_api_key(lambda m: None)
    text = (home() / ".env").read_text()
    assert "OPENROUTER_API_KEY=abc" in text and f"API_SERVER_KEY={key}" in text and "short" not in text


def test_connector_is_copied_without_tests_and_only_when_it_differs():
    assert hermes_setup.install_connector(lambda m: None) is True
    target = home() / "plugins" / "jarvis"
    assert (target / "plugin.yaml").is_file() and (target / "wiring.py").is_file()
    assert not (target / "tests").exists()
    assert hermes_setup.connector_current()
    assert hermes_setup.install_connector(lambda m: None) is False
    (target / "wiring.py").write_text("# changed\n")
    assert not hermes_setup.connector_current()
    assert hermes_setup.install_connector(lambda m: None) is True
    assert hermes_setup.installed_connector_version() == hermes_setup.bundled_connector_version()


def test_persona_keeps_the_previous_soul_once():
    (home() / "SOUL.md").write_text("my own persona\n")
    hermes_setup.apply_persona(lambda m: None)
    assert "You are JARVIS" in (home() / "SOUL.md").read_text()
    assert (home() / "SOUL.md.before-jarvis").read_text() == "my own persona\n"
    (home() / "SOUL.md").write_text("edited again\n")
    hermes_setup.apply_persona(lambda m: None)
    assert (home() / "SOUL.md.before-jarvis").read_text() == "my own persona\n"


def test_old_volume_loses_the_retired_plugins_and_the_dead_hermes_link():
    (home() / "plugins" / "openjarvis-browser").mkdir(parents=True)
    (home() / "plugins" / "openjarvis-browser" / "plugin.yaml").write_text("name: openjarvis-browser\n")
    (home() / "config.yaml").write_text(
        "# kept comment\nplugins:\n  enabled:\n    - openjarvis-browser\n  disabled: []\nmodel:\n  default: x\n")
    bindir = home() / ".local" / "bin"
    bindir.mkdir(parents=True)
    os.symlink("/opt/hermes/.venv/bin/hermes", bindir / "hermes")
    os.symlink("/somewhere/else", bindir / "hermes-other")
    hermes_setup.prepare_old_volume(lambda m: None)
    assert not (home() / "plugins" / "openjarvis-browser").exists()
    assert (config.STATE_DIR / "retired-plugins" / "openjarvis-browser" / "plugin.yaml").is_file()
    text = (home() / "config.yaml").read_text()
    assert "# kept comment" in text and "openjarvis-browser" not in text and "  enabled: []\n" in text
    assert not os.path.lexists(bindir / "hermes")
    assert os.path.lexists(bindir / "hermes-other")


def test_retired_toolsets_leave_every_platform(monkeypatch):
    (home() / "config.yaml").write_text(
        "platform_toolsets:\n  cli: [web, openjarvis_browser]\n  api_server:\n    - openjarvis_browser\n    - memory\n"
        "  telegram: [hermes-telegram]\n")
    calls = []
    monkeypatch.setattr(hermes_setup, "hermes", lambda *a, **k: (calls.append(a), (0, ""))[1])
    hermes_setup.retire_old_plugins(lambda m: None)
    assert ("config", "set", "platform_toolsets.cli", "[web]") in calls
    assert ("config", "set", "platform_toolsets.api_server", "[memory]") in calls
    assert not any("telegram" in c[2] for c in calls if len(c) > 2)


def test_installed_needs_both_the_command_and_the_checkout():
    assert not hermes_setup.installed()
    config.HERMES_BIN.parent.mkdir(parents=True)
    config.HERMES_BIN.write_text("#!/bin/sh\n")
    config.HERMES_BIN.chmod(0o755)
    assert not hermes_setup.installed()
    (config.HERMES_INSTALL_DIR / ".git").mkdir(parents=True)
    assert hermes_setup.installed()
