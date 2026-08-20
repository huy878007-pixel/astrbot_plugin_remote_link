"""stubs.astrbot.api.message_components —— 模拟消息组件（仅测试需要的最小面）。"""


class Image:
    @classmethod
    def fromFileSystem(cls, path=None, file=None):
        return cls()

    @classmethod
    def fromURL(cls, url):
        return cls()


class Video:
    @classmethod
    def fromFileSystem(cls, path=None, file=None):
        return cls()

    @classmethod
    def fromURL(cls, url):
        return cls()


class Plain:
    def __init__(self, text=""):
        self.text = text
