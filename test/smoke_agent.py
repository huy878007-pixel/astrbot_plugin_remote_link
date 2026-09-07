#!/usr/bin/env python3
"""代理端冒烟测试：用假云端服务端测真实 agent/local_agent.py 的连接与应答。

运行：
    python test/smoke_agent.py
"""

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from local_agent import LocalAgent  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


UI_WORKFLOW = {
    "nodes": [
        {"id": 4, "type": "CheckpointLoaderSimple", "widgets_values": ["m.safetensors"]},
        {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["__PROMPT__"]},
    ],
    "links": [[1, 4, 1, 6, 0, "CLIP"]],
}

API_WORKFLOW = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "__CHECKPOINT__"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "__PROMPT__", "clip": ["4", 1]}},
}


async def stub_plugin_server(port: int, received: dict):
    """模拟云端插件：依次发 ping / info / workflows / run_workflow / 未知服务 请求。"""

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=256 * 1024 * 1024)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            t = data.get("type")
            if t == "hello":
                received["hello"] = data.get("data") or {}
                await ws.send_str(
                    json.dumps({"type": "request", "id": "r1", "service": "ping", "payload": {}})
                )
            elif t == "response" and data.get("id") == "r1":
                received["ping_ok"] = data.get("ok") is True
                await ws.send_str(
                    json.dumps({"type": "request", "id": "r2", "service": "info", "payload": {}})
                )
            elif t == "response" and data.get("id") == "r2":
                received["info_ok"] = data.get("ok") is True
                received["info"] = data.get("result") or {}
                await ws.send_str(
                    json.dumps({"type": "request", "id": "r3", "service": "workflows", "payload": {}})
                )
            elif t == "response" and data.get("id") == "r3":
                received["wf_ok"] = data.get("ok") is True
                received["wf"] = (data.get("result") or {}).get("workflows") or []
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "request",
                            "id": "r4",
                            "service": "comfyui",
                            "payload": {
                                "action": "run_workflow",
                                "workflow": "",
                                "timeout": 10,
                                "params": {
                                    "prompt": "a cat",
                                    "width": 64,
                                    "height": 64,
                                    "steps": 2,
                                    "cfg": 7.0,
                                    "seed": -1,
                                    "batch": 1,
                                    "checkpoint": "",
                                },
                            },
                        }
                    )
                )
            elif t == "response" and data.get("id") == "r4":
                # 本地没有真 ComfyUI（base_url 指向未监听端口），预期失败但错误信息要清晰
                received["run_ok"] = data.get("ok") is False
                received["run_err"] = str(data.get("error") or "")
                await ws.send_str(
                    json.dumps({"type": "request", "id": "r5", "service": "capabilities", "payload": {}})
                )
            elif t == "response" and data.get("id") == "r5":
                received["caps_ok"] = data.get("ok") is True
                received["caps"] = data.get("result") or {}
                await ws.send_str(
                    json.dumps({"type": "request", "id": "r6", "service": "no_such", "payload": {}})
                )
            elif t == "response" and data.get("id") == "r6":
                received["err_ok"] = data.get("ok") is False and "未知服务" in str(data.get("error"))
                await ws.close()
                break
        return ws

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


async def wait_until(received: dict, keys: list[str]):
    while not all(k in received for k in keys):
        await asyncio.sleep(0.2)


async def main():
    port = free_port()
    received: dict = {}
    runner = await stub_plugin_server(port, received)

    # 建一个临时 user 目录，放一个 UI 格式和一个 API 格式的工作流
    tmpdir = Path(tempfile.mkdtemp(prefix="remote_link_test_"))
    wf_root = tmpdir / "default" / "workflows"
    wf_root.mkdir(parents=True)
    (wf_root / "demo_ui.json").write_text(json.dumps(UI_WORKFLOW), encoding="utf-8")
    (wf_root / "demo_api.json").write_text(json.dumps(API_WORKFLOW), encoding="utf-8")

    cfg = {
        "server_url": f"ws://127.0.0.1:{port}/ws",
        "token": "test-token",
        "reconnect_seconds": 1,
        "request_timeout": 30,
        "dashboard_port": 0,  # 测试里关闭看板
        # 故意指向未监听的端口：验证 run_workflow 在无 ComfyUI 时给出清晰错误
        "comfyui": {
            "base_url": "http://127.0.0.1:9",
            "timeout": 10,
            "userdata_dir": str(tmpdir),
            "default_checkpoint": "default.safetensors",
            "default_workflow_file": "",
        },
        "openai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "", "timeout": 30},
        "shell": {"enabled": False, "timeout": 30},
    }
    agent = LocalAgent(cfg)
    task = asyncio.ensure_future(agent.run())
    try:
        await asyncio.wait_for(
            wait_until(
                received, ["hello", "ping_ok", "info_ok", "wf_ok", "run_ok", "caps_ok", "err_ok"]
            ),
            timeout=30,
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await runner.cleanup()

    assert received.get("ping_ok") is True, received
    assert received.get("info_ok") is True, received
    assert isinstance(received.get("info"), dict) and received["info"].get("hostname"), received
    assert received.get("wf_ok") is True, received
    wfs = received.get("wf") or []
    assert len(wfs) == 2, wfs
    fmt = {w["name"]: w["format"] for w in wfs}
    assert fmt.get("demo_ui") == "ui" and fmt.get("demo_api") == "api", fmt
    assert received.get("run_ok") is True, received
    assert received.get("run_err"), "无 ComfyUI 时应返回清晰错误信息"
    assert received.get("caps_ok") is True, received
    caps = received.get("caps") or {}
    cap_wfs = caps.get("workflows") or []
    assert len(cap_wfs) == 2, caps
    # demo_api 是 API 格式，无需 object_info 即可扫出占位符参数
    api_cap = next((w for w in cap_wfs if w.get("name") == "demo_api"), None)
    assert api_cap and "PROMPT" in (api_cap.get("tokens") or {}), api_cap
    assert caps.get("queue") == {"running": 0, "pending": 0}, caps
    assert received.get("err_ok") is True, received

    print("hello:     ", json.dumps(received.get("hello"), ensure_ascii=False)[:200])
    print("工作流列表: ", json.dumps(wfs, ensure_ascii=False))
    print("能力清单:   ", json.dumps(caps, ensure_ascii=False)[:300])
    print("run 错误:  ", received.get("run_err", "")[:120])
    print("\nAGENT SMOKE PASS")


if __name__ == "__main__":
    asyncio.run(main())
