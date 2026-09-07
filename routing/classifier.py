"""任务分类与 JSON 提取等纯路由工具。"""
from __future__ import annotations

import json
import re

DEFAULT_ROUTER_PROMPT = """你是云信互联的任务分类器。根据用户意图判断任务类型（不要选具体工作流，也不要写提示词）。
只输出一个 JSON 对象，不要输出任何其他文字。格式：
{"kind":"image|video|audio|text","explanation":"一句话理由"}
规则：
1. 画图/生成图片/照片/插画/头像/壁纸/表情包等视觉图像任务 → image；
2. 生成视频/动画/短片/动态图等任务 → video；
3. 生成音乐/音频/歌曲/配音/音效等任务 → audio；
4. 问答/解释/翻译/总结/写作/计算等纯文本任务 → text；
5. 拿不准时选最接近的一类，并在 explanation 说明理由。"""


def param_intent_detected(intent: str) -> bool:
    """判断用户输入是否【明确提到】生成参数（分辨率/步数/CFG/采样器/种子/宽高比等）。"""
    t = (intent or "").lower()
    patterns = (
        r"\d{3,4}\s*[x×*]\s*\d{3,4}",   # 1920×1080
        r"\d+\s*:\s*\d+",                # 9:16 宽高比
        r"步\s*数|迭代|steps|step",
        r"\bcfg\b|引导|指引",
        r"采样|sampler|调度|scheduler",
        r"种子|seed",
        r"分辨|尺寸|像素|resolution|宽高比|比例|aspect",
        r"\d+(?:\.\d+)?\s*p\b|4k|2k|8k|1080p|720p|高清|超清",
    )
    return any(re.search(p, t, re.I) for p in patterns)


def extract_json(text: str) -> dict | None:
    """从 LLM 输出里提取第一个合法 JSON 对象（dict）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    candidates = [m.group(0) for m in re.finditer(r"\{[^{}]*\}", text, re.S)]
    for cand in reversed(candidates):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            continue
    return None
