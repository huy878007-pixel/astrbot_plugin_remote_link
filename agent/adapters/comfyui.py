"""ComfyUI Adapter：确定性探测 /object_info、/system_stats，能力映射。"""
from __future__ import annotations

from typing import Any

from aiohttp import ClientTimeout

from .base import Adapter, AdapterInfo


class ComfyUIAdapter(Adapter):
    def __init__(self, session, base_url: str = "http://127.0.0.1:8188") -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, timeout: float = 3.0) -> tuple[int, Any]:
        async with self.session.get(self.base_url + path, timeout=ClientTimeout(total=timeout)) as r:
            try:
                return r.status, await r.json()
            except Exception:  # noqa: BLE001
                return r.status, None

    async def discover(self) -> AdapterInfo:
        status, _ = await self._get("/system_stats")
        return AdapterInfo(id="comfyui", metadata={"base_url": self.base_url, "ok": status == 200})

    async def healthcheck(self) -> dict:
        status, data = await self._get("/system_stats")
        return {"ok": status == 200, "detail": data if status == 200 else f"http {status}"}

    async def capabilities(self) -> list[str]:
        status, _ = await self._get("/system_stats")
        if status != 200:
            return []
        return ["image.generate", "image.edit", "video.generate"]

    async def execute(self, capability: str, payload: dict[str, Any]) -> Any:
        raise NotImplementedError("ComfyUI 执行请继续使用 local_agent.run_workflow（v0.2 兼容层）")
