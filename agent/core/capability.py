"""Capability 模型与注册表。

能力 ID 使用粗粒度最小集合：image.generate / image.edit / video.generate /
video.image_to_video / audio.generate / llm.chat / media.transcode / system.inspect。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 推荐的能力 ID 最小集合
CAPABILITY_IDS = {
    "image.generate",
    "image.edit",
    "video.generate",
    "video.image_to_video",
    "audio.generate",
    "llm.chat",
    "media.transcode",
    "system.inspect",
}


@dataclass
class Capability:
    id: str
    provider: str
    status: str = "unknown"  # ready / degraded / unavailable
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    last_healthcheck: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "status": self.status,
            "metadata": self.metadata,
            "evidence": self.evidence,
            "last_healthcheck": self.last_healthcheck,
        }



def capabilities_from_workflows(workflows: list[dict]) -> list[Capability]:
    """根据工作流证据确定性推导 ComfyUI 能力。

    规则：
    - outputs 含 image + 无图片输入 → image.generate
    - outputs 含 image + 有图片输入 → image.edit
    - outputs 含 video + 无图片输入 → video.generate
    - outputs 含 video + 有图片输入 → video.image_to_video
    - outputs 含 audio → audio.generate
    """
    caps: list[Capability] = []
    for wf in workflows or []:
        outputs = set(wf.get("outputs") or [])
        injects = wf.get("injects") or {}
        nodes = wf.get("nodes") or []
        has_image_input = int(injects.get("images", 0) or 0) > 0
        has_video_output = "video" in outputs or any(
            "SaveVideo" in str(n) or "VHS" in str(n) for n in nodes
        )
        evidence = {"type": "workflow", "workflow": wf.get("name") or wf.get("relpath") or ""}
        if "image" in outputs:
            caps.append(Capability(
                id="image.edit" if has_image_input else "image.generate",
                provider="comfyui", status="ready", evidence=evidence,
            ))
        if has_video_output:
            caps.append(Capability(
                id="video.image_to_video" if has_image_input else "video.generate",
                provider="comfyui", status="ready", evidence=evidence,
            ))
        if "audio" in outputs:
            caps.append(Capability(id="audio.generate", provider="comfyui", status="ready", evidence=evidence))
    return caps


class CapabilityRegistry:
    """维护 capability_id -> list[Capability]，支持多提供者。"""

    def __init__(self) -> None:
        self._capabilities: dict[str, list[Capability]] = {}

    def register(self, capability: Capability | dict) -> None:
        if isinstance(capability, dict):
            capability = Capability(**capability)
        if capability.id not in CAPABILITY_IDS:
            # 未来扩展允许自定义能力，这里只做记录不硬拒绝
            pass
        items = self._capabilities.setdefault(capability.id, [])
        for i, existing in enumerate(items):
            if existing.provider == capability.provider:
                items[i] = capability
                return
        items.append(capability)

    def unregister(self, capability_id: str, provider: str | None = None) -> int:
        items = self._capabilities.get(capability_id)
        if not items:
            return 0
        before = len(items)
        if provider is None:
            del self._capabilities[capability_id]
            return before
        remaining = [c for c in items if c.provider != provider]
        if remaining:
            self._capabilities[capability_id] = remaining
        else:
            self._capabilities.pop(capability_id, None)
        return before - len(remaining)

    def update_status(self, capability_id: str, provider: str, status: str, metadata: dict | None = None) -> None:
        for cap in self._capabilities.get(capability_id, []):
            if cap.provider == provider:
                cap.status = status
                cap.last_healthcheck = time.time()
                if metadata is not None:
                    cap.metadata.update(metadata)
                return
        # 不存在则自动注册一个
        self.register(Capability(id=capability_id, provider=provider, status=status,
                                 metadata=metadata or {}, last_healthcheck=time.time()))

    def get(self, capability_id: str) -> list[Capability]:
        return list(self._capabilities.get(capability_id, []))

    def ready(self, capability_id: str) -> list[Capability]:
        return [c for c in self.get(capability_id) if c.status == "ready"]

    def snapshot(self) -> dict[str, list[dict]]:
        return {cid: [c.to_dict() for c in caps] for cid, caps in self._capabilities.items()}

    def all_capability_ids(self) -> list[str]:
        return sorted(self._capabilities.keys())
