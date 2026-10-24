"""An unlocked session's persisted model pins its turns only while the provider that served it is still the
main provider; after a provider switch (hermes model, the dashboard's free setup) it follows the new default."""

import pytest

from jarvis.wiring import _session_provider_is_current


@pytest.fixture
def main_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def write(provider):
        body = "model:\n  default: some-model\n" + (f"  provider: {provider}\n" if provider else "")
        (tmp_path / "config.yaml").write_text(body)
        import hermes_cli.config as config
        for name in ("_CONFIG_CACHE", "_config_cache"):
            if hasattr(config, name):
                monkeypatch.setattr(config, name, None if not isinstance(getattr(config, name), dict) else {})
        cache_clear = getattr(config.load_config_readonly, "cache_clear", None)
        if cache_clear:
            cache_clear()
    return write


@pytest.mark.parametrize("recorded, current, pinned", [
    ("anthropic", "anthropic", True),     # same provider: the session keeps its model
    ("anthropic", "openrouter", False),   # provider switched: follow the new default
    ("Anthropic", "anthropic", True),     # case doesn't matter
    (None, "openrouter", True),           # nothing recorded yet: keep today's behaviour
    ("", "openrouter", True),
    ("anthropic", None, True),            # no main provider configured: keep the pin
    ("anthropic", "auto", True),
])
def test_session_provider_is_current(main_provider, recorded, current, pinned):
    main_provider(current)
    session = {"id": "s1", "model": "claude-sonnet-5", "billing_provider": recorded}
    assert _session_provider_is_current(session) is pinned


def test_non_dict_session_keeps_the_pin(main_provider):
    main_provider("openrouter")
    assert _session_provider_is_current(None) is True
