"""Protocol v2 hello 解析测试。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.main import PROTOCOL_VERSION  # noqa: E402
from astrbot_plugin_remote_link.core.tunnel import TunnelServer  # noqa: E402


async def main():
    assert PROTOCOL_VERSION == 2
    tunnel = TunnelServer({"auth_token": "test-token"})
    hello = {
        "type": "hello",
        "v": 2,
        "agent_version": "0.2.0",
        "machine": {"name": "DESKTOP-X", "os": "Windows 11"},
        "capabilities": ["image.generate", "llm.chat"],
        "data": {"hostname": "DESKTOP-X", "platform": "Windows 11"},
    }
    await tunnel.handle_text(json.dumps(hello))
    agent = tunnel.current_agent
    assert agent is not None
    assert agent.protocol_version == 2
    assert agent.agent_version == "0.2.0"
    assert agent.capabilities == ["image.generate", "llm.chat"]
    assert agent.machine["name"] == "DESKTOP-X"
    assert agent.info.get("hostname") == "DESKTOP-X"
    print("PROTOCOL V2 PASS")


asyncio.run(main())
