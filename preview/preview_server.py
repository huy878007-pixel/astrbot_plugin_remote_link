#!/usr/bin/env python3
"""云信互联 控制台 · 本地预览服务器

在不安装 AstrBot 的情况下预览 pages/ 下的云端控制台页面：
    python preview/preview_server.py
然后浏览器打开 http://127.0.0.1:8890

页面里的 api.js 检测不到 window.AstrBotPluginPage 时，会自动回退到
本服务器提供的 /api/* mock 接口（数据来自 preview/mock_*）。
上传到云端安装进 AstrBot 后，同一套页面会走真实的插件后端接口。
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
PAGES_DIR = PROJECT / "pages"
MOCK_WF_DIR = HERE / "mock_workflows"

PORT = 8890

HUB_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>云信互联 · 预览导航</title>
<style>
body { background:#0d1017; color:#e8ebf2; font-family:"Microsoft YaHei UI",sans-serif; display:flex;
       align-items:center; justify-content:center; height:100vh; margin:0; }
.box { text-align:center; }
a.btn { display:inline-block; margin:10px; padding:15px 36px; background:rgba(255,255,255,0.04);
        border:1px solid rgba(255,255,255,0.1); border-radius:12px; color:#e8ebf2;
        text-decoration:none; font-size:16px; transition:.15s; }
a.btn:hover { border-color:#4f7cff; background:rgba(79,124,255,0.12); }
h1 { background:linear-gradient(120deg,#7ea2ff,#a78bfa); -webkit-background-clip:text; background-clip:text; color:transparent; }
.dim { color:#8b93a7; margin-top:24px; font-size:13px; }
</style></head><body>
<div class="box">
  <h1>☁️ 云信互联 控制台 · 本地预览</h1>
  <a class="btn" href="/pages/console/index.html">🚀 打开控制台（概览 / 工作流 / 预设 / 设置）</a>
  <div class="dim">预览模式使用 mock 数据；安装进 AstrBot 后走真实隧道数据</div>
</div>
</body></html>
"""


def load_mock(name: str):
    with open(HERE / name, "r", encoding="utf-8") as f:
        return json.load(f)


async def static_page(request):
    rel = request.match_info.get("tail", "")
    if not rel:
        raise web.HTTPFound("/")
    path = (PAGES_DIR / rel).resolve()
    if not str(path).startswith(str(PAGES_DIR.resolve())):
        raise web.HTTPForbidden()
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def api_overview_snapshot(request):
    info = {
        "hostname": "PC-HOME-4090",
        "platform": "Windows-11 · Python 3.14",
        "comfyui": {
            "ok": True,
            "error": "",
            "devices": [
                {"name": "cuda:0 NVIDIA GeForce RTX 4090", "vram_total_gb": 24.0, "vram_free_gb": 13.2}
            ],
        },
        "openai": {"ok": True, "error": "", "models": ["qwen2.5:14b", "llama3.1:8b", "gemma2:9b"]},
    }
    return web.json_response(
        {
            "version": "2.0.0-preview",
            "agents": [
                {
                    "id": "h1",
                    "name": "家里的电脑",
                    "connected": True,
                    "connected_seconds": int(time.time() % 100000) % 86400,
                    "hostname": info["hostname"],
                    "platform": info["platform"],
                    "comfyui": info["comfyui"],
                    "openai": info["openai"],
                }
            ],
            "pending_requests": random.randint(0, 2),
            "server_port": 8468,
            "capabilities": {
                "workflows": 3,
                "checkpoints": 5,
                "llm_models": 3,
                "queue": {"running": 0, "pending": 1},
            },
        }
    )


async def api_overview_events(request):
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    messages = [
        "心跳正常，隧道往返 38ms",
        "ComfyUI 队列空闲",
        "云端请求: /remote wf list（42ms）",
        "本地 LLM 心跳正常",
        "ComfyUI 正在执行任务 p-7f3a…（12%）",
    ]
    try:
        while True:
            data = {
                "t": time.time(),
                "connected": True,
                "pending_requests": random.randint(0, 2),
                "event": random.choice(messages),
            }
            await resp.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
            await asyncio.sleep(2.5)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    return resp


