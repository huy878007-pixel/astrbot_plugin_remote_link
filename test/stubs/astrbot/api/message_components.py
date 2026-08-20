"""stubs.astrbot.api.message_components —— 模拟消息组件（仅测试需要的最小面）。"""


class Image:
    type = "image"

    def __init__(self, url=None, file=None, path=None):
        self.url = url
        self.file = file
        self.path = path

    @classmethod
    def fromFileSystem(cls, path=None, file=None):
        return cls(path=path or file)

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)


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


class Reply:
    """引用（回复）段：被引用消息的组件在 chain 里（与 AstrBot 官方字段一致）。"""

    type = "reply"

    def __init__(self, id=0, chain=None, message_str="", **kw):
        self.id = id
        self.chain = chain or []
        self.message_str = message_str
        for k, v in kw.items():
            setattr(self, k, v)
