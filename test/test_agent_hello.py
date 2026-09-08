"""hello v2 capability 必须来自 Registry 而非服务在线猜测。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from local_agent import LocalAgent  # noqa: E402
from core.capability import Capability, CapabilityRegistry, capabilities_from_workflows  # noqa: E402


def make_agent():
    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",
        "token": "t",
        "comfyui": {"base_url": "http://127.0.0.1:8188", "userdata_dir": ""},
        "openai": {"base_url": "", "api_key": "", "timeout": 5},
        "shell": {"enabled": False, "allowed_patterns": []},
    }
    return LocalAgent(cfg)


def test_hello_capabilities_only_from_registry():
    agent = make_agent()
    reg = CapabilityRegistry()
    reg.register(Capability(id="system.inspect", provider="local", status="ready"))
    for cap in capabilities_from_workflows([
        {"name": "T2I", "outputs": ["image"], "injects": {"images": 0, "videos": 0}}
    ]):
        reg.register(cap)
    agent._registry = reg

    caps = agent.ready_capability_ids()
    assert "image.generate" in caps
    assert "image.edit" not in caps
    assert "video.generate" not in caps
    assert "video.image_to_video" not in caps


if __name__ == "__main__":
    test_hello_capabilities_only_from_registry()
    print("AGENT HELLO PASS")
