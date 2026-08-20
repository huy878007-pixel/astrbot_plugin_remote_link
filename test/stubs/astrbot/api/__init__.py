"""stubs.astrbot.api —— 模拟 AstrBotConfig / FunctionTool。"""

from dataclasses import dataclass, field


class AstrBotConfig(dict):
    """模拟 AstrBotConfig：dict 子类，与真实行为一致。"""


@dataclass
class FunctionTool:
    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)
    handler: object = None
