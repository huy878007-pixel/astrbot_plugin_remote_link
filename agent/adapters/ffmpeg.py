"""FFmpeg Adapter：检测 PATH 与版本。"""
from __future__ import annotations

import asyncio
import shutil

from .base import Adapter, AdapterInfo


class FFmpegAdapter(Adapter):
    async def discover(self) -> AdapterInfo:
        health = await self.healthcheck()
        return AdapterInfo(id="ffmpeg", metadata=health)

    async def healthcheck(self) -> dict:
        path = shutil.which("ffmpeg")
        if not path:
            return {"ok": False, "detail": "ffmpeg not found in PATH"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            version = out.decode("utf-8", errors="ignore").splitlines()[0] if out else ""
            return {"ok": proc.returncode == 0, "path": path, "version": version}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}

    async def capabilities(self) -> list[str]:
        health = await self.healthcheck()
        return ["media.transcode"] if health.get("ok") else []

    async def execute(self, capability: str, payload: dict[str, Any]) -> Any:
        raise NotImplementedError("FFmpeg 转码将在 v0.3+ 提供")
