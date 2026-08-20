"""LLM 工具构建器：remote_local_compute —— 万能本地能力调用入口。

用户意图用自然语言描述，云端 LLM 调本工具后，插件侧的调度器会：
  1. 拉取本地能力清单（工作流+参数+checkpoint+LLM 模型，动态发现、自动适应新增）；
  2. 交给 LLM 智能决策：选工作流还是本地大模型、挑模型、填参数；
  3. 严格校验后经隧道执行，产物（图片/视频）直接发回聊天。
"""

import astrbot.api.message_components as Comp
from astrbot.api import FunctionTool


def build_compute_tool(plugin) -> FunctionTool:
    async def handler(event, intent: str = "", workflow_hint: str = "", image_urls=None, video_urls=None):
        # 用户要求：外部 Agent 只负责「决定是否调用本工具」；一旦调用，
        # 执行一律使用用户消息的【原始原话】（从 event 直接取），
        # 不使用外部 Agent 可能主观改写/加工过的 intent 参数——
        # 内生循环（分类 → 选工作流 → 子 LLM 提示词增强）必须基于用户原话。
        raw = plugin._raw_user_text(event)
        real_intent = raw or intent  # 纯图片/无文字消息时退回外部 Agent 填写的描述
        # 异步任务：提交后立即返回，后台执行完成后主动推送产物到会话。
        # （AstrBot 工具层对 LLM 工具调用有 ~120s 硬超时，长任务（视频）必须异步化）
        # 向用户反馈一句"已接收"（不暴露 task_id/参数，保持简洁）。
        rec = plugin.submit_local_task(real_intent, workflow_hint, event, image_urls, video_urls)
        yield event.plain_result("✅ 已收到你的需求，正在调度本地工作流生成…完成后自动发送～")
        yield f"任务 {rec['task_id']} 已受理"  # 工具返回值给外部 Agent（不发用户）

    return FunctionTool(
        name="remote_local_compute",
        description=(
            "调用用户本地电脑上的模型完成生成类任务（画图/生成图片/生成视频/生成音乐/修图/换装等）。"
            "\n【唯一判断标准·必须照做】只根据用户消息的字面内容判断："
            "用户是否明确提出了生成类请求（包含画/生成/制作/做一张/做一段/来一张/修图/换图/视频/音乐/"
            "音频/头像/壁纸/海报等生成意图词汇，或明确说要用本地电脑出图/出视频）。"
            "只要字面上是生成类请求，就必须调用本工具，无论语气、上下文、情绪、时机如何——"
            "不要自己判断『用户是不是真的想要』『现在适不适合』『会不会冒犯』，禁止这类主观裁量；"
            "拿不准时也调用（本地生成是无害的）。不是生成类请求（如纯聊天/问答/查询）则不调用。"
            "\n【禁止】不要自己生成或改写内容；不要替用户决定画什么/改什么。"
            "intent 参数照抄用户消息原文（系统以用户原始消息为准）。"
            "\n调用后系统会自动完成：分类 → 选工作流 → 该工作流配套提示词模型按模板生成正式提示词"
            " → 本地执行 → 自动把产物发回群里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "用户想完成的任务的【原始描述，保持用户原话，不要翻译、不要改写、不要加词】。"
                        "例如用户说“画一只赛博朋克的猫”就填“画一只赛博朋克的猫”，"
                        "用户说“生成一个青年骑摩托车的视频”就填“生成一个青年骑摩托车的视频”。"
                        "系统会自行判断用哪个工作流、并交给对应的提示词模板处理。"
                        "只有问答/解释等纯文本任务才填用户的提问内容。"
                    ),
                },
                "workflow_hint": {
                    "type": "string",
                    "description": "已知要用的工作流名（可选，一般留空让系统自动判断）",
                },
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "需要处理的图片的 URL 列表（图生图/改图类任务时提供，"
                        "系统会下载并注入到目标工作流的图片入口）。"
                    ),
                },
                "video_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "需要处理的视频的 URL 列表（视频编辑类任务时提供，"
                        "系统会下载并注入到目标工作流的视频入口）。"
                    ),
                },
            },
            "required": ["intent"],
        },
        handler=handler,
    )
