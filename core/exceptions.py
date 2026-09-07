"""云信互联统一异常类型。"""


class RemoteLinkError(Exception):
    """隧道调用过程中的业务错误，消息内容会直接展示给用户。"""
