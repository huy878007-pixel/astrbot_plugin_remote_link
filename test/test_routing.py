"""Routing 纯函数测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_remote_link.routing.classifier import extract_json, param_intent_detected  # noqa: E402
from astrbot_plugin_remote_link.routing.subtype import pick_subtype  # noqa: E402


def test_pick_subtype():
    assert pick_subtype("image", 0, "生成一张猫图") == "text2image"
    assert pick_subtype("image", 1, "改成雨天") == "image2image"
    assert pick_subtype("video", 1, "做成视频") == "image2video"
    assert pick_subtype("video", 2, "转场视频") == "multi_image2video"
    assert pick_subtype("video", 0, "生成星空视频") == "text2video"
    assert pick_subtype("audio", 0, "生成配乐") == "audio_gen"


def test_classifier_utils():
    assert param_intent_detected("1920x1080 高清") is True
    assert param_intent_detected("画一只猫") is False
    assert extract_json('prefix {"positive":"a","negative":"b"} suffix') == {"positive": "a", "negative": "b"}
    assert extract_json("not json") is None


if __name__ == "__main__":
    test_pick_subtype()
    test_classifier_utils()
    print("ROUTING PASS")
