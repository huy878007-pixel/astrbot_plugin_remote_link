"""AstrBot Offline 独立性测试：未连接 AstrBot 时，环境扫描/Brain 摘要仍可用。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from local_agent import LocalAgent  # noqa: E402


class FakeDiscovery:
    async def run(self, session=None):
        return SimpleNamespace(
            machine={"name": "PC", "os": "Windows 11", "ram_total_gb": 64.0, "gpus": []},
            services=[
                {"ok": False, "type": "comfyui", "base_url": "http://127.0.0.1:8188"},
                {"ok": False, "type": "llm", "provider_type": "ollama", "base_url": "http://127.0.0.1:11434"},
            ],
            capabilities=[],
            issues=[],
            brain={},
            scanned_at=0.0,
        )


async def main():
    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",
        "token": "test-token",
        "comfyui": {"base_url": "http://127.0.0.1:8188", "userdata_dir": ""},
        "openai": {"base_url": "", "api_key": "", "timeout": 5},
        "shell": {"enabled": False, "allowed_patterns": []},
    }
    agent = LocalAgent(cfg)
    agent._discovery = FakeDiscovery()
    agent._check_comfyui = lambda: asyncio.sleep(0) or {"ok": False, "error": "offline"}
    agent._check_openai = lambda: asyncio.sleep(0) or {"ok": False, "error": "offline", "models": []}
    agent._check_openai_services = lambda: asyncio.sleep(0) or []
    agent._check_ffmpeg = lambda: asyncio.sleep(0) or {"ok": False, "error": "no ffmpeg"}
    agent._list_workflows_fs = lambda: []

    snap = await agent.scan_environment()
    assert snap["machine"]["name"] == "PC"
    assert "system.inspect" in snap["capabilities"]
    # 没有 ComfyUI/LLM 在线时不应伪造 image.generate / llm.chat
    cap_ids = set()
    for caps in snap["capabilities"].values():
        for c in caps:
            cap_ids.add(c["id"])
    assert "image.generate" not in cap_ids
    assert "llm.chat" not in cap_ids
    # Brain 摘要不依赖 AstrBot
    brain = agent.brain_summary()
    assert brain["status"] == "not_configured"
    assert "api_key" not in brain["profile"] and "api_key" not in snap["brain"]["profile"]
    print("LOCAL AGENT OFFLINE PASS")


asyncio.run(main())
