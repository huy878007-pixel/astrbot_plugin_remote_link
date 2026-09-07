#!/usr/bin/env python3
"""WS 驱动 + 入口注入测试：用假 ComfyUI（HTTP + 原生 WS）端到端验证代理的执行链路。

场景 1：WS 推送模式（官方示例顺序：先连 WS 再提交，executing 完成事件推送 + progress 转发）
场景 2：无 WS 支持（老版本 ComfyUI）→ 自动回退 /history 轮询
同时校验：ETN_LoadImageBase64 / Simple String 入口节点注入、占位符替换、产物回传。

运行：
    python test/test_ws_driver.py
"""

import asyncio
import base64
import json
import socket
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "agent"))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from local_agent import (  # noqa: E402
    LocalAgent,
    inject_texts,
    parse_comfyui_400_summary,
    randomize_negative_seeds,
    smart_merge_texts,
)

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

WORKFLOW = {
    "1": {"class_type": "ETN_LoadImageBase64", "inputs": {"image": ""}},
    "2": {"class_type": "Simple String", "inputs": {"text": ""}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "__PROMPT__", "clip": ["4", 1]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
    "5": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["4", 0],
            "positive": ["3", 0],
            "negative": ["3", 0],
            "latent_image": ["6", 0],
            "seed": "__SEED__",
            "steps": "__STEPS__",
            "cfg": "__CFG__",
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1,
        },
    },
    "6": {"class_type": "EmptyLatentImage", "inputs": {"width": "__WIDTH__", "height": "__HEIGHT__", "batch_size": 1}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "Yunxin", "images": ["8", 0]}},
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def fake_comfyui(port: int, ws_enabled: bool, state: dict):
    """假 ComfyUI：记录 /prompt 提交内容，按官方协议发 WS 事件，返回一个 PNG 产物。"""

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["ws_connected"] = True
        # 等 /prompt 提交后由 post_prompt 向本连接发完成事件
        state["ws"] = ws
        async for _msg in ws:
            pass
        return ws

    async def post_prompt(request):
        body = await request.json()
        state["prompt_body"] = body
        state["prompt_id"] = "p-test"
        ws = state.get("ws")
        if ws is not None and not ws.closed:
            await ws.send_str(json.dumps({"type": "progress", "data": {"value": 1, "max": 2, "prompt_id": "p-test"}}))
            await ws.send_str(json.dumps({"type": "progress", "data": {"value": 2, "max": 2, "prompt_id": "p-test"}}))
            await ws.send_str(json.dumps({"type": "executing", "data": {"node": None, "prompt_id": "p-test"}}))
        return web.json_response({"prompt_id": "p-test"})

    async def upload(request):
        data = await request.post()
        if "image" in data:
            kind, f = "image", data["image"]
        else:
            kind, f = "video", data["video"]
        state.setdefault("uploads", []).append({"kind": kind, "name": f.filename})
        return web.json_response({"name": f.filename, "subfolder": "", "type": "input"})

    async def get_history(request):
        outputs = state.get(
            "outputs_override",
            {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}},
        )
        return web.json_response(
            {"p-test": {"status": {"completed": True, "status_str": "success"}, "outputs": outputs}}
        )

    async def get_view(request):
        return web.Response(body=PNG_1PX, content_type="image/png")

    app = web.Application()
    if ws_enabled:
        app.router.add_get("/ws", ws_handler)
    else:
        app.router.add_get("/ws", lambda r: web.Response(status=404))
    app.router.add_post("/prompt", post_prompt)
    app.router.add_post("/upload/image", upload)
    app.router.add_post("/upload/video", upload)
    app.router.add_get("/history/{pid}", get_history)
    app.router.add_get("/view", get_view)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner


async def run_scenario(ws_enabled: bool) -> dict:
    port = free_port()
    state: dict = {}
    runner = await fake_comfyui(port, ws_enabled, state)

    tmpdir = Path(tempfile.mkdtemp(prefix="yunxin_ws_test_"))
    wf_root = tmpdir / "default" / "workflows"
    wf_root.mkdir(parents=True)
    (wf_root / "inject_demo.json").write_text(json.dumps(WORKFLOW), encoding="utf-8")

    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",  # 云端隧道用不到（不走隧道）
        "token": "test-token",
        "reconnect_seconds": 5,
        "request_timeout": 30,
        "dashboard_port": 0,
        "comfyui": {
            "base_url": f"http://127.0.0.1:{port}",
            "timeout": 30,
            "userdata_dir": str(tmpdir),
            "default_checkpoint": "m.safetensors",
            "default_workflow_file": "",
        },
        "openai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "", "timeout": 10},
        "shell": {"enabled": False, "timeout": 10},
    }
    agent = LocalAgent(cfg)
    agent.session = aiohttp.ClientSession()
    try:
        result = await agent.handle_service(
            "rid-1",
            "comfyui",
            {
                "action": "run_workflow",
                "workflow": "inject_demo",
                "timeout": 30,
                "params": {"prompt": "a cat", "seed": 1, "steps": 8, "cfg": 7, "width": 512, "height": 512},
                "inject": {"images": ["aW1nYmFzZTY0"], "texts": ["第一段文案"]},
            },
        )
    finally:
        await agent.session.close()
        await runner.cleanup()
    return {"result": result, "state": state, "ws_connected": state.get("ws_connected", False)}


