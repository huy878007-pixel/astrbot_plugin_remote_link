#!/usr/bin/env python3
"""插件端冒烟测试：假代理拨入插件，验证隧道、工作流执行、动态工具与 HTTP 代理。

运行：
    python test/smoke_plugin.py
"""

import asyncio
import base64
import json
import socket
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "stubs"))
sys.path.insert(0, str(ROOT.parent))  # 让 astrbot_plugin_remote_link 包可导入

import aiohttp  # noqa: E402

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.api.star import Context  # noqa: E402
from astrbot_plugin_remote_link.main import RemoteLinkPlugin, RemoteLinkError  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32  # 假 mp4 头，仅用于验证字节回传


class StubEvent:
    """模拟 AstrMessageEvent：记录工具 handler 产出。"""

    def __init__(self):
        self.sent = []
        self.message_str = ""
        self.unified_msg_origin = "test:private:test"

    def plain_result(self, text):
        self.sent.append(("text", text))
        return f"MER:text:{text}"

    def image_result(self, path):
        self.sent.append(("image", path))
        return f"MER:image:{path}"

    def chain_result(self, chain):
        self.sent.append(("chain", chain))
        return f"MER:chain:{len(chain)}"


class FakeProvider:
    """模拟 LLM 提供商：text_chat 返回固定 JSON（提示词增强/路由决策用）。"""

    def __init__(self, model="fake-model"):
        self.model = model
        self.id = "fake"

    class _Resp:
        completion_text = (
            '{"positive": "masterpiece, best quality, 1girl, cyberpunk city, neon lights, cat", '
            '"negative": "worst quality, low quality, blurry"}'
        )

    async def text_chat(self, prompt="", system_prompt="", model=None, temperature=None, **kwargs):
        return self._Resp()

    async def text_chat_stream(self, **kwargs):
        return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def fake_agent(ws_url: str, stop: asyncio.Event):
    """模拟本地代理：接收 request 并按服务名回复。"""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(ws_url) as ws:
            await ws.send_str(
                json.dumps({"type": "hello", "data": {"hostname": "fake-pc", "platform": "Test"}})
            )
            async for msg in ws:
                if stop.is_set():
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                req = json.loads(msg.data)
                if req.get("type") != "request":
                    continue
                rid = req["id"]
                svc = req["service"]
                payload = req.get("payload") or {}
                if svc == "ping":
                    await ws.send_str(
                        json.dumps({"type": "response", "id": rid, "ok": True, "result": {"pong": True}})
                    )
                elif svc == "info":
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "response",
                                "id": rid,
                                "ok": True,
                                "result": {
                                    "hostname": "fake-pc",
                                    "platform": "Test",
                                    "comfyui": {
                                        "ok": True,
                                        "devices": [
                                            {"name": "cuda:0 FakeRTX", "vram_free_gb": 12.0, "vram_total_gb": 24.0}
                                        ],
                                    },
                                    "openai": {"ok": True, "models": ["fake-model"]},
                                },
                            }
                        )
                    )
                elif svc == "workflows":
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "response",
                                "id": rid,
                                "ok": True,
                                "result": {
                                    "workflows": [
                                        {"name": "demo", "relpath": "demo.json", "format": "ui", "size": 10, "modified": 0}
                                    ],
                                    "source": "filesystem",
                                },
                            }
                        )
                    )
                elif svc == "capabilities":
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "response",
                                "id": rid,
                                "ok": True,
                                "result": {
                                    "workflows": [
                                        {
                                            "name": "txt2img",
                                            "relpath": "txt2img.json",
                                            "format": "api",
                                            "tokens": {
                                                "PROMPT": {"type": "string"},
                                                "STEPS": {"type": "int"},
                                                "SEED": {"type": "int"},
                                            },
                                            "outputs": ["image"],
                                            "nodes": ["KSampler", "SaveImage"],
                                        },
                                        {
                                            "name": "wan_t2v",
                                            "relpath": "wan_t2v.json",
                                            "format": "api",
                                            "tokens": {"PROMPT": {"type": "string"}},
                                            "outputs": ["video"],
                                            "nodes": ["WanVideoSampler", "SaveVideo"],
                                        },
                                    ],
                                    "checkpoints": ["sd_xl.safetensors"],
                                    "llm_models": ["qwen2.5:14b"],
                                    "gpu": [],
                                    "queue": {"running": 0, "pending": 0},
                                },
                            }
                        )
                    )
                elif svc == "comfyui" and payload.get("action") == "run_workflow":
                    # 验证插件下发的参数同时回传图片+视频两类产物
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "response",
                                "id": rid,
                                "ok": True,
                                "result": {
                                    "prompt_id": "p1",
                                    "files": [
                                        {
                                            "kind": "image",
                                            "filename": "a.png",
                                            "mime": "image/png",
                                            "base64": base64.b64encode(PNG_1PX).decode("ascii"),
                                        },
                                        {
                                            "kind": "video",
                                            "filename": "v.mp4",
                                            "mime": "video/mp4",
                                            "base64": base64.b64encode(FAKE_MP4).decode("ascii"),
                                        },
                                    ],
                                },
                            }
                        )
                    )
                elif svc == "comfyui" and payload.get("action") == "checkpoints":
                    await ws.send_str(
                        json.dumps(
                            {"type": "response", "id": rid, "ok": True, "result": {"checkpoints": ["m1.safetensors"]}}
                        )
                    )
                elif svc == "comfyui" and payload.get("action") == "queue":
                    await ws.send_str(
                        json.dumps(
                            {"type": "response", "id": rid, "ok": True, "result": {"running": 0, "pending": 2}}
                        )
                    )
                elif svc == "openai":
                    body = payload.get("body") or {}
                    if body.get("stream"):
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "stream_chunk",
                                    "id": rid,
                                    "data": 'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n',
                                }
                            )
                        )
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "response",
                                    "id": rid,
                                    "ok": True,
                                    "result": {"status": 200, "streamed": True, "json": {}},
                                }
                            )
                        )
                    else:
                        await ws.send_str(
                            json.dumps(
                                {
                                    "type": "response",
                                    "id": rid,
                                    "ok": True,
                                    "result": {
                                        "status": 200,
                                        "json": {"choices": [{"message": {"content": "本地回复"}}]},
                                    },
                                }
                            )
                        )
                else:
                    await ws.send_str(
                        json.dumps(
                            {"type": "response", "id": rid, "ok": False, "error": f"unknown service {svc}"}
                        )
                    )


