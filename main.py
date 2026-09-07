"""云信互联（astrbot_plugin_remote_link）—— 让云端 AstrBot 通过反向隧道调用你本地电脑上的 ComfyUI / LLM。

架构
----
  云端 AstrBot（本插件 = WebSocket 服务端 + OpenAI 兼容 HTTP 代理）
        ▲  ▲
        │  │ 一条 WebSocket 长连接（token 认证），由本地主动拨入
        │  │
  本地电脑 local_agent.py（WS 客户端，断线自动重连，自带本地看板）
        │
        ├── ComfyUI   (127.0.0.1:8188)  执行任意工作流（文生图/图生图/视频…）
        │                               自适应读取本地保存的全部工作流，UI 格式自动转换
        ├── Ollama / LM Studio / vLLM   OpenAI 兼容接口（含 SSE 流式）
        └── Shell    （默认关闭，双端开关都打开才生效）

本地电脑通常没有公网 IP，所以连接方向必须由本地发起：插件在云端监听，
本地代理拨入并保持长连接，所有请求都在这条隧道上双向转发。

本插件提供：
  1. /remote 指令组：status / ping / wf list / comfyui list|queue|gen / llm / shell / do
  2. LLM 工具：remote_local_compute（智能调度入口）、remote_llm_chat、remote_shell
  3. OpenAI 兼容 HTTP 代理：/v1/chat/completions、/v1/models（配合隧道把
     本地 LLM 直接配置成 AstrBot 的服务提供商，支持 SSE 流式）
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import time
import uuid
from collections import deque
from pathlib import Path

import astrbot.api.message_components as Comp
import aiohttp
from aiohttp import web

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

from .core.exceptions import RemoteLinkError
from .core.protocol import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION
from .core.task_manager import TaskManager
from .core.tunnel import TunnelServer
from .services.media import MediaService
from .tools.local_llm import build_llm_tool
from .tools.shell import build_shell_tool
from .tools.smart import build_compute_tool

PLUGIN_NAME = "astrbot_plugin_remote_link"

# v0.2.0 起将插件、代理、协议版本显式分离，握手时用于兼容性判断。
PLUGIN_VERSION = "0.2.0"

# 产物扩展名 → MIME（Web 预览用）
EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
}

# 模块级 logger：部分 AstrBot 版本（如 v4.24.x）的插件基类不提供 self.logger，
# 统一使用模块级 logger 以保证跨版本兼容。
logger = logging.getLogger("astrbot_plugin_remote_link")

# 插件页面后端 API（AstrBot >= v4.24 才有；老版本降级为无页面）
try:
    from astrbot.api.web import error_response, json_response, request, stream_response
except ImportError:  # noqa: BLE001
    error_response = json_response = request = stream_response = None

try:
    from astrbot.core.star.star_tools import StarTools

    def _plugin_data_dir() -> Path:
        return StarTools.get_data_dir("astrbot_plugin_remote_link")

except Exception:  # noqa: BLE001  # 老版本/异常时退回插件目录

    def _plugin_data_dir() -> Path:
        return Path(__file__).parent / "data"


# 内置 txt2img 预设已移除：文生图等任务统一走智能调度（remote_local_compute → 子分类 → 对应工作流），
# 不再提供依赖本地 checkpoint 的 demo 模板。

_CONTAINER_IP: str | None = None


def _container_ip() -> str:
    """本机/容器内网 IP（供 NapCat 从 Docker 内网拉取媒体 URL；缓存）。

    Docker 容器里经 UDP 连接探测得到容器 IP（如 172.21.x.x），
    NapCat 与 AstrBot 同 Docker 网络时可直接访问。
    """
    global _CONTAINER_IP
    if _CONTAINER_IP is None:
        try:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # 不实际发包，仅取本机出网地址
            _CONTAINER_IP = s.getsockname()[0]
            s.close()
        except Exception:  # noqa: BLE001
            _CONTAINER_IP = "127.0.0.1"
    return _CONTAINER_IP

# 提示词增强不可用时的兜底负提示词（质量类负面词）
DEFAULT_NEGATIVE = (
    "(worst quality, low quality:1.4), (normal quality:1.2), (monochrome:0.8), "
    "bad anatomy, extra fingers, missing fingers, fused fingers, mutated hands, "
    "bad hands, distorted face, ugly eyes, asymmetrical eyes, cross-eyed, "
    "cropped composition, out of frame, watermark, signature, text, username, "
    "simple background, plain color, overexposed, underexposed, dirty, "
    "lowres, jpeg artifacts."
)

# 中文检测（提示词增强输出校验：扩散模型更吃英文 tag）
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# ---------------- 子分类体系（v0.1.0 配置表驱动） ----------------
SUBTYPES = [
    "text2image", "image2image", "prompt_analysis",
    "text2video", "image2video", "multi_image2video",
    "audio_gen",
]
SUBTYPE_LABELS = {
    "text2image": "文生图", "image2image": "图生图", "prompt_analysis": "画面分析提示词",
    "text2video": "文生视频", "image2video": "单图生视频", "multi_image2video": "多图生视频",
    "audio_gen": "音频生成",
}
SUBTYPE_CATEGORY = {
    "text2image": "image", "image2image": "image", "prompt_analysis": "image",
    "text2video": "video", "image2video": "video", "multi_image2video": "video",
    "audio_gen": "audio",
}
SUBTYPE_HINTS = {
    "text2image": ("文生图", "文转图", "text2image"),
    "image2image": ("图生图", "改图", "重绘", "image2image"),
    "prompt_analysis": ("画面分析", "提示词分析", "反推提示词", "prompt analysis"),
    "text2video": ("文生视频", "文转视频", "text2video"),
    "image2video": ("单图生视频", "图生视频", "image2video"),
    "multi_image2video": ("多图生视频", "multi_image2video", "多图视频"),
    "audio_gen": ("音频生成", "配乐", "音效", "audio_gen"),
}

# 智能调度的默认决策提示词（一级分类：只判断任务类型，不选工作流、不写提示词；
# 工作流由默认配置决定，提示词由第二级"提示词增强"LLM 生成。可在设置页自定义覆盖）
DEFAULT_ROUTER_PROMPT = """你是云信互联的任务分类器。根据用户意图判断任务类型（不要选具体工作流，也不要写提示词）。
只输出一个 JSON 对象，不要输出任何其他文字。格式：
{"kind":"image|video|audio|text","explanation":"一句话理由"}
规则：
1. 画图/生成图片/照片/插画/头像/壁纸/表情包等视觉图像任务 → image；
2. 生成视频/动画/短片/动态图等任务 → video；
3. 生成音乐/音频/歌曲/配音/音效等任务 → audio；
4. 问答/解释/翻译/总结/写作/计算等纯文本任务 → text；
5. 拿不准时选最接近的一类，并在 explanation 说明理由。"""

# 提示词增强的默认模板（{policy} 会被替换成 NSFW 策略说明；可在设置页自定义覆盖）
DEFAULT_ENHANCE_PROMPT = """你是 Stable Diffusion 生图提示词翻译器。把用户的自然语言描述改写成英文 tag 提示词。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"positive":"英文正面提示词","negative":"英文负面提示词"}
硬性规则：
1. {policy}
2. positive 必须是【纯英文 tag】、逗号分隔，绝对禁止中文/日文或任何非英文字符；先写风格质量词（masterpiece, best quality, highly detailed, anime illustration），再把用户描述的全部要素翻译成英文 tag（主体/发色/瞳色/服装/表情/动作/场景/背景/光影/构图/镜头）；
3. negative 必须是【纯英文 tag】、逗号分隔，包含质量类负面词（worst quality, low quality, bad anatomy, bad hands, extra fingers, watermark, text, lowres, jpeg artifacts），再根据画面内容补充针对性负面词；
4. 违反英文要求的输出视为失败。"""

# 工作流识别的默认分析提示词（初始化按钮用：LLM 给每个工作流打标签分类）
DEFAULT_ANALYZE_PROMPT = """你是 ComfyUI 工作流分析专家。根据每个工作流的节点组成、产物类型、参数和入口，判断它用来干什么。
只输出一个 JSON 数组，不要输出任何其他文字，格式：
[{"name":"工作流名","category":"image|video|audio|other","subtype":"文生图|图生图|文生视频|图生视频|音频|文本|其他","summary":"中文一句话说明","tags":["中文标签1","中文标签2"]}]
判断规则：
1. 产物含 image → category=image；有图片入口（入口含图片）且需要输入图 → subtype=图生图，否则 subtype=文生图；
2. 产物含 video → category=video；有图片入口 → subtype=图生视频，否则 subtype=文生视频；
3. 产物含 audio → category=audio，subtype=音频；
4. 都没有产物 → category=other，subtype 按节点判断（文本/其他）；
5. summary 用中文一句话说清楚它生成什么；tags 给 3~6 个概括用途的中文标签（如 动漫、写实、放大、修复、风格转绘）。"""


def pick_subtype(kind: str, n_images: int, intent: str) -> str:
    """子分类规则（纯函数）：手动关键词 > 图数规则（带图→图生类，多图→多图生视频）。

    手动关键词取"最长（最具体）"命中，避免「多图生视频」被「图生视频」抢先。
    """
    best = ("", 0)
    for st in SUBTYPES:
        for h in SUBTYPE_HINTS.get(st, ()):
            if h and h in intent and len(h) > best[1]:
                best = (st, len(h))
    if best[0]:
        return best[0]
    if kind == "image":
        return "image2image" if n_images > 0 else "text2image"
    if kind == "video":
        if n_images > 1:
            return "multi_image2video"
        if n_images == 1:
            return "image2video"
        return "text2video"
    if kind == "audio":
        return "audio_gen"
    return ""


class RemoteLinkPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # v0.2.0 安全基线：禁止空 token 裸奔。TunnelServer 负责自动生成与校验。
        self.tunnel = TunnelServer(config)

        # ---- 内嵌 HTTP 服务（隧道入口 + OpenAI 兼容代理）----
        self._runner = None
        self._server_task = None

        # ---- 数据目录 / Media Service ----
        self._media_dir = _plugin_data_dir() / "media"
        self.media = MediaService(
            self._media_dir,
            self.config,
            token_provider=lambda: self.tunnel.auth_token,
            fetch_stream=self._call_local_stream,
            container_ip_provider=_container_ip,
        )

        # ---- 任务队列（异步执行：工具立即返回，后台完成后再主动推送）----
        self.tasks = TaskManager()

        # ---- 提示词增强会话缓存（按 origin）----
        # 用户要求「保留对话记录，一直做分析」：OpenAI 兼容 API 无状态，
        # system prompt 每次仍会发送（但 deepseek 自动前缀缓存命中相同 system 会加速）；
        # 历史上下文（最近 6 轮）拼入 prompt，让子 LLM 有延续性。
        self._enhance_sessions: dict = {}  # subtype -> {"system": str, "history": [{user, assistant, negative}]}
        self._enhance_streams: dict = {}  # subtype -> {"text": 累积文本, "done": bool, "ts": float}（真流式实时推送）
        self._enhance_sessions_path = _plugin_data_dir() / "enhance_sessions.json"
        self._load_enhance_sessions()  # 持久化：重载/重装/重启后对话仍保留（除非主动初始化/重置）

        # ---- 能力清单缓存（LLM 智能调度的依据，来自本地代理的动态发现）----
        self._capabilities: dict | None = None
        self._capabilities_ts = 0.0

        # 注册 LLM 工具：本地 LLM + （可选）Shell + 万能智能调度入口。
        # （工作流预设工具已移除：任务统一走 remote_local_compute 智能调度）
        self._register_llm_tool(build_llm_tool(self))
        if self.config.get("enable_shell"):
            self._register_llm_tool(build_shell_tool(self))
        # 万能智能调度入口：工具名稳定，能力清单动态发现
        self._register_llm_tool(build_compute_tool(self))

        # 云端控制台（插件页面，需 AstrBot >= v4.24）
        self._register_pages()

        self._ensure_server()

    # ==================== 安全基础 ====================

    def _ensure_auth_token(self) -> str:
        """保证插件一定存在非空 auth_token（v0.2.0 P0），委托 TunnelServer。"""
        return self.tunnel.auth_token

    def _check_rate_limit(self, request, limit: int = 60, window: int = 60) -> bool:
        """极简滑动窗口限流，委托 TunnelServer。"""
        return self.tunnel.check_rate_limit(request, limit=limit, window=window)

    def _media_signature(self, filename: str, expires: int) -> str:
        return self.media.signature(filename, expires)

    def _verify_media_signature(self, request) -> bool:
        return self.media.verify_signature(request)

    def _signed_media_url(self, filename: str, ttl: int = 1800) -> str:
        """生成短期有效的 /media 签名 URL（默认 30 分钟）。"""
        return self.media.signed_url(filename, ttl=ttl)

    # ==================== 工具注册 ====================

    def _register_llm_tool(self, tool):
        """把 FunctionTool 注册进 AstrBot 的 LLM 工具管理器（兼容新老版本 API）。"""
        # >= v4.5.1 的推荐方式
        try:
            add_tools = getattr(self.context, "add_llm_tools", None)
            if add_tools is not None:
                add_tools(tool)
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] add_llm_tools 注册失败: {e}")
        # < v4.5.1 的兜底方式（ToolSet.add_tool 会按名字去重，重载插件不会重复注册）
        try:
            mgr = self.context.provider_manager.llm_tools
            mgr.add_tool(tool)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[remote_link] LLM 工具注册失败: {e}")

    # ==================== 云端控制台（AstrBot 插件页面后端） ====================

    def _register_pages(self):
        """注册插件页面后端 API（pages/ 目录下的页面经 window.AstrBotPluginPage 调用）。"""
        if stream_response is None:
            logger.warning(
                "[remote_link] 当前 AstrBot 版本不支持插件页面（需 >= v4.24），云端控制台不可用"
            )
            return
        reg = getattr(self.context, "register_web_api", None)
        if reg is None:
            logger.warning("[remote_link] context.register_web_api 不存在，云端控制台不可用")
            return
        p = PLUGIN_NAME
        reg(f"/{p}/overview/snapshot", self._api_overview_snapshot, ["GET"], "云信互联·概览快照")
        reg(f"/{p}/overview/queue", self._api_overview_queue, ["GET"], "云信互联·任务队列")
        reg(f"/{p}/overview/queue/action", self._api_queue_action, ["POST"], "云信互联·任务审批")
        reg(f"/{p}/task/submit", self._api_submit_task, ["POST"], "云信互联·提交生成任务")
        reg(f"/{p}/task/send", self._api_task_send, ["POST"], "云信互联·发送产物到会话")
        reg(f"/{p}/media", self._api_media_file, ["GET"], "云信互联·产物文件")
        reg(f"/{p}/overview/events", self._api_overview_events, ["GET"], "云信互联·实时事件流")
        reg(f"/{p}/workflows/list", self._api_workflows_list, ["GET"], "云信互联·工作流列表")
        reg(f"/{p}/workflows/read", self._api_workflows_read, ["GET"], "云信互联·读取工作流")
        reg(f"/{p}/workflows/upload", self._api_workflows_upload, ["POST"], "云信互联·上传工作流")
        reg(f"/{p}/workflows/delete", self._api_workflows_delete, ["POST"], "云信互联·删除工作流")
        reg(f"/{p}/workflows/analysis", self._api_workflows_analysis, ["GET"], "云信互联·工作流识别结果")
        reg(f"/{p}/workflows/analyze", self._api_workflows_analyze, ["POST"], "云信互联·LLM 识别工作流")
        reg(f"/{p}/workflows/tag", self._api_workflows_tag, ["POST"], "云信互联·手动标记工作流")
        reg(f"/{p}/settings/default_workflows", self._api_default_workflows_save, ["POST"], "云信互联·保存默认工作流")
        reg(f"/{p}/settings/subtype_configs", self._api_subtype_configs_get, ["GET"], "云信互联·读取子分类配置")
        reg(f"/{p}/settings/subtype_configs", self._api_subtype_configs_save, ["POST"], "云信互联·保存子分类配置")
        reg(f"/{p}/settings/provider_models", self._api_provider_models, ["GET"], "云信互联·提供商模型列表")
        reg(f"/{p}/object_info", self._api_object_info, ["GET"], "云信互联·ComfyUI 节点定义")
        reg(f"/{p}/settings/get", self._api_settings_get, ["GET"], "云信互联·读取设置")
        reg(f"/{p}/settings/save", self._api_settings_save, ["POST"], "云信互联·保存设置")
        reg(f"/{p}/settings/gen_token", self._api_settings_gen_token, ["POST"], "云信互联·重新生成 token")
        reg(f"/{p}/settings/clear_media", self._api_settings_clear_media, ["POST"], "云信互联·清理媒体缓存")
        reg(f"/{p}/settings/toggle_tool", self._api_settings_toggle_tool, ["POST"], "云信互联·工具开关")
        reg(f"/{p}/enhance/sessions", self._api_enhance_sessions, ["GET"], "云信互联·子LLM对话记录")
        reg(f"/{p}/enhance/reset", self._api_enhance_reset, ["POST"], "云信互联·初始化子LLM对话")
        logger.info("[remote_link] 云端控制台页面已注册（概览/工作流/设置）")

    async def _api_overview_snapshot(self):
        connected = self.tunnel.connected
        info: dict = self.tunnel.agent_info or {}
        # 在线时取一次实时信息（带超时保护，失败则退回连接时缓存）
        if connected:
            try:
                info = await self._call_local("info", {}, timeout=8) or {}
            except RemoteLinkError:
                info = self.tunnel.agent_info or {}
        comfy = info.get("comfyui") or {}
        oai = info.get("openai") or {}
        return json_response(
            {
                "version": PLUGIN_VERSION,
                "agents": [
                    {
                        "id": "default",
                        "name": info.get("hostname") or "本地代理",
                        "connected": connected,
                        "connected_seconds": (
                            int(time.time() - self.tunnel.agent_connected_at)
                            if connected and self.tunnel.agent_connected_at
                            else 0
                        ),
                        "hostname": info.get("hostname"),
                        "platform": info.get("platform"),
                        "comfyui": {
                            "ok": bool(comfy.get("ok")),
                            "error": comfy.get("error", ""),
                            "devices": comfy.get("devices") or [],
                            "queue": comfy.get("queue") or {"running": 0, "pending": 0},
                        },
                        "openai": {
                            "ok": bool(oai.get("ok")),
                            "error": oai.get("error", ""),
                            "models": oai.get("models") or [],
                        },
                    }
                ],
                "pending_requests": self.tunnel.pending_count,
                "server_port": int(self.config.get("server_port", 8468)),
                "progress": self._fresh_progress(),
                "capabilities": await self._api_capabilities_summary(),
            }
        )

    async def _api_overview_queue(self):
        """任务队列详情（异步任务的状态/进度/产物）。"""
        return json_response({"queue": self._queue_snapshot()})

    async def _api_queue_action(self):
        """任务审批：POST {task_id, action: approve|reject}。"""
        payload = await request.json(default={})
        task_id = str(payload.get("task_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if not task_id or action not in ("approve", "reject"):
            return error_response("需要 task_id 与 action（approve/reject）", status_code=400)
        origin = self.tasks.confirm_by_task.get(task_id)
        if origin is None:
            return error_response(f"任务 {task_id} 不在待审批状态", status_code=404)
        pending = self.tasks.confirm_pending.get(origin)
        if pending is None:
            return error_response(f"任务 {task_id} 待审批数据丢失", status_code=404)
        self.tasks.clear_confirmation(origin, task_id)
        rec = pending["rec"]
        if action == "reject":
            self._queue_update(rec, status="failed", finished_at=time.time(), error="已在控制台拒绝")
            await self._send_to_origin(pending["event"], text=f"❌ 任务 {task_id} 已在控制台拒绝")
            return json_response({"ok": True, "task_id": task_id, "action": "reject"})
        # approve：后台执行（不阻塞 Web 请求）
        loop = asyncio.get_running_loop()
        loop.create_task(self._execute_pending(pending))
        return json_response({"ok": True, "task_id": task_id, "action": "approve", "status": "running"})

    async def _api_submit_task(self):
        """Web 控制台提交生成任务：POST {intent, workflow_hint?, images?}。

        images：图片 base64 列表（Web/本地 GUI 上传），落盘为本地文件后
        走与 QQ 群同一内生链路（识图 + 注入工作流）。产物不发到会话
        （Web 提交无会话），只在控制台队列里可见/可下载/可发送到 QQ。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        intent = str(payload.get("intent") or "").strip()
        if not intent:
            return error_response("缺少 intent（要生成的内容描述）", status_code=400)
        images = self._save_uploaded_images(payload.get("images"))
        from types import SimpleNamespace

        fake_event = SimpleNamespace(
            unified_msg_origin=f"web:{secrets.token_hex(4)}",
            message_str=intent,
            is_at_or_wake_command=True,
            get_sender_id=lambda: "",
            get_sender_name=lambda: "",
            message_obj=SimpleNamespace(message=[]),
        )
        rec = self.submit_local_task(
            intent, str(payload.get("workflow_hint") or ""), fake_event, pre_images=images
        )
        rec["web_submit"] = True  # 完成后只更新队列，不发会话
        return json_response({"ok": True, "task_id": rec["task_id"], "status": "queued", "images": len(images)})

    def _save_uploaded_images(self, images) -> list[dict]:
        """把上传的图片 base64 落盘为本地文件，返回 [{path, base64, name}]。"""
        out: list[dict] = []
        if not isinstance(images, list):
            return out
        in_dir = self._media_dir / "inputs"
        in_dir.mkdir(parents=True, exist_ok=True)
        for i, b64 in enumerate(images[:6]):
            if not isinstance(b64, str) or not b64.strip():
                continue
            try:
                data = base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                continue
            if not data or len(data) > 20 * 1024 * 1024:
                continue
            path = in_dir / f"upload_{int(time.time())}_{i}.png"
            path.write_bytes(data)
            out.append({"path": str(path), "base64": b64, "name": path.name})
        return out

    async def _api_task_send(self):
        """把已完成的产物发送到它来源的会话（QQ 群）：POST {task_id}。"""
        payload = await request.json(default={})
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return error_response("缺少 task_id", status_code=400)
        rec = self.tasks.find_by_task_id(task_id)
        if rec is None:
            return error_response(f"任务 {task_id} 不存在", status_code=404)
        files = rec.get("files") or []
        origin = str(rec.get("origin") or "")
        if not files or not origin:
            return error_response("该任务没有可发送的产物或来源会话", status_code=400)
        try:
            ok = await self._send_media_to_origin(origin, files)
            return json_response({"ok": True, "sent": ok})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 控制台发送产物失败: {e}")
            return error_response(f"发送失败：{e}", status_code=502)

    async def _api_media_file(self):
        """产物文件服务：GET ?filename=xxx，返回媒体文件供 Web 预览。

        文件尚未拉取时，先从本地代理实时拉取（懒加载，幂等）。
        """
        filename = request.query.get("filename", "").strip()
        if not filename:
            return error_response("缺少 filename 参数", status_code=400)
        # 只允许访问 media 目录下的文件，防目录穿越
        safe = Path(filename).name
        path = self._media_dir / safe
        if not path.is_file():
            # 懒拉取：在任务队列里找到对应产物元数据，从本地代理拉取落盘
            rec = self._find_media_record(safe)
            if rec is not None:
                try:
                    await self._pull_media(rec)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[remote_link] Web 预览拉取 {safe} 失败: {e}")
        if not path.is_file():
            return error_response("文件不存在", status_code=404)
        ext = safe.lower().rsplit(".", 1)[-1] if "." in safe else ""
        mime = EXT_MIME.get("." + ext, "application/octet-stream")
        headers = None
        if str(request.query.get("download", "")).strip() in ("1", "true"):
            # 下载模式：附加 Content-Disposition
            from urllib.parse import quote

            headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}"}
        return web.Response(body=path.read_bytes(), content_type=mime, headers=headers)

    def _find_media_record(self, filename: str):
        """在任务队列的产物记录里按文件名找元数据（供 Web 预览懒拉取）。"""
        return self.tasks.find_media_record(filename)

    async def _api_capabilities_summary(self) -> dict:
        """能力清单摘要（概览页展示，拉取失败时给出空摘要）。"""
        try:
            caps = await asyncio.wait_for(self._get_capabilities(), timeout=20)
        except Exception:  # noqa: BLE001
            return {"workflows": 0, "checkpoints": 0, "llm_models": 0, "queue": {"running": 0, "pending": 0}}
        return {
            "workflows": len(caps.get("workflows") or []),
            "checkpoints": len(caps.get("checkpoints") or []),
            "llm_models": len(caps.get("llm_models") or []),
            "queue": caps.get("queue") or {"running": 0, "pending": 0},
        }

    async def _api_overview_events(self):
        async def events():
            try:
                while True:
                    connected = self.tunnel.connected
                    snap = {
                        "t": time.time(),
                        "connected": connected,
                        "pending_requests": self.tunnel.pending_count,
                        "progress": self._fresh_progress(),
                        "enhance_streams": self._fresh_enhance_streams(),
                    }
                    yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(2)
            except (asyncio.CancelledError, GeneratorExit):
                return

        return stream_response(events())

    def _fresh_enhance_streams(self) -> dict:
        """子 LLM 增强流式状态（Web 打字机实时显示用）：30 秒内有更新的才保留。"""
        out = {}
        now = time.time()
        for sub, st in list(self._enhance_streams.items()):
            if now - float(st.get("ts") or 0) < 30:
                out[sub] = {"text": st.get("text", "")[:2000], "done": bool(st.get("done"))}
        return out

    def _fresh_progress(self) -> dict | None:
        """当前任务进度（超过 30 秒没更新视为结束，返回 None）。"""
        p = self.tunnel.progress
        if p and time.time() - float(p.get("updated_at") or 0) < 30:
            return {
                "text": p.get("text", ""),
                "percent": p.get("percent"),
                "node": p.get("node"),
                "node_type": p.get("node_type"),
                "updated_at": p.get("updated_at"),
            }
        return None

    async def _api_workflows_list(self):
        try:
            result = await self._call_local("workflows", {}, timeout=20)
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        return json_response(result)

    async def _api_workflows_read(self):
        name = request.query.get("name", "").strip()
        if not name:
            return error_response("缺少 name 参数", status_code=400)
        try:
            result = await self._call_local(
                "workflows", {"action": "read", "name": name}, timeout=30
            )
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        return json_response(result)

    async def _api_workflows_upload(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        content = payload.get("content")
        if not name or not isinstance(content, str):
            return error_response("需要 name 与 content（JSON 字符串）", status_code=400)
        try:
            result = await self._call_local(
                "workflows", {"action": "upload", "name": name, "content": content}, timeout=60
            )
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        return json_response(result)

    async def _api_workflows_delete(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少 name 参数", status_code=400)
        try:
            result = await self._call_local(
                "workflows", {"action": "delete", "name": name}, timeout=30
            )
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        return json_response(result)

    async def _api_workflows_analysis(self):
        """工作流列表 + LLM 识别结果 + 默认工作流配置（合并返回）。"""
        try:
            result = await self._call_local("workflows", {}, timeout=20)
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        analysis = self._workflow_analysis()
        for w in result.get("workflows") or []:
            w["analysis"] = analysis.get(w.get("name"))
        result["defaults"] = self._default_workflows()
        return json_response(result)

    async def _api_workflows_analyze(self):
        """识别按钮：LLM 增量识别本地工作流（只识别未识别/已修改/非手动标记的），已识别的跳过。"""
        provider = await self._resolve_router_provider(None)
        if provider is None:
            return error_response("没有可用的路由 LLM：请先在设置页配置「路由大模型」", status_code=503)
        caps = await self._get_capabilities()
        if not caps.get("workflows"):
            return error_response("本地没有发现工作流", status_code=503)
        analysis = self._parse_json_config("workflow_analysis")
        todo = []
        for w in caps.get("workflows") or []:
            name = str(w.get("name") or "")
            if not name:
                continue
            prev = analysis.get(name) or {}
            if prev.get("manual"):
                continue  # 手动标记的跳过
            mod = float(w.get("modified") or 0)
            prev_ts = float(prev.get("analyzed_at") or 0)
            if prev and prev_ts and mod and prev_ts >= mod:
                continue  # 已识别且文件未变
            todo.append(w)
        skipped = len(caps.get("workflows") or []) - len(todo)
        if not todo:
            logger.info(f"[remote_link] 工作流识别：全部已识别（跳过 {skipped} 个），无需重新识别")
            return json_response({"ok": True, "analysis": analysis, "skipped": skipped, "new": 0})
        try:
            new_analysis = await asyncio.wait_for(
                self._analyze_workflows(provider, {"workflows": todo}),
                timeout=float(self.config.get("router_timeout", 60)) + 30,
            )
        except Exception as e:  # noqa: BLE001
            return error_response(f"识别失败：{e}", status_code=502)
        analysis.update(new_analysis)
        self.config["workflow_analysis"] = json.dumps(analysis, ensure_ascii=False)
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        logger.info(f"[remote_link] 工作流识别完成：新增 {len(new_analysis)} 个（跳过 {skipped} 个已识别）")
        return json_response({"ok": True, "analysis": analysis, "skipped": skipped, "new": len(new_analysis)})

    async def _api_workflows_tag(self):
        """手动标记工作流：{name, category?, subtype?, tags?}。手动标记优先于 LLM 识别结果。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少 name", status_code=400)
        analysis = self._parse_json_config("workflow_analysis")
        entry = dict(analysis.get(name) or {})
        if "category" in payload:
            cat = str(payload.get("category") or "").strip()
            if cat and cat not in ("image", "video", "audio", "other"):
                return error_response("category 必须是 image/video/audio/other（或空）", status_code=400)
            entry["category"] = cat
        if "subtype" in payload:
            entry["subtype"] = str(payload.get("subtype") or "").strip()[:20]
        if "tags" in payload:
            tags = payload.get("tags")
            entry["tags"] = (
                [str(t).strip()[:30] for t in tags if str(t).strip()][:8]
                if isinstance(tags, list) else []
            )
        entry["manual"] = True  # 手动标记优先，LLM 增量识别跳过
        entry["analyzed_at"] = time.time()
        analysis[name] = entry
        self.config["workflow_analysis"] = json.dumps(analysis, ensure_ascii=False)
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        return json_response({"ok": True, "analysis": entry})

    async def _api_default_workflows_save(self):
        """保存默认工作流配置 {image, video, audio}。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        # 校验名字存在（空串表示不设置）
        try:
            wfs = await self._call_local("workflows", {}, timeout=20)
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        names = {str(w.get("name")) for w in wfs.get("workflows") or []}
        data = self._parse_json_config("default_workflows")
        for key in ("image", "video", "audio"):
            if key not in payload:
                continue
            val = str(payload.get(key) or "").strip()
            if val and val not in names:
                return error_response(f"工作流不存在: {val}", status_code=400)
            data[key] = val
        self.config["default_workflows"] = json.dumps(data, ensure_ascii=False)
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        return json_response({"ok": True, "defaults": self._default_workflows()})

    async def _api_subtype_configs_get(self):
        """子分类配置表（设置页用）：7 个子分类的 {workflow, llm_provider, llm_model, prompt_template}。"""
        return json_response(
            {
                "subtypes": self._subtype_configs(),
                "labels": SUBTYPE_LABELS,
                "categories": SUBTYPE_CATEGORY,
            }
        )

    async def _api_subtype_configs_save(self):
        """保存子分类配置：{subtype: {workflow, llm_provider, llm_model, prompt_template}}。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        # 校验工作流名存在（空串表示不设置）
        try:
            wfs = await self._call_local("workflows", {}, timeout=20)
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        names = {str(w.get("name")) for w in wfs.get("workflows") or []}
        data = self._parse_json_config("subtype_configs")
        for st in SUBTYPES:
            if st not in payload:
                continue
            item = payload[st]
            if not isinstance(item, dict):
                continue
            entry: dict = {}
            for key in ("workflow", "llm_provider", "llm_model", "prompt_template"):
                val = str(item.get(key) or "").strip()
                if key == "workflow" and val and val not in names:
                    return error_response(f"工作流不存在: {val}", status_code=400)
                entry[key] = val
            data[st] = entry
        self.config["subtype_configs"] = json.dumps(data, ensure_ascii=False)
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        return json_response({"ok": True, "subtypes": self._subtype_configs()})

    async def _api_provider_models(self):
        """查询指定提供商（AskBot 已配置的）的模型列表，供设置页模型下拉联动。"""
        provider_id = request.query.get("provider_id", "").strip()
        if not provider_id:
            return error_response("缺少 provider_id", status_code=400)
        try:
            provider = self.context.get_provider_by_id(provider_id=provider_id)
            if provider is None:
                return error_response("提供商不存在", status_code=404)
            models = await provider.get_models()
            return json_response({"models": models or []})
        except Exception as e:  # noqa: BLE001
            return error_response(f"获取模型列表失败: {e}", status_code=502)

    async def _api_object_info(self):
        try:
            result = await self._call_local(
                "comfyui", {"action": "proxy", "method": "GET", "path": "/object_info"}, timeout=60
            )
        except RemoteLinkError as e:
            return error_response(str(e), status_code=503)
        return json_response(result.get("json") or {})

    # ---- 设置页 API ----

    SETTING_KEYS = {
        "router_enabled": "bool",
        "router_provider": "str",
        "router_model": "str",
        "router_temperature": "float",
        "router_timeout": "int",
        "server_host": "str",
        "server_port": "int",
        "request_timeout": "int",
        "comfyui_timeout": "int",
        "llm_model": "str",
        "enable_shell": "bool",
        "nsfw_enabled": "bool",
        "notify_on_start": "bool",
        "router_system_prompt": "str",
        "enhance_system_prompt": "str",
    }

    async def _api_settings_get(self):
        token = str(self.config.get("auth_token") or "")
        masked = token[:4] + "…" + token[-4:] if len(token) > 10 else ("已设置" if token else "未设置")

        providers = []
        try:
            # AstrBot 的 Provider 列表：优先 provider_manager.get_insts()，
            # 每个 Provider 实例的 meta() 返回 ProviderMeta(id/model/type)。
            all_providers = None
            pm = getattr(self.context, "provider_manager", None)
            if pm is not None and hasattr(pm, "get_insts"):
                try:
                    all_providers = pm.get_insts() or []
                except Exception:  # noqa: BLE001
                    all_providers = None
            if not all_providers:
                all_providers = self.context.get_all_providers() or []
            seen = set()
            for p in all_providers:
                meta = None
                try:
                    meta = p.meta() if callable(getattr(p, "meta", None)) else None
                except Exception:  # noqa: BLE001
                    meta = None
                # ProviderMeta 是对象，字段 id/model/type
                pid = (
                    (getattr(meta, "id", "") if meta else "")
                    or getattr(p, "provider_id", None)
                    or getattr(p, "id", None)
                    or ""
                )
                if not pid:
                    continue
                # 显示名：用 model 或 provider_config['provider']
                pname = ""
                if meta:
                    pname = getattr(meta, "model", "") or getattr(meta, "id", "")
                if not pname:
                    cfg = getattr(p, "provider_config", None) or {}
                    pname = str(cfg.get("provider") or cfg.get("id") or pid)
                key = str(pid)
                if key in seen:
                    continue
                seen.add(key)
                providers.append({"id": str(pid), "name": str(pname or pid), "type": "llm"})
        except Exception:  # noqa: BLE001
            pass

        tools = []
        try:
            mgr = self.context.get_llm_tool_manager()
            tool_list = getattr(mgr, "func_list", None) or list(getattr(mgr, "tools", None) or [])
            for t in tool_list:
                name = getattr(t, "name", "") or ""
                if name.startswith("remote_"):
                    tools.append(
                        {
                            "name": name,
                            "active": bool(getattr(t, "active", True)),
                            "description": str(getattr(t, "description", "") or "")[:80],
                        }
                    )
        except Exception:  # noqa: BLE001
            pass

        media = {"count": 0, "size_mb": 0.0}
        try:
            if self._media_dir.is_dir():
                files = list(self._media_dir.iterdir())
                media["count"] = len(files)
                media["size_mb"] = round(sum(f.stat().st_size for f in files if f.is_file()) / 1024**2, 2)
        except Exception:  # noqa: BLE001
            pass

        config_out = {}
        for key in self.SETTING_KEYS:
            config_out[key] = self.config.get(key)
        config_out["auth_token_masked"] = masked
        config_out["auth_token"] = token  # 设置页「点开才显示」用（页面继承 WebUI 管理员鉴权）
        return json_response(
            {
                "config": config_out,
                "providers": providers,
                "tools": tools,
                "media": media,
                "server_running": self._runner is not None,
                "defaults": {
                    "router_system_prompt": DEFAULT_ROUTER_PROMPT,
                    "enhance_system_prompt": DEFAULT_ENHANCE_PROMPT,
                },
            }
        )

    async def _api_settings_save(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是对象", status_code=400)
        changed = {}
        for key, ptype in self.SETTING_KEYS.items():
            if key not in payload:
                continue
            raw = payload[key]
            try:
                if ptype == "bool":
                    val = bool(raw)
                elif ptype == "int":
                    val = int(raw)
                elif ptype == "float":
                    val = float(raw)
                else:
                    val = str(raw)
            except (TypeError, ValueError):
                return error_response(f"{key} 类型不正确（需要 {ptype}）", status_code=400)
            self.config[key] = val
            changed[key] = val
        if not changed:
            return json_response({"ok": True, "changed": []})
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        # 监听地址/端口变化 → 重启隧道服务端任务
        server_restarted = False
        if "server_host" in changed or "server_port" in changed:
            if self._server_task is not None:
                self._server_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self._server_task), timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                self._server_task = None
            self._ensure_server()
            server_restarted = True
        note = ""
        if server_restarted:
            note = "隧道服务端已按新地址/端口重启（本地代理会自动重连）"
        return json_response({"ok": True, "changed": list(changed), "note": note})

    async def _api_settings_gen_token(self):
        token = secrets.token_hex(16)
        self.config["auth_token"] = token
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            return error_response(f"配置保存失败: {e}", status_code=500)
        return json_response(
            {"ok": True, "token": token, "hint": "请把新 token 同步到本地 YunxinAgent 的配置里"}
        )

    async def _api_settings_clear_media(self):
        removed, freed = 0, 0
        try:
            if self._media_dir.is_dir():
                for f in self._media_dir.iterdir():
                    if f.is_file():
                        freed += f.stat().st_size
                        f.unlink()
                        removed += 1
        except Exception as e:  # noqa: BLE001
            return error_response(f"清理失败: {e}", status_code=500)
        return json_response({"ok": True, "removed": removed, "freed_mb": round(freed / 1024**2, 2)})

    async def _api_settings_toggle_tool(self):
        payload = await request.json(default={})
        name = str(payload.get("name") or "")
        active = bool(payload.get("active"))
        if not name or not name.startswith("remote_"):
            return error_response("只允许切换本插件的工具", status_code=400)
        try:
            if active:
                await self.context.activate_llm_tool_async(name)
            else:
                await self.context.deactivate_llm_tool_async(name)
        except Exception:  # noqa: BLE001  # 老版本没有异步接口时退回同步
            try:
                if active:
                    self.context.activate_llm_tool(name)
                else:
                    self.context.deactivate_llm_tool(name)
            except Exception as e:  # noqa: BLE001
                return error_response(f"切换失败: {e}", status_code=500)
        return json_response({"ok": True, "name": name, "active": active})

    def _load_enhance_sessions(self):
        """从磁盘加载子 LLM 增强对话（重载/重装/服务器重启后保留）。"""
        try:
            if self._enhance_sessions_path.is_file():
                data = json.loads(self._enhance_sessions_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._enhance_sessions = data
                    logger.info(f"[remote_link] 已加载 {len(data)} 个子 LLM 增强对话（持久化）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 加载增强对话失败: {e}")

    def _save_enhance_sessions(self):
        """把子 LLM 增强对话落盘（新增轮次/初始化后调用）。"""
        try:
            self._enhance_sessions_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._enhance_sessions_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._enhance_sessions, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._enhance_sessions_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 保存增强对话失败: {e}")

    async def _api_enhance_sessions(self):
        """展示子 LLM 增强对话记录（GET）：每个【已配置子分类】的 system 摘要 + 最近几轮完整对话。"""
        sessions = {}
        for sub, ses in list(self._enhance_sessions.items()):
            sessions[sub] = {
                "label": SUBTYPE_LABELS.get(sub, sub),
                "workflow": (self._subtype_configs().get(sub) or {}).get("workflow", ""),
                "system": (ses.get("system") or "")[:20000],
                "history": [
                    {
                        "user": (h.get("user") or "")[:2000],
                        "assistant": (h.get("assistant") or "")[:2000],
                        "negative": (h.get("negative") or "")[:2000],
                    }
                    for h in (ses.get("history") or [])
                ],
            }
        return json_response({"ok": True, "sessions": sessions, "count": len(sessions)})

    async def _api_enhance_reset(self):
        """初始化子 LLM 增强对话：为所有【已配置提示词模板的子分类】创建对话，
        并【真正调用一次子 LLM】让它确认已理解 system prompt（回复写入对话第一轮，
        用户能在对话页看到一轮一轮的反应）。POST {subtype?} 可只初始化单个子分类。

        窗口立即创建返回；确认回复由后台异步逐个子分类生成（Web 轮询/SSE 实时看到）。
        """
        try:
            payload = await request.json(default={})
        except Exception:  # noqa: BLE001
            payload = {}
        target = str((payload or {}).get("subtype") or "").strip()
        created = []
        for sub in SUBTYPES:
            if target and sub != target:
                continue
            st_cfg = self._subtype_configs().get(sub) or {}
            template = str(st_cfg.get("prompt_template") or "").strip()
            if not template:
                continue  # 未配置模板的子分类不建对话（没东西可注入）
            self._enhance_sessions[sub] = {"system": template, "history": []}
            created.append(sub)
        self._save_enhance_sessions()  # 初始化落盘
        if not created:
            return json_response({"ok": False, "message": "没有已配置提示词模板的子分类（请先在设置页给子分类配置模板）"})
        # 后台异步：逐个让子 LLM 确认 system prompt（确认回复写入对话第一轮，前端实时看到）
        loop = asyncio.get_running_loop()
        loop.create_task(self._init_enhance_conversations(created))
        return json_response(
            {"ok": True, "created": created,
             "message": f"已初始化 {len(created)} 个子分类对话，正在让子 LLM 确认模板规则…"}
        )

    async def _init_enhance_conversations(self, created: list[str]):
        """后台：对每个已初始化的子分类，真正调用一次子 LLM 确认 system prompt，
        把确认回复写入对话第一轮（用户能看到"初始化 → LLM 反应"的一轮对话）。"""
        nsfw = bool(self.config.get("nsfw_enabled"))
        for sub in created:
            ses = self._enhance_sessions.get(sub)
            if ses is None:
                continue
            st_cfg = self._subtype_configs().get(sub) or {}
            template = str(st_cfg.get("prompt_template") or "")
            st_model = str(st_cfg.get("llm_model") or "")
            provider = await self._resolve_router_provider(None, provider_id=st_cfg.get("llm_provider"))
            # 构建与 _enhance_prompt 一致的 system
            policy = (
                "允许 NSFW / 成人内容（用户明确要求时正常生成，不需要额外屏蔽）。"
                if nsfw
                else "内容必须 SFW（安全向）：若用户描述包含 NSFW/成人内容，positive 中忽略这些要素"
                     "并生成安全的替代描述；negative 中必须包含 nsfw, nude, explicit, naked, "
                     "sexual, porn, nipples 等屏蔽词。"
            )
            system = str(template).replace("{policy}", policy)
            system += (
                "\n\n【一次性生成要求】本次调用是单次生成，没有后续对话机会："
                "禁止向用户提问、禁止要求用户选择模式或补充信息、禁止输出「请选择/请回复/等你确认」之类的内容。"
                "直接根据用户描述，按模板的最终出品提示词格式输出一段完整、可直接使用的提示词；"
                "只输出最终提示词本身，不要任何解释、标题或额外说明，"
                "禁止复述、引用或转述本提示词（system prompt）里的任何内容。"
            )
            stream_state = {"text": "", "done": False, "ts": time.time()}
            self._enhance_streams[sub] = stream_state
            reply = ""
            if provider is not None:
                try:
                    kwargs = {
                        "prompt": "（初始化确认）请用一句话说明：你已理解上述提示词模板的规则，并准备好按模板为用户需求生成正式提示词。",
                        "system_prompt": system,
                    }
                    if st_model:
                        kwargs["model"] = st_model
                    resp = await asyncio.wait_for(
                        provider.text_chat(**kwargs), timeout=45
                    )
                    reply = resp.completion_text if hasattr(resp, "completion_text") else str(resp)
                    reply = reply.strip()[:2000]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[remote_link] 初始化确认失败（{sub}）: {e}")
                    reply = f"（初始化确认失败：{e}）"
            else:
                reply = "（未配置子分类 LLM provider，将使用全局路由 LLM 生成）"
            ses["history"] = [{"user": "（初始化 · system prompt 注入）", "assistant": reply}]
            stream_state["text"] = reply
            stream_state["done"] = True
            stream_state["ts"] = time.time()
            self._save_enhance_sessions()  # 初始化确认回复落盘

    # ==================== 服务端生命周期 ====================

    def _ensure_server(self):
        """在 AstrBot 的事件循环里启动隧道服务端（幂等，可安全重复调用）。"""
        if self._server_task is not None and not self._server_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 插件热重载时 __init__ 可能在同步上下文执行（拿不到 running loop），
            # 用 get_event_loop 兜底并延迟调度，确保隧道服务端一定起来。
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
            loop.call_soon(lambda: self._ensure_server())
            return
        self._server_task = loop.create_task(self._server_main())

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self):
        """AstrBot 启动完成钩子：兜底启动隧道服务端。"""
        self._ensure_server()

    async def _server_main(self):
        """隧道服务端主循环：WS 隧道 + OpenAI 兼容 HTTP 代理，跑在同一个 aiohttp 应用里。"""
        host = str(self.config.get("server_host", "0.0.0.0"))
        port = int(self.config.get("server_port", 8468))
        app = web.Application()
        app.router.add_get("/ws", self.tunnel.handle_ws)
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_get("/healthz", self._handle_healthz)
        # 媒体下载端点（NapCat 从 URL 拉视频用）：不走 AstrBot 面板鉴权，
        # 只防目录穿越；文件是产物（随机文件名），暴露面可控。
        app.router.add_get("/media", self._http_media)
        # 本地 GUI / 外部客户端用的任务端点（auth_token 鉴权）
        app.router.add_post("/submit", self._http_submit)
        app.router.add_get("/queue", self._http_queue)
        app.router.add_post("/task_send", self._http_task_send)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
            self._runner = runner
            logger.info(
                f"[remote_link] 隧道服务端已启动: ws://{host}:{port}/ws "
                f"（生产建议通过 Nginx/Caddy 反代为 wss://；Docker 部署需映射此端口）"
            )
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[remote_link] 隧道服务端启动失败: {e}")
            self._runner = None
        finally:
            await runner.cleanup()
            if self._runner is runner:
                self._runner = None

    # ==================== 认证与 WebSocket 隧道（委托 TunnelServer） ====================

    def _authorized(self, request) -> bool:
        """统一鉴权：默认 Bearer Token；旧版 ?token= 仅作 deprecated 兼容。"""
        return self.tunnel.authorized(request)

    async def _call_local(self, service: str, payload: dict, timeout: float | None = None) -> dict:
        """发起一次隧道请求并等待结果（请求-响应模式）。"""
        return await self.tunnel.request(service, payload, timeout=timeout)

    async def _call_local_stream(self, service, payload, on_chunk, timeout=None) -> dict:
        """隧道流式调用：每个 SSE 分块到达时调用 on_chunk(text)。"""
        return await self.tunnel.request_stream(service, payload, on_chunk, timeout=timeout)

    def _ensure_connected(self):
        self.tunnel.ensure_connected()

    def _fail_all_pending(self, reason: str):
        self.tunnel.fail_all_pending(reason)

    # ==================== OpenAI 兼容 HTTP 代理（把本地 LLM 变成 AstrBot 的提供商） ====================

    async def _handle_healthz(self, request):
        return web.json_response(
            {"ok": True, "agent_connected": self.tunnel.connected}
        )

    async def _http_submit(self, request):
        """本地 GUI 提交任务端点（POST /submit，auth_token 鉴权）。

        与 QQ/Web 同一套内生调度；产物在队列可见（web: 来源，无会话可发）。
        """
        if not self._check_rate_limit(request, limit=30, window=60):
            return web.Response(status=429, text="too many requests")
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "message": "请求体必须是 JSON"}, status=400)
        intent = str((payload or {}).get("intent") or "").strip()
        if not intent:
            return web.json_response({"ok": False, "message": "缺少 intent"}, status=400)
        images = self._save_uploaded_images((payload or {}).get("images"))
        from types import SimpleNamespace

        fake_event = SimpleNamespace(
            unified_msg_origin=f"web:{secrets.token_hex(4)}",
            message_str=intent,
            is_at_or_wake_command=True,
            get_sender_id=lambda: "",
            get_sender_name=lambda: "",
            message_obj=SimpleNamespace(message=[]),
        )
        rec = self.submit_local_task(
            intent, str((payload or {}).get("workflow_hint") or ""), fake_event, pre_images=images
        )
        rec["web_submit"] = True
        return web.json_response({"ok": True, "task_id": rec["task_id"], "status": "queued", "images": len(images)})

    async def _http_media(self, request):
        """内嵌 HTTP 媒体下载端点（GET /media?filename=xxx&expires=...&sig=...）。

        v0.2.0 起要求短期 HMAC 签名；未签名/过期/伪造一律 403。
        只允许读取 media 目录下的产物文件；文件未拉取时先按需从本地代理拉取。
        """
        if not self._verify_media_signature(request):
            return web.Response(status=403, text="invalid or expired media signature")
        filename = request.query.get("filename", "").strip()
        if not filename:
            return web.json_response({"ok": False, "message": "缺少 filename"}, status=400)
        safe = Path(filename).name
        path = self._media_dir / safe
        if not path.is_file():
            rec = self._find_media_record(safe)
            if rec is not None:
                try:
                    await self._pull_media(rec)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[remote_link] 内嵌媒体拉取失败 {safe}: {e}")
        if not path.is_file():
            return web.json_response({"ok": False, "message": "文件不存在"}, status=404)
        ext = safe.lower().rsplit(".", 1)[-1] if "." in safe else ""
        mime = EXT_MIME.get("." + ext, "application/octet-stream")
        return web.Response(body=path.read_bytes(), content_type=mime)

    async def _http_task_send(self, request):
        """本地 GUI 发送产物到会话（POST /task_send，auth_token 鉴权）。"""
        if not self._check_rate_limit(request, limit=30, window=60):
            return web.Response(status=429, text="too many requests")
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "message": "请求体必须是 JSON"}, status=400)
        task_id = str((payload or {}).get("task_id") or "").strip()
        if not task_id:
            return web.json_response({"ok": False, "message": "缺少 task_id"}, status=400)
        rec = self.tasks.find_by_task_id(task_id)
        if rec is None:
            return web.json_response({"ok": False, "message": f"任务 {task_id} 不存在"}, status=404)
        files = rec.get("files") or []
        origin = str(rec.get("origin") or "")
        if not files or not origin:
            return web.json_response({"ok": False, "message": "该任务没有可发送的产物或来源会话"}, status=400)
        try:
            ok = await self._send_media_to_origin(origin, files)
            return web.json_response({"ok": True, "sent": ok})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 本地GUI发送产物失败: {e}")
            return web.json_response({"ok": False, "message": str(e)}, status=502)

    async def _http_queue(self, request):
        """本地 GUI 查询任务队列（GET /queue，auth_token 鉴权）。"""
        if not self._check_rate_limit(request, limit=30, window=60):
            return web.Response(status=429, text="too many requests")
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        return web.json_response({"queue": self._queue_snapshot()})

    async def _handle_models(self, request):
        if not self._check_rate_limit(request, limit=120, window=60):
            return web.Response(status=429, text="too many requests")
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        try:
            result = await self._call_local(
                "openai", {"method": "GET", "path": "/v1/models", "body": None}, timeout=30
            )
        except RemoteLinkError as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "remote_link_error"}}, status=503
            )
        data = result.get("json") or {}
        # Ollama 的 /v1/models 返回 {"models": [...]}，归一化成 OpenAI 的 {"data": [...]}
        if isinstance(data, dict) and "data" not in data and isinstance(data.get("models"), list):
            data = {
                "object": "list",
                "data": [
                    {
                        "id": m.get("id") or m.get("name") if isinstance(m, dict) else str(m),
                        "object": "model",
                    }
                    for m in data["models"]
                ],
            }
        return web.json_response(data, status=int(result.get("status", 200)))

    async def _handle_chat_completions(self, request):
        """OpenAI 兼容的 /v1/chat/completions：把请求经隧道转发给本地 LLM。

        在 AstrBot WebUI 里添加一个 OpenAI 兼容提供商：
          base_url = http://127.0.0.1:<server_port>/v1
          api_key  = <auth_token>
        即可把本地 LLM 直接当作 AstrBot 的大脑使用。
        """
        if not self._check_rate_limit(request, limit=120, window=60):
            return web.Response(status=429, text="too many requests")
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="invalid json body")
        try:
            self._ensure_connected()
        except RemoteLinkError as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "remote_link_error"}}, status=503
            )

        payload = {"method": "POST", "path": "/v1/chat/completions", "body": body}

        if body.get("stream"):
            # SSE 流式转发：本地代理把每个 SSE 事件原样转发过来，这里逐块写回请求方
            resp = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await resp.prepare(request)
            queue: asyncio.Queue = asyncio.Queue()

            def on_chunk(text: str):
                queue.put_nowait(text)

            task = asyncio.ensure_future(
                self._call_local_stream(
                    "openai",
                    payload,
                    on_chunk,
                    timeout=float(self.config.get("request_timeout", 300)),
                )
            )
            try:
                while True:
                    if task.done():
                        task.result()  # 若失败会抛出 RemoteLinkError，由下方统一写入错误事件
                        break
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=1.0)
                        await resp.write(chunk.encode("utf-8"))
                    except asyncio.TimeoutError:
                        continue
                await resp.write(b"data: [DONE]\n\n")
            except RemoteLinkError as e:
                err_payload = json.dumps({"error": {"message": str(e)}})
                await resp.write(f"data: {err_payload}\n\n".encode("utf-8"))
                await resp.write(b"data: [DONE]\n\n")
            finally:
                if not task.done():
                    task.cancel()
                await resp.write_eof()
            return resp

        try:
            result = await self._call_local("openai", payload)
        except RemoteLinkError as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "remote_link_error"}}, status=503
            )
        data = result.get("json") or {}
        return web.json_response(data, status=int(result.get("status", 200)))

    # ==================== 业务逻辑：工作流执行 / 本地 LLM / Shell ====================

    def _save_media(self, files: list[dict]) -> list[dict]:
        """记录隧道回传的产物元数据，返回 [{kind, path, filename, mime, ...}]。"""
        return self.media.save_media(files)

    def _prune_media(self):
        """性能/存储优化：媒体目录自动清理。"""
        self.media.prune_media()

    async def _pull_media(self, f: dict) -> dict:
        """按需从本地代理拉取产物文件并落盘（幂等：已落盘直接返回）。"""
        return await self.media.pull_media(f)

    def _media_captions(self, files: list[dict]) -> list[str]:
        """多产物时的编号说明（主产物优先已在代理端排好序）。"""
        return self.media.media_captions(files)

    def _media_results(self, event: AstrMessageEvent, files: list[dict]):
        """把产物列表转成 AstrBot 的消息结果（图片 / 视频 / 音频组件）。"""
        for f in files:
            yield from self._yield_media_result(event, f)

    def _media_file_url(self, filename: str) -> str:
        """产物媒体 URL（NapCat 从 URL 下载视频用）。"""
        return self.media.file_url(filename)

    async def _send_media_to_origin(self, origin: str, files: list[dict]) -> bool:
        """把产物直接发送到指定会话（不依赖事件对象，供 Web 控制台"发送到QQ"用）。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain

            chain = []
            for f in files:
                f = await self._pull_media(f)
                if not f.get("path"):
                    continue
                kind = f.get("kind")
                if kind == "image":
                    chain.append(Comp.Image.fromFileSystem(path=f["path"]))
                elif kind == "video":
                    # NapCat(OneBot) 与 AstrBot 文件系统隔离，本地路径读不到(ENOENT)：
                    # 视频必须用 URL（NapCat 从 URL 下载上传），文件路径仅兜底。
                    try:
                        chain.append(Comp.Video.fromURL(self._media_file_url(f["path"])))
                    except Exception:  # noqa: BLE001
                        try:
                            chain.append(Comp.File.fromFileSystem(path=f["path"]))
                        except Exception:  # noqa: BLE001
                            try:
                                chain.append(Comp.Video.fromFileSystem(path=f["path"]))
                            except Exception:  # noqa: BLE001
                                pass
                elif kind == "audio":
                    try:
                        chain.append(Comp.Audio.fromFileSystem(path=f["path"]))
                    except Exception:  # noqa: BLE001
                        pass
            if not chain:
                return False
            await self.context.send_message(origin, MessageChain(chain=chain))
            logger.info(f"[remote_link] 已发送 {len(chain)} 个产物到会话 {origin}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 发送产物到会话失败: {e}")
            return False

    async def _send_media_direct(self, event: AstrMessageEvent, files: list[dict],
                                 rec: dict | None = None) -> bool:
        """直接把产物发到会话（不依赖机器人 LLM 决定是否调用 send 工具）。

        v4.27+ 的工具运行器会把工具产出的图片放进缓存让 LLM「审核后决定是否发送」，
        经常不发送；这里改为插件主动直发，保证交付。发送前先按需从本地拉取真实文件。
        失败返回 False，并把具体原因写进 rec['error']（Web 队列可见，方便定位）。
        """
        try:
            from astrbot.core.message.message_event_result import MessageChain

            chain = []
            for f in files:
                f = await self._pull_media(f)
                if not f.get("path"):
                    continue
                kind = f.get("kind")
                if kind == "image":
                    chain.append(Comp.Image.fromFileSystem(path=f["path"]))
                elif kind == "video":
                    # 关键：NapCat(OneBot) 与 AstrBot 文件系统隔离，本地路径读不到(ENOENT)。
                    # 视频必须用 URL（NapCat 从 URL 下载上传）；文件路径仅作兜底。
                    try:
                        chain.append(Comp.Video.fromURL(self._media_file_url(f["path"])))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[remote_link] 视频 fromURL 失败: {e}")
                        try:
                            chain.append(Comp.File.fromFileSystem(path=f["path"]))
                        except Exception:  # noqa: BLE001
                            try:
                                chain.append(Comp.Video.fromFileSystem(path=f["path"]))
                            except Exception as e2:  # noqa: BLE001
                                logger.warning(f"[remote_link] 视频组件构建失败: {e2}")
                                if rec is not None:
                                    rec["error"] = f"视频组件构建失败: {e2}"
                elif kind == "audio":
                    try:
                        chain.append(Comp.Audio.fromFileSystem(path=f["path"]))
                    except Exception:  # noqa: BLE001
                        pass
            if not chain:
                if rec is not None and not rec.get("error"):
                    rec["error"] = "产物组件构建为空（视频/音频可能不被平台支持）"
                return False
            await self.context.send_message(event.unified_msg_origin, MessageChain(chain=chain))
            logger.info(f"[remote_link] 已直接发送 {len(chain)} 个产物到会话")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 直接发送产物失败，回退事件链: {e}")
            if rec is not None:
                rec["error"] = f"直接发送产物失败: {e}"
            return False

    async def _deliver_files(self, event: AstrMessageEvent, files: list[dict]):
        """发送产物：先按需从本地拉取，再优先插件直发；失败则退回 yield 事件结果（旧行为）。"""
        for f in files:
            await self._pull_media(f)
        if await self._send_media_direct(event, files):
            return
        for res in self._media_results(event, files):
            yield res

    async def _local_llm_available(self) -> bool:
        """本地 LLM（Ollama/LM Studio 等）是否可达——用能力清单里的 llm_models 判断（带缓存）。"""
        try:
            caps = await self._get_capabilities()
            return bool(caps.get("llm_models"))
        except Exception:  # noqa: BLE001
            return False

    async def local_llm_chat(self, prompt: str, model: str = "") -> str:
        """通过隧道调用本地 OpenAI 兼容 LLM，返回回复文本。"""
        body = {"messages": [{"role": "user", "content": prompt}], "stream": False}
        model = model or str(self.config.get("llm_model", ""))
        if model:
            body["model"] = model
        result = await self._call_local(
            "openai", {"method": "POST", "path": "/v1/chat/completions", "body": body}
        )
        data = result.get("json") or {}
        choices = data.get("choices") or []
        if not choices:
            raise RemoteLinkError(
                "本地 LLM 响应中没有 choices: " + json.dumps(data, ensure_ascii=False)[:300]
            )
        message = choices[0].get("message") or {}
        return message.get("content") or ""

    async def local_shell(self, command: str) -> dict:
        """通过隧道在本地电脑执行命令（需双端开关都打开）。"""
        return await self._call_local("shell", {"command": command}, timeout=120)

    # ==================== 智能调度：能力清单 + LLM 路由 ====================

    async def _get_capabilities(self, force: bool = False) -> dict:
        """拉取本地能力清单（代理动态发现：工作流/参数/checkpoint/LLM 模型/GPU/队列）。"""
        now = time.time()
        if not force and self._capabilities is not None and now - self._capabilities_ts < 120:
            return self._capabilities
        try:
            caps = await self._call_local("capabilities", {}, timeout=60)
        except RemoteLinkError as e:
            logger.warning(f"[remote_link] 获取能力清单失败: {e}")
            if self._capabilities is not None:
                return self._capabilities
            raise
        self._capabilities = caps
        self._capabilities_ts = now
        return caps

    # ---------------- 工作流识别（初始化）与默认工作流 ----------------

    def _parse_json_config(self, key: str) -> dict:
        """解析 JSON 形态的插件配置（workflow_analysis / default_workflows / subtype_configs），坏 JSON 返回空。"""
        raw = str(self.config.get(key) or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 配置 {key} 解析失败: {e}")
            return {}

    def _default_workflows(self) -> dict:
        """默认工作流配置：{image: 名字, video: 名字, audio: 名字}（未配置为空串）。"""
        data = self._parse_json_config("default_workflows")
        return {
            "image": str(data.get("image") or ""),
            "video": str(data.get("video") or ""),
            "audio": str(data.get("audio") or ""),
        }

    def _workflow_analysis(self) -> dict:
        """已保存的工作流识别结果：{name: {category, subtype, summary, tags, analyzed_at}}。"""
        return self._parse_json_config("workflow_analysis")

    def _subtype_configs(self) -> dict:
        """子分类配置表：{subtype: {workflow, llm_provider, llm_model, prompt_template}}。"""
        data = self._parse_json_config("subtype_configs")
        out: dict = {}
        for st in SUBTYPES:
            item = data.get(st) or {}
            if not isinstance(item, dict):
                item = {}
            out[st] = {
                "workflow": str(item.get("workflow") or ""),
                "llm_provider": str(item.get("llm_provider") or ""),
                "llm_model": str(item.get("llm_model") or ""),
                "prompt_template": str(item.get("prompt_template") or ""),
            }
        return out

    def _workflow_for_subtype(self, subtype: str, caps: dict, workflow_hint: str = "") -> dict | None:
        """按子分类解析工作流：用户指定 > 子分类配置 > 大类默认 > 按产物类型匹配。"""
        wfs = caps.get("workflows") or []
        if workflow_hint:
            cand = next((w for w in wfs if w.get("name") == workflow_hint), None)
            if cand is not None:
                return cand
        name = (self._subtype_configs().get(subtype) or {}).get("workflow") or ""
        if not name:
            name = self._default_workflows().get(SUBTYPE_CATEGORY.get(subtype, "")) or ""
        if name:
            cand = next((w for w in wfs if w.get("name") == name), None)
            if cand is not None:
                return cand
        cat = SUBTYPE_CATEGORY.get(subtype, "")
        cands = [w for w in wfs if cat in (w.get("outputs") or [])]
        return cands[0] if cands else None

    def _pick_subtype(self, kind: str, n_images: int, intent: str) -> str:
        """子分类规则：手动关键词 > 图数规则（带图→图生类，多图→多图生视频）。"""
        return pick_subtype(kind, n_images, intent)

    async def _analyze_workflows(self, provider, caps: dict) -> dict:
        """LLM 识别每个工作流的用途：返回 {name: {category, subtype, summary, tags}}。"""
        lines = []
        for w in (caps.get("workflows") or []):
            outs = ",".join(w.get("outputs") or []) or "无"
            nodes = ",".join((w.get("nodes") or [])[:14])
            inj = w.get("injects") or {}
            inj_str = "、".join(
                f"{k}{inj.get(k, 0)}" for k in ("images", "texts", "videos") if inj.get(k)
            ) or "无"
            lines.append(f"- {w.get('name')} [产物:{outs} | 入口(图片/文本/视频):{inj_str} | 节点:{nodes}]")
        user = "本地工作流清单：\n" + "\n".join(lines)
        kwargs = {"prompt": user, "system_prompt": DEFAULT_ANALYZE_PROMPT}
        router_model = str(self.config.get("router_model") or "").strip()
        if router_model:
            kwargs["model"] = router_model
        try:
            resp = await provider.text_chat(**kwargs)
        except TypeError:
            resp = await provider.text_chat(prompt=user, system_prompt=DEFAULT_ANALYZE_PROMPT, model=router_model or None)
        text = ""
        if hasattr(resp, "completion_text"):
            text = resp.completion_text or ""
        else:
            text = str(resp)
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            raise ValueError(f"工作流识别没有输出 JSON 数组: {text[:200]}")
        data = json.loads(m.group(0))
        if not isinstance(data, list):
            raise ValueError("工作流识别输出不是数组")
        names = {str(w.get("name")) for w in caps.get("workflows") or []}
        out: dict = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name not in names:
                continue
            category = str(item.get("category") or "other").strip()
            if category not in ("image", "video", "audio", "other"):
                category = "other"
            out[name] = {
                "category": category,
                "subtype": str(item.get("subtype") or "")[:20],
                "summary": str(item.get("summary") or "")[:100],
                "tags": [str(t)[:20] for t in (item.get("tags") or []) if str(t)][:6],
                "analyzed_at": time.time(),
            }
        return out

    async def _route_local_task(self, intent: str, workflow_hint: str, event, n_images: int = 0) -> dict:
        """两级流水线：一级分类（image/video/audio/text）→ 子分类规则 → 配置表解析工作流。"""
        caps = await self._get_capabilities()
        kind = await self._classify_kind(intent, event)
        if kind == "text":
            return {
                "kind": "llm",
                "workflow": "",
                "params": {"prompt": intent, "model": ""},
                "explanation": "任务分类：纯文本 → 本地 LLM",
                "subtype": "",
            }
        subtype = self._pick_subtype(kind, n_images, intent)
        wf = self._workflow_for_subtype(subtype, caps, workflow_hint)
        label = SUBTYPE_LABELS.get(subtype, subtype)
        if wf is None:
            raise RemoteLinkError(
                f"没有可用的【{label}】工作流：请在设置页「子分类配置」中为它指定默认工作流"
            )
        return {
            "kind": "comfyui",
            "workflow": str(wf.get("name")),
            "params": {},
            "explanation": f"任务分类：{label}",
            "subtype": subtype,
        }

    async def _classify_kind(self, intent: str, event) -> str:
        """一级分类：关键词优先（秒级、零 LLM 开销），命中即返回；
        关键词无法判断时再调 LLM（避免"生成视频/画图"这种明确任务白白等一次 LLM）。"""
        low = intent.lower()
        # 关键词优先（最常见任务秒级判定）
        if any(k in low for k in ("视频", "动画", "短片", "视频生成", "video")):
            return "video"
        if any(k in low for k in ("音乐", "音频", "歌曲", "配音", "音效", "audio", "music")):
            return "audio"
        if any(k in low for k in ("画", "图", "照片", "插画", "头像", "壁纸", "海报", "image", "photo", "anime")):
            return "image"
        # 关键词不明确才用 LLM（router_enabled 开启时）
        if self.config.get("router_enabled", True):
            provider = await self._resolve_router_provider(event)
            if provider is not None:
                try:
                    decision = await asyncio.wait_for(
                        self._llm_classify(provider, intent),
                        timeout=float(self.config.get("router_timeout", 30)),
                    )
                    if decision in ("image", "video", "audio", "text"):
                        return decision
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[remote_link] 一级分类失败，关键词兜底: {e}")
        return "text"

    async def _llm_classify(self, provider, intent: str) -> str:
        """用路由 LLM 做一级分类（默认模板只输出 {kind}）。"""
        system = str(self.config.get("router_system_prompt") or DEFAULT_ROUTER_PROMPT)
        kwargs = {"prompt": f"用户需求：{intent}", "system_prompt": system}
        router_model = str(self.config.get("router_model") or "").strip()
        if router_model:
            kwargs["model"] = router_model
        try:
            kwargs["temperature"] = float(self.config.get("router_temperature", 0.1))
        except (TypeError, ValueError):
            pass
        try:
            resp = await provider.text_chat(**kwargs)
        except TypeError as e:
            if "temperature" in kwargs and "temperature" in str(e):
                kwargs.pop("temperature")
                resp = await provider.text_chat(
                    prompt=f"用户需求：{intent}", system_prompt=system,
                    **({} if not router_model else {"model": router_model}),
                )
            else:
                raise
        text = ""
        if hasattr(resp, "completion_text"):
            text = resp.completion_text or ""
        else:
            text = str(resp)
        data = self._extract_json(text)
        if not isinstance(data, dict):
            raise ValueError(f"分类模型没有输出 JSON: {text[:200]}")
        return str(data.get("kind") or "").strip()

    def _param_intent_detected(self, intent: str) -> bool:
        """判断用户输入是否【明确提到】生成参数（分辨率/步数/CFG/采样器/种子/宽高比等）。

        只有明确提到参数时才启用 Anima 参数适配段（并供后续参数提取/注入使用）；
        未提到 → 返回 False，工作流完全用默认参数。
        """
        t = (intent or "").lower()
        patterns = (
            r"\d{3,4}\s*[x×*]\s*\d{3,4}",   # 1920×1080
            r"\d+\s*:\s*\d+",                # 9:16 宽高比
            r"步\s*数|迭代|steps|step",
            r"\bcfg\b|引导|指引",
            r"采样|sampler|调度|scheduler",
            r"种子|seed",
            r"分辨|尺寸|像素|resolution|宽高比|比例|aspect",
            r"\d+(?:\.\d+)?\s*p\b|4k|2k|8k|1080p|720p|高清|超清",
        )
        return any(re.search(p, t, re.I) for p in patterns)

    def _extract_json(self, text: str) -> dict | None:
        """从 LLM 输出里提取第一个合法 JSON 对象（dict）。

        先尝试整体解析；失败则用「不含嵌套花括号」的正则找出所有 {..} 候选，
        【从后往前】逐个尝试——LLM 通常把实际 JSON 放在最后输出，前面可能是
        复述 system prompt / 解释 / 模板示例（此前用贪婪的 {.*} 正则会把它们一起
        抓进来导致解析失败、进而把整段输出当成提示词，混入模板内容）。扁平 JSON
        （{"positive":"...","negative":"..."}）恰好没有嵌套花括号，适配此正则。
        """
        text = (text or "").strip()
        if not text:
            return None
        try:
            d = json.loads(text)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            pass
        candidates = [m.group(0) for m in re.finditer(r"\{[^{}]*\}", text, re.S)]
        for cand in reversed(candidates):
            try:
                d = json.loads(cand)
                if isinstance(d, dict):
                    return d
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _resolve_router_provider(self, event, provider_id: str = ""):
        """路由用的 LLM 提供商：provider_id/配置指定 > 跟随 AstrBot 当前会话提供商。"""
        provider = None
        rid = str(provider_id or "").strip() or str(self.config.get("router_provider") or "").strip()
        if rid:
            try:
                provider = self.context.get_provider_by_id(provider_id=rid)
            except Exception:  # noqa: BLE001
                provider = None
            if provider is None:
                logger.warning(f"[remote_link] 配置的路由提供商不存在: {rid}，回退默认")
        if provider is None:
            try:
                provider = await self.context.get_using_provider_async(
                    umo=getattr(event, "unified_msg_origin", None)
                )
            except Exception:  # noqa: BLE001
                provider = None
        return provider

    async def _enhance_prompt(
        self, provider, description: str, nsfw_enabled: bool,
        template: str = "", model: str = "", chat_messages: list[dict] | None = None,
        image_urls: list | None = None, stream_state: dict | None = None,
        anima_adapter: bool = False,
    ) -> dict:
        """自然语言描述 → {"positive": 正面提示词, "negative": 负面提示词}（LLM 提示词增强）。

        两种模板模式：
        - 自定义模板（子分类 prompt_template 非空）：模板决定输出格式，直接把 LLM 输出文本
          作为 positive（模板可能要求中文自然语言提示词，如 Minimax 视频模板，不强制英文 tag）；
        - 默认 JSON 模板：强制 {"positive","negative"} 输出 + 英文 tag 翻译兜底。
        template/model 可传子分类专属配置（为空回退全局）。
        """
        policy = (
            "允许 NSFW / 成人内容（用户明确要求时正常生成，不需要额外屏蔽）。"
            if nsfw_enabled
            else "内容必须 SFW（安全向）：若用户描述包含 NSFW/成人内容，positive 中忽略这些要素"
                 "并生成安全的替代描述；negative 中必须包含 nsfw, nude, explicit, naked, "
                 "sexual, porn, nipples 等屏蔽词。"
        )
        is_custom = bool(str(template or "").strip())
        system = str(
            template or self.config.get("enhance_system_prompt") or DEFAULT_ENHANCE_PROMPT
        ).replace("{policy}", policy)
        # 自定义模板（如视频模板）：一次性生成，不要向用户提问/等待确认。
        # 模板里若有"先问模式/等你回复"之类的交互指令，直接忽略，按模板的最终出品格式
        # 输出一段可直接使用的提示词（用户没指定模式时选一个合理默认）。
        if is_custom:
            system += (
                "\n\n【一次性生成要求】本次调用是单次生成，没有后续对话机会："
                "禁止向用户提问、禁止要求用户选择模式或补充信息、禁止输出「请选择/请回复/等你确认」之类的内容。"
                "直接根据用户描述，按模板的最终出品提示词格式输出一段完整、可直接使用的提示词；"
                "用户未明确指定模式时，从模板支持的模式里选一个最合理的默认并直接生成。"
                "只输出最终提示词本身，不要任何解释、标题或额外说明，"
                "禁止复述、引用或转述本提示词（system prompt）里的任何内容。"
            )
            if anima_adapter:
                # Anima 2.9B 模型适配（官方推荐参数；仅当用户输入提到参数时附加，否则不加，工作流用默认）
                system += (
                    "\n\n【Anima-2.9B 模型适配 · 官方推荐参数】"
                    "目标模型为 Anima-2.9B 动漫模型。官方推荐："
                    "采样器 euler / res-multistep / er-sde；调度器 sgm-uniform / beta / beta57 / linear-quadratic；"
                    "分辨率 812×1216 / 1152×1536 / 1536×1536（最长边上限 1536）；步数 28~50；CFG 3.5~5。"
                    "提示词要求：细节 Tag 数量与分辨率匹配——812×1216 约 30~50 个，1536 上限内可到 50~60 个；"
                    "禁止堆砌超过模型上限的超细节 Tag（易出伪影）。"
                    "用户提到的采样器/步数/CFG/分辨率/种子等参数由系统注入，你【不要输出参数】，"
                    "只需让提示词语义与用户意图及分辨率匹配。"
                )
        router_model = str(model or self.config.get("router_model") or "").strip()
        temperature = None
        try:
            temperature = float(self.config.get("router_temperature", 0.1))
        except (TypeError, ValueError):
            pass

        if chat_messages and not image_urls:
            text = await self._call_llm_messages(
                provider, system, chat_messages, router_model, temperature,
            )
        else:
            kwargs = {"prompt": description, "system_prompt": system}
            if router_model:
                kwargs["model"] = router_model
            if temperature is not None:
                kwargs["temperature"] = temperature
            if image_urls:
                # 多模态：有图时把图片本地路径一并传给子 LLM（看图+文字+模板 → 提示词）
                kwargs["image_urls"] = [str(p) for p in image_urls]
            # 真流式：尝试 provider.text_chat(stream=True)，逐 token 推送给 Web（打字机实时）
            # 不支持流式的 provider 会抛 TypeError/返回普通响应 → 自动回退一次性返回。
            streamed = await self._text_chat_stream(provider, kwargs, stream_state)
            if streamed is not None:
                text = streamed
            else:
                try:
                    resp = await provider.text_chat(**kwargs)
                except TypeError as e:
                    if "temperature" in kwargs and "temperature" in str(e):
                        kwargs.pop("temperature")
                        resp = await provider.text_chat(**kwargs)
                    else:
                        raise
                text = ""
                if hasattr(resp, "completion_text"):
                    text = resp.completion_text or ""
                else:
                    text = str(resp)
        text = text.strip()
        # 自定义模板：先尝试解析 JSON（模板要求 JSON 时，如 text2image 模板），
        # 解析失败或不是 JSON 时（如视频模板输出纯文本），直接把文本作为 positive。
        if is_custom:
            if not text:
                raise ValueError("提示词增强输出为空")
            data = self._extract_json(text)
            if isinstance(data, dict):
                pos = str(data.get("positive") or "").strip()
                neg = str(data.get("negative") or "").strip()
                if pos:
                    return {"positive": pos, "negative": neg}
            return {"positive": text, "negative": ""}
        # 默认 JSON 模板：解析 {"positive","negative"}
        data = self._extract_json(text)
        if not isinstance(data, dict):
            raise ValueError(f"提示词增强没有输出 JSON: {text[:200]}")
        positive = str(data.get("positive") or "").strip()
        negative = str(data.get("negative") or "").strip()
        if not positive:
            raise ValueError("提示词增强缺少 positive")
        # 模型不听话输出了中文 → 追加一次纯翻译重试
        if CJK_RE.search(positive):
            positive = await self._retranslate_tags(provider, positive, router_model)
        if negative and CJK_RE.search(negative):
            negative = await self._retranslate_tags(provider, negative, router_model)
        return {"positive": positive, "negative": negative}

    async def _call_llm_messages(
        self, provider, system: str, messages: list[dict],
        model: str = "", temperature: float | None = None,
    ) -> str:
        """通过多轮 messages 数组调 LLM（system 只注入一次，后续只追加 user/assistant）。

        优先尝试 provider.chat(messages) 或 provider.request；都没有则回退 text_chat
        （把 history 拼进 prompt，system 仍每次发但 DeepSeek 前缀缓存命中相同 system）。
        """
        full_messages = [{"role": "system", "content": system}] + messages
        for method_name in ("chat", "request"):
            method = getattr(provider, method_name, None)
            if method is None or not callable(method):
                continue
            try:
                if method_name == "chat":
                    kw = {"messages": full_messages}
                    if model:
                        kw["model"] = model
                    if temperature is not None:
                        kw["temperature"] = temperature
                    resp = await method(**kw)
                else:
                    body = {"messages": full_messages}
                    if model:
                        body["model"] = model
                    if temperature is not None:
                        body["temperature"] = temperature
                    resp = await method("POST", "/v1/chat/completions", body)
                if hasattr(resp, "completion_text"):
                    return resp.completion_text or ""
                if isinstance(resp, dict):
                    choices = resp.get("choices") or []
                    if choices:
                        return str(choices[0].get("message", {}).get("content", ""))
                return str(resp)
            except Exception:  # noqa: BLE001
                continue
        # 回退 text_chat：把 messages 历史拼进 prompt
        history_text = "\n\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:300]}"
            for m in messages[:-1]
        )
        prompt = messages[-1]["content"] if messages else ""
        if history_text:
            prompt = f"【历史上下文】\n{history_text}\n\n【当前需求】\n{prompt}"
        kw = {"prompt": prompt, "system_prompt": system}
        if model:
            kw["model"] = model
        if temperature is not None:
            kw["temperature"] = temperature
        try:
            resp = await provider.text_chat(**kw)
        except TypeError:
            if "temperature" in kw:
                kw.pop("temperature")
                resp = await provider.text_chat(**kw)
            else:
                raise
        if hasattr(resp, "completion_text"):
            return resp.completion_text or ""
        return str(resp)

    async def _text_chat_stream(self, provider, kwargs: dict, stream_state: dict | None):
        """尝试流式调用 provider.text_chat(stream=True)，逐 token 累积并推送。

        返回累积的完整文本；若 provider 不支持流式（抛 TypeError/返回非可迭代），返回 None
        由调用方回退一次性调用。stream_state 用于把实时文本暴露给 Web（打字机效果）。
        """
        if stream_state is None:
            return None
        try:
            resp = await provider.text_chat(**kwargs, stream=True)
        except TypeError:
            return None  # provider 不支持 stream 参数
        # 流式响应：async 可迭代（每项可能是 str / 带 completion_text 的对象 / dict choices）
        if not hasattr(resp, "__aiter__"):
            return None
        chunks: list[str] = []
        try:
            async for piece in resp:
                t = ""
                if isinstance(piece, str):
                    t = piece
                elif hasattr(piece, "completion_text"):
                    t = piece.completion_text or ""
                elif isinstance(piece, dict):
                    choices = piece.get("choices") or []
                    if choices:
                        t = str(choices[0].get("delta", {}).get("content") or "")
                if t:
                    chunks.append(t)
                    stream_state["text"] = "".join(chunks)
                    stream_state["ts"] = time.time()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 流式增强中断，使用已累积文本: {e}")
        stream_state["done"] = True
        return "".join(chunks) or None

    async def _retranslate_tags(self, provider, text: str, model: str) -> str:
        """把模型输出的中文提示词再翻译成英文 tag（一次重试，失败返回原文）。"""
        try:
            kwargs = {
                "prompt": f"把下面的提示词翻译成英文 tag（逗号分隔，只输出英文，不要解释）：\n{text}",
                "system_prompt": "你是提示词翻译器。只输出英文 tag，逗号分隔，禁止任何其他内容。",
            }
            if model:
                kwargs["model"] = model
            resp = await provider.text_chat(**kwargs)
            translated = ""
            if hasattr(resp, "completion_text"):
                translated = resp.completion_text or ""
            else:
                translated = str(resp)
            translated = translated.strip().strip('"').strip()
            if translated and len(translated) > 2 and not CJK_RE.search(translated):
                logger.info("[remote_link] 提示词已自动翻译为英文 tag")
                return translated[:2000]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 提示词翻译重试失败: {e}")
        return text

    async def _maybe_enhance_prompts(
        self, wf_name: str, params: dict, intent: str, event, subtype: str = "",
        image_paths: list | None = None,
    ) -> dict:
        """工作流含 PROMPT/NEGATIVE 占位符（或含文本提示词节点）时，把自然语言意图增强为正/负提示词。

        无占位符但含文本节点（CLIPTextEncode 等）的工作流同样增强：代理端会自动把
        正/负提示词填进识别出的提示词框，用户无需手工改造工作流。
        subtype 非空时使用该子分类配置的专属 LLM 与提示词模板（未配回退全局）。
        """
        caps = await self._get_capabilities()
        wf = next((w for w in caps.get("workflows") or [] if w.get("name") == wf_name), None)
        tokens = (wf or {}).get("tokens") or {}
        nodes = (wf or {}).get("nodes") or []
        text_capable = any(re.search(r"Text|String|Multiline|Prompt", str(t), re.I) for t in nodes)
        if not text_capable and "PROMPT" not in tokens and "NEGATIVE" not in tokens:
            return params
        need = [t for t in ("PROMPT", "NEGATIVE") if (t in tokens or text_capable)]
        need = [t for t in need if not str(params.get(t) or "").strip()]
        if not need:
            return params
        nsfw = bool(self.config.get("nsfw_enabled"))
        # Anima 适配段：仅当「图片类子分类」且「用户输入明确提到生成参数」时附加；
        # 用户没提参数 → 不附加，工作流用用户调好的默认参数，一个不动。
        anima_adapter = bool(SUBTYPE_CATEGORY.get(subtype) == "image") and self._param_intent_detected(intent)
        positive = negative = None
        st_cfg = (self._subtype_configs().get(subtype) or {}) if subtype else {}
        template = str(st_cfg.get("prompt_template") or "")
        st_model = str(st_cfg.get("llm_model") or "")
        # 增强必须走「子分类模板 LLM」链路（用户要求，不得跳过）：
        # - 有子分类模板 → 用该模板跑子分类 LLM（或全局路由 LLM）；
        # - 无子分类模板 → 用全局增强模板（enhance_system_prompt / 默认）同样走 LLM；
        # - 增强失败不再静默兜底用户原话（那会导致注入的不是正式提示词），重试一次后明确报错。
        if not self.config.get("router_enabled", True):
            raise RemoteLinkError("router_enabled 已关闭，无法执行提示词增强（生成任务依赖 LLM 增强）")
        provider = await self._resolve_router_provider(event, provider_id=st_cfg.get("llm_provider"))
        if provider is None:
            raise RemoteLinkError(
                f"提示词增强需要 LLM 提供商，但 router_provider 解析失败"
                f"（子分类 {subtype or '全局'}，配置的 provider={st_cfg.get('llm_provider') or self.config.get('router_provider')}）"
            )
        # 会话缓存：用户要求「保留对话记录，一直做分析」。
        # 对话按【子分类】组织（每个配置了模板的工作流对应一个对话）：
        # 「初始化」按钮会为所有已配置模板的子分类预创建对话（system 注入一次），
        # 之后生成任务沿用该对话（只追加 user/assistant），不重开新对话。
        ses_key = subtype or "default"
        ses = self._enhance_sessions.get(ses_key)
        if ses is None or ses.get("system") != template:
            ses = {"system": template, "history": []}
            self._enhance_sessions[ses_key] = ses
        history = ses.get("history") or []
        # 构建多轮 messages 数组（system 只在 messages[0] 注入一次，后续只追加 user/assistant）
        chat_messages = []
        for c in history[-6:]:
            chat_messages.append({"role": "user", "content": c["user"]})
            chat_messages.append({"role": "assistant", "content": c["assistant"][:500]})
        chat_messages.append({"role": "user", "content": intent})
        # 真流式：增强开始时暴露流状态（Web 打字机实时观看提示词生成）
        stream_state = self._enhance_streams.get(ses_key)
        if stream_state is None:
            stream_state = {"text": "", "done": False, "ts": time.time()}
            self._enhance_streams[ses_key] = stream_state
        stream_state.update({"text": "", "done": False, "ts": time.time()})
        # 先把用户意图记入对话（assistant 占位空）——Web 对话窗口立即显示"用户输入 + 生成中"，
        # 而不是等增强完成才出现（用户要求看到读取意图→输出的完整过程）。
        history.append({"user": intent, "assistant": "", "negative": ""})
        try:
            enhanced = await asyncio.wait_for(
                self._enhance_prompt(provider, intent, nsfw, template=template, model=st_model, chat_messages=chat_messages, image_urls=image_paths, stream_state=stream_state, anima_adapter=anima_adapter),
                timeout=float(self.config.get("router_timeout", 60)) + 30,
            )
            positive = enhanced.get("positive")
            negative = enhanced.get("negative")
            history[-1]["assistant"] = (positive or "")[:2000]
            history[-1]["negative"] = (negative or "")[:2000]
            stream_state["done"] = True
            self._save_enhance_sessions()  # 持久化：新轮次落盘
            logger.info(f"[remote_link] 提示词增强完成（{wf_name}，子分类={subtype or '全局'}，对话延续={len(history)}轮，messages={len(chat_messages)}条）")
        except Exception as e:  # noqa: BLE001
            stream_state["done"] = True
            history[-1]["assistant"] = f"（提示词增强失败：{e}）"
            self._save_enhance_sessions()  # 失败也落盘（用户可见失败原因）
            logger.warning(f"[remote_link] 提示词增强失败，重试一次: {e}")
            try:
                enhanced = await asyncio.wait_for(
                    self._enhance_prompt(provider, intent, nsfw, template=template, model=st_model, image_urls=image_paths, stream_state=None, anima_adapter=anima_adapter),
                    timeout=float(self.config.get("router_timeout", 60)) + 30,
                )
                positive = enhanced.get("positive")
                negative = enhanced.get("negative")
                history[-1]["assistant"] = (positive or "")[:2000]  # 重试成功覆盖占位
                history[-1]["negative"] = (negative or "")[:2000]
                self._save_enhance_sessions()
            except Exception as e2:  # noqa: BLE001
                raise RemoteLinkError(
                    f"提示词增强失败（已重试一次）：{e2}。未生成正式提示词，任务已中止，请稍后再试"
                ) from None
        if "PROMPT" in need:
            params["PROMPT"] = positive or intent
        if "NEGATIVE" in need:
            params["NEGATIVE"] = negative or DEFAULT_NEGATIVE
        return params

    def _raw_user_text(self, event) -> str:
        """取用户消息的原始文字（去掉 @ 机器人 / 引用 / 组件标记），供内生调度使用。

        外部 Agent 调用工具时可能主观改写 intent，内生流程必须以用户原话为准，
        这里直接从事件里取原始文本，避免加工内容污染分类与提示词增强。
        """
        text = (event.message_str or "").strip()
        text = re.sub(r"\[At:[^\]]*\]", "", text).strip()
        text = re.sub(r"\[.*?\]", "", text).strip()
        text = text.strip("@").strip()
        return text

    def _extract_event_images(self, event) -> list[str]:
        """从消息事件的消息链里提取图片（URL 或本地文件路径），供图生图/图生视频任务使用。

        兼容多种图片组件字段（url / file / path / raw）与 file:// 前缀；
        拿不到明确来源时也记录原始字段值，交由 _materialize_images 兜底下载。

        **包含引用（回复）段里的图片**：群里常见用法是「先发一张图 → 引用它 → 说要求」，
        此时图片在 Reply 组件的 chain 里而不在顶层消息链——不递归就会既取不到图，
        又因图片数为 0 让子分类路由误判成文生类（如图生视频退化成文生视频）。
        当前消息自带的图排在引用图之前（更贴近用户当下的意图），并按来源去重。
        """
        sources: list[str] = []

        def collect(chain, depth: int = 0) -> None:
            # ponytail: 深度上限 3 —— 引用套引用极少超过两层，够用且防成环
            if depth > 3:
                return
            for comp in chain or []:
                ctype = str(getattr(comp, "type", None) or "").lower()
                if "reply" in ctype:  # 引用段：图片在被引用消息的 chain 里
                    collect(getattr(comp, "chain", None), depth + 1)
                    continue
                if ctype not in ("image",):
                    continue
                cand = (
                    getattr(comp, "url", None)
                    or getattr(comp, "path", None)
                    or getattr(comp, "file", None)
                    or getattr(comp, "raw", None)
                )
                if not isinstance(cand, str) or not cand.strip():
                    continue
                c = cand.strip()
                if c.startswith("file://"):
                    c = c[len("file://"):]
                if c and c not in sources:  # 去重：同图重复会被误判成多图任务
                    sources.append(c)

        try:
            chain = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return sources
        # 两轮：先收顶层图（当前意图优先），再收引用段里的图
        collect([c for c in chain if "reply" not in str(getattr(c, "type", None) or "").lower()])
        collect([c for c in chain if "reply" in str(getattr(c, "type", None) or "").lower()])
        return sources

    async def _fetch_reply_images(self, event) -> list[str]:
        """兜底补拉引用消息里的图片：仅当消息里有引用段、但它的 chain 为空时才调用。

        AstrBot 解析 reply 段时会调协议端 get_msg 去填充 Reply.chain；一旦这步失败
        （日志里的「获取引用消息失败」/「(无法获取引用内容)」），Reply 就只剩一个 id，
        图片彻底拿不到——群友「先发图 → 引用它 → 说要求」就会退化成文生类。
        这里用同一个 id 自己再调一次 get_msg 把图捞回来。

        ponytail: 只认 OneBot 的 image 段（url/file），够覆盖 QQ 侧；其它协议端
        的自定义段留给上游 Reply.chain 正常路径处理。
        """
        try:
            chain = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return []
        # 只处理"有引用段但 chain 为空"的情况；chain 有内容时 _extract_event_images 已取到
        ids = [
            getattr(c, "id", None)
            for c in chain
            if "reply" in str(getattr(c, "type", None) or "").lower()
            and not (getattr(c, "chain", None) or [])
        ]
        ids = [i for i in ids if i not in (None, "", 0)]
        if not ids:
            return []
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return []
        out: list[str] = []
        for mid in ids[:2]:  # 最多补拉 2 条引用，避免刷 API
            try:
                data = await bot.call_action(action="get_msg", message_id=int(mid))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[remote_link] 补拉引用消息 {mid} 失败: {e}")
                continue
            for seg in (data or {}).get("message") or []:
                if not isinstance(seg, dict) or str(seg.get("type") or "").lower() != "image":
                    continue
                d = seg.get("data") or {}
                cand = d.get("url") or d.get("file") or d.get("path")
                if isinstance(cand, str) and cand.strip():
                    c = cand.strip()
                    if c.startswith("file://"):
                        c = c[len("file://"):]
                    if c not in out:
                        out.append(c)
        if out:
            logger.info(f"[remote_link] 引用消息 chain 为空，已通过 get_msg 补拉 {len(out)} 张图")
        return out

    async def _materialize_images(self, sources: list[str]) -> list[dict]:
        """把图片来源（URL / 本地路径）下载/读取为【服务器本地文件】，返回 [{path, base64, name}]。

        务实原则：图片一律落盘成本地文件再使用（识图传路径、注入工作流传 base64），
        不依赖 URL 中转。URL 只是下载入口，下载完成后即弃。
        """
        out: list[dict] = []
        in_dir = self._media_dir / "inputs"
        in_dir.mkdir(parents=True, exist_ok=True)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            for i, u in enumerate(sources[:6]):
                if not isinstance(u, str) or not u.strip():
                    continue
                u = u.strip()
                data = b""
                name = f"input_{i}.png"
                try:
                    if u.startswith(("http://", "https://")):
                        async with session.get(u, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as r:
                            if r.status != 200:
                                raise RemoteLinkError(f"下载图片失败 {u}: HTTP {r.status}")
                            data = await r.read()
                        # 从 URL 推断扩展名
                        tail = u.split("?")[0].rsplit("/", 1)[-1].lower()
                        if "." in tail:
                            name = f"input_{i}.{tail.rsplit('.', 1)[-1]}"
                    else:
                        # 本地路径（云端 AstrBot 所在服务器的文件）
                        p = Path(u)
                        if p.is_file():
                            data = p.read_bytes()
                            name = f"input_{i}{p.suffix or '.png'}"
                        else:
                            # 本地相对路径尝试常见目录
                            for base in (Path("/AstrBot/data"), Path("/root"), Path(".")):
                                cand = base / u
                                if cand.is_file():
                                    data = cand.read_bytes()
                                    name = f"input_{i}{cand.suffix or '.png'}"
                                    break
                            if not data:
                                raise RemoteLinkError(f"图片不存在: {u}")
                except RemoteLinkError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise RemoteLinkError(f"图片获取失败 {u}: {e}") from None
                if len(data) > 20 * 1024 * 1024:
                    raise RemoteLinkError(f"图片过大（>{len(data) // 1024 // 1024}MB）")
                if not data:
                    raise RemoteLinkError(f"图片内容为空: {u}")
                safe_name = Path(name).name
                path = in_dir / safe_name
                path.write_bytes(data)
                out.append({"path": str(path), "base64": base64.b64encode(data).decode("ascii"), "name": safe_name})
        return out

    async def _download_images_to_base64(self, urls: list) -> list[str]:
        """把图片 URL/本地路径下载并转成 base64（注入本地工作流的图片入口用）。"""
        out: list[str] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            for u in urls[:6]:
                if not isinstance(u, str) or not u.strip():
                    continue
                u = u.strip()
                if not u.startswith(("http://", "https://")):
                    # 本地路径（云端 AstrBot 所在服务器的文件）
                    p = Path(u)
                    if not p.is_file():
                        raise RemoteLinkError(f"本地图片不存在: {u}")
                    data = p.read_bytes()
                else:
                    try:
                        async with session.get(u, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as r:
                            if r.status != 200:
                                raise RemoteLinkError(f"下载图片失败 {u}: HTTP {r.status}")
                            data = await r.read()
                    except RemoteLinkError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        raise RemoteLinkError(f"下载图片失败 {u}: {e}") from None
                if len(data) > 20 * 1024 * 1024:
                    raise RemoteLinkError(f"图片过大（>{len(data) // 1024 // 1024}MB）: {u}")
                if not data:
                    raise RemoteLinkError(f"图片内容为空: {u}")
                out.append(base64.b64encode(data).decode("ascii"))
        return out

    # ==================== 异步任务队列（工具立即返回，后台执行，完成后主动推送） ====================

    def _queue_record(self, **kw) -> dict:
        """创建一条任务队列记录（内部结构）。"""
        return self.tasks.create_record(**kw)

    def _queue_update(self, rec: dict, **kw):
        self.tasks.update(rec, **kw)

    def _queue_snapshot(self) -> list[dict]:
        """对外队列快照（供 API / Web 展示）。提示词/意图/错误给完整内容，Web 端折叠展示。"""
        return self.tasks.snapshot()

    async def _send_to_origin(self, event, text: str = "", files: list[dict] | None = None) -> str:
        """把文本/产物主动推送到任务来源会话（后台任务完成后用）。

        返回发送结果描述（供队列记录诊断）。
        """
        try:
            if text:
                # v4.27 的 context.send_message 需要 MessageChain，传 str 会
                # 抛 "'str' object has no attribute 'chain'" → 所有主动推送静默失败。
                from astrbot.core.message.message_event_result import MessageChain

                await self.context.send_message(event.unified_msg_origin, MessageChain().message(text))
            if files:
                ok = await self._send_media_direct(event, files)
                return "产物已发送" if ok else "产物发送失败（_send_media_direct 返回 False）"
            return "文本已发送" if text else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 主动推送失败: {e}")
            return f"推送异常: {e}"

    async def _send_done_note(self, event, kind_label: str) -> None:
        """产物发送后，@ 提出要求的用户说一句极简确认（如「@某人 图片已生成完毕」）。

        不再发清单/提示词/参数/历史记录之类多余内容（用户要求：直接发图 + 一句话）。
        """
        try:
            from astrbot.core.message.message_event_result import MessageChain

            sender_id = ""
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                pass
            chain = []
            if sender_id:
                try:
                    chain.append(Comp.At(qq=sender_id))
                except Exception:  # noqa: BLE001
                    pass
            chain.append(Comp.Plain(f"{kind_label}已生成完毕"))
            await self.context.send_message(
                event.unified_msg_origin, MessageChain(chain=chain)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] @发送完成确认失败，回退纯文本: {e}")
            await self._send_to_origin(event, text=f"{kind_label}已生成完毕")

    async def _run_task_background(self, rec: dict, intent: str, workflow_hint: str, event,
                                   image_urls: list | None, video_urls: list | None,
                                   decision: dict | None = None, pre_images: list | None = None):
        """后台执行任务（run_local_task 的异步壳），完成后更新队列并主动推送。"""
        self._queue_update(rec, status="running", started_at=time.time())
        try:
            # 先做路由+提示词增强（这样 manual 确认模式可以先展示提示词）
            image_sources = list(image_urls or [])
            if event is not None:
                image_sources = image_sources + self._extract_event_images(event)
                if not image_sources:  # 引用段 chain 为空时兜底补拉（NapCat get_msg 失败场景）
                    image_sources = image_sources + await self._fetch_reply_images(event)
            # 图片一律落盘为本地文件（识图传路径、注入传 base64，不用 URL 中转）
            if pre_images is not None:
                images = list(pre_images)
            else:
                images = []
                if image_sources:
                    images = await self._materialize_images(image_sources)
            if decision is None:
                decision = await self._route_local_task(intent, workflow_hint, event, len(image_sources))
            if decision.get("kind") == "llm":
                # 纯文本任务：直接执行，无确认流程
                result = await self._run_local_task_core(
                    intent, workflow_hint, event, image_urls, video_urls, decision=decision,
                    pre_images=images,
                )
            else:
                wf_name = decision.get("workflow") or ""
                subtype = str(decision.get("subtype") or "")
                params = decision.get("params") or {}
                # 有图时增强带图（多模态子 LLM 看图+文字+模板 → 提示词）
                params = await self._maybe_enhance_prompts(
                    wf_name, params, intent, event, subtype,
                    image_paths=[im["path"] for im in images],
                )
                prompt = str(params.get("PROMPT") or "")
                rec.update(prompt=prompt, workflow=wf_name, subtype=subtype)
                # 人工确认模式：先展示提示词，等用户确认后才执行
                if str(self.config.get("prompt_confirm_mode", "auto")) == "manual":
                    await self._ask_confirm(event, rec, intent, workflow_hint, image_urls, video_urls, params, decision, images)
                    return
                result = await self._run_local_task_core(
                    intent, workflow_hint, event, image_urls, video_urls,
                    decision=decision, pre_params=params, pre_images=images,
                )
            files = result.get("files") or []
            kind = result.get("kind", "")
            self._queue_update(
                rec, status="success", finished_at=time.time(), files=[
                    dict(f) for f in files  # 保留完整元数据（含 subfolder/type，供按需拉取）
                ],
            )
            # 主动推送：直接发产物 + 一句极简确认（用户要求：不要清单/提示词/参数/历史话术）
            if kind == "llm":
                txt = result.get("text") or ""
                send_note = await self._send_to_origin(event, text=txt[:4000])
            else:
                origin = str(getattr(event, "unified_msg_origin", "") or "")
                self.tasks.set_recent(origin, {
                    "task_id": rec["task_id"],
                    "sender": self._event_sender_id(event),
                })
                explain = result.get("explanation") or ""
                if files:
                    if rec.get("web_submit"):
                        # Web 控制台提交：产物不发会话（无会话），只更新队列（Web 可见/可下载/可发送到QQ）
                        send_note = f"产物已就绪（{len(files)} 个），可在控制台查看/下载/发送到QQ"
                    else:
                        # 1) 直接发送产物（从本地实时拉取直发；rec 传入以便记录具体失败原因）
                        sent_ok = await self._send_media_direct(event, files, rec=rec)
                        # 2) 一句话确认：@用户 图片/视频/音频已生成完毕
                        kind_label = {
                            "image": "图片", "video": "视频", "audio": "音频",
                        }.get(files[0].get("kind"), "产物")
                        await self._send_done_note(event, kind_label)
                        send_note = "产物已发送" if sent_ok else "产物发送失败（见日志）"
                else:
                    send_note = ""
                    await self._send_to_origin(event, text=f"⚠️ {explain or '执行完成'}，但没有返回图片/视频。")
            if send_note:
                self._queue_update(rec, progress=send_note)
        except asyncio.CancelledError:
            self._queue_update(rec, status="failed", finished_at=time.time(), error="任务被取消")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[remote_link] 后台任务 {rec['task_id']} 失败")
            self._queue_update(rec, status="failed", finished_at=time.time(), error=str(e))
            await self._send_to_origin(event, text=f"❌ 任务失败：{e}")

    async def _ask_confirm(self, event, rec: dict, intent: str, workflow_hint: str,
                           image_urls, video_urls, params: dict, decision: dict,
                           images: list | None = None):
        """人工确认：把增强后的提示词发给用户，挂起任务等确认（QQ 回复或 Web 审批）。"""
        self._queue_update(rec, status="waiting_confirm")
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        pending = {
            "rec": rec,
            "intent": intent,
            "workflow_hint": workflow_hint,
            "image_urls": image_urls,
            "video_urls": video_urls,
            "params": params,
            "decision": decision,
            "event": event,
            "task_id": rec["task_id"],
            "images": images or [],
        }
        self.tasks.set_confirmation(origin, pending)
        label = SUBTYPE_LABELS.get(rec.get("subtype") or "", rec.get("workflow") or "")
        prompt = str(params.get("PROMPT") or params.get("prompt") or "")[:500]
        msg = (
            f"🛡️ 任务 {rec['task_id']} 待确认\n"
            f"类别：{label}\n"
            f"工作流：{rec.get('workflow')}\n"
            f"提示词：{prompt or '（无）'}\n\n"
            f"回复【确认】执行，回复【取消】放弃。"
        )
        try:
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(origin, MessageChain().message(msg))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 发送确认询问失败: {e}")
            # 发不出确认就直接执行（避免任务卡死）
            self.tasks.clear_confirmation(origin, rec["task_id"])
            self._queue_update(rec, status="running")
            await self._run_task_background(rec, intent, workflow_hint, event, image_urls, video_urls, decision)

    async def _execute_pending(self, pending: dict):
        """执行一个待确认任务（QQ 确认 或 Web 审批通过 共用）。"""
        rec = pending["rec"]
        self._queue_update(rec, status="running", started_at=time.time())
        try:
            result = await self._run_local_task_core(
                pending["intent"], pending["workflow_hint"], pending["event"],
                pending["image_urls"], pending["video_urls"],
                decision=pending["decision"], pre_params=pending["params"],
                pre_images=pending.get("images") or None,
            )
            files = result.get("files") or []
            kind = result.get("kind", "")
            self._queue_update(
                rec, status="success", finished_at=time.time(), files=[dict(f) for f in files],
            )
            if kind == "llm":
                await self._send_to_origin(pending["event"], text=(result.get("text") or "")[:4000])
            else:
                if files:
                    await self._send_media_direct(pending["event"], files, rec=rec)
                    kind_label = {
                        "image": "图片", "video": "视频", "audio": "音频",
                    }.get(files[0].get("kind"), "产物")
                    await self._send_done_note(pending["event"], kind_label)
        except asyncio.CancelledError:
            self._queue_update(rec, status="failed", finished_at=time.time(), error="任务被取消")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[remote_link] 确认后执行失败 {rec['task_id']}")
            self._queue_update(rec, status="failed", finished_at=time.time(), error=str(e))
            await self._send_to_origin(pending["event"], text=f"❌ 任务失败：{e}")

    async def _handle_confirm_reply(self, event):
        """全局消息监听：捕获对待确认任务的回复（确认/取消）。"""
        if not self.tasks.confirm_pending:
            return False
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        pending = self.tasks.confirm_pending.get(origin)
        if pending is None:
            return False
        text = (event.message_str or "").strip().lower()
        # 去掉 @ 和空格
        text = re.sub(r"\[.*?\]", "", text).strip()
        confirmed = any(k in text for k in ("确认", "确定", "ok", "好的", "继续", "执行", "yes", "y"))
        cancelled = any(k in text for k in ("取消", "不要", "no", "算了", "停止"))
        if not confirmed and not cancelled:
            return False
        self.tasks.clear_confirmation(origin, pending.get("task_id"))
        rec = pending["rec"]
        if cancelled:
            self._queue_update(rec, status="failed", finished_at=time.time(), error="用户取消")
            await self._send_to_origin(pending["event"], text=f"❌ 任务 {rec['task_id']} 已取消")
            return True
        # 确认：执行
        await self._execute_pending(pending)
        return True

    def submit_local_task(self, intent: str, workflow_hint: str, event,
                          image_urls: list | None = None, video_urls: list | None = None,
                          decision: dict | None = None, pre_images: list | None = None) -> dict:
        """提交异步任务：入队 + 后台执行，立即返回队列记录（不阻塞工具调用）。

        防重：同一会话已有「排队中/运行中」的生成任务时，直接复用旧记录，不重复提交
        （外部群聊 Agent 可能与直接指令接管同时触发，避免重复生成）。
        pre_images：已落盘的图片（[{path, base64, name}]），供 Web/本地 GUI 上传的图直接用。
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.time()
        # 防重：只拦截「同一条消息的重复提交」——同一会话 60 秒内、相同意图 才视为重复
        # （外部群聊 Agent 与直接指令接管可能对同一条消息同时触发）。
        # 不同意图的新任务正常入队（同一会话可排队多个任务）。
        for rec in self.tasks.queue:
            if (
                rec.get("origin") == origin
                and rec.get("status") in ("queued", "running", "waiting_confirm")
                and rec.get("intent") == intent
                and now - float(rec.get("created_at") or 0) < 60
            ):
                logger.info(f"[remote_link] 防重：会话 {origin} 60 秒内已有相同任务 {rec['task_id']}，跳过重复提交")
                return rec
        rec = self._queue_record(
            status="queued", intent=intent, workflow=workflow_hint or "",
            origin=origin,
        )
        loop = asyncio.get_running_loop()
        loop.create_task(
            self._run_task_background(rec, intent, workflow_hint, event, image_urls, video_urls, decision, pre_images)
        )
        return rec

    async def _run_local_task_core(
        self, intent: str, workflow_hint: str, event,
        image_urls: list | None, video_urls: list | None,
        decision: dict | None = None, pre_params: dict | None = None,
        pre_images: list | None = None,
    ) -> dict:
        """run_local_task 的执行核心（路由可预计算传入，供确认模式复用）。"""
        image_sources = list(image_urls or [])
        if event is not None:
            image_sources = image_sources + self._extract_event_images(event)
            if not image_sources:  # 引用段 chain 为空时兜底补拉（NapCat get_msg 失败场景）
                image_sources = image_sources + await self._fetch_reply_images(event)
        # 图片一律落盘为本地文件（务实：不用 URL 中转，识图传路径、注入传 base64）
        if pre_images is not None:
            images = list(pre_images)
        else:
            images = []
            if image_sources:
                images = await self._materialize_images(image_sources)
        if decision is None:
            decision = await self._route_local_task(intent, workflow_hint, event, len(image_sources))
        explanation = decision.get("explanation", "")
        if decision.get("kind") == "llm":
            params = decision.get("params") or {}
            prompt = params.get("prompt") or intent
            model = params.get("model") or ""
            text = await self.local_llm_chat(prompt, model)
            return {"kind": "llm", "workflow": "", "explanation": explanation, "files": [], "text": text}
        wf_name = decision.get("workflow") or ""
        subtype = str(decision.get("subtype") or "")
        params = pre_params if pre_params is not None else (decision.get("params") or {})
        if pre_params is None:
            # 有图片时传给子 LLM 识图（多模态一次调用：看图+文字+模板 → 提示词）
            params = await self._maybe_enhance_prompts(
                wf_name, params, intent, event, subtype,
                image_paths=[im["path"] for im in images],
            )
        inject: dict = {}
        if images:
            inject["images"] = [im["base64"] for im in images]
        if video_urls:
            inject["videos"] = await self._download_images_to_base64(list(video_urls))
        timeout = float(self.config.get("comfyui_timeout", 600)) + 60
        result = await self._call_local(
            "comfyui",
            {
                "action": "run_workflow",
                "workflow": wf_name,
                "params": params,
                "inject": inject,
                "timeout": int(timeout - 60),
            },
            timeout=timeout,
        )
        files = self._save_media(result.get("files") or [])
        return {
            "kind": "comfyui",
            "workflow": wf_name,
            "explanation": explanation,
            "files": files,
            "texts": result.get("texts") or [],
            "captions": self._media_captions(files),
            "text": "",
            "prompt_used": str(params.get("PROMPT") or ""),
        }

    # ==================== 全局消息监听（人工确认回复 / 产物选择） ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_any_message(self, event: AstrMessageEvent):
        """监听用户消息：捕获对待确认任务的回复（确认/取消）、产物数字选择/自然语言取产物。

        生成请求的两级处理：
        - 直接指令式（祈使句+产物词，如"生成一张猫图"）：字面意图 100% 明确，
          插件直接接管（第 3 步），不经过外部 Agent 判断；
        - 委婉/模糊表达：交给外部群聊 Agent 判断是否调用 remote_local_compute 工具，
          调用后完全由插件内生逻辑完成（分类 → 选工作流 → 子分类模板 LLM 生成提示词
          → 注入 → 执行 → 产物自动发送），外部 Agent 不再参与。
        内生流程一律使用用户原始消息（_raw_user_text），不用 Agent 加工内容。

        handler 为 async generator：yield 出的 MessageEventResult 会交给框架发送
        （call_handler 里 event.set_result(ret)）；yield True 表示「已拦截」，不再走 LLM。
        """
        # 1) 人工确认回复
        if self.tasks.confirm_pending:
            try:
                handled = await self._handle_confirm_reply(event)
                if handled:
                    yield True  # 已消费，拦截
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[remote_link] 确认回复处理失败: {e}")
        # 2) 产物数字选择 / 自然语言取产物（用户回复 1/2/3、"全部"或"把视频发给我"）
        try:
            async for res in self._handle_media_select(event):
                if res is True:
                    yield True  # 已消费，拦截
                    return
                yield res  # MessageEventResult → 框架发送（图片/视频/音频/文本）
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 产物选择处理失败: {e}")
        # 3) 直接指令式生成请求：字面祈使句（如"生成一张猫图""帮我画个猫"）
        #    或口语化「再来一张/换个风格」——意图明确是生成新的，插件直接接管。
        try:
            if self._is_direct_generation_command(event):
                raw = self._raw_user_text(event)
                intent = self._generation_intent(event, raw)
                rec = self.submit_local_task(intent, "", event)
                logger.info(f"[remote_link] 直接指令接管 → 任务 {rec['task_id']}: {intent[:60]}")
                try:
                    event.stop_event()  # 阻止外部群聊 Agent 重复处理
                except Exception:  # noqa: BLE001
                    pass
                yield event.plain_result("✅ 已收到，正在本地生成…完成后自动发送～")
                yield True
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[remote_link] 直接指令接管失败: {e}")

    def _is_direct_generation_command(self, event) -> bool:
        """判断是否为「直接指令式」生成请求：@ 了机器人/唤醒前缀 + 生成意图。

        两类命中（任一即视为生成请求，插件直接接管，不经外部 Agent 判断）：
        A. 以生成动词开头的祈使句 + 含产物词（如"生成一张猫图""帮我画个猫"）；
        B. 口语化「再/换/新」表达（如"再来一张""换个风格""重新生成"）——
           字面语义是"生成新的"，不是索取历史产物。
        仅限 @ 机器人 + 明确生成语义，避免误伤普通聊天（如"这个视频很好看"）。
        """
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        text = self._raw_user_text(event)
        if not text or text.startswith(("/", "remote ")):
            return False
        # A. 祈使开头（@ 机器人 + 生成动词开头即视为生成请求）。
        # 不再强制"含产物词"——真实需求如「生成一位身穿战术服的军械少女，手持一把枪械」
        # 没有图/画/图片等字样，旧判定会漏判，进而被「取历史产物」逻辑抢走发旧图。
        prefixes = (
            "生成", "画", "做", "制作", "来一张", "来段", "来张", "来首", "来一首", "来一个",
            "给我生成", "帮我生成", "给我画", "帮我画", "帮我做", "给我做", "请生成", "请画",
            "创建", "设计", "写一首", "做一段", "生成一段", "换一个", "给我来",
        )
        if any(text.startswith(p) for p in prefixes):
            return True
        # B. 口语化「再/换/新」：再来一张 / 换个风格 / 重新生成 / 换张图 …
        #    需要明确是"生成新的"语义，且（含产物词 或 最近有产物可参考类型）。
        media = (
            "图片", "图", "照片", "插画", "头像", "壁纸", "海报", "封面",
            "视频", "动画", "短片", "音乐", "音频", "歌曲", "配音", "音效", "画", "曲",
        )
        rephrases = (
            "再来一张", "再来张", "再来个", "再来一", "再来一段", "再来首",
            "再画", "再生成", "再做一个", "再做个",
            "换一个", "换个", "换张", "换一", "换风格", "换种",
            "重新生成", "重新来", "重来", "重新做",
            "来张新的", "来一张新的", "新的图片", "新的图", "别的图", "另来一张",
        )
        if any(r in text for r in rephrases):
            if any(m in text for m in media):
                return True
            # 没带产物词（如"再来一张"）：只要最近有产物可参考类型，也视为生成
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            if self._find_recent_files(origin):
                return True
        return False

    def _generation_intent(self, event, raw: str) -> str:
        """生成意图文本：口语化表达（如"再来一张"）没带产物词时，参考最近任务类型补全，
        让路由能正确分类（图片/视频/音频）。
        """
        media = (
            "图片", "图", "照片", "插画", "头像", "壁纸", "海报", "封面",
            "视频", "动画", "短片", "音乐", "音频", "歌曲", "配音", "音效", "画", "曲",
        )
        if any(m in raw for m in media):
            return raw
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        recent = self._find_recent_files(origin)
        if not recent:
            return raw
        recent_kind = recent[0].get("kind")
        if recent_kind == "video":
            return f"生成一段视频：{raw}"
        if recent_kind == "audio":
            return f"生成一段音频：{raw}"
        return f"生成一张图片：{raw}"

    def _recent_meta(self, origin: str) -> dict:
        """按会话找最近完成任务的记录（task_id + 发起者 sender_id）。"""
        return self.tasks.recent_meta(origin)

    def _find_recent_files(self, origin: str) -> list[dict]:
        """按会话找最近完成任务的产物文件列表（带 path）。"""
        return self.tasks.find_recent_files(origin)

    @staticmethod
    def _event_sender_id(event) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _handle_media_select(self, event):
        """从最近完成任务的产物里选择并发送（事件链 yield，绕开自动发送 bug）。

        支持三种交互（图片/视频/音频通用）：
          - 数字：回复 1/2/3…选第 N 个产物；
          - 「全部」：一次发完；
          - 自然语言：如「把最新的视频发给我」「看看图片」——直接命中最近任务的对应类型产物，
            避免交给 LLM agent 乱猜路径/起临时服务。
        发送前先 _pull_media 从本地电脑实时拉取真实文件，而不是发服务器上可能损坏的缓存副本。
        """
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        files = self._find_recent_files(origin)
        if not files:
            return  # 未消费：不是对最近产物的选择回复
        # 权限校验：只有「提出任务的那个人」能取产物（回复数字/全部/把视频发给我）；
        # 群友发普通数字（如"1"）且没有 @ 机器人时，不拦截、不误判为选产物。
        meta = self._recent_meta(origin)
        is_owner = bool(meta.get("sender")) and self._event_sender_id(event) == meta.get("sender")
        if not is_owner and not getattr(event, "is_at_or_wake_command", False):
            return  # 非任务发起者且未 @ 机器人 → 普通聊天消息，不拦截
        text = (event.message_str or "").strip()
        # 去掉 @ 与空格
        text = re.sub(r"\[.*?\]", "", text).strip().lower()
        if not text:
            return

        async def _send_files(picked: list[dict], note: str = "") -> None:
            """拉取并发送选中的产物（async gen 内部辅助，只能 yield 到外层）。"""
            sent = 0
            for f in picked:
                f = await self._pull_media(f)
                if not f.get("path"):
                    continue
                sent += 1
                for res in self._yield_media_result(event, f):
                    yield res
            yield event.plain_result(note or f"📎 已发送 {sent} 个产物")

        # 1) 自然语言：发/看 + 类型词（优先判断，避免「视频」等词进普通聊天）
        KIND_WORDS = {
            "视频": "video",
            "录像": "video",
            "图片": "image",
            "图像": "image",
            "照片": "image",
            "音频": "audio",
            "声音": "audio",
            "音乐": "audio",
        }
        want_kind = next((k for w, k in KIND_WORDS.items() if w in text), None)
        # 排除生成类表达：复用 _is_direct_generation_command 的完整判定，
        # 而不是在这里维护第二份关键词表——「帮我生成一张图片」同时含产物词「图片」和
        # 「给/发」类动词，旧的短名单（换/再来/重新…）拦不住，会被误判成索取历史产物，
        # 于是新任务直接回了上一次的旧图。
        if self._is_direct_generation_command(event):
            return  # 交给生成接管（见 _on_any_message 的直接指令分支）
        if any(g in text for g in ("换", "再来", "重新", "再画", "再生成", "重来")):
            return  # 交给生成接管（_is_direct_generation_command）
        want_send = bool(re.search(r"(发|给|看看|看下|给我看|拿|取)", text)) or "最新" in text
        if want_send and want_kind:
            picked = [f for f in files if f.get("kind") == want_kind]
            if picked:
                # 多个同类产物：优先带音频版视频/主产物，其余按原顺序
                if want_kind == "video":
                    picked = sorted(
                        picked,
                        key=lambda f: (
                            0 if str(f.get("filename", "")).endswith("-audio") else 1,
                            0 if f.get("main") else 1,
                            f.get("index", 0),
                        ),
                    )
                else:
                    picked = sorted(picked, key=lambda f: (0 if f.get("main") else 1, f.get("index", 0)))
                async for res in _send_files(picked, f"📎 已发送 {len(picked)} 个{'视频' if want_kind == 'video' else '图片' if want_kind == 'image' else '音频'}产物"):
                    yield res
                yield True
                return
            # 最近任务没有该类型产物：提示但不拦截消息？——提示并拦截，避免重复问
            yield event.plain_result(f"⚠️ 最近任务里没有{'视频' if want_kind == 'video' else '图片' if want_kind == 'image' else '音频'}产物")
            yield True
            return

        # 2) 「全部」/「都发」
        if text in ("全部", "全部发", "都发", "全发", "全部发过来", "全发过来"):
            async for res in _send_files(files):
                yield res
            yield True
            return

        # 3) 数字：第 N 个
        m = re.match(r"^(?:第|#|号)?(\d{1,2})(?:个|号)?$", text)
        if not m:
            return  # 未消费：普通聊天消息，不拦截
        idx = int(m.group(1))
        if idx < 1 or idx > len(files):
            yield event.plain_result(f"❌ 编号超出范围（1~{len(files)}），回复「全部」可一次查看")
            yield True
            return
        f = await self._pull_media(files[idx - 1])
        if not f.get("path"):
            yield event.plain_result(f"❌ 产物 {f.get('filename')} 拉取失败（本地代理未连接或文件已删除）")
            yield True
            return
        for res in self._yield_media_result(event, f):
            yield res
        yield event.plain_result(f"📎 {f.get('filename')}")
        yield True

    @staticmethod
    def _yield_media_result(event, f: dict):
        """按产物类型生成对应消息结果：图片 / 视频 / 音频。

        视频优先用文件路径直发；失败则回退到媒体 API URL（QQ 支持从 URL 拉取）。
        """
        kind = f.get("kind")
        path = f["path"]
        if kind == "image":
            yield event.image_result(path)
        elif kind == "audio":
            try:
                yield event.chain_result([Comp.Audio.fromFileSystem(path=path)])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[remote_link] 音频组件发送失败，回退提示: {e}")
                yield event.plain_result(f"🔊 音频已生成：{f.get('filename')}（当前平台不支持直接发送）")
        else:
            # NapCat(OneBot) 与 AstrBot 文件系统隔离：视频必须用 URL（NapCat 从 URL 下载上传）
            try:
                yield event.chain_result([Comp.Video.fromURL(self._media_file_url(path))])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[remote_link] 视频 fromURL 失败，尝试文件: {e}")
                try:
                    yield event.chain_result([Comp.Video.fromFileSystem(path=path)])
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[remote_link] 视频组件全部失败: {e2}")
                    yield event.plain_result(f"🎬 视频已生成：{f.get('filename')}（发送失败，可在控制台查看）")

    async def run_local_task(
        self,
        intent: str,
        workflow_hint: str,
        event,
        image_urls: list | None = None,
        video_urls: list | None = None,
    ) -> dict:
        """同步执行入口（供 /remote 指令等需要同步结果的路径用）。"""
        return await self._run_local_task_core(intent, workflow_hint, event, image_urls, video_urls)

    # ==================== 指令解析辅助 ====================

    def _args_after(self, event: AstrMessageEvent, *markers: str) -> list[str]:
        """从原始消息文本中取出指定命令路径之后的参数（shlex 解析，支持引号）。"""
        raw = (event.message_str or "").strip()
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if not tokens:
            return []
        # 唤醒前缀（如 /）只可能出现在第一个 token 上
        if tokens[0].startswith("/") and not markers[0].startswith("/"):
            tokens[0] = tokens[0][1:]
        ml = [m.lower() for m in markers]
        for i in range(len(tokens) - len(ml) + 1):
            if [t.lower() for t in tokens[i : i + len(ml)]] == ml:
                return tokens[i + len(ml) :]
        return []

    # ==================== 指令 ====================

    @filter.command_group("remote")
    def remote(self):
        """远程隧道指令组。"""

    @remote.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看本地代理与本地模型服务的连接状态。"""
        try:
            info = await self._call_local("info", {}, timeout=15)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ 本地代理未连接：{e}")
            return
        lines = [f"🖥️ 本地主机: {info.get('hostname', '?')}（{info.get('platform', '?')}）"]
        comfy = info.get("comfyui") or {}
        if comfy.get("ok"):
            devs = comfy.get("devices") or []
            dev_str = ", ".join(
                f"{d.get('name', '?')} {d.get('vram_free_gb', '?')}/{d.get('vram_total_gb', '?')}GB"
                for d in devs
            ) or "无 GPU 信息"
            lines.append(f"🎨 ComfyUI: ✅ 在线 | {dev_str}")
        else:
            lines.append(f"🎨 ComfyUI: ❌ {comfy.get('error', '离线')}")
        oai = info.get("openai") or {}
        if oai.get("ok"):
            models = oai.get("models") or []
            lines.append(f"🤖 本地 LLM(OpenAI兼容): ✅ 在线 | 模型: {', '.join(models[:8])}")
        else:
            lines.append(f"🤖 本地 LLM: ❌ {oai.get('error', '离线')}")
        if self.tunnel.agent_connected_at:
            lines.append(f"⏱️ 已连接 {int(time.time() - self.tunnel.agent_connected_at)} 秒")
        yield event.plain_result("\n".join(lines))

    @remote.command("ping")
    async def cmd_ping(self, event: AstrMessageEvent):
        """测试与本地电脑的隧道往返延迟。"""
        try:
            t0 = time.time()
            await self._call_local("ping", {}, timeout=10)
            ms = int((time.time() - t0) * 1000)
            yield event.plain_result(f"🏓 隧道畅通，往返延迟约 {ms} ms")
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")

    @remote.group("wf")
    def wf(self):
        """工作流相关指令。"""

    @wf.command("list")
    async def cmd_wf_list(self, event: AstrMessageEvent):
        """自适应读取本地 ComfyUI 保存的全部工作流。"""
        yield event.plain_result("正在读取本地 ComfyUI 的工作流…")
        try:
            result = await self._call_local("workflows", {}, timeout=20)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")
            return
        wfs = result.get("workflows") or []
        if not wfs:
            yield event.plain_result(
                "本地没有读到任何工作流：请确认代理配置了 comfyui.userdata_dir"
                "（ComfyUI 安装目录下的 user 文件夹）"
            )
            return
        lines = [f"📁 本地工作流（{len(wfs)} 个）："]
        for w in wfs[:30]:
            fmt = "API" if w.get("format") == "api" else ("UI" if w.get("format") == "ui" else "?")
            lines.append(f"· {w.get('name')}  [{fmt}]  {w.get('relpath', '')}")
        if len(wfs) > 30:
            lines.append(f"…以及另外 {len(wfs) - 30} 个")
        yield event.plain_result("\n".join(lines)[:4000])

    @remote.command("do")
    async def cmd_do(self, event: AstrMessageEvent):
        """智能调用本地能力：/remote do <想做什么>（LLM 自动选工作流/本地模型并填参数，可选 wf=工作流名 指定）"""
        args = self._args_after(event, "remote", "do")
        if not args:
            yield event.plain_result(
                "用法: /remote do <想做什么>\n"
                "例如: /remote do 画一只赛博朋克的猫，蒸汽波风格\n"
                "      /remote do 生成 5 秒的星空延时视频\n"
                "      /remote do 用本地模型解释什么是熵\n"
                "可用 wf=工作流名 指定工作流（/remote wf list 查看）"
            )
            return
        wf_hint = ""
        if args[0].startswith("wf="):
            wf_hint = args[0].split("=", 1)[1]
            args = args[1:]
        if not args:
            yield event.plain_result("❌ 任务描述不能为空")
            return
        intent = " ".join(args)
        if self.config.get("notify_on_start", True):
            yield event.plain_result("🧠 正在分析意图、匹配本地能力…")
        try:
            result = await self.run_local_task(intent, wf_hint, event)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ 执行失败：{e}")
            return
        if result.get("explanation"):
            yield event.plain_result(f"💡 {result['explanation']}")
        if result.get("kind") == "llm":
            yield event.plain_result((result.get("text") or "")[:4000])
            return
        for cap in result.get("captions") or []:
            yield event.plain_result(cap)
        if not result.get("files"):
            yield event.plain_result("⚠️ 执行完成但未返回图片/视频")
        else:
            async for res in self._deliver_files(event, result["files"]):
                yield res
        for t in result.get("texts") or []:
            yield event.plain_result(f"📄 {t[:4000]}")

    @remote.group("comfyui")
    def comfyui(self):
        """本地 ComfyUI 相关指令。"""

    @comfyui.command("list")
    async def cmd_comfyui_list(self, event: AstrMessageEvent):
        """列出本地 ComfyUI 的模型（checkpoints）。"""
        yield event.plain_result("正在查询本地 ComfyUI 模型列表…")
        try:
            result = await self._call_local("comfyui", {"action": "checkpoints"}, timeout=20)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")
            return
        ckpts = result.get("checkpoints") or []
        if not ckpts:
            yield event.plain_result("本地 ComfyUI 没有返回任何 checkpoint，请检查模型目录。")
            return
        text = "📦 本地 ComfyUI 模型：\n" + "\n".join(f"· {c}" for c in ckpts)
        yield event.plain_result(text[:4000])

    @comfyui.command("queue")
    async def cmd_comfyui_queue(self, event: AstrMessageEvent):
        """查看本地 ComfyUI 当前队列。"""
        try:
            result = await self._call_local("comfyui", {"action": "queue"}, timeout=20)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")
            return
        yield event.plain_result(
            f"🎨 ComfyUI 队列：运行中 {result.get('running', 0)} 个，排队中 {result.get('pending', 0)} 个"
        )

    @comfyui.command("gen")
    async def cmd_comfyui_gen(self, event: AstrMessageEvent):
        """文生图快捷指令：/remote comfyui gen <提示词>，整段文本都作为提示词（走智能调度）"""
        args = self._args_after(event, "remote", "comfyui", "gen")
        if not args:
            yield event.plain_result("用法: /remote comfyui gen <提示词>（提示词可含空格，无需引号）")
            return
        intent = " ".join(args)
        if self.config.get("notify_on_start", True):
            yield event.plain_result("🎨 已提交到本地 ComfyUI，正在生成…（大图可能需要几分钟）")
        try:
            result = await self.run_local_task(intent, "", event)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ 生成失败：{e}")
            return
        if not result.get("files"):
            yield event.plain_result("⚠️ 生成完成但未返回图片")
            return
        async for res in self._deliver_files(event, result["files"]):
            yield res

    @remote.command("llm")
    async def cmd_llm(self, event: AstrMessageEvent):
        """调用本地 LLM（OpenAI 兼容接口）：/remote llm <问题>，可用 model=xxx 指定模型"""
        args = self._args_after(event, "remote", "llm")
        if not args:
            yield event.plain_result("用法: /remote llm <问题> [model=模型名]")
            return
        model = ""
        tokens = [t for t in args]
        model_tokens = [t for t in tokens if t.startswith("model=")]
        if model_tokens:
            model = model_tokens[-1].split("=", 1)[1]
            tokens = [t for t in tokens if not t.startswith("model=")]
        prompt = " ".join(tokens)
        if not prompt:
            yield event.plain_result("❌ 问题不能为空")
            return
        yield event.plain_result("🤖 正在询问本地 LLM…")
        try:
            text = await self.local_llm_chat(prompt, model)
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")
            return
        yield event.plain_result(text[:4000])

    @remote.command("shell")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_shell(self, event: AstrMessageEvent):
        """【管理员】在本地电脑执行 shell 命令（需插件 enable_shell 与代理端 shell.enabled 都为 true）"""
        if not self.config.get("enable_shell"):
            yield event.plain_result(
                "❌ shell 功能未启用：请在插件配置中开启 enable_shell，"
                "并确认本地代理配置 shell.enabled=true。"
            )
            return
        args = self._args_after(event, "remote", "shell")
        if not args:
            yield event.plain_result("用法: /remote shell <命令>")
            return
        yield event.plain_result("⚙️ 正在本地执行…")
        try:
            result = await self.local_shell(" ".join(args))
        except RemoteLinkError as e:
            yield event.plain_result(f"❌ {e}")
            return
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        code = result.get("exit_code")
        text = f"退出码 {code}"
        if out:
            text += f"\nstdout:\n{out}"
        if err:
            text += f"\nstderr:\n{err}"
        yield event.plain_result(text[:4000])

    # ==================== 清理 ====================

    async def terminate(self):
        """插件被卸载/停用时的清理：停掉隧道服务端并结束所有等待中的请求。"""
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._server_task = None
        self._fail_all_pending("插件已停用")