def test_inject_unit():
    """注入单元测试：文本智能合并（图片数量校验走 apply_injects 的集成路径）。"""
    wf = {
        "2": {"class_type": "Simple String", "inputs": {"text": ""}},
        "3": {"class_type": "Simple String", "inputs": {"text": ""}},
    }
    inject_texts(wf, ["你好"])
    assert wf["2"]["inputs"]["text"] == "你好"


def test_adoptions_unit():
    """社区插件经验采纳的单元测试：文本合并 / 种子随机化 / 400 错误摘要。"""
    # 文本槽智能合并
    assert smart_merge_texts(["a", "b", "c"], 1) == ["a b c"]
    assert smart_merge_texts(["a", "b", "c"], 2) == ["a", "b c"]
    assert smart_merge_texts(["a", "b"], 3) == ["a", "b"]
    assert smart_merge_texts([], 2) == []
    # 种子随机化：只随机负数，尊重显式正数
    wf = {"3": {"inputs": {"seed": -1, "steps": 20}}, "4": {"inputs": {"seed": 42}}}
    out = randomize_negative_seeds(wf)
    assert out["3"]["inputs"]["seed"] > 0 and out["3"]["inputs"]["steps"] == 20
    assert out["4"]["inputs"]["seed"] == 42
    # 400 错误摘要：模型不存在 → 给出可用清单
    body = json.dumps(
        {
            "node_errors": {
                "4": {
                    "errors": [
                        {
                            "type": "value_not_in_list",
                            "details": "value not in list",
                            "extra_info": {
                                "input_name": "ckpt_name",
                                "received_value": "ghost.safetensors",
                                "input_config": [["a.safetensors", "b.safetensors"], {}],
                            },
                        }
                    ]
                }
            }
        }
    )
    summary = parse_comfyui_400_summary(body)
    assert summary and "ghost.safetensors" in summary and "a.safetensors" in summary, summary


