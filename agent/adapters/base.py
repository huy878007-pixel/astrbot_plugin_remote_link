"""统一 Adapter 接口：能力发现、健康检查、能力列表与执行。

v0.2.0 只建立最小接口；适配器实现应保持轻量、可 mock、不引入重量级依赖。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterInfo:
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Adapter(abc.ABC):
    """本地能力适配器的基类。"""

    @abc.abstractmethod
    async def discover(self) -> AdapterInfo:
        """发现适配器当前可用的基础信息（无副作用，确定性探测优先）。"""
        raise NotImplementedError

    @abc.abstractmethod
    async def healthcheck(self) -> dict:
        """轻量健康检查，返回 {"ok": bool, "detail": ...}。"""
        raise NotImplementedError

    @abc.abstractmethod
    async def capabilities(self) -> list[str]:
        """返回该适配器当前提供的能力 ID 列表。"""
        raise NotImplementedError

    async def execute(self, capability: str, payload: dict[str, Any]) -> Any:
        """执行能力。默认抛出不支持；具体适配器按需覆盖。"""
        raise NotImplementedError(f"adapter {type(self).__name__} does not implement execute")
