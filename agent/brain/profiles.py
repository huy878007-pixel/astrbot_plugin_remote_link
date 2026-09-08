"""Brain Profile：Local Agent 自己的 LLM 配置。

AstrBot 不负责保存或提供这些配置；YunxinAgent 半独立运行。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_PROFILE_ID = "default"

DEFAULT_LLM_PROFILES: list[dict[str, Any]] = [
    {
        "id": DEFAULT_PROFILE_ID,
        "name": "默认 Agent 模型",
        "provider_type": "openai_compatible",
        "base_url": "",
        "api_key": "",
        "model": "",
        "timeout": 60,
    }
]

DEFAULT_BRAIN = {
    "enabled": False,
    "default_profile": DEFAULT_PROFILE_ID,
}


@dataclass
class BrainProfile:
    id: str = DEFAULT_PROFILE_ID
    name: str = "默认 Agent 模型"
    provider_type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 60

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BrainProfile":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # not used below
        allowed = {"id", "name", "provider_type", "base_url", "api_key", "model", "timeout"}
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


def normalize_brain_config(cfg: dict) -> dict:
    """确保 cfg 内存在 llm_profiles 与 brain 两个字段（不覆盖已有值）。"""
    cfg.setdefault("llm_profiles", [dict(p) for p in DEFAULT_LLM_PROFILES])
    cfg.setdefault("brain", dict(DEFAULT_BRAIN))
    return cfg


def load_profiles(cfg: dict) -> list[BrainProfile]:
    normalize_brain_config(cfg)
    profiles_data = cfg.get("llm_profiles") or []
    if not profiles_data:
        profiles_data = [dict(DEFAULT_LLM_PROFILES[0])]
    return [BrainProfile.from_dict(p) for p in profiles_data if isinstance(p, dict)]


def get_default_profile(cfg: dict) -> BrainProfile | None:
    profiles = load_profiles(cfg)
    brain = cfg.get("brain") or DEFAULT_BRAIN
    default_id = brain.get("default_profile") or DEFAULT_PROFILE_ID
    for p in profiles:
        if p.id == default_id:
            return p
    return profiles[0] if profiles else None


def save_profiles(cfg: dict, profiles: list[BrainProfile], brain: dict | None = None) -> None:
    """把 Profile 写回 cfg 内存对象；由 LocalAgent/GUI 负责持久化整个 config。"""
    cfg["llm_profiles"] = [p.to_dict() for p in profiles]
    cfg["brain"] = brain or cfg.get("brain") or dict(DEFAULT_BRAIN)
