# -*- coding: utf-8 -*-
"""误判检查：生成类请求不得被「取历史产物」拦截。

真实语料来自群里实际用法（含数字、含"图片/给/发"等触发词）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.main import RemoteLinkPlugin  # noqa: E402


class _MsgObj:
    def __init__(self):
        self.message = []


class _Event:
    def __init__(self, text):
        self.message_str = text
        self.message_obj = _MsgObj()
        self.is_at_or_wake_command = True
        self.unified_msg_origin = "test:Group:1"


def is_gen(text):
    p = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    p._find_recent_files = lambda origin: [{"kind": "image", "filename": "old.png"}]
    return RemoteLinkPlugin._is_direct_generation_command(p, _Event(text))


# 应判定为「生成」的语料（含产物词 + 含发/给等词，最容易被取产物逻辑抢走）
GEN = [
    "生成一张图片",
    "帮我生成一张图片",
    "给我画一张猫娘的图",
    "帮我生成一张图：香霖堂店内，森近霖之助递出迷你八卦炉",
    "生成一位身穿战术服的军械少女，手持一把枪械",
    "画3只猫",
    "生成1girl的图片",
    "帮我做一段视频",
    "再来一张",
    "换个风格的图",
    "生成一张初音未来在霓虹都市顶楼比耶的图片，赛璐珞风格",
]
# 应判定为「取历史产物」的语料（真正在索取已有产物）
FETCH = [
    "把视频发给我",
    "看看图片",
    "把最新的视频发给我",
    "给我看下那个图片",
]

bad_gen = [t for t in GEN if not is_gen(t)]
bad_fetch = [t for t in FETCH if is_gen(t)]

print("=== 应为生成但被漏判（会被误当取产物 → 发旧图） ===")
for t in bad_gen:
    print("  ❌", t)
print("=== 应为取产物但被误判成生成 ===")
for t in bad_fetch:
    print("  ❌", t)

assert not bad_gen, f"{len(bad_gen)} 条生成语料被漏判"
assert not bad_fetch, f"{len(bad_fetch)} 条取产物语料被误判"
print("GENERATION VS FETCH PASS")
