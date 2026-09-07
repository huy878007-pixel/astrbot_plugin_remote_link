"""Tunnel 模块单元测试：AgentConnection、hello v2、请求/流式回调、认证限流。"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.core.tunnel import AgentConnection, TunnelServer  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, text):
        self.sent.append(text)


def fake_request(headers=None, query=None, remote="1.2.3.4", path="/ws"):
    return SimpleNamespace(headers=headers or {}, query=query or {}, remote=remote, path=path)


async def test_hello_v2():
    t = TunnelServer({"auth_token": "t"})
    hello = {
        "type": "hello",
        "v": 2,
        "agent_version": "0.2.0",
        "machine": {"name": "pc", "os": "Windows 11"},
        "capabilities": ["image.generate", "llm.chat"],
        "data": {"hostname": "pc", "platform": "Windows 11"},
    }
    await t.handle_text(json.dumps(hello))
    assert t.current_agent is not None
    assert t.current_agent.protocol_version == 2
    assert t.current_agent.agent_version == "0.2.0"
    assert t.current_agent.capabilities == ["image.generate", "llm.chat"]
    # 仅 handle_text 时 ws 可能为 None（真实连接时由 handle_ws 设置 ws），不检查 connected


async def test_request_response():
    t = TunnelServer({"auth_token": "t", "request_timeout": 5})
    ws = FakeWS()
    t._agent = AgentConnection(ws=ws)
    fut = asyncio.ensure_future(t.request("ping", {}, timeout=5))
    await asyncio.sleep(0)
    assert len(ws.sent) == 1
    req = json.loads(ws.sent[0])
    await t.handle_text(json.dumps({"type": "response", "id": req["id"], "ok": True, "result": {"pong": True}}))
    result = await fut
    assert result == {"pong": True}


async def test_request_stream_chunk():
    t = TunnelServer({"auth_token": "t", "request_timeout": 5})
    ws = FakeWS()
    t._agent = AgentConnection(ws=ws)
    chunks = []
    fut = asyncio.ensure_future(t.request_stream("openai", {"stream": True}, on_chunk=chunks.append, timeout=5))
    await asyncio.sleep(0)
    rid = json.loads(ws.sent[0])["id"]
    await t.handle_text(json.dumps({"type": "stream_chunk", "id": rid, "data": "hello"}))
    await t.handle_text(json.dumps({"type": "response", "id": rid, "ok": True, "result": {}}))
    await fut
    assert chunks == ["hello"]


def test_auth_and_rate_limit():
    t = TunnelServer({"auth_token": "secret"})
    assert t.authorized(fake_request(headers={"Authorization": "Bearer secret"}))
    assert not t.authorized(fake_request(headers={"Authorization": "Bearer no"}))
    for _ in range(3):
        assert t.check_rate_limit(fake_request(path="/ws", remote="9.9.9.9"), limit=3, window=60) is True
    assert t.check_rate_limit(fake_request(path="/ws", remote="9.9.9.9"), limit=3, window=60) is False


if __name__ == "__main__":
    asyncio.run(test_hello_v2())
    asyncio.run(test_request_response())
    asyncio.run(test_request_stream_chunk())
    test_auth_and_rate_limit()
    print("TUNNEL PASS")
