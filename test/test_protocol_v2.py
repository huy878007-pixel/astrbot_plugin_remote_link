"""Protocol v2 hello 解析测试。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.main import PROTOCOL_VERSION, RemoteLinkPlugin  # noqa: E402


def make_plugin():
    p = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    p._pending = {}
    p._chunk_cbs = {}
    p._run_progress = None
    p._agent_info = {}
    p._agent_connected_at = 0.0
    return p


async def main():
    assert PROTOCOL_VERSION == 2
    p = make_plugin()
    hello = {
        "type": "hello",
        "v": 2,
        "agent_version": "0.2.0",
        "machine": {"name": "DESKTOP-X", "os": "Windows 11"},
        "capabilities": ["image.generate", "llm.chat"],
        "data": {"hostname": "DESKTOP-X", "platform": "Windows 11"},
    }
    await p._on_ws_text(json.dumps(hello))
    assert p._agent_info["protocol_version"] == 2
    assert p._agent_info["agent_version"] == "0.2.0"
    assert p._agent_info["capabilities"] == ["image.generate", "llm.chat"]
    assert p._agent_info["machine"]["name"] == "DESKTOP-X"
    assert p._agent_info.get("hostname") == "DESKTOP-X"
    print("PROTOCOL V2 PASS")


asyncio.run(main())
