"""媒体签名 URL 单元测试。"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "test" / "stubs"))
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.core.tunnel import TunnelServer
from astrbot_plugin_remote_link.main import RemoteLinkPlugin  # noqa: E402


def test_signed_media_url_roundtrip():
    p = RemoteLinkPlugin.__new__(RemoteLinkPlugin)
    p.config = {"auth_token": "secret-token"}
    p.tunnel = TunnelServer(p.config)
    url = p._signed_media_url("sub/folder/../../evil.png", ttl=300)
    assert "/media?" in url and "sig=" in url and "expires=" in url
    # 文件名取 basename，目录穿越被去除
    assert "filename=evil.png" in url, url
    # 签名可被 _verify_media_signature 接受
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    req = SimpleNamespace(query={k: v[0] for k, v in q.items()})
    assert p._verify_media_signature(req) is True


if __name__ == "__main__":
    test_signed_media_url_roundtrip()
    print("MEDIA SIGNATURE PASS")
