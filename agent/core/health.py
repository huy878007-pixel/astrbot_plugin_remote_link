"""轻量 Health Monitor：周期性刷新注册表状态，不频繁扫描文件系统。"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("yunxin_health")


class HealthMonitor:
    def __init__(self, interval: float = 30.0) -> None:
        self.interval = interval
        self._checkers: list[tuple[str, Callable[[], Awaitable[dict]]]] = []
        self._last: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    def add_check(self, name: str, checker: Callable[[], Awaitable[dict]]) -> None:
        self._checkers.append((name, checker))

    async def check_all(self) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for name, checker in self._checkers:
            try:
                results[name] = await checker() or {}
            except Exception as e:  # noqa: BLE001
                results[name] = {"ok": False, "error": str(e)}
        self._last = results
        return results

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"health monitor check_all failed: {e}")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def snapshot(self) -> dict[str, dict]:
        return dict(self._last)
