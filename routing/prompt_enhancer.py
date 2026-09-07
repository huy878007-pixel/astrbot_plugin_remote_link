"""提示词增强相关纯工具。"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("astrbot_plugin_remote_link")

DEFAULT_ENHANCE_PROMPT = """你是 Stable Diffusion 生图提示词翻译器。把用户的自然语言描述改写成英文 tag 提示词。
只输出一个 JSON 对象，不要输出任何其他文字，格式：
{"positive":"英文正面提示词","negative":"英文负面提示词"}
硬性规则：
1. {policy}
2. positive 必须是【纯英文 tag】、逗号分隔，绝对禁止中文/日文或任何非英文字符；先写风格质量词（masterpiece, best quality, highly detailed, anime illustration），再把用户描述的全部要素翻译成英文 tag（主体/发色/瞳色/服装/表情/动作/场景/背景/光影/构图/镜头）；
3. negative 必须是【纯英文 tag】、逗号分隔，包含质量类负面词（worst quality, low quality, bad anatomy, bad hands, extra fingers, watermark, text, lowres, jpeg artifacts），再根据画面内容补充针对性负面词；
4. 违反英文要求的输出视为失败。"""

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


async def retranslate_tags(provider, text: str, model: str) -> str:
    """把模型输出的中文提示词再翻译成英文 tag（一次重试，失败返回原文）。"""
    try:
        kwargs = {
            "prompt": f"把下面的提示词翻译成英文 tag（逗号分隔，只输出英文，不要解释）：\n{text}",
            "system_prompt": "你是提示词翻译器。只输出英文 tag，逗号分隔，禁止任何其他内容。",
        }
        if model:
            kwargs["model"] = model
        resp = await provider.text_chat(**kwargs)
        translated = ""
        if hasattr(resp, "completion_text"):
            translated = resp.completion_text or ""
        else:
            translated = str(resp)
        translated = translated.strip().strip('"').strip()
        if translated and len(translated) > 2 and not CJK_RE.search(translated):
            logger.info("[remote_link] 提示词已自动翻译为英文 tag")
            return translated[:2000]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[remote_link] 提示词翻译重试失败: {e}")
    return text
