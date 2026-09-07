"""Task Manager 单元测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.task_manager import TaskManager  # noqa: E402


def test_create_update_snapshot():
    tm = TaskManager()
    rec = tm.create_record(intent="画猫", origin="group:1", status="queued")
    assert rec["task_id"].startswith("task-")
    assert rec["status"] == "queued"
    tm.update(rec, status="running", started_at=1)
    assert rec["status"] == "running"
    snap = tm.snapshot()
    assert snap[0]["task_id"] == rec["task_id"]
    assert snap[0]["status"] == "running"
    assert tm.find_by_task_id(rec["task_id"]) is rec


def test_confirmation_and_recent():
    tm = TaskManager()
    rec = tm.create_record(intent="x", origin="group:1")
    pending = {"task_id": rec["task_id"], "rec": rec, "origin": "group:1"}
    tm.set_confirmation("group:1", pending)
    assert tm.confirm_by_task[rec["task_id"]] == "group:1"
    tm.clear_confirmation("group:1", rec["task_id"])
    assert tm.confirm_pending == {}
    assert tm.confirm_by_task == {}

    tm.set_recent("group:1", {"task_id": rec["task_id"], "sender": "u1"})
    meta = tm.recent_meta("group:1")
    assert meta["task_id"] == rec["task_id"]
    assert tm.find_recent_files("group:1") == []


if __name__ == "__main__":
    test_create_update_snapshot()
    test_confirmation_and_recent()
    print("TASK MANAGER PASS")
