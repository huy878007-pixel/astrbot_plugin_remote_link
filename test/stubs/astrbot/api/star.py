"""stubs.astrbot.api.star —— 模拟 Context / Star。"""

import logging


class Context:
    def __init__(self):
        self.tools = []  # 记录注册的 LLM 工具
        self._provider = None

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def set_provider(self, provider):
        self._provider = provider

    def get_provider_by_id(self, provider_id=""):
        return self._provider

    async def get_using_provider_async(self, umo=None):
        return self._provider


class Star:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("remote_link_test")

    async def terminate(self):
        pass
