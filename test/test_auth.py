"""认证/限流/媒体签名单元测试（纯内存，不起真实服务）。"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.core.tunnel import TunnelServer
from astrbot_plugin_remote_link.main import RemoteLinkPlugin  # noqa: E402


def make_plugin(token="test-token-123"):
    p = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    p.config = {"auth_token": token}
    p.tunnel = TunnelServer(p.config)
    return p


def fake_request(headers=None, query=None, remote="127.0.0.1", path="/ws"):
    return SimpleNamespace(headers=headers or {}, query=query or {}, remote=remote, path=path)


def test_authorized_bearer_and_deprecated_query():
    p = make_plugin()
    assert p._authorized(fake_request(headers={"Authorization": "Bearer test-token-123"}))
    assert p._authorized(fake_request(query={"token": "test-token-123"}))
    assert not p._authorized(fake_request(headers={"Authorization": "Bearer wrong"}))
    assert not p._authorized(fake_request(query={"token": "wrong"}))


def test_empty_token_is_generated_not_open():
    # config 只有空 token：调用鉴权必须先生成随机 token，不能放行
    p = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    p.config = {"auth_token": ""}
    p.tunnel = TunnelServer(p.config)
    req = fake_request(headers={})
    assert p._authorized(req) is False, "空 token 不得放行未认证请求"
    assert len(str(p.config.get("auth_token") or "")) >= 32


def test_rate_limit():
    p = make_plugin()
    # 同一路径 + 同一 IP 连续请求超过 3 次/秒后应被限流
    for _ in range(3):
        assert p._check_rate_limit(fake_request(path="/submit", remote="1.2.3.4"), limit=3, window=60) is True
    assert p._check_rate_limit(fake_request(path="/submit", remote="1.2.3.4"), limit=3, window=60) is False
    # 不同 IP 不受影响
    assert p._check_rate_limit(fake_request(path="/submit", remote="5.6.7.8"), limit=3, window=60) is True


def test_media_signature():
    p = make_plugin()
    name = "a.png"
    expires = int(time.time()) + 600
    sig = p._media_signature(name, expires)
    req = fake_request(query={"filename": name, "expires": str(expires), "sig": sig})
    assert p._verify_media_signature(req) is True
    bad = fake_request(query={"filename": name, "expires": str(expires), "sig": "x"})
    assert p._verify_media_signature(bad) is False
    expired = fake_request(query={"filename": name, "expires": str(int(time.time()) - 10), "sig": sig})
    assert p._verify_media_signature(expired) is False


if __name__ == "__main__":
    test_authorized_bearer_and_deprecated_query()
    test_empty_token_is_generated_not_open()
    test_rate_limit()
    test_media_signature()
    print("AUTH PASS")
