"""Capability Registry 单元测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from core.capability import Capability, CapabilityRegistry, capabilities_from_workflows  # noqa: E402


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



def test_duplicate_scan_is_idempotent():
    reg = CapabilityRegistry()
    for _ in range(3):
        reg.register(Capability(
            id="image.generate", provider="comfyui", status="ready",
            evidence={"type": "workflow", "workflow": "T2I"},
        ))
    assert len(reg.get("image.generate")) == 1, "同一 capability_id + provider 不得重复"
    assert reg.get("image.generate")[0].evidence["workflow"] == "T2I"


def test_evidence_field():
    reg = CapabilityRegistry()
    reg.register(Capability(
        id="video.image_to_video", provider="comfyui", status="ready",
        evidence={"type": "workflow", "workflow": "Wan2.2-I2V"},
    ))
    item = reg.ready("video.image_to_video")[0]
    assert item.evidence["workflow"] == "Wan2.2-I2V"


def test_capability_truth_from_workflows():
    caps = capabilities_from_workflows([
        {"name": "T2I", "outputs": ["image"], "injects": {"images": 0, "videos": 0}},
        {"name": "I2V", "outputs": ["video"], "injects": {"images": 1, "videos": 0}},
        {"name": "T2V", "outputs": ["video"], "injects": {"images": 0, "videos": 0}},
        {"name": "Audio", "outputs": ["audio"], "injects": {}},
    ])
    ids = {c.id for c in caps}
    assert "image.generate" in ids
    assert "image.edit" not in ids
    assert "video.image_to_video" in ids
    assert "video.generate" in ids
    assert "audio.generate" in ids
    # 在线但无对应工作流时不应推导出 ready（由调用方不注册该能力）
    caps_no_video = capabilities_from_workflows([
        {"name": "T2I", "outputs": ["image"], "injects": {}}
    ])
    assert all(c.id != "video.generate" for c in caps_no_video)

if __name__ == "__main__":
    test_register_and_ready()
    test_duplicate_scan_is_idempotent()
    test_evidence_field()
    test_capability_truth_from_workflows()
    test_update_status_and_unregister()
    print("CAPABILITY REGISTRY PASS")
