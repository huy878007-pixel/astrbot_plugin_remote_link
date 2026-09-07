"""Tunnel 模块：WebSocket 隧道生命周期、认证、pending 请求与流式回调。

目标：
- 业务代码不再直接操作 self._ws；
- 为未来多 Agent 保留 AgentConnection 概念；
- 当前仍只维护 current_agent。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import web

from .exceptions import RemoteLinkError
from .protocol import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION

logger = logging.getLogger("astrbot_plugin_remote_link")


@dataclass
class AgentConnection:
    """当前已连接的本地代理（未来多 Agent 时扩展为连接表）。"""

    ws: Any
    protocol_version: int = 1
    agent_version: str = ""
    machine: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    connected_at: float = field(default_factory=time.time)
    info: dict[str, Any] = field(default_factory=dict)
    protocol_error: str = ""

    @property
    def closed(self) -> bool:
        try:
            return self.ws is None or self.ws.closed
        except Exception:
            return True


class TunnelServer:
    """封装云端插件侧 WebSocket 隧道服务。

    main.py 只需调用：
        tunnel.request("comfyui", payload, timeout=...)
        tunnel.request_stream("openai", payload, on_chunk=...)
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._agent: AgentConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._chunk_cbs: dict[str, Callable[[str], Any]] = {}
        self._rate_hits: dict[str, deque] = {}
        self._run_progress: dict | None = None

    # ---------- 对外状态 ----------

    @property
    def current_agent(self) -> AgentConnection | None:
        return self._agent

    @property
    def connected(self) -> bool:
        return self._agent is not None and not self._agent.closed

    @property
    def agent_info(self) -> dict:
        return dict(self._agent.info) if self._agent else {}

    @property
    def agent_connected_at(self) -> float:
        return self._agent.connected_at if self._agent else 0.0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def progress(self) -> dict | None:
        return self._run_progress

    @property
    def auth_token(self) -> str:
        return self._ensure_auth_token()

    # ---------- 认证与限流 ----------

    def _ensure_auth_token(self) -> str:
        token = str(self._config.get("auth_token") or "").strip()
        if token:
            return token
        token = secrets.token_urlsafe(32)
        self._config["auth_token"] = token
        save = getattr(self._config, "save_config", None)
        try:
            if callable(save):
                save()
                logger.warning("[remote_link] 检测到 auth_token 为空，已自动生成并保存随机 token")
        except Exception as e:
            logger.error(f"[remote_link] auth_token 为空，已生成随机 token 但保存配置失败: {e}")
        return token

    def _rate_limit_key(self, request) -> str:
        ip = getattr(request, "remote", None) or "unknown"
        path = getattr(request, "path", None) or "/"
        return f"{path} {ip}"

    def check_rate_limit(self, request, limit: int = 60, window: int = 60) -> bool:
        key = self._rate_limit_key(request)
        now = time.time()
        q = self._rate_hits.setdefault(key, deque())
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def authorized(self, request) -> bool:
        """统一鉴权：默认 Bearer Token；旧版 ?token= 仅作 deprecated 兼容。"""
        token = self._ensure_auth_token()
        if request.headers.get("Authorization", "") == f"Bearer {token}":
            return True
        if request.query.get("token") == token:
            return True
        return False

    # ---------- WebSocket 生命周期 ----------

    async def handle_ws(self, request):
        if not self.check_rate_limit(request, limit=60, window=60):
            return web.Response(status=429, text="too many requests")
        if not self.authorized(request):
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse(max_msg_size=256 * 1024 * 1024, heartbeat=15)
        await ws.prepare(request)

        old_agent = self._agent
        if old_agent is not None and not old_agent.closed:
            try:
                await old_agent.ws.close(code=4001, message=b"replaced by new connection")
            except Exception:
                pass
            self.fail_all_pending("本地代理已重连，旧请求被取消")
        self._agent = AgentConnection(ws=ws)
        logger.info(f"[remote_link] 本地代理已连接: {request.remote}")

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self.handle_text(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"[remote_link] WebSocket 错误: {ws.exception()}")
                    break
                elif msg.type == web.WSMsgType.CLOSE:
                    logger.info(f"[remote_link] 客户端关闭连接: {msg.data}")
                    break
                elif msg.type == web.WSMsgType.CLOSING:
                    break
        except asyncio.CancelledError:
            logger.info("[remote_link] WS 循环被取消（插件停止/重载）")
            raise
        except Exception as e:
            logger.warning(f"[remote_link] WS 连接异常断开: {type(e).__name__}: {e}")
        finally:
            if self._agent is not None and self._agent.ws is ws:
                self._agent = None
            self.fail_all_pending("本地代理连接已断开")
            logger.info("[remote_link] 本地代理已断开")
        return ws

    async def handle_text(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = data.get("type")
        if mtype == "hello":
            self._update_hello(data)
        elif mtype == "response":
            fut = self._pending.pop(data.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
        elif mtype == "stream_chunk":
            cb = self._chunk_cbs.get(data.get("id"))
            if cb is not None:
                try:
                    cb(data.get("data", ""))
                except Exception as e:
                    logger.error(f"[remote_link] 流式回调异常: {e}")
        elif mtype == "progress":
            self._update_progress(data)

    def _update_hello(self, data: dict) -> None:
        info = data.get("data") or {}
        self._agent = self._agent or AgentConnection(ws=None)
        self._agent.info = info
        self._agent.connected_at = time.time()
        self._agent.agent_version = str(data.get("agent_version") or "")
        self._agent.machine = data.get("machine") or {}
        self._agent.capabilities = list(data.get("capabilities") or [])
        proto = data.get("v")
        try:
            proto = int(proto or 1)
        except (TypeError, ValueError):
            proto = 1
        self._agent.protocol_version = proto
        self._agent.protocol_error = ""
        if proto < MIN_PROTOCOL_VERSION or proto > PROTOCOL_VERSION:
            logger.error(
                f"[remote_link] 代理协议版本 {proto} 不受支持（需 {MIN_PROTOCOL_VERSION}-{PROTOCOL_VERSION}）"
            )
            self._agent.protocol_error = (
                f"Protocol version {proto} is not supported. Server requires protocol >=2."
            )
        elif proto == 1:
            logger.info("[remote_link] 检测到 v1 旧协议，按兼容模式继续")
        hostname = (
            info.get("hostname")
            or (self._agent.machine or {}).get("name")
            or "unknown"
        )
        platform_name = (
            info.get("platform")
            or (self._agent.machine or {}).get("os")
            or "unknown"
        )
        logger.info(
            f"[remote_link] 代理 hello v{proto}: {hostname} ({platform_name}) "
            f"capabilities={','.join(self._agent.capabilities)}"
        )

    def _update_progress(self, data: dict) -> None:
        raw_p = data.get("data")
        if isinstance(raw_p, dict):
            text = str(raw_p.get("text") or "")
            pct = raw_p.get("percent")
            node = str(raw_p.get("node") or "") or None
            node_type = str(raw_p.get("node_type") or "") or None
        else:
            text = str(raw_p or "")
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
            pct = float(m.group(1)) if m else None
            node = node_type = None
        self._run_progress = {
            "text": text[:200],
            "percent": pct,
            "node": node,
            "node_type": node_type,
            "rid": str(data.get("id") or ""),
            "updated_at": time.time(),
        }
        logger.info(f"[remote_link] 本地进度: {text}")

    def fail_all_pending(self, reason: str):
        for _rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RemoteLinkError(reason))
        self._pending.clear()
        self._chunk_cbs.clear()

    # ---------- 请求封装 ----------

    def ensure_connected(self):
        if self._agent is None or self._agent.closed:
            raise RemoteLinkError("本地代理未连接：请先在你本地电脑上运行 agent/local_agent.py")

    async def _send_request(self, service: str, payload: dict):
        self.ensure_connected()
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._agent.ws.send_str(
                json.dumps(
                    {"type": "request", "id": rid, "service": service, "payload": payload},
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self._pending.pop(rid, None)
            raise RemoteLinkError(f"向本地代理发送请求失败: {e}") from None
        return rid, fut

    async def request(self, service: str, payload: dict, timeout: float | None = None) -> dict:
        timeout = timeout if timeout is not None else float(self._config.get("request_timeout", 300))
        rid, fut = await self._send_request(service, payload)
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RemoteLinkError(
                f"本地代理响应超时（>{int(timeout)} 秒），任务可能仍在本地继续执行"
            ) from None
        finally:
            self._pending.pop(rid, None)
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = "未知错误"
            if isinstance(resp, dict):
                err = resp.get("error") or err
            raise RemoteLinkError(str(err))
        return resp.get("result") or {}

    async def request_stream(self, service, payload, on_chunk, timeout=None) -> dict:
        timeout = timeout if timeout is not None else float(self._config.get("request_timeout", 300))
        rid, fut = await self._send_request(service, payload)
        self._chunk_cbs[rid] = on_chunk
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RemoteLinkError(f"本地代理响应超时（>{int(timeout)} 秒）") from None
        finally:
            self._pending.pop(rid, None)
            self._chunk_cbs.pop(rid, None)
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = "未知错误"
            if isinstance(resp, dict):
                err = resp.get("error") or err
            raise RemoteLinkError(str(err))
        return resp.get("result") or {}
