# -*- coding: utf-8 -*-
"""缓存 key 含 subfolder 的检查：Anima_v7 与 Anima_v10 同名产物不得串。"""
import asyncio
import base64
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))  # 让 astrbot_plugin_remote_link 包可导入

from astrbot_plugin_remote_link.main import RemoteLinkPlugin  # noqa: E402

CACHE = Path(r"D:\Temp\_media_cache_test")
shutil.rmtree(CACHE, ignore_errors=True)
CACHE.mkdir(parents=True)


class _P(RemoteLinkPlugin):
    def __init__(self):
        self._media_dir = CACHE
        self.requests = []

    async def _call_local_stream(self, service, payload, on_chunk=None, timeout=300):
        self.requests.append(payload)
        data = b"V7IMG" if payload.get("subfolder") == "Anima_v7" else b"V10IMG"
        if on_chunk:
            on_chunk(base64.b64encode(data).decode())
        return {"ok": True}


async def main():
    p = _P()
    f7 = {"filename": "精修完成_x.png", "subfolder": "Anima_v7", "type": "output"}
    f10 = {"filename": "精修完成_x.png", "subfolder": "Anima_v10", "type": "output"}

    r7 = await p._pull_media(f7)
    r10 = await p._pull_media(f10)

    assert r7["path"] != r10["path"], "两个子目录的同名产物缓存路径不能相同"
    assert Path(r7["path"]).read_bytes() == b"V7IMG", "v7 缓存内容不对"
    assert Path(r10["path"]).read_bytes() == b"V10IMG", "v10 缓存内容不对"
    assert len(p.requests) == 2, f"应请求 2 次 media_get，实际 {len(p.requests)}"

    r10b = await p._pull_media(f10)
    assert Path(r10b["path"]).read_bytes() == b"V10IMG"
    assert len(p.requests) == 2, "v10 第二次应命中缓存不再请求"

    print("CACHE SUBFOLDER KEY PASS")


asyncio.run(main())