async def api_workflows_list(request):
    items = []
    for f in sorted(MOCK_WF_DIR.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(obj.get("nodes"), list) and "links" in obj:
                fmt = "ui"
            elif obj and all(isinstance(v, dict) and "class_type" in v for v in obj.values()):
                fmt = "api"
            else:
                fmt = "unknown"
        except Exception:  # noqa: BLE001
            fmt = "unknown"
        # 轻量能力扫描（占位符 + 产物类型 + 入口节点）
        raw = f.read_text(encoding="utf-8")
        tokens = sorted(set(re.findall(r'"__([A-Z0-9_]+)__"', raw)))
        outputs = []
        if re.search(r"SaveVideo|VideoCombine|VHS_", raw):
            outputs.append("video")
        if re.search(r"SaveImage|PreviewImage", raw):
            outputs.append("image")
        injects = {
            "images": len(re.findall(r"ETN_LoadImageBase64", raw)),
            "texts": len(re.findall(r"Simple String", raw)),
            "videos": len(re.findall(r"VHS_LoadVideo", raw)),
        }
        items.append(
            {
                "name": f.stem,
                "relpath": f.name,
                "format": fmt,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "tokens": tokens,
                "outputs": outputs,
                "injects": injects,
            }
        )
    return web.json_response({"workflows": items, "source": "mock"})


async def api_workflows_read(request):
    name = request.query.get("name", "").strip()
    if not name:
        return web.json_response({"message": "缺少 name 参数"}, status=400)
    p = (MOCK_WF_DIR / name).resolve()
    if not p.is_file():
        # 与真实代理一致：允许省略 .json 后缀
        p2 = (MOCK_WF_DIR / (name + ".json")).resolve()
        if p2.is_file():
            p = p2
    if not str(p).startswith(str(MOCK_WF_DIR.resolve())) or not p.is_file():
        return web.json_response({"message": f"找不到工作流: {name}"}, status=404)
    content = p.read_text(encoding="utf-8")
    obj = json.loads(content)
    if isinstance(obj.get("nodes"), list) and "links" in obj:
        fmt = "ui"
    else:
        fmt = "api"
    return web.json_response({"name": name, "format": fmt, "content": content})


async def api_workflows_upload(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "请求体不是合法 JSON"}, status=400)
    name = str(payload.get("name") or "").strip()
    content = payload.get("content")
    if not name or not isinstance(content, str) or "/" in name or "\\" in name:
        return web.json_response({"message": "name 非法或缺少 content"}, status=400)
    try:
        obj = json.loads(content)
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "内容不是合法 JSON"}, status=400)
    p = MOCK_WF_DIR / (name + ("" if name.endswith(".json") else ".json"))
    p.write_text(content, encoding="utf-8")
    return web.json_response({"ok": True, "name": p.stem, "format": "ui" if "links" in obj else "api"})


async def api_workflows_delete(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "请求体不是合法 JSON"}, status=400)
    name = str(payload.get("name") or "").strip()
    p = (MOCK_WF_DIR / (name + ("" if name.endswith(".json") else ".json"))).resolve()
    if not str(p).startswith(str(MOCK_WF_DIR.resolve())) or not p.is_file():
        return web.json_response({"message": f"找不到工作流: {name}"}, status=404)
    p.unlink()
    return web.json_response({"ok": True, "name": name})


async def api_object_info(request):
    return web.json_response(load_mock("mock_object_info.json"))


# ---- 设置页 mock（与真实后端同构） ----

MOCK_SETTINGS = {
    "router_enabled": True,
    "router_provider": "",
    "router_model": "",
    "router_temperature": 0.1,
    "router_timeout": 60,
    "server_host": "0.0.0.0",
    "server_port": 8468,
    "request_timeout": 300,
    "comfyui_timeout": 3600,
    "llm_model": "",
    "enable_shell": False,
    "auth_token_masked": "a1b2…c3d4",
    "prompt_confirm_mode": "auto",
}

