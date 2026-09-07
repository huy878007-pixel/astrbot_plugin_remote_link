"""Capability Registry 单元测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from core.capability import Capability, CapabilityRegistry  # noqa: E402


def test_register_and_ready():
    reg = CapabilityRegistry()
    reg.register(Capability(id="image.generate", provider="comfyui", status="ready", metadata={"port": 8188}))
    reg.register({"id": "llm.chat", "provider": "ollama", "status": "ready"})
    reg.register({"id": "image.generate", "provider": "remote-server", "status": "unavailable"})

    assert len(reg.get("image.generate")) == 2
    assert len(reg.ready("image.generate")) == 1
    assert reg.ready("image.generate")[0].provider == "comfyui"
    assert reg.all_capability_ids() == ["image.generate", "llm.chat"]


def test_update_status_and_unregister():
    reg = CapabilityRegistry()
    reg.register(Capability(id="llm.chat", provider="openai", status="ready"))
    reg.update_status("llm.chat", "openai", "degraded", {"note": "slow"})
    assert reg.get("llm.chat")[0].status == "degraded"
    assert reg.get("llm.chat")[0].metadata["note"] == "slow"

    assert reg.unregister("llm.chat", "openai") == 1
    assert reg.get("llm.chat") == []


if __name__ == "__main__":
    test_register_and_ready()
    test_update_status_and_unregister()
    print("CAPABILITY REGISTRY PASS")
