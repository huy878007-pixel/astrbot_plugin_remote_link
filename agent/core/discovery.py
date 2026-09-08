"""确定性本地环境发现：不依赖 LLM，优先使用配置/进程/常见端口/HTTP 验证。

第一版避免扫描整个磁盘或猜测系统环境；每个探测都带超时与异常兜底。
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession, ClientTimeout

try:
    import psutil
except ImportError:  # 非 GUI 环境可不装
    psutil = None


def _system_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": socket.gethostname(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            info["ram_total_gb"] = round(vm.total / 1024**3, 2)
            info["ram_used_gb"] = round(vm.used / 1024**3, 2)
            info["cpu_count"] = psutil.cpu_count(logical=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        disk = shutil.disk_usage(".")
        info["disk_total_gb"] = round(disk.total / 1024**3, 2)
        info["disk_free_gb"] = round(disk.free / 1024**3, 2)
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        nvidia = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if nvidia.returncode == 0:
            gpus = []
            for line in nvidia.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")] if line.strip() else []
                if len(parts) >= 3:
                    gpus.append({
                        "name": parts[0],
                        "vram_total_gb": round(int(parts[1]) / 1024, 2),
                        "vram_free_gb": round(int(parts[2]) / 1024, 2),
                    })
            info["gpus"] = gpus
    except Exception:  # noqa: BLE001
        pass
    return info


async def _http_ok(session, url: str, timeout: float = 3.0, headers: dict | None = None) -> bool:
    try:
        async with session.get(url, headers=headers or {}, timeout=ClientTimeout(total=timeout)) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


async def _probe_comfyui(session, base_url: str | None) -> dict[str, Any]:
    candidates = []
    if base_url:
        candidates.append(base_url.rstrip("/"))
    candidates.append("http://127.0.0.1:8188")
    for url in dict.fromkeys(candidates):
        source = "configured" if base_url and url == base_url.rstrip("/") else "default_port"
        if await _http_ok(session, url + "/system_stats", timeout=2):
            return {
                "ok": True,
                "type": "comfyui",
                "provider_type": "comfyui",
                "base_url": url,
                "source": source,
                "workflows": 0,
            }
    return {
        "ok": False,
        "type": "comfyui",
        "provider_type": "comfyui",
        "base_url": candidates[0] if candidates else "",
        "source": "configured" if base_url else "default_port",
        "workflows": 0,
    }


def _infer_llm_provider(url: str) -> str:
    u = url.lower()
    if "11434" in u:
        return "ollama"
    if "1234" in u:
        return "lm_studio"
    return "openai_compatible"


async def _probe_openai_compatible(session, base_url: str, source: str = "configured") -> dict[str, Any]:
    url = base_url.rstrip("/") + "/models"
    try:
        async with session.get(url, timeout=ClientTimeout(total=2)) as r:
            if r.status == 200:
                data = await r.json()
                models = sorted({str(m.get("id") or m.get("name")) for m in data.get("data") or data.get("models") or []})
                return {
                    "ok": True,
                    "type": "llm",
                    "provider_type": _infer_llm_provider(base_url),
                    "base_url": base_url.rstrip("/"),
                    "source": source,
                    "models": models,
                }
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": False,
        "type": "llm",
        "provider_type": _infer_llm_provider(base_url),
        "base_url": base_url.rstrip("/"),
        "source": source,
        "models": [],
    }



async def _probe_ffmpeg() -> dict[str, Any]:
    path = shutil.which("ffmpeg")
    if not path:
        return {"ok": False, "detail": "ffmpeg not found in PATH"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        first = out.decode("utf-8", errors="ignore").splitlines()[0] if out else ""
        return {"ok": proc.returncode == 0, "source": "path", "path": path, "version": first}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


@dataclass
class DiscoveryResult:
    machine: dict[str, Any] = field(default_factory=dict)
    services: list[dict[str, Any]] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)
    brain: dict[str, Any] = field(default_factory=dict)
    scanned_at: float = field(default_factory=time.time)


class Discovery:
    """本地环境发现控制器。"""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._opts = self.config.get("discovery") or {}

    async def run(self, session=None) -> DiscoveryResult:
        if session is None:
            async with ClientSession() as own_session:
                return await self._run(own_session)
        return await self._run(session)

    async def _run(self, session) -> DiscoveryResult:
        machine = _system_info()
        services: list[dict[str, Any]] = []

        # 显式配置优先，再自动发现
        comfyui_cfg = self.config.get("comfyui") or {}
        comfyui = await _probe_comfyui(session, comfyui_cfg.get("base_url"))
        services.append(comfyui)

        llm_services = []
        openai_cfg = self.config.get("openai") or {}
        if openai_cfg.get("base_url"):
            llm_services.append(await _probe_openai_compatible(session, openai_cfg["base_url"], source="configured"))
        for svc in (self.config.get("openai_services") or [])[:8]:
            base = svc.get("base_url")
            if base and base not in [s.get("base_url") for s in llm_services]:
                llm_services.append(await _probe_openai_compatible(session, base, source="configured"))
        if not llm_services:
            for url in ("http://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1"):
                result = await _probe_openai_compatible(session, url, source="default_port")
                if result.get("ok"):
                    llm_services.append(result)
                    break
        services.extend(llm_services)

        ffmpeg = await _probe_ffmpeg()
        services.append(ffmpeg)

        # Service online != Capability available。
        # ComfyUI 的能力必须由工作流证据决定（LocalAgent 负责）；这里不直接注册生成能力。
        capabilities: list[str] = []
        if any(s.get("ok") and (s.get("models")) for s in llm_services):
            capabilities.append("llm.chat")
        if ffmpeg.get("ok"):
            capabilities.append("media.transcode")
        capabilities.append("system.inspect")

        issues: list[dict[str, str]] = []
        if comfyui.get("ok") and not comfyui.get("workflows"):
            issues.append({"severity": "warn", "message": "ComfyUI 在线，但尚未确认到工作流文件，具体生成能力待进一步验证。"})
        if any(s.get("ok") and not s.get("models") for s in llm_services):
            issues.append({"severity": "warn", "message": "本地 LLM 服务在线，但没有可用模型。"})

        return DiscoveryResult(
            machine=machine,
            services=services,
            capabilities=sorted(set(capabilities)),
            issues=issues,
            brain={
                "configured": bool(self.config.get("brain", {}).get("enabled")),
                "default_profile": (self.config.get("brain") or {}).get("default_profile", "default"),
            },
            scanned_at=time.time(),
        )
