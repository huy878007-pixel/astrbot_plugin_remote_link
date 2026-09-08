"""Health Refresh → Environment Snapshot 同步测试（ready → offline → ready）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from local_agent import LocalAgent  # noqa: E402
from core.capability import Capability, CapabilityRegistry  # noqa: E402


def make_agent():
    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",
        "token": "t",
        "comfyui": {"base_url": "http://127.0.0.1:8188", "userdata_dir": ""},
        "openai": {"base_url": "", "api_key": "", "timeout": 5},
        "shell": {"enabled": False, "allowed_patterns": []},
    }
    return LocalAgent(cfg)


def ready_registry():
    reg = CapabilityRegistry()
    reg.register(Capability(id="system.inspect", provider="local", status="ready"))
    reg.register(Capability(id="image.generate", provider="comfyui", status="ready",
                            evidence={"type": "workflow", "workflow": "T2I"}))
    return reg


def offline_registry():
    reg = CapabilityRegistry()
    reg.register(Capability(id="system.inspect", provider="local", status="ready"))
    reg.register(Capability(id="image.generate", provider="comfyui", status="unavailable"))
    return reg


def test_snapshot_follows_health_without_full_rescan():
    agent = make_agent()
    agent._environment_snapshot = {
        "machine": {"name": "PC"},
        "services": [
            {"type": "comfyui", "provider_type": "comfyui", "ok": True,
             "base_url": "http://127.0.0.1:8188", "source": "configured"},
            {"type": "llm", "provider_type": "ollama", "ok": True,
             "base_url": "http://127.0.0.1:11434", "models": ["qwen"]},
        ],
        "capabilities": {},
        "issues": [],
        "brain": {},
        "scanned_at": 0.0,
    }

    # ready
    agent._registry = ready_registry()
    snap = agent.refresh_environment_snapshot({
        "comfyui": {"ok": True, "base_url": "http://127.0.0.1:8188", "devices": [], "queue": {}},
        "openai": {"ok": True, "base_url": "http://127.0.0.1:11434", "models": ["qwen"]},
        "openai_services": [],
        "ffmpeg": {"ok": False},
    })
    assert snap["services"][0]["ok"] is True
    assert any(c.get("status") == "ready" and c.get("id") == "image.generate"
               for caps in snap["capabilities"].values() for c in caps)

    # offline
    agent._registry = offline_registry()
    snap = agent.refresh_environment_snapshot({
        "comfyui": {"ok": False, "base_url": "http://127.0.0.1:8188", "error": "offline"},
        "openai": {"ok": True, "base_url": "http://127.0.0.1:11434", "models": ["qwen"]},
        "openai_services": [],
        "ffmpeg": {"ok": False},
    })
    assert snap["services"][0]["ok"] is False
    assert not any(c.get("status") == "ready" and c.get("id") == "image.generate"
                   for caps in snap["capabilities"].values() for c in caps)

    # ready again
    agent._registry = ready_registry()
    snap = agent.refresh_environment_snapshot({
        "comfyui": {"ok": True, "base_url": "http://127.0.0.1:8188", "devices": [], "queue": {}},
        "openai": {"ok": True, "base_url": "http://127.0.0.1:11434", "models": ["qwen"]},
        "openai_services": [],
        "ffmpeg": {"ok": False},
    })
    assert snap["services"][0]["ok"] is True
    assert any(c.get("status") == "ready" and c.get("id") == "image.generate"
               for caps in snap["capabilities"].values() for c in caps)
    assert "health_checked_at" in snap


if __name__ == "__main__":
    test_snapshot_follows_health_without_full_rescan()
    print("HEALTH SNAPSHOT PASS")
