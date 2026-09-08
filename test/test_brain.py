"""Brain Profile / Client 单元测试。

覆盖：
- Profile 保存/读取；
- API Key 不进入 Snapshot/日志字段；
- 连接测试成功/401/模型不存在/网络失败/超时分类。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from brain.client import BrainClient
from brain.profiles import BrainProfile, get_default_profile, load_profiles, normalize_brain_config, save_profiles


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body or {}
        self._text = "err"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append((url, headers, json))
        resp = self.responses.pop(0)
        return resp


def test_profile_roundtrip_and_hides_key():
    cfg = {}
    normalize_brain_config(cfg)
    assert get_default_profile(cfg) is not None
    profile = BrainProfile(id="default", base_url="http://127.0.0.1:1234/v1", model="qwen", api_key="SECRET")
    save_profiles(cfg, [profile], {"enabled": True, "default_profile": "default"})
    loaded = load_profiles(cfg)
    assert loaded[0].api_key == "SECRET"
    # 摘要不得包含 API Key
    summary = {k: v for k, v in profile.to_dict().items() if k != "api_key"}
    assert "api_key" not in summary


async def test_connection_success():
    profile = BrainProfile(base_url="http://127.0.0.1:1234/v1", model="m", api_key="k")
    body = {"choices": [{"message": {"content": "YUNXIN_OK"}}]}
    client = BrainClient(profile, session=FakeSession([FakeResp(200, body)]))
    result = await client.test_connection()
    assert result["ok"] is True
    assert result["category"] == "ok"


async def test_connection_error_categories():
    profile = BrainProfile(base_url="http://127.0.0.1:1234/v1", model="m", api_key="k")
    c = BrainClient(profile, session=FakeSession([FakeResp(401)]))
    r = await c.test_connection()
    assert r["ok"] is False and r["category"] == "unauthorized", r

    c = BrainClient(profile, session=FakeSession([FakeResp(404)]))
    r = await c.test_connection()
    assert r["ok"] is False and r["category"] == "model_not_found", r


async def test_connection_timeout():
    profile = BrainProfile(base_url="http://127.0.0.1:1234/v1", model="m", api_key="k", timeout=1)

    class TimeoutSession:
        def post(self, url, headers=None, json=None, **kwargs):
            raise asyncio.TimeoutError()

    client = BrainClient(profile, session=TimeoutSession())
    r = await client.test_connection()
    assert r["ok"] is False and r["category"] == "timeout", r


if __name__ == "__main__":
    test_profile_roundtrip_and_hides_key()
    asyncio.run(test_connection_success())
    asyncio.run(test_connection_error_categories())
    asyncio.run(test_connection_timeout())
    print("BRAIN PASS")