# ---- 任务队列 mock ----
MOCK_QUEUE = [
    {
        "task_id": "task-3", "status": "success", "workflow": "【Work-Fisher】Minimax-H3 文生视频",
        "subtype": "text2video", "prompt": "一个青年男子骑摩托车，风驰电掣在公路上，动态感强，电影感镜头，写实风格，4K",
        "intent": "生成一个青年男子骑摩托车的视频", "origin": "qq:test",
        "created_at": 1786900000, "finished_at": 1786900200,
        "files": [{"kind": "video", "filename": "t2v_00001.mp4"}, {"kind": "image", "filename": "t2v_00001.png"}],
        "error": "",
    },
    {
        "task_id": "task-2", "status": "waiting_confirm", "workflow": "【Work-Fisher】Minimax-H3 文生视频",
        "subtype": "text2video", "prompt": "生成一位女子喝下午茶的视频，优雅氛围，阳光洒在窗边，女子端起茶杯轻抿一口，电影感镜头，唯美暖色调",
        "intent": "生成一位女子喝下午茶的视频", "origin": "qq:test",
        "created_at": 1786900300, "files": [], "error": "",
    },
    {
        "task_id": "task-1", "status": "failed", "workflow": "Anima_官方模板精修版_v5",
        "subtype": "text2image", "prompt": "一只戴墨镜的猫在海边看日落", "intent": "画一只戴墨镜的猫",
        "origin": "qq:test", "created_at": 1786900400, "finished_at": 1786900500,
        "files": [], "error": "ComfyUI 生成出错：HostBuffer.read_file_slice failed",
    },
]


async def api_overview_queue(request):
    return web.json_response({"queue": MOCK_QUEUE})


async def api_queue_action(request):
    payload = await request.json(default={})
    return web.json_response({"ok": True, "task_id": payload.get("task_id"), "action": payload.get("action"), "status": "running"})


async def api_media_file(request):
    filename = request.query.get("filename", "")
    return web.Response(text=f"[mock media: {filename}]", content_type="text/plain")


async def api_settings_get(request):
    return web.json_response(
        {
            "config": dict(MOCK_SETTINGS),
            "providers": [
                {"id": "", "name": "默认 · 跟随 AstrBot 当前提供商", "type": "llm"},
                {"id": "provider-deepseek", "name": "DeepSeek", "type": "llm"},
                {"id": "provider-siliconflow", "name": "硅基流动", "type": "llm"},
            ],
            "tools": [
                {"name": "remote_local_compute", "active": True, "description": "万能本地能力调用入口（智能调度）"},
                {"name": "remote_llm_chat", "active": True, "description": "调用本地 LLM 对话"},
                {"name": "remote_workflow_txt2img", "active": True, "description": "文生图工作流预设"},
                {"name": "remote_shell", "active": False, "description": "本地执行命令（默认关）"},
            ],
            "media": {"count": 3, "size_mb": 12.5},
            "server_running": True,
        }
    )


async def api_settings_save(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "请求体不是合法 JSON"}, status=400)
    changed = []
    for k in MOCK_SETTINGS:
        if k in payload:
            MOCK_SETTINGS[k] = payload[k]
            changed.append(k)
    note = "隧道服务端已按新地址/端口重启（本地代理会自动重连）" if "server_port" in changed else ""
    return web.json_response({"ok": True, "changed": changed, "note": note})


async def api_settings_gen_token(request):
    return web.json_response({"ok": True, "token": "mock-" + random.hex(16), "hint": "请把新 token 同步到本地 YunxinAgent 的配置里"})


async def api_settings_clear_media(request):
    return web.json_response({"ok": True, "removed": 3, "freed_mb": 12.5})


async def api_settings_toggle_tool(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "请求体不是合法 JSON"}, status=400)
    return web.json_response({"ok": True, "name": payload.get("name"), "active": payload.get("active")})


# ---- 子分类配置 mock（与真实后端同构） ----

