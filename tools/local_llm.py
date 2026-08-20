"""LLM 工具构建器：调用用户本地电脑上运行的 LLM（OpenAI 兼容接口）。"""

from astrbot.api import FunctionTool


def build_llm_tool(plugin) -> FunctionTool:
    async def handler(event, prompt: str, model: str = ""):
        # 快速失败：本地 LLM 未运行时直接返回（不再长时间卡在连接拒绝），
        # 返回值给外部 Agent 参考，不向用户刷屏。
        if not await plugin._local_llm_available():
            yield ("本地大模型当前未运行（Ollama/LM Studio 未启动或未配置）。"
                   "请不要再调用本工具；这类请求改由云端模型回答，或让用户先启动本地模型。")
            return
        try:
            text = await plugin.local_llm_chat(prompt, model)
        except Exception as e:  # noqa: BLE001
            yield f"本地 LLM 调用失败：{e}"
            return
        yield text[:4000]

    return FunctionTool(
        name="remote_llm_chat",
        description=(
            "调用用户本地电脑上运行的 LLM（Ollama / LM Studio 等 OpenAI 兼容接口）进行纯文本问答。"
            "【重要·必须遵守】仅当用户【明确要求】使用本地大模型/本地 Ollama/本地跑模型时才可调用本工具；"
            "其他任何情况都【禁止】调用——包括云端模型回答不了、需要更专业回答、随便试试等理由都不行。"
            "用户通常没有运行本地模型，调用会失败并浪费一轮。生成图片/视频/音乐等一律用 remote_local_compute。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "发给本地模型的问题或提示词"},
                "model": {"type": "string", "description": "本地模型名，留空使用默认模型"},
            },
            "required": ["prompt"],
        },
        handler=handler,
    )
