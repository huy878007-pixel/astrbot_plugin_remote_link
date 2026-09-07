"""Task Manager：统一管理任务队列、状态、确认、最近产物。

保持与现有 UI/页面兼容的状态命名：
    queued / analyzing / waiting_confirm / running / success / failed
未来若引入 cancelled，可通过映射兼容。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

# 状态常量（对外继续使用既有字符串，避免破坏页面）
STATUS_QUEUED = "queued"
STATUS_ANALYZING = "analyzing"
STATUS_WAITING_CONFIRM = "waiting_confirm"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 兼容映射：统一语义 → 既有页面状态字符串
CANONICAL_TO_LEGACY = {
    "queued": STATUS_QUEUED,
    "analyzing": STATUS_ANALYZING,
    "waiting_confirmation": STATUS_WAITING_CONFIRM,
    "running": STATUS_RUNNING,
    "completed": STATUS_SUCCESS,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
}


class TaskManager:
    def __init__(self, maxlen: int = 50) -> None:
        self._queue: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._confirm_pending: dict[str, dict] = {}
        self._confirm_by_task: dict[str, str] = {}
        self._recent_by_origin: dict[str, dict] = {}

    # ---------- 队列 ----------

    @property
    def queue(self) -> deque[dict]:
        return self._queue

    def create_record(self, **kw) -> dict:
        self._seq += 1
        rec: dict[str, Any] = {
            "id": self._seq,
            "task_id": f"task-{self._seq}",
            "status": kw.get("status", STATUS_QUEUED),
            "workflow": kw.get("workflow", ""),
            "subtype": kw.get("subtype", ""),
            "prompt": kw.get("prompt", ""),
            "intent": kw.get("intent", ""),
            "origin": kw.get("origin", ""),
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "files": [],
            "error": "",
            "progress": "",
        }
        self._queue.appendleft(rec)
        return rec

    def update(self, rec: dict, **kw) -> None:
        for k, v in kw.items():
            rec[k] = v

    def snapshot(self) -> list[dict]:
        out = []
        for r in self._queue:
            out.append(
                {
                    "task_id": r["task_id"],
                    "status": r["status"],
                    "workflow": r["workflow"],
                    "subtype": r["subtype"],
                    "prompt": r.get("prompt") or "",
                    "intent": r.get("intent") or "",
                    "origin": r.get("origin", ""),
                    "created_at": r.get("created_at"),
                    "started_at": r.get("started_at"),
                    "finished_at": r.get("finished_at"),
                    "files": [
                        {"kind": f.get("kind"), "filename": f.get("filename")}
                        for f in (r.get("files") or [])
                    ],
                    "error": r.get("error") or "",
                    "progress": r.get("progress", ""),
                }
            )
        return out

    def find_by_task_id(self, task_id: str) -> dict | None:
        for rec in self._queue:
            if rec.get("task_id") == task_id:
                return rec
        return None

    def find_media_record(self, filename: str) -> dict | None:
        for rec in self._queue:
            for f in rec.get("files") or []:
                if f.get("filename") == filename:
                    return f
        return None

    # ---------- 确认 ----------

    @property
    def confirm_pending(self) -> dict[str, dict]:
        return self._confirm_pending

    @property
    def confirm_by_task(self) -> dict[str, str]:
        return self._confirm_by_task

    def set_confirmation(self, origin: str, pending: dict) -> None:
        self._confirm_pending[origin] = pending
        self._confirm_by_task[pending["task_id"]] = origin

    def clear_confirmation(self, origin: str, task_id: str | None = None) -> None:
        self._confirm_pending.pop(origin, None)
        if task_id is not None:
            self._confirm_by_task.pop(task_id, None)
        else:
            # 按 origin 反查清理
            for tid, o in list(self._confirm_by_task.items()):
                if o == origin:
                    self._confirm_by_task.pop(tid, None)

    # ---------- 最近完成 ----------

    def set_recent(self, origin: str, meta: dict) -> None:
        self._recent_by_origin[origin] = meta

    def recent_meta(self, origin: str) -> dict:
        meta = self._recent_by_origin.get(origin) or {}
        task_id = meta.get("task_id") if isinstance(meta, dict) else meta
        for rec in self._queue:
            if rec.get("task_id") == task_id:
                return {"task_id": task_id, "sender": meta.get("sender", "")}
        return {}

    def find_recent_files(self, origin: str) -> list[dict]:
        meta = self.recent_meta(origin)
        if not meta:
            return []
        for rec in self._queue:
            if rec.get("task_id") == meta["task_id"]:
                return rec.get("files") or []
        return []
