"""OpenAI 兼容 Adapter：Ollama / LM Studio / vLLM。"""
from __future__ import annotations

from typing import Any

from aiohttp import ClientTimeout

from .base import Adapter, AdapterInfo


class OpenAICompatibleAdapter(Adapter):
    def __init__(self, session, base_url: str, api_key: str = "") -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def healthcheck(self) -> dict:
        try:
            async with self.session.get(self.base_url + "/models", headers=self.headers, timeout=ClientTimeout(total=3)) as r:
                if r.status != 200:
                    return {"ok": False, "detail": f"http {r.status}"}
                data = await r.json()
                models = [m.get("id") or m.get("name") for m in data.get("data") or data.get("models") or []]
                return {"ok": True, "models": models}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}

    async def discover(self) -> AdapterInfo:
        health = await self.healthcheck()
        return AdapterInfo(id="openai", metadata={"base_url": self.base_url, **health})

    async def capabilities(self) -> list[str]:
        health = await self.healthcheck()
        return ["llm.chat"] if health.get("ok") else []

    async def execute(self, capability: str, payload: dict[str, Any]) -> Any:
        raise NotImplementedError("OpenAI 转发请继续使用 local_agent.svc_openai（v0.2 兼容层）")
