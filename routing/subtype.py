"""子分类路由：一级类别 + 图数规则 + 手动关键词。"""
from __future__ import annotations

SUBTYPES = [
    "text2image", "image2image", "prompt_analysis",
    "text2video", "image2video", "multi_image2video",
    "audio_gen",
]
SUBTYPE_LABELS = {
    "text2image": "文生图", "image2image": "图生图", "prompt_analysis": "画面分析提示词",
    "text2video": "文生视频", "image2video": "单图生视频", "multi_image2video": "多图生视频",
    "audio_gen": "音频生成",
}
SUBTYPE_CATEGORY = {
    "text2image": "image", "image2image": "image", "prompt_analysis": "image",
    "text2video": "video", "image2video": "video", "multi_image2video": "video",
    "audio_gen": "audio",
}
SUBTYPE_HINTS = {
    "text2image": ("文生图", "文转图", "text2image"),
    "image2image": ("图生图", "改图", "重绘", "image2image"),
    "prompt_analysis": ("画面分析", "提示词分析", "反推提示词", "prompt analysis"),
    "text2video": ("文生视频", "文转视频", "text2video"),
    "image2video": ("单图生视频", "图生视频", "image2video"),
    "multi_image2video": ("多图生视频", "multi_image2video", "多图视频"),
    "audio_gen": ("音频生成", "配乐", "音效", "audio_gen"),
}


def pick_subtype(kind: str, n_images: int, intent: str) -> str:
    """子分类规则（纯函数）：手动关键词 > 图数规则（带图→图生类，多图→多图生视频）。"""
    best = ("", 0)
    for st in SUBTYPES:
        for h in SUBTYPE_HINTS.get(st, ()):
            if h and h in intent and len(h) > best[1]:
                best = (st, len(h))
    if best[0]:
        return best[0]
    if kind == "image":
        return "image2image" if n_images > 0 else "text2image"
    if kind == "video":
        if n_images > 1:
            return "multi_image2video"
        if n_images == 1:
            return "image2video"
        return "text2video"
    if kind == "audio":
        return "audio_gen"
    return ""
