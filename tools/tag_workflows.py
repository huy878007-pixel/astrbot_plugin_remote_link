#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云信互联 · 工作流提示词框自动打标工具

群友/用户在自己电脑上跑一次，自动给 ComfyUI 保存的所有工作流的
正面/负面提示词节点打上 title 标签（「正面提示词」/「负面提示词」）。

插件（YunxinAgent）识别到标签后 100% 按标签注入提示词，不再依赖内容猜测，
从而彻底避免「正负提示词反转 → 图上有文字水印/怪物」的问题。

用法：
    python tag_workflows.py                    # 自动扫描并打标（交互确认）
    python tag_workflows.py --dir D:/ComfyUI/user   # 指定 ComfyUI user 目录
    python tag_workflows.py --apply            # 直接应用（不逐个询问）

说明：
    - 扫描目录下所有 *.json 工作流（含子目录）；
    - 对每个工作流，找出「文本类节点」（CLIPTextEncode / 名字含 Text/String/Prompt），
      按内容启发式区分正/负提示词框；
    - 打标前自动备份原文件为 <name>.json.bak_tag；
    - 无法确定的节点会列出，让用户手动确认或跳过（--apply 时跳过）。
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# ---- 与 agent/local_agent.py 一致的正/负内容启发式 ----
NEG_LIKE_RE = re.compile(
    r"worst quality|low quality|bad anatomy|bad hands|extra fingers|watermark|lowres|jpeg artifacts",
    re.I,
)
POS_LIKE_RE = re.compile(
    r"masterpiece|best quality|high quality|highly detailed|1girl|1boy|anime|photo", re.I
)
TEXT_NODE_RE = re.compile(r"CLIPTextEncode|Text|String|Multiline|Prompt", re.I)
TEXT_INPUT_NAMES = ("text", "string", "value", "prompt", "multiline")

POS_TITLE = "正面提示词"
NEG_TITLE = "负面提示词"


def find_workflows(root: Path) -> list[Path]:
    return sorted(root.rglob("*.json"))


def text_value(node: dict) -> str:
    """从 UI 节点里取文本内容（widgets_values 通常是列表，第一个是文本）。"""
    wv = node.get("widgets_values") or []
    if isinstance(wv, dict):
        for k in TEXT_INPUT_NAMES:
            v = wv.get(k)
            if isinstance(v, str):
                return v
        return ""
    for v in wv:
        if isinstance(v, str):
            return v
    return ""


def is_text_node(node: dict) -> bool:
    ntype = str(node.get("type") or "")
    return bool(TEXT_NODE_RE.search(ntype))


def decide_role(node: dict) -> str | None:
    """按内容启发式判断节点是正框/负框/不确定。"""
    text = text_value(node).lower()
    if NEG_LIKE_RE.search(text):
        return "negative"
    if POS_LIKE_RE.search(text):
        return "positive"
    return None  # 无法确定


def tag_workflow(wf_path: Path, apply: bool, dry_run: bool) -> tuple[int, list[str]]:
    """对单个工作流打标。返回 (修改节点数, 警告列表)。"""
    try:
        wf = json.loads(wf_path.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        return 0, [f"无法解析 {wf_path.name}: {e}"]

    nodes = wf.get("nodes") or []
    if not nodes:
        return 0, []

    text_nodes = [(i, n) for i, n in enumerate(nodes) if is_text_node(n)]
    if not text_nodes:
        return 0, []

    # 已有标签的节点跳过
    unlabeled = [(i, n) for i, n in text_nodes
                 if not str(n.get("title") or "") or "提示词" not in str(n.get("title") or "")]
    if not unlabeled:
        return 0, []

    # 逐节点判断角色
    role_map = {}  # 节点 index -> role
    warnings = []
    for i, n in unlabeled:
        role = decide_role(n)
        if role:
            role_map[i] = role
        else:
            # 无法确定：若其他节点能确定，这个可能是正框（默认当正面，但不覆盖已有判断）
            warnings.append(f"节点 #{n.get('id')}（{n.get('type')}）内容无法自动判断：{text_value(n)[:40]!r}")

    if not role_map:
        return 0, warnings

    # 备份
    if not dry_run:
        bak = wf_path.with_name(wf_path.name + ".bak_tag")
        if not bak.exists():
            shutil.copy2(wf_path, bak)

    changed = 0
    for i, role in role_map.items():
        nodes[i]["title"] = POS_TITLE if role == "positive" else NEG_TITLE
        changed += 1

    if not dry_run:
        wf_path.write_text(
            json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return changed, warnings


def main():
    ap = argparse.ArgumentParser(description="云信互联 · 工作流提示词框自动打标")
    ap.add_argument("--dir", default="", help="ComfyUI 的 user 目录（默认自动探测常见位置）")
    ap.add_argument("--apply", action="store_true", help="直接应用（不询问）")
    ap.add_argument("--dry-run", action="store_true", help="只预览不修改")
    args = ap.parse_args()

    # 探测 ComfyUI user 目录
    root = Path(args.dir) if args.dir else None
    if root is None or not root.is_dir():
        candidates = [
            Path("D:/ComfyUI/user"), Path("D:/ComfyUI_windows_portable/ComfyUI/user"),
            Path("C:/ComfyUI/user"), Path.home() / "ComfyUI/user",
        ]
        # 尝试从 agent_config.json 读
        for cfg in (Path("agent_config.json"), Path.home() / "agent_config.json"):
            try:
                d = json.loads(cfg.read_text(encoding="utf-8"))
                ud = d.get("comfyui", {}).get("userdata_dir")
                if ud:
                    candidates.insert(0, Path(ud))
            except Exception:  # noqa: BLE001
                pass
        for c in candidates:
            if c.is_dir():
                root = c
                break
    if root is None or not root.is_dir():
        print("❌ 找不到 ComfyUI user 目录，请用 --dir 指定")
        sys.exit(1)

    wfs = find_workflows(root)
    print(f"📁 扫描目录: {root}")
    print(f"📄 找到 {len(wfs)} 个工作流\n")

    total_changed = 0
    for wf_path in wfs:
        changed, warns = tag_workflow(wf_path, args.apply, args.dry_run)
        if changed:
            print(f"  ✅ {wf_path.name}: 打标 {changed} 个节点" + ("（预览）" if args.dry_run else ""))
            total_changed += changed
        elif warns:
            print(f"  ⚠️  {wf_path.name}:")
            for w in warns:
                print(f"      - {w}")
        else:
            print(f"  —  {wf_path.name}: 无需修改")

    print(f"\n{'🔍 预览' if args.dry_run else '✅ 完成'}: 共打标 {total_changed} 个节点")
    if not args.apply and not args.dry_run:
        print("提示：已自动备份原文件为 *.json.bak_tag；如需撤销，用备份覆盖回即可。")
    elif args.dry_run:
        print("这是预览模式，未实际修改。加 --apply 才会写入。")


if __name__ == "__main__":
    main()
