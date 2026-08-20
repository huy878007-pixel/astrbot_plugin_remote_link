# -*- coding: utf-8 -*-
"""引用消息图片提取检查：群友「先发图 → 引用它 → 说要求」必须能取到图。

失败即说明 _extract_event_images 没有递归引用段（Reply.chain），
后果：图片数量为 0 → 子分类路由落到文生视频 → 且无图可注入。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))  # 让 astrbot_plugin_remote_link 包可导入

from astrbot.api.message_components import Image, Plain, Reply  # noqa: E402
from astrbot_plugin_remote_link.main import RemoteLinkPlugin  # noqa: E402


class _MsgObj:
    def __init__(self, chain):
        self.message = chain


class _Event:
    def __init__(self, chain, text=""):
        self.message_obj = _MsgObj(chain)
        self.message_str = text


def extract(chain, text=""):
    plugin = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    return RemoteLinkPlugin._extract_event_images(plugin, _Event(chain, text))


URL = "http://example.com/cat.jpg"

# 1) 直接带图（原本就支持）
got = extract([Image.fromURL(URL), Plain("做成视频")])
assert got == [URL], f"直接带图应取到 1 张，实际 {got}"

# 2) 引用一条图片消息 + 新要求（群友的实际用法）
got = extract([Reply(id=123, chain=[Image.fromURL(URL)]), Plain("用这张图做成视频")])
assert got == [URL], f"引用图片应取到 1 张，实际 {got}"

# 3) 引用图片 + 当前消息也带图 → 两张都要，且当前消息的图排前（更贴近用户当下意图）
CUR = "http://example.com/now.png"
got = extract([Reply(id=1, chain=[Image.fromURL(URL)]), Image.fromURL(CUR), Plain("首尾帧")])
assert got == [CUR, URL], f"当前图应排在引用图之前，实际 {got}"

# 4) 引用纯文本消息 → 不应误取
got = extract([Reply(id=2, chain=[Plain("昨天那个")]), Plain("再来一张")])
assert got == [], f"引用纯文本不该取到图，实际 {got}"

# 5) 去重：同一张图既在引用里又在当前消息里，只算一次（否则会被误判成多图生视频）
got = extract([Reply(id=3, chain=[Image.fromURL(URL)]), Image.fromURL(URL)])
assert got == [URL], f"重复图片应去重，实际 {got}"

# ---- 端到端：引用图片后，子分类必须路由到图生类而不是文生类 ----
from astrbot_plugin_remote_link.main import pick_subtype  # noqa: E402

# 引用一张图 + 要视频 → 单图生视频（修复前 n_images=0 会退化成 text2video）
n = len(extract([Reply(id=4, chain=[Image.fromURL(URL)]), Plain("做成视频")]))
assert n == 1, f"引用图应计数 1，实际 {n}"
assert pick_subtype("video", n, "做成视频") == "image2video", "引用图 + 视频应走单图生视频"

# 引用一张图 + 要图 → 图生图
assert pick_subtype("image", n, "改成雨天") == "image2image", "引用图 + 图应走图生图"

# 引用两张图 → 多图生视频
n2 = len(extract([Reply(id=5, chain=[Image.fromURL(URL), Image.fromURL(CUR)]), Plain("转场视频")]))
assert n2 == 2, f"引用两张图应计数 2，实际 {n2}"
assert pick_subtype("video", n2, "转场视频") == "multi_image2video", "两张引用图应走多图生视频"

# 没有图 → 仍是文生视频（不能误判）
n0 = len(extract([Plain("生成星空延时视频")]))
assert n0 == 0 and pick_subtype("video", n0, "生成星空延时视频") == "text2video"

print("REPLY IMAGE EXTRACT PASS")
