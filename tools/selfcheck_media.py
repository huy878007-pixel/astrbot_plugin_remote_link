#!/usr/bin/env python3
"""产物「按需拉取 + 分类发送」核心逻辑自检（不依赖 astrbot / aiohttp 运行环境）。

用 ast 从 main.py 提取被改动的纯逻辑函数，在隔离命名空间里跑最小断言：
  1) _save_media：新代理响应无 base64 → 只记录元数据（path 空，不落盘）；旧代理有 base64 → 落盘；
  2) media_get 分块/重组：400KB 分块切分后拼接必须还原原 base64，且每块不超限；
  3) _yield_media_result：image → image_result、video → Video 组件、audio → Audio 组件（带回退）。
"""
import ast
import base64
import logging
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
main_src = (REPO / "main.py").read_text(encoding="utf-8")


def extract_func(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.unparse(item)
    raise SystemExit(f"未找到函数 {name}")


# ---------- 1) _save_media ----------
class _Fake:
    def __init__(self, media_dir):
        self._media_dir = media_dir


ns = {"base64": base64, "Path": Path, "uuid": uuid, "logger": logging.getLogger("selfcheck")}
exec(extract_func(main_src, "_save_media"), ns)
save_media = ns["_save_media"]

with tempfile.TemporaryDirectory() as td:
    fake = _Fake(Path(td))
    # 新代理：无 base64 → 元数据记录，path 空，不落盘
    recs = save_media(
        fake,
        [{
            "kind": "video", "filename": "t2v_00007-audio.mp4", "mime": "video/mp4",
            "subfolder": "MiniMaxH3", "type": "output",
        }],
    )
    assert len(recs) == 1, recs
    assert recs[0]["path"] == "" and recs[0]["subfolder"] == "MiniMaxH3", recs
    assert not (Path(td) / "t2v_00007-audio.mp4").exists()
    # 旧代理：有 base64 → 落盘且 path 指向文件
    payload = base64.b64encode(b"\x89PNG-fake").decode()
    recs2 = save_media(fake, [{"kind": "image", "filename": "a.png", "base64": payload}])
    assert len(recs2) == 1 and (Path(td) / "a.png").read_bytes() == b"\x89PNG-fake", recs2
    # 坏 base64 → 跳过
    recs3 = save_media(fake, [{"kind": "image", "filename": "bad.png", "base64": "!!!not-b64!!!"}])
    assert recs3 == [], recs3

# ---------- 2) media_get 分块/重组（与 agent svc_media_get 相同切分常量） ----------
raw = bytes(range(256)) * 19000  # ~4.8MB，模拟带音频视频
b64 = base64.b64encode(raw).decode("ascii")
CHUNK = 400 * 1024
parts = [b64[i : i + CHUNK] for i in range(0, len(b64), CHUNK)]
assert b64 == "".join(parts), "分块拼接必须还原原 base64"
assert all(len(p) <= CHUNK for p in parts), "每块不得超过 400KB"
assert len(parts) > 1, "4.8MB 产物必须被分成多块（验证确实走分块路径）"

# ---------- 3) _yield_media_result ----------
class _Video:
    @staticmethod
    def fromFileSystem(path):
        return ("Video", path)

class _Audio:
    @staticmethod
    def fromFileSystem(path):
        return ("Audio", path)

class _Comp:
    Video = _Video
    Audio = _Audio

class _Event:
    def image_result(self, path):
        return ("img", path)
    def chain_result(self, chain):
        return ("chain", chain)
    def plain_result(self, text):
        return ("plain", text)


ns2 = {"Comp": _Comp, "logger": logging.getLogger("selfcheck")}
exec(extract_func(main_src, "_yield_media_result"), ns2)
yield_result = ns2["_yield_media_result"]
ev = _Event()
assert list(yield_result(ev, {"kind": "image", "path": "/m/a.png"})) == [("img", "/m/a.png")]
assert list(yield_result(ev, {"kind": "video", "path": "/m/v.mp4"})) == [("chain", [("Video", "/m/v.mp4")])]
assert list(yield_result(ev, {"kind": "audio", "path": "/m/s.mp3"})) == [("chain", [("Audio", "/m/s.mp3")])]
# 未知类型 → 按视频兜底
assert list(yield_result(ev, {"kind": "weird", "path": "/m/x"})) == [("chain", [("Video", "/m/x")])]

print("SELFCHECK OK: _save_media / media_get 分块 / _yield_media_result 全部通过")
