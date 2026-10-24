"""A confirmed model lock on a named custom endpoint (the guide's Local route) keeps its reply; real mismatches don't."""

from types import SimpleNamespace

import pytest

from jarvis import wiring


class Adapter:
    def __init__(self):
        self.calls = []

    def _turn_runtime_metadata(self, agent, *, route, requested_runtime, route_source, confirmed_runtime_lock):
        self.calls.append(confirmed_runtime_lock)
        expected = (route or {}).get("provider") or (requested_runtime or {}).get("provider")
        if confirmed_runtime_lock and expected != agent.provider:
            raise RuntimeError(f"confirmed model lock runtime mismatch: expected provider={expected}")
        return {"provider": agent.provider, "model": agent.model}


def wrapped():
    adapter = Adapter()
    wiring._accept_named_custom_providers(adapter)
    return adapter


def call(adapter, agent, provider="ollama-11434", model="llama3"):
    return adapter._turn_runtime_metadata(
        agent, route=None, requested_runtime={"provider": provider, "model": model}, route_source="request",
        confirmed_runtime_lock=True)


def test_named_custom_endpoint_with_the_locked_model_is_accepted():
    adapter = wrapped()
    assert call(adapter, SimpleNamespace(provider="custom", model="llama3")) == {"provider": "custom", "model": "llama3"}
    assert adapter.calls == [True, False]


def test_a_different_model_still_fails():
    with pytest.raises(RuntimeError):
        call(wrapped(), SimpleNamespace(provider="custom", model="other-model"))


def test_a_different_real_provider_still_fails():
    with pytest.raises(RuntimeError):
        call(wrapped(), SimpleNamespace(provider="openrouter", model="llama3"))


def test_wrapping_twice_is_harmless():
    adapter = wrapped()
    first = adapter._turn_runtime_metadata
    wiring._accept_named_custom_providers(adapter)
    assert adapter._turn_runtime_metadata is first
