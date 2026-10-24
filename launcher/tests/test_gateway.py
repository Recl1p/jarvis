"""Starting the managed gateway survives Hermes's first-start s6 race, and gives up cleanly when it must."""

from jarvis_launcher import hermes_setup, managed


async def test_start_retries_until_s6_supervises_the_new_slot(monkeypatch):
    answers = [(111, "✗ s6-svc start on 'gateway-default' failed (rc=102): supervisor not listening"), (0, "started")]
    calls = []

    def fake_hermes(*args, **kwargs):
        calls.append(args)
        return answers.pop(0)

    waits = []

    async def fake_wait(seconds=15):
        waits.append(seconds)
        return True

    monkeypatch.setattr(hermes_setup, "hermes", fake_hermes)
    monkeypatch.setattr(managed.Gateway, "_wait_supervised", staticmethod(fake_wait))
    gateway = managed.Gateway()
    code, out = await gateway.start()
    assert (code, out) == (0, "started")
    assert calls == [("gateway", "start"), ("gateway", "start")] and len(waits) == 1
    assert gateway.running


async def test_start_gives_up_after_three_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(hermes_setup, "hermes", lambda *a, **k: (calls.append(a), (1, "broken"))[1])

    async def fake_wait(seconds=15):
        return False

    monkeypatch.setattr(managed.Gateway, "_wait_supervised", staticmethod(fake_wait))
    gateway = managed.Gateway()
    code, _ = await gateway.start()
    assert code == 1 and len(calls) == 4 and not gateway.running


def test_gateway_log_tail_without_a_log():
    assert "no gateway log" in managed.gateway_log_tail()[0]