async def test_workflow_file_manage():
    """工作流上传/删除（云端管理本地文件，覆盖前备份）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="yunxin_wfmgr_"))
    wf_root = tmpdir / "default" / "workflows"
    wf_root.mkdir(parents=True)
    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",
        "token": "test-token",
        "reconnect_seconds": 5,
        "request_timeout": 30,
        "dashboard_port": 0,
        "comfyui": {
            "base_url": "http://127.0.0.1:9",
            "timeout": 30,
            "userdata_dir": str(tmpdir),
            "default_checkpoint": "x",
            "default_workflow_file": "",
        },
        "openai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "", "timeout": 10},
        "shell": {"enabled": False, "timeout": 10},
    }
    agent = LocalAgent(cfg)
    # 上传
    r1 = await agent.svc_workflows({"action": "upload", "name": "新工作流.json", "content": json.dumps(WORKFLOW)})
    assert r1["ok"] and r1["format"] == "api"
    assert (wf_root / "新工作流.json").exists()
    # 覆盖上传 → 自动备份
    r2 = await agent.svc_workflows({"action": "upload", "name": "新工作流", "content": json.dumps(WORKFLOW)})
    assert r2["ok"]
    assert list(wf_root.glob("新工作流.bak.*.json")), "覆盖前应有备份"
    # 路径穿越拒绝
    try:
        await agent.svc_workflows({"action": "upload", "name": "../escape", "content": json.dumps(WORKFLOW)})
        raise AssertionError("路径穿越应被拒绝")
    except ValueError as e:
        assert "非法" in str(e)
    # 删除 → 备份
    r3 = await agent.svc_workflows({"action": "delete", "name": "新工作流"})
    assert r3["ok"] and list(wf_root.glob("新工作流.del.*.json")), "删除应有备份"
    assert not (wf_root / "新工作流.json").exists()


UPLOAD_WORKFLOW = {
    "10": {"class_type": "LoadImage", "inputs": {"image": "__IMAGE__"}},
    "20": {"class_type": "VHS_LoadVideo", "inputs": {"video": "__VIDEO__"}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "Step_preview"}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "Final_upload"}},
}


async def test_upload_inject_and_metadata():
    """上传注入（LoadImage __IMAGE__ 占位 + VHS __VIDEO__ 占位）+ 产物元数据与主产物排序。"""
    port = free_port()
    state: dict = {
        "outputs_override": {
            "8": {"images": [{"filename": "Step_preview_00001_.png", "subfolder": "", "type": "output"}]},
            "9": {"images": [{"filename": "Final_upload_00001_.png", "subfolder": "", "type": "output"}]},
        }
    }
    runner = await fake_comfyui(port, ws_enabled=True, state=state)
    tmpdir = Path(tempfile.mkdtemp(prefix="yunxin_up_test_"))
    wf_root = tmpdir / "default" / "workflows"
    wf_root.mkdir(parents=True)
    (wf_root / "upload_demo.json").write_text(json.dumps(UPLOAD_WORKFLOW), encoding="utf-8")
    cfg = {
        "server_url": "ws://127.0.0.1:1/ws",
        "token": "test-token",
        "reconnect_seconds": 5,
        "request_timeout": 30,
        "dashboard_port": 0,
        "comfyui": {
            "base_url": f"http://127.0.0.1:{port}",
            "timeout": 30,
            "userdata_dir": str(tmpdir),
            "default_checkpoint": "m.safetensors",
            "default_workflow_file": "",
        },
        "openai": {"base_url": "http://127.0.0.1:9/v1", "api_key": "", "timeout": 10},
        "shell": {"enabled": False, "timeout": 10},
    }
    agent = LocalAgent(cfg)
    agent.session = aiohttp.ClientSession()
    try:
        # 数量不匹配校验
        try:
            await agent.apply_injects(
                {"10": {"class_type": "LoadImage", "inputs": {"image": "__IMAGE__"}}},
                {"images": ["AA==", "BB=="]},
                aiohttp.ClientTimeout(total=30),
            )
            raise AssertionError("图片数量不匹配应报错")
        except ValueError as e:
            assert "1 个图片入口" in str(e), e
        # 正常注入：图片→上传 LoadImage、视频→上传 VHS
        result = await agent.handle_service(
            "rid-2",
            "comfyui",
            {
                "action": "run_workflow",
                "workflow": "upload_demo",
                "timeout": 30,
                "params": {},
                "inject": {
                    "images": [base64.b64encode(PNG_1PX).decode("ascii")],
                    "videos": [base64.b64encode(b"fake-video").decode("ascii")],
                },
            },
        )
    finally:
        await agent.session.close()
        await runner.cleanup()

    prompt = state["prompt_body"]["prompt"]
    assert prompt["10"]["inputs"]["image"].startswith("yunxin_") and prompt["10"]["inputs"]["image"].endswith(".png"), prompt["10"]
    assert prompt["20"]["inputs"]["video"].startswith("yunxin_") and prompt["20"]["inputs"]["video"].endswith(".mp4"), prompt["20"]
    uploads = state.get("uploads") or []
    assert len(uploads) == 2 and {u["kind"] for u in uploads} == {"image", "video"}, uploads

    files = result["files"]
    assert len(files) == 2, files
    # 主产物（Final_ 前缀）排最前，附节点元数据
    assert files[0]["main"] is True and files[0]["filename"].startswith("Final"), files[0]
    assert files[1]["main"] is False and files[1]["filename"].startswith("Step"), files[1]
    assert files[0]["node_type"] == "SaveImage" and files[0]["node"] == "9", files[0]
    # 本地代理工作流响应只返回产物元数据，实际文件由 media_get 分块拉取
    assert "base64" not in files[0], files[0]
    return files


async def main():
    test_inject_unit()
    print("[1/5] 注入单元测试 OK")
    test_adoptions_unit()
    print("[2/5] 采纳经验单元测试（文本合并/种子/400摘要）OK")

    out = await run_scenario(ws_enabled=True)
    assert out["ws_connected"] is True, "WS 未被连接"
    body = out["state"]["prompt_body"]
    prompt = body["prompt"]
    # 入口注入
    assert prompt["1"]["inputs"]["image"] == "aW1nYmFzZTY0", prompt["1"]
    assert prompt["2"]["inputs"]["text"] == "第一段文案", prompt["2"]
    # 占位符替换
    assert prompt["3"]["inputs"]["text"] == "a cat"
    assert prompt["5"]["inputs"]["seed"] == 1 and prompt["5"]["inputs"]["steps"] == 8
    assert prompt["6"]["inputs"]["width"] == 512
    # 产物
    files = out["result"]["files"]
    assert len(files) == 1 and files[0]["kind"] == "image"
    assert files[0]["filename"] == "out.png", files[0]
    print("[3/6] WS 推送模式 + 入口注入 + 产物元数据 OK")

    out2 = await run_scenario(ws_enabled=False)
    files2 = out2["result"]["files"]
    assert len(files2) == 1 and files2[0]["filename"] == "out.png", files2
    print("[4/6] 无 WS 支持 → 轮询兜底 OK")

    await test_workflow_file_manage()
    print("[5/6] 工作流上传/删除（备份+防穿越）OK")

    await test_upload_inject_and_metadata()
    print("[6/6] 上传注入（图片/视频入口）+ 产物元数据与主产物排序 OK")

    print("\nWS DRIVER TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
