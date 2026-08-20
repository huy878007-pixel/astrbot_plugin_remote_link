"""stubs.astrbot.api.event —— 模拟 filter 装饰器与 AstrMessageEvent。"""


class AstrMessageEvent:
    """占位：冒烟测试不经过消息事件。"""


class _CommandGroup:
    def __init__(self, name):
        self.name = name

    def __call__(self, fn):
        # 真实 AstrBot 中 command_group 装饰器会把函数替换成带 .command/.group 的组对象
        self.fn = fn
        return self

    def command(self, name, **kwargs):
        def deco(fn):
            return fn

        return deco

    def group(self, name):
        return _CommandGroup(name)


class _Filter:
    class EventMessageType:
        ALL = "ALL"

    class PlatformAdapterType:
        AIOCQHTTP = 1

    class PermissionType:
        ADMIN = "ADMIN"

    @staticmethod
    def command(name, **kwargs):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def command_group(name, **kwargs):
        return _CommandGroup(name)

    @staticmethod
    def permission_type(t):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def on_astrbot_loaded():
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def llm_tool(**kwargs):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def event_message_type(t):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def event_type(*types):
        def deco(fn):
            return fn

        return deco


filter = _Filter()
