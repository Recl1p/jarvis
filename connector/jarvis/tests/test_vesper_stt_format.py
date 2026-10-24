"""/vesper/stt names its temp file after the recording's real container format.

Safari/iOS MediaRecorder can only produce audio/mp4, and the STT pipeline validates input by file
extension, so a fixed ".webm" suffix would mislabel every iPhone recording."""

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

AUTH = {"Authorization": "Bearer test-vesper-key"}


def app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-vesper-key"}))
    application = web.Application()
    for method, path, handler in adapter._http_route_table():
        application.router.add_route(method, path, handler)
    return application


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type, suffix", [
    ("audio/mp4", ".m4a"),
    ("audio/webm;codecs=opus", ".webm"),
    ("audio/ogg;codecs=opus", ".ogg"),
])
async def test_recording_suffix_follows_content_type(monkeypatch, content_type, suffix):
    seen = {}

    def fake_transcribe(path, *_args):
        seen["suffix"] = Path(path).suffix
        seen["bytes"] = Path(path).read_bytes()
        return {"success": True, "transcript": "hello"}

    import tools.transcription_tools as transcription_tools
    monkeypatch.setattr(transcription_tools, "transcribe_audio", fake_transcribe)
    async with TestClient(TestServer(app())) as client:
        resp = await client.post("/vesper/stt", data=b"\x00" * 2048,
                                 headers={**AUTH, "Content-Type": content_type})
        assert resp.status == 200
        assert (await resp.json())["text"] == "hello"
    assert seen == {"suffix": suffix, "bytes": b"\x00" * 2048}
