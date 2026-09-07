"""旧 agent_config.json 读取时自动备份测试。"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from local_agent import load_config  # noqa: E402


def test_backup_without_overwrite():
    tmp = Path(tempfile.mkdtemp(prefix="yunxin_cfg_migrate_"))
    cfg_path = tmp / "agent_config.json"
    old = {
        "server_url": "ws://example/ws",
        "token": "keep-me",
        "shell": {"enabled": False},
        "my_unknown_field": [1, 2, 3],
    }
    cfg_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    loaded = load_config(str(cfg_path))
    # 不覆盖、不删除未知字段
    assert loaded["token"] == "keep-me"
    assert loaded["my_unknown_field"] == [1, 2, 3]
    backup = cfg_path.with_name(cfg_path.name + ".bak-v0.1.1")
    assert backup.exists(), "旧配置应自动备份"
    backup_data = json.loads(backup.read_text(encoding="utf-8-sig"))
    assert backup_data["token"] == "keep-me"

    # 再次读取不会重复备份
    backup.unlink()
    load_config(str(cfg_path))
    assert backup.exists()


if __name__ == "__main__":
    test_backup_without_overwrite()
    print("CONFIG MIGRATION PASS")
