#!/usr/bin/env python3
"""UI 格式 → API 格式转换器单元测试（无需 ComfyUI）。

运行：
    python test/test_convert.py
"""

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "agent"))

from local_agent import convert_ui_to_api, detect_workflow_format  # noqa: E402

OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["m.safetensors"], {}]}, "optional": {}}
    },
    "CLIPTextEncode": {
        "input": {
            "required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]},
            "optional": {},
        }
    },
    "KSampler": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "positive": ["CONDITIONING"],
                "negative": ["CONDITIONING"],
                "latent_image": ["LATENT"],
            },
            "optional": {
                "seed": ["INT", {"default": 0}],
                "steps": ["INT", {"default": 20}],
                "cfg": ["FLOAT", {"default": 8.0}],
                "sampler_name": [["euler"], {}],
                "scheduler": [["normal"], {}],
                "denoise": ["FLOAT", {"default": 1.0}],
            },
        }
    },
}

UI_WORKFLOW = {
    "nodes": [
        {"id": 4, "type": "CheckpointLoaderSimple", "widgets_values": ["m.safetensors"]},
        {"id": 6, "type": "CLIPTextEncode", "widgets_values": ["a cat"]},
        {"id": 7, "type": "CLIPTextEncode", "widgets_values": ["bad quality"]},
        {
            "id": 3,
            "type": "KSampler",
            "widgets_values": [123, 20, 7.0, "euler", "normal", 1.0],
        },
    ],
    "links": [
        # UI 格式里 target_slot 只统计连线型输入（widget 不占槽位）
        [1, 4, 1, 6, 0, "CLIP"],
        [2, 4, 1, 7, 0, "CLIP"],
        [3, 4, 0, 3, 0, "MODEL"],
        [4, 6, 0, 3, 1, "CONDITIONING"],
        [5, 7, 0, 3, 2, "CONDITIONING"],
    ],
}

EXPECTED = {
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "a cat", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "bad quality", "clip": ["4", 1]},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "seed": 123,
            "steps": 20,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
        },
    },
}


def test_detect():
    assert detect_workflow_format(UI_WORKFLOW) == "ui"
    assert detect_workflow_format(EXPECTED) == "api"
    assert detect_workflow_format({"foo": 1}) == "unknown"


def test_convert():
    result, role_hints = convert_ui_to_api(UI_WORKFLOW, OBJECT_INFO)
    assert result == EXPECTED, f"转换结果不符:\n{result}\n期望:\n{EXPECTED}"
    assert role_hints == {}, "无标题标签时应返回空 role_hints"


def test_convert_missing_node_def():
    # 无连线的"孤儿"缺失节点（前端专属笔记类）→ 跳过，不报错
    result, _ = convert_ui_to_api(
        {"nodes": [{"id": 1, "type": "NoSuchNode", "widgets_values": []}], "links": []},
        OBJECT_INFO,
    )
    assert result == {}, f"孤儿缺失节点应被跳过: {result}"
    # 有连线的缺失节点（真实缺失的计算节点）→ 必须报错
    try:
        convert_ui_to_api(
            {
                "nodes": [{"id": 1, "type": "NoSuchNode", "widgets_values": []}],
                "links": [[1, 1, 0, 4, 0, "MODEL"]],
            },
            OBJECT_INFO,
        )
        raise AssertionError("有连线的缺失节点应报错")
    except ValueError as e:
        assert "NoSuchNode" in str(e), e


if __name__ == "__main__":
    test_detect()
    test_convert()
    test_convert_missing_node_def()
    print("CONVERT TESTS PASS")