MOCK_SUBTYPES = {
    "text2image": {"workflow": "Anima_官方模板精修版_v5", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "image2image": {"workflow": "", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "prompt_analysis": {"workflow": "", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "text2video": {"workflow": "【Work-Fisher】Minimax-H3 整合流程", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "image2video": {"workflow": "", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "multi_image2video": {"workflow": "", "llm_provider": "", "llm_model": "", "prompt_template": ""},
    "audio_gen": {"workflow": "", "llm_provider": "", "llm_model": "", "prompt_template": ""},
}
MOCK_SUBTYPE_META = {
    "labels": {
        "text2image": "文生图", "image2image": "图生图", "prompt_analysis": "画面分析提示词",
        "text2video": "文生视频", "image2video": "单图生视频", "multi_image2video": "多图生视频",
        "audio_gen": "音频生成",
    },
    "categories": {
        "text2image": "image", "image2image": "image", "prompt_analysis": "image",
        "text2video": "video", "image2video": "video", "multi_image2video": "video",
        "audio_gen": "audio",
    },
}


async def api_subtype_configs_get(request):
    return web.json_response({"subtypes": dict(MOCK_SUBTYPES), **MOCK_SUBTYPE_META})


async def api_subtype_configs_save(request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"message": "请求体不是合法 JSON"}, status=400)
    for st in MOCK_SUBTYPES:
        if st in payload and isinstance(payload[st], dict):
            MOCK_SUBTYPES[st] = {
                k: str(payload[st].get(k) or "") for k in ("workflow", "llm_provider", "llm_model", "prompt_template")
            }
    return web.json_response({"ok": True, "subtypes": dict(MOCK_SUBTYPES)})


async def api_provider_models(request):
    provider_id = request.query.get("provider_id", "")
    models_by_provider = {
        "provider-deepseek": ["deepseek-chat", "deepseek-reasoner"],
        "provider-siliconflow": ["Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-32B-Instruct", "THUDM/glm-4-9b-chat"],
    }
    return web.json_response({"models": models_by_provider.get(provider_id, ["deepseek-chat", "qwen2.5:72b", "qwen2.5:32b"])})


async def api_workflows_analysis(request):
    """工作流列表 + 识别结果 + 默认配置（mock 合并）。"""
    result = await api_workflows_list(request)
    items = json.loads(result.body).get("workflows", [])
    analysis = {
        "Anima_官方模板精修版_v5": {"category": "image", "subtype": "文生图", "summary": "官方模板 + 脸手精修", "tags": ["文生图", "精修"]},
        "【Work-Fisher】Minimax-H3 整合流程": {"category": "video", "subtype": "文生视频", "summary": "Minimax H3 文生视频", "tags": ["文生视频", "Minimax"]},
    }
    for w in items:
        w["analysis"] = analysis.get(w["name"])
    return web.json_response({"workflows": items, "defaults": {"image": "Anima_官方模板精修版_v5", "video": "【Work-Fisher】Minimax-H3 整合流程", "audio": ""}})


def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text=HUB_HTML, content_type="text/html", charset="utf-8"))
    app.router.add_get("/debug", lambda r: web.FileResponse(HERE / "debug_graph.html"))
    app.router.add_get("/pages/{tail:.*}", static_page)
    app.router.add_get("/api/overview/snapshot", api_overview_snapshot)
    app.router.add_get("/api/overview/queue", api_overview_queue)
    app.router.add_post("/api/overview/queue/action", api_queue_action)
    app.router.add_get("/api/media", api_media_file)
    app.router.add_get("/api/overview/events", api_overview_events)
    app.router.add_get("/api/workflows/list", api_workflows_list)
    app.router.add_get("/api/workflows/read", api_workflows_read)
    app.router.add_post("/api/workflows/upload", api_workflows_upload)
    app.router.add_post("/api/workflows/delete", api_workflows_delete)
    app.router.add_get("/api/object_info", api_object_info)
    app.router.add_get("/api/settings/get", api_settings_get)
    app.router.add_post("/api/settings/save", api_settings_save)
    app.router.add_post("/api/settings/gen_token", api_settings_gen_token)
    app.router.add_post("/api/settings/clear_media", api_settings_clear_media)
    app.router.add_post("/api/settings/toggle_tool", api_settings_toggle_tool)
    app.router.add_get("/api/settings/subtype_configs", api_subtype_configs_get)
    app.router.add_post("/api/settings/subtype_configs", api_subtype_configs_save)
    app.router.add_get("/api/settings/provider_models", api_provider_models)
    app.router.add_get("/api/workflows/analysis", api_workflows_analysis)
    print(f"云信互联 控制台预览: http://127.0.0.1:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
