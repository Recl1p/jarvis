"""The connect prompt, the connect script and the connector download for someone's own Hermes."""

import io
import subprocess
import tarfile
import time

from jarvis_launcher import connect


def test_prompt_names_this_jarvis_and_the_code_and_keeps_the_key_out_of_chat():
    text = connect.prompt("http://10.0.0.5:8642", "abc123")
    assert "curl -fsSL http://10.0.0.5:8642/jarvis/connect.sh -o /tmp/jarvis-connect.sh" in text
    assert "bash /tmp/jarvis-connect.sh --pair abc123" in text
    assert "Don't repeat the API key in this chat" in text
    assert "--persona-only" in text and "Only if I say yes" in text


def test_script_is_valid_bash_with_the_address_filled_in(tmp_path):
    script = connect.script("http://10.0.0.5:8642")
    assert 'JARVIS_URL="http://10.0.0.5:8642"' in script and "@JARVIS_URL@" not in script
    path = tmp_path / "connect.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


def test_script_refuses_unknown_options(tmp_path):
    path = tmp_path / "connect.sh"
    path.write_text(connect.script("http://127.0.0.1:9"))
    done = subprocess.run(["bash", str(path), "--bogus"], capture_output=True, text=True)
    assert done.returncode == 2 and "unknown option" in done.stderr


def test_script_needs_hermes_on_path(tmp_path):
    path = tmp_path / "connect.sh"
    path.write_text(connect.script("http://127.0.0.1:9"))
    done = subprocess.run(["bash", str(path)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert done.returncode == 1 and "hermes command is not on PATH" in done.stderr


def test_pairing_codes_work_once_and_expire(monkeypatch):
    code = connect.new_pairing_code()
    assert connect.pairing_valid(code)
    assert not connect.pairing_valid("other")
    connect.consume_pairing(code)
    assert not connect.pairing_valid(code)
    code = connect.new_pairing_code()
    real = time.monotonic
    monkeypatch.setattr(connect.time, "monotonic", lambda: real() + connect.PAIR_TTL_SECONDS + 1)
    assert not connect.pairing_valid(code)


def test_connector_tarball_has_one_jarvis_folder_without_tests():
    data = connect.connector_tarball()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "jarvis/plugin.yaml" in names and "jarvis/wiring.py" in names
    assert all(n.startswith("jarvis/") for n in names)
    assert not any("/tests/" in n or "__pycache__" in n for n in names)


def test_soul_is_the_jarvis_persona():
    assert "You are JARVIS" in connect.soul()
