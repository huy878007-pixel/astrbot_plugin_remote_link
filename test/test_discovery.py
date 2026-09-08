"""Discovery 单元测试：mock 网络/进程，不依赖开发者真实环境。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import core.discovery as discovery  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kwargs):
        payload, status = self.routes.get(url, ({}, 404))
        return FakeResponse(status=status, payload=payload)


async def main():
    session = FakeSession({
        "http://127.0.0.1:8188/system_stats": ({"status": "ok"}, 200),
        "http://127.0.0.1:11434/v1/models": (
            {"models": [{"id": "qwen"}, {"id": "llama"}]}, 200,
        ),
    })
    original_ffmpeg = discovery._probe_ffmpeg
    async def fake_ffmpeg():
        return {"ok": True, "path": "/usr/bin/ffmpeg", "version": "ffmpeg test"}

    discovery._probe_ffmpeg = fake_ffmpeg
    try:
        d = discovery.Discovery({
            "comfyui": {"base_url": "http://127.0.0.1:8188"},
            "openai": {"base_url": "http://127.0.0.1:11434/v1"},
        })
        result = await d.run(session)
    finally:
        discovery._probe_ffmpeg = original_ffmpeg

    assert result.machine.get("name")
    assert isinstance(result.services, list)
    comfyui = next(s for s in result.services if s.get("type") == "comfyui")
    assert comfyui["ok"] is True and comfyui["base_url"] == "http://127.0.0.1:8188"
    assert any(s.get("ok") and "llama" in (s.get("models") or []) for s in result.services)
    assert "image.generate" not in result.capabilities, "服务在线不等于能力可用"
    assert "llm.chat" in result.capabilities
    assert "media.transcode" in result.capabilities
    assert any("ComfyUI 在线" in i.get("message", "") for i in result.issues)
    print("DISCOVERY PASS")


asyncio.run(main())
