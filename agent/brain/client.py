"""Brain Client：OpenAI Compatible 本地 LLM 调用。

本轮只支持非流式 chat completion，不用于执行 Shell/系统操作。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiohttp import ClientTimeout

from .profiles import BrainProfile

logger = logging.getLogger("yunxin_brain")


class BrainClient:
    def __init__(self, profile: BrainProfile, session: aiohttp.ClientSession | None = None) -> None:
        self.profile = profile
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=max(1, int(self.profile.timeout or 60)))
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def chat(self, prompt: str) -> str:
        session = await self._get_session()
        headers = {"Content-Type": "application/json"}
        api_key = (self.profile.api_key or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body: dict[str, Any] = {
            "model": self.profile.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        url = self.profile.base_url.rstrip("/") + "/chat/completions"
        async with session.post(url, headers=headers, json=body) as r:
            if r.status != 200:
                text = (await r.text())[:300]
                raise BrainClientError(self._classify_http_status(r.status), f"HTTP {r.status}: {text}")
            data = await r.json()
        choices = data.get("choices") or []
        if not choices:
            raise BrainClientError("incompatible", "响应中缺少 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise BrainClientError("incompatible", "响应缺少文本内容")
        return content.strip()

    async def test_connection(self) -> dict:
        started = time.time()
        try:
            content = await self.chat("请只返回字符串 YUNXIN_OK")
            latency = round((time.time() - started) * 1000)
            return {
                "ok": True,
                "category": "ok",
                "message": "API 可访问，模型可用，响应正常",
                "latency_ms": latency,
                "content": content[:200],
            }
        except asyncio.TimeoutError:
            return {"ok": False, "category": "timeout", "message": "请求超时"}
        except BrainClientError as e:
            return {"ok": False, "category": e.category, "message": str(e)}
        except aiohttp.ClientError as e:
            return {"ok": False, "category": "network", "message": f"网络失败: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "category": "unknown", "message": str(e)}

    @staticmethod
    def _classify_http_status(status: int) -> str:
        if status in (401, 403):
            return "unauthorized"
        if status == 404:
            return "model_not_found"
        if status >= 400:
            return "bad_request"
        return "unknown"


class BrainClientError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