async def main():
    port = free_port()
    ctx = Context()
    cfg = AstrBotConfig(
        {
            "server_host": "127.0.0.1",
            "server_port": port,
            "auth_token": "test-token-123",
            "request_timeout": 30,
            "comfyui_timeout": 30,
            "enable_shell": False,
        }
    )
    plugin = RemoteLinkPlugin(ctx, cfg)
    ctx.set_provider(FakeProvider())  # 桩环境注入假 LLM 提供商（增强/路由决策用）
    tool_names = {getattr(t, "name", "") for t in ctx.tools}
    # 当前架构：智能调度(remote_local_compute) + 本地LLM(remote_llm_chat) [+ 可选 remote_shell]
    # （旧版内置 txt2img 预设工具已随"两级流水线"演进移除，见 main.py 顶部注释）
    assert tool_names == {"remote_llm_chat", "remote_local_compute"}, tool_names
    print("[1/9] 动态工具 + 智能调度工具注册 OK:", sorted(tool_names))

    # 等隧道服务端绑定完成
    for _ in range(50):
        if plugin._runner is not None:
            break
        await asyncio.sleep(0.1)
    assert plugin._runner is not None, "隧道服务端未启动"

    stop = asyncio.Event()
    agent_task = asyncio.ensure_future(fake_agent(f"ws://127.0.0.1:{port}/ws?token=test-token-123", stop))
    # 等代理拨入
    for _ in range(50):
        if plugin.tunnel.connected:
            break
        await asyncio.sleep(0.1)
    assert plugin.tunnel.connected, "假代理未能连接"
    if agent_task.done():
        agent_task.result()

    # 2) 隧道 ping
    r = await plugin._call_local("ping", {}, timeout=10)
    assert r.get("pong") is True, r
    print("[2/9] 隧道 ping OK")

    # 3) 工作流执行：图片+视频回传落盘（当前架构：run_local_task → 分类 → wan_t2v 视频工作流）
    ev1 = StubEvent()
    result = await plugin.run_local_task("生成一张带视频的图", "", ev1)
    assert result["kind"] == "comfyui" and result["workflow"] == "wan_t2v", result
    files = result["files"]
    assert len(files) == 2, files
    by_kind = {f["kind"]: f for f in files}
    assert Path(by_kind["image"]["path"]).read_bytes() == PNG_1PX
    assert Path(by_kind["video"]["path"]).read_bytes() == FAKE_MP4
    print("[3/9] 工作流执行（图+视频产物落盘）OK")

    # 4) 指令层校验：/remote do 空描述应返回用法(而不是挂起)
    ev4 = StubEvent()
    ev4.message_str = "/remote do"
    outs = [o async for o in plugin.cmd_do(ev4)]
    assert any("用法" in str(o) or "不能为空" in str(o) for o in outs), outs
    print("[4/9] 指令层空描述校验 OK")

    # 5) 工作流列表服务
    wf = await plugin._call_local("workflows", {}, timeout=10)
    assert wf["workflows"][0]["name"] == "demo"
    print("[5/9] 工作流列表服务 OK")

    # 6) 智能调度（无 LLM provider 的桩环境 → 规则匹配降级路径）
    ev2 = StubEvent()
    result = await plugin.run_local_task("画一只赛博朋克的猫", "", ev2)
    assert result["kind"] == "comfyui" and result["workflow"] == "txt2img", result
    assert len(result["files"]) == 2, result
    result2 = await plugin.run_local_task("解释一下什么是熵", "", ev2)
    assert result2["kind"] == "llm" and result2["text"] == "本地回复", result2
    print("[6/9] 智能调度（能力清单 + 规则匹配降级）OK")

    # 7) LLM 工具 handler：remote_local_compute 异步受理——先回执给用户,再返回任务号给外部 Agent
    tool = next(t for t in ctx.tools if getattr(t, "name", "") == "remote_local_compute")
    ev = StubEvent()
    outputs = []
    async for out in tool.handler(ev, intent="画一只赛博朋克的猫"):
        outputs.append(out)
    assert "MER:text:✅ 已收到" in str(outputs[0]), outputs
    assert "已受理" in str(outputs[-1]), outputs
    print("[7/9] 智能调度工具 handler（异步受理+任务回执）OK")

    # 8) 本地 LLM
    text = await plugin.local_llm_chat("hi")
    assert text == "本地回复", text
    print("[8/9] 本地 LLM 调用 OK")

    # 9) HTTP 代理 + 鉴权
    async with aiohttp.ClientSession() as s:
        async with s.get(f"http://127.0.0.1:{port}/healthz") as r:
            assert r.status == 200 and (await r.json())["agent_connected"] is True
        async with s.get(
            f"http://127.0.0.1:{port}/v1/models", headers={"Authorization": "Bearer test-token-123"}
        ) as r:
            assert r.status == 200
        async with s.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": "Bearer test-token-123"},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        ) as r:
            assert r.status == 200
            data = await r.json()
            assert data["choices"][0]["message"]["content"] == "本地回复"
        async with s.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": "Bearer test-token-123"},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as r:
            assert r.status == 200
            sse = await r.text()
            assert "你好" in sse and "[DONE]" in sse, sse[:200]
        async with s.get(f"http://127.0.0.1:{port}/v1/models") as r:
            assert r.status == 401
    print("[9/9] HTTP 代理（healthz/models/非流式/流式/鉴权）OK")

    # 断开后等待中的请求应立即收到错误（而不是挂死）
    stop.set()
    agent_task.cancel()
    try:
        await agent_task
    except (asyncio.CancelledError, Exception):
        pass
    await asyncio.sleep(0.3)
    try:
        await plugin._call_local("ping", {}, timeout=5)
        raise AssertionError("代理断开后请求不应成功")
    except Exception as e:
        assert "未连接" in str(e) or "断开" in str(e), e

    await plugin.terminate()
    print("\nPLUGIN SMOKE PASS")


if __name__ == "__main__":
    asyncio.run(main())
