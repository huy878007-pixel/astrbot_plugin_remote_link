#!/usr/bin/env python3
"""云信互联 本地代理：跑在你本地电脑上，主动拨入云端 AstrBot 插件并代执行请求。

它做四件事：
  1. 以 WebSocket 客户端身份拨入云端插件（方向：本地 → 云端，穿透 NAT，无需公网 IP 或端口映射）；
  2. 断线自动重连；
  3. 代执行请求：
     - workflows : 读取本地 ComfyUI 保存的全部工作流（UI 格式自动转换为 API 格式）；
     - comfyui   : 执行任意工作流（文生图 / 图生图 / 视频…）、查模型、查队列；
     - openai    : 转发本地 OpenAI 兼容接口（Ollama / LM Studio / vLLM…），支持 SSE 流式；
     - shell     : 在本机执行命令（默认关闭，云端 enable_shell 与本地 shell.enabled 都为 true 才生效）；
  4. 在 http://127.0.0.1:<dashboard_port> 提供一个本地看板（连接状态 / 工作流列表 / 日志）。

用法（Python）:
    pip install aiohttp
    python local_agent.py -c agent_config.json

用法（EXE）:
    双击 YunxinAgent.exe（配置文件 agent_config.json 放在 exe 同目录）

Windows 开机自启（可选）：
    任务计划程序 → 触发器“当计算机启动时” →
    程序: YunxinAgent.exe（或 python.exe + local_agent.py）
"""

import argparse
import asyncio
import base64
import json
import logging
import platform
import random
import re
import socket
import sys
import time
import uuid
from collections import deque
from pathlib import Path

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
if sys.stderr is None:
    # PyInstaller --windowed 打包后没有控制台：清掉默认的 stderr handler，避免打印报错
    logging.getLogger().handlers.clear()
logger = logging.getLogger("yunxin_agent")


def app_dir() -> Path:
    """程序所在目录：源码运行 = 脚本目录；PyInstaller 打包后 = exe 同目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_file(name: str) -> Path:
    """优先找程序同目录的文件，找不到再退回 PyInstaller 打包进 exe 的默认资源。"""
    p = app_dir() / name
    if p.exists():
        return p
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p2 = Path(meipass) / name
        if p2.exists():
            return p2
    return p


DEFAULT_CONFIG = app_dir() / "agent_config.json"

AGENT_VERSION = "0.1.0"  # 云信互联本地代理版本（随 hello 上报）

# ComfyUI 输出文件扩展名 → MIME 映射（用于回传时标注类型）
EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

# UI 格式工作流里的辅助节点：API 格式不存在，转换时跳过
SKIP_NODE_TYPES = {"Reroute", "PrimitiveNode", "Note", "MarkdownNote", "GroupNode", "Group"}


def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    # utf-8-sig：兼容 Windows 记事本保存的带 BOM 的 JSON
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}  # 配置文件不存在：调用方用默认值兜底
    except json.JSONDecodeError:
        return {}


def detect_workflow_format(obj) -> str:
    """识别工作流 JSON 格式：ui（编辑器保存格式）/ api（API 导出格式）/ unknown。"""
    if isinstance(obj, dict) and isinstance(obj.get("nodes"), list) and "links" in obj:
        return "ui"
    if isinstance(obj, dict) and obj and all(
        isinstance(v, dict) and "class_type" in v for v in obj.values()
    ):
        return "api"
    return "unknown"


def normalize_combo_value(value, spec) -> object:
    """COMBO 输入值归一化：前端保存时可能写入显示名（如 "T2VA — 文生音视频"），
    提交时需还原为枚举值（"T2VA"）。匹配规则：值本身在选项里 → 原样；否则
    取选项里以该值开头的项；再不行按 " 或 — 前缀匹配。非 COMBO 输入原样返回。

    兼容两种 COMBO 规格：
      - 旧版：["选项列表", {...}]
      - v0.33+：["COMBO", {"options": [...]}]
    """
    if not isinstance(spec, list) or not spec:
        return value
    head = spec[0]
    options = None
    if isinstance(head, list):
        options = head
    elif head == "COMBO" and isinstance(spec[1], dict):
        options = spec[1].get("options")
    if not isinstance(options, list):
        return value
    if not isinstance(value, str):
        return value
    if value in options:
        return value
    for opt in options:
        if isinstance(opt, str) and (opt == value or opt.startswith(value)):
            return opt
    # 显示名形如 "T2VA — 文生音视频" / "xxx — yyy"：取分隔符前段精确匹配
    for sep in (" — ", " - ", " —", "："):
        head = value.split(sep, 1)[0].strip()
        if head and head in options:
            return head
    return value


def convert_ui_to_api(workflow: dict, object_info: dict) -> dict:
    """把 UI 格式工作流转换为 API 格式（等价于 ComfyUI 前端提交时的转换逻辑）。

    原理：
      - 节点类型 → class_type；
      - 节点的小部件值 widgets_values 按 object_info 中输入的声明顺序，
        依次填进“非连线型”输入；
      - 连线 links 按目标插槽序号（只统计连线型输入）填进“连线型”输入，
        格式为 [来源节点id, 来源输出插槽]。

    注意：包含 Reroute/分组等复杂结构的工作流可能转换不完全，
    这类工作流建议在 ComfyUI 里用“Save (API Format)”另存一份。
    """

    def is_link_spec(spec) -> bool:
        """object_info 输入规格 → 是否连线型输入。

        兼容两种格式：
          - 老版本：连线型是单元素列表 ["MODEL"]；widget 型是 ["INT", {...}]；
          - v0.33+：所有规格都是两元素 ["MODEL", {"tooltip":...}] / ["INT", {...}] /
            [["选项"...], {...}]——首元素是字符串且不在基本类型里 = 连线型，
            首元素是列表（选项列表）= widget 型。
        """
        if isinstance(spec, str):
            return True
        if isinstance(spec, list) and spec:
            head = spec[0]
            if isinstance(head, str):
                # 防御性处理：["INT"] 这种理论上存在的无选项 widget 也归为 widget
                return head not in ("INT", "FLOAT", "STRING", "BOOLEAN")
        return False

    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []
    # 所有连线涉及到的节点 id（"孤儿节点" = 没有任何连线）
    linked_ids: set = set()
    for link in links:
        if isinstance(link, (list, tuple)) and len(link) >= 5:
            linked_ids.add(link[1])  # 来源节点
            linked_ids.add(link[3])  # 目标节点

    # ---- 绕过/静默语义（mode 2=NEVER、4=BYPASS，rgthree 分组开关用 4） ----
    # 与 ComfyUI 前端一致：被绕过节点不出现在 API 里；
    # 它的输入按"同序号输出"穿透——若输入来自另一个被绕过节点则递归上溯到活跃来源。
    muted = {
        node.get("id")
        for node in nodes
        if node.get("mode") in (2, 4)
    }

    def resolve_source(nid, slot, seen):
        if nid in seen:
            return None
        seen.add(nid)
        for link in links:
            if len(link) >= 5 and link[3] == nid and link[4] == slot:
                if link[1] in muted:
                    return resolve_source(link[1], link[2], seen)
                return (link[1], link[2])
        return None

    if muted:
        rewired: list = []
        for link in links:
            if len(link) < 5:
                rewired.append(link)
                continue
            lid, origin, oslot, target, tslot, ltype = link[0], link[1], link[2], link[3], link[4], link[5]
            if target in muted:
                if origin in muted:
                    continue  # 两端都静默：整条丢弃
                continue  # 目标静默：丢弃
            if origin in muted:
                # 穿透：被绕过节点的输出 ← 其同序号输入的活跃来源
                src = resolve_source(origin, oslot, set())
                if src is None:
                    continue  # 无来源可穿透：丢弃（下游分支本身也应静默）
                rewired.append([lid, src[0], src[1], target, tslot, ltype])
                continue
            rewired.append(link)
        links = rewired

    prompt: dict = {}
    # 节点 title 标签 → 角色（正面/负面提示词框），供 auto_inject_prompt 优先使用。
    # 用户可在 ComfyUI 里把 CLIPTextEncode 节点的 Title 改成「正面提示词/负面提示词」
    # （或含 positive/negative），插件 100% 按标签注入，不依赖内容猜测。
    role_hints: dict[str, str] = {}
    for node in nodes:
        ntype = node.get("type")
        nid = node.get("id")
        if ntype in SKIP_NODE_TYPES or nid in muted:
            continue
        title = str(node.get("title") or "")
        if title:
            tl = title.lower()
            if POS_TITLE_RE.search(tl):
                role_hints[str(nid)] = "positive"
            elif NEG_TITLE_RE.search(tl):
                role_hints[str(nid)] = "negative"
        info = object_info.get(ntype)
        if not info:
            if nid not in linked_ids:
                # 前端专属/纯说明节点（MarkdownNote、rgthree Fast Groups Bypasser 等）：
                # 后端没有定义且没有任何连线——提交时前端本来就会忽略它们，直接跳过
                logger.warning(
                    f"[remote_link] 跳过前端专属节点 {ntype}（无后端定义且无连线）"
                )
                continue
            raise ValueError(
                f"本地 ComfyUI 缺少节点定义 {ntype}（未安装对应自定义节点？），"
                "该工作流请用 Save (API Format) 另存为 API 格式"
            )
        widgets = node.get("widgets_values") or []
        widgets_by_name = widgets if isinstance(widgets, dict) else None
        node_inputs: dict = {}
        ui_inputs = node.get("inputs") or []
        if ui_inputs:
            # ---- 主路径：按节点序列化的 inputs 数组（与 ComfyUI 前端 graphToPrompt 一致） ----
            # 每个输入槽：有 link → 按 link id 找来源（已应用静音穿透）；无 link → 按
            # 顺序消费 widgets_values。widget 被连线"升级"成输入时同样占一个 widget 值。
            wi = 0  # widgets_values 游标（只统计带 widget 字段的输入槽）
            for slot in ui_inputs:
                if not isinstance(slot, dict):
                    continue
                name = slot.get("name")
                if not name:
                    continue
                link_id = slot.get("link")
                wv = None
                # 输入规格（用于 COMBO 显示名归一化 / control_after_generate 判断）
                spec = None
                inp = info.get("input") or {}
                for key in ("required", "optional"):
                    if name in (inp.get(key) or {}):
                        spec = (inp.get(key) or {})[name]
                        break
                if "widget" in slot:
                    widget_name = (slot.get("widget") or {}).get("name") if isinstance(slot.get("widget"), dict) else None
                    if widgets_by_name is not None:
                        wv = normalize_combo_value(widgets_by_name.get(widget_name or name), spec)
                    else:
                        if wi < len(widgets):
                            wv = widgets[wi]
                        wi += 1
                        # control_after_generate：seed 类 widget 后额外跟一个控制值。
                        # 前端对名为 seed/noise_seed 的输入会自动加控制控件（即使
                        # object_info 没声明标记，如 FaceDetailer）；只有下一个值
                        # 确实是控制字符串时才跳过，避免误伤没有控制项的旧数据。
                        flagged = (
                            isinstance(spec, list)
                            and len(spec) > 1
                            and isinstance(spec[1], dict)
                            and bool(spec[1].get("control_after_generate"))
                        )
                        nxt = widgets[wi] if wi < len(widgets) else None
                        if flagged or (
                            name in ("seed", "noise_seed")
                            and isinstance(nxt, str)
                            and nxt in ("fixed", "randomize", "increment", "decrement")
                        ):
                            wi += 1
                if link_id is not None:
                    src = None
                    for link in links:
                        if len(link) >= 5 and link[0] == link_id:
                            src = (link[1], link[2])
                            break
                    if src is not None:
                        node_inputs[name] = [str(src[0]), src[1]]
                elif wv is not None:
                    node_inputs[name] = normalize_combo_value(wv, spec)
        else:
            # ---- 兜底路径：老格式工作流没有 inputs 数组，用 object_info 按位置推断 ----
            inputs_def: list = []
            inp = info.get("input") or {}
            for key in ("required", "optional"):
                for name, spec in (inp.get(key) or {}).items():
                    inputs_def.append((name, spec))
            wi = 0  # 小部件值游标
            li = 0  # 连线型输入插槽序号（只统计连线型输入）
            for name, spec in inputs_def:
                if is_link_spec(spec):
                    # 连线型输入：找目标为 (本节点, 第 li 个连线插槽) 的连线
                    cands = [l for l in links if len(l) >= 5 and l[3] == nid and l[4] == li]
                    if cands:
                        node_inputs[name] = [str(cands[0][1]), cands[0][2]]
                    li += 1
                elif widgets_by_name is not None:
                    # 对象形式的 widgets_values：按 widget 名取
                    if name in widgets_by_name:
                        node_inputs[name] = normalize_combo_value(widgets_by_name[name], spec)
                else:
                    # 小部件型输入：按顺序取 widgets_values
                    if wi < len(widgets):
                        node_inputs[name] = normalize_combo_value(widgets[wi], spec)
                    wi += 1
                    # control_after_generate：前端专属控制项（如 KSampler 的 "fixed"/"randomize"），
                    # 在 widgets_values 里占一个值但不映射到后端输入——跳过它；
                    # 前端对名为 seed/noise_seed 的输入会自动加控制控件（无标记时
                    # 仅当下一个值确实是控制字符串才跳过，避免误伤旧数据）
                    flagged = (
                        isinstance(spec, list)
                        and len(spec) > 1
                        and isinstance(spec[1], dict)
                        and bool(spec[1].get("control_after_generate"))
                    )
                    nxt = widgets[wi] if wi < len(widgets) else None
                    if flagged or (
                        name in ("seed", "noise_seed")
                        and isinstance(nxt, str)
                        and nxt in ("fixed", "randomize", "increment", "decrement")
                    ):
                        wi += 1
        prompt[str(nid)] = {"class_type": ntype, "inputs": node_inputs}
    # 附加 title 角色标签（内部约定 key，注入时读取并清理，不提交给 ComfyUI）
    if role_hints:
        prompt[ROLE_HINTS_KEY] = role_hints
    return prompt


TOKEN_RE = re.compile(r"^__[A-Z0-9_]+__$")

# 节点类名 → 产物类型（用于 LLM 调度时判断工作流"能出什么"）
OUTPUT_KIND_RULES = [
    (re.compile(r"SaveVideo|VideoCombine|VHS_|AnimateDiff"), "video"),
    (re.compile(r"SaveImage|PreviewImage"), "image"),
    (re.compile(r"SaveAudio|AudioCombine"), "audio"),
]


def scan_api_workflow(wf: dict, object_info: dict | None = None):
    """扫描 API 格式工作流：返回 (tokens, outputs, nodes)。

    tokens: {TOKEN: "int"|"float"|"bool"|"string"} —— 占位符参数及类型（由 object_info 推断）
    outputs: ["image", "video", ...] —— 产物类型
    nodes: [节点类名, ...]
    """
    object_info = object_info or {}
    tokens: dict = {}
    outputs: set = set()
    nodes: list = []
    for _nid, node in wf.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ctype = node["class_type"]
        nodes.append(ctype)
        for rule, kind in OUTPUT_KIND_RULES:
            if rule.search(ctype):
                outputs.add(kind)
        inputs_def: list = []
        inp = (object_info.get(ctype) or {}).get("input") or {}
        for key in ("required", "optional"):
            for name, spec in (inp.get(key) or {}).items():
                inputs_def.append((name, spec))
        node_inputs = node.get("inputs") or {}
        if not inputs_def:
            # object_info 不可用时降级：直接扫所有输入值里的占位符（类型按 string）
            for _name, val in node_inputs.items():
                if isinstance(val, str) and TOKEN_RE.match(val):
                    tokens.setdefault(val.strip("_"), "string")
            continue
        for name, spec in inputs_def:
            val = node_inputs.get(name)
            if isinstance(val, str) and TOKEN_RE.match(val):
                tok = val.strip("_")
                if isinstance(spec, list) and spec and isinstance(spec[0], str):
                    head = spec[0]
                    ptype = {"INT": "int", "FLOAT": "float", "BOOLEAN": "bool"}.get(head, "string")
                else:
                    ptype = "string"
                tokens[tok] = ptype
    return tokens, sorted(outputs), sorted(set(nodes))


def scan_ui_workflow_tokens(wf: dict) -> list:
    """轻量扫描 UI 格式工作流的占位符名（不依赖 object_info，类型未知）。"""
    names: list = []
    for node in (wf or {}).get("nodes", []):
        wv = node.get("widgets_values") or []
        # 对象形式的 widgets_values（键=widget 名）要遍历 values
        iterable = wv.values() if isinstance(wv, dict) else wv
        for val in iterable:
            if isinstance(val, str) and TOKEN_RE.match(val):
                names.append(val.strip("_"))
    return sorted(set(names))


# 文本类节点名特征（自动提示词注入的目标候选）
TEXT_NODE_RE = re.compile(r"Text|String|Multiline|Prompt", re.I)
# Minimax 等音视频工作流：提示词通过 AudioConditioning 类节点的 prompt 输入传入
TEXT_NODE_AUDIO_RE = re.compile(r"AudioConditioning", re.I)
TEXT_INPUT_NAMES = ("text", "string", "value", "prompt", "multiline")
NEG_LIKE_RE = re.compile(
    r"worst quality|low quality|bad anatomy|bad hands|extra fingers|watermark|lowres|jpeg artifacts|nsfw",
    re.I,
)
POS_LIKE_RE = re.compile(
    r"masterpiece|best quality|high quality|highly detailed|1girl|1boy|anime|photo", re.I
)

# 节点 Title 标签（用户自定义，优先于内容启发式）：
#  - 正面：标题含「正面/正提示/正向/positive」；
#  - 负面：标题含「负面/负提示/负向/negative」。
POS_TITLE_RE = re.compile(r"正面|正向|positive|prompt", re.I)
NEG_TITLE_RE = re.compile(r"负面|负向|negative", re.I)
# 内部约定 key：UI→API 转换时收集的「节点 title 标签 → 角色」映射，
# auto_inject_prompt 读取后删除，绝不提交给 ComfyUI（大写、双下划线包裹、非合法节点名）。
ROLE_HINTS_KEY = "__YUNXIN_ROLE_HINTS__"


def auto_inject_prompt(prompt_data: dict, params: dict) -> None:
    """无 __PROMPT__/__NEGATIVE__ 占位符的工作流：自动识别正/负提示词框并填入。

    识别优先级（自上而下）：
      1. 【用户标签】转换器收集的节点 title 标签（节点 Title 含「正面/positive」或
         「负面/negative」）—— 最可靠，不同用户工作流节点 ID 各异也 100% 准确；
      2. 【内容启发式】负框 = 内容像负面词（worst quality/bad anatomy/…）的候选；
         正框 = 其余候选中第一个（优先内容像正面词）；
      3. 【兜底】没认出负框时，取「未被选为正框」的其他文本节点；单节点时保守跳过
         负面注入（避免把负面词覆盖进唯一提示词框）。
    """
    positive = str(params.get("PROMPT") or params.get("prompt") or "").strip()
    negative = str(params.get("NEGATIVE") or params.get("negative") or "").strip()
    if not positive and not negative:
        return

    # 1) 用户 title 标签优先（转换器收集，存于内部约定 key；读取后清理）
    hints = prompt_data.pop(ROLE_HINTS_KEY, None) or {}
    if hints:
        neg_hint = pos_hint = None
        for nid, role in hints.items():
            node = prompt_data.get(nid)
            if not node or not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            if not isinstance(inputs, dict):
                continue
            text_key = None
            if isinstance(inputs.get("text"), str):
                text_key = "text"
            else:
                for k in TEXT_INPUT_NAMES:
                    if isinstance(inputs.get(k), str):
                        text_key = k
                        break
            if text_key is None:
                continue
            if role == "negative" and neg_hint is None:
                neg_hint = (nid, node, text_key)
            elif role == "positive" and pos_hint is None:
                pos_hint = (nid, node, text_key)
        if (positive and pos_hint) or (negative and neg_hint):
            # 有标签时完全按标签注入（不依赖内容猜测）
            if positive and pos_hint:
                pos_hint[1]["inputs"][pos_hint[2]] = positive
                logger.info(f"[remote_link] 按节点标签注入正提示词 → 节点 {pos_hint[0]}")
            if negative and neg_hint:
                neg_hint[1]["inputs"][neg_hint[2]] = negative
                logger.info(f"[remote_link] 按节点标签注入负提示词 → 节点 {neg_hint[0]}")
            return

    # 2) 内容启发式（无标签 / 标签节点不可注入时）
    text_nodes: list = []
    for nid, node in prompt_data.items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type") or ""
        inputs = node.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        text_key = None
        if isinstance(inputs.get("text"), str):
            text_key = "text"
        elif TEXT_NODE_RE.search(ctype) or TEXT_NODE_AUDIO_RE.search(ctype):
            for k in TEXT_INPUT_NAMES:
                if isinstance(inputs.get(k), str):
                    text_key = k
                    break
        if text_key is not None:
            text_nodes.append((nid, node, text_key, str(inputs.get(text_key) or "")))

    if not text_nodes:
        logger.info("[remote_link] 未找到可自动注入的提示词框，跳过")
        return

    neg_node = None
    pos_candidates = []
    for nid, node, key, text in text_nodes:
        if neg_node is None and NEG_LIKE_RE.search(text):
            neg_node = (nid, node, key)
        else:
            pos_candidates.append((nid, node, key, text))
    pos_node = None
    for nid, node, key, text in pos_candidates:
        if pos_node is None or POS_LIKE_RE.search(text):
            pos_node = (nid, node, key)
            if POS_LIKE_RE.search(text):
                break

    if positive and pos_node is not None:
        pos_node[1]["inputs"][pos_node[2]] = positive
        logger.info(f"[remote_link] 自动注入正提示词 → 节点 {pos_node[0]}（{pos_node[1].get('class_type')}）")
    if negative:
        target = neg_node
        if target is None and len(text_nodes) > 1:
            # 负框兜底：取「未被选为正框」的节点，且绝不覆盖正框——
            # 盲目取 text_nodes[1] 在字典顺序下可能把负面词注入正面框（正/负错乱、图上出乱码文字）。
            # 优先级：内容像负面词的 > 不是正框的其他节点 > 保守不注入。
            if pos_node is not None:
                for nid, node, key, text in text_nodes:
                    if (nid, node, key) != (pos_node[0], pos_node[1], pos_node[2]):
                        target = (nid, node, key)
                        break
            if target is None:
                # 只有一个文本节点且它已被当正框：不注入负面词（避免覆盖正面提示词）
                logger.warning("[remote_link] 仅识别到单一提示词框（已被当作正面），跳过负面词注入，避免覆盖正面提示词")
        if target is not None:
            target[1]["inputs"][target[2]] = negative
            logger.info(f"[remote_link] 自动注入负提示词 → 节点 {target[0]}（{target[1].get('class_type')}）")


DATE_TOKEN_RE = re.compile(r"%date:([^%]+)%")


def substitute_prefix_dates(prompt_data: dict) -> None:
    """把 filename_prefix 里的 %date:格式% 替换为当前日期（与 ComfyUI 前端提交时一致）。"""
    for node in prompt_data.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") or {}
        prefix = inputs.get("filename_prefix")
        if isinstance(prefix, str):
            inputs["filename_prefix"] = DATE_TOKEN_RE.sub(
                lambda m: time.strftime(m.group(1)), prefix
            )


# 约定入口节点（参考 AstrBot 社区插件与 ComfyUI 生态的通行做法）：
# 工作流里放几个这类节点，就按顺序注入几个对应的输入
INJECT_IMAGE_TYPES = {"ETN_LoadImageBase64"}          # 图片（base64 直接注入）
INJECT_TEXT_TYPES = {"Simple String"}                 # 文本
INJECT_VIDEO_TYPES = {"VHS_LoadVideo"}                # 视频（文件名）

# LoadImage / VHS 的"显式占位"约定：widget 值为这些占位符时，才是上传注入目标
# （避免误改工作流里固定文件名的参考图/参考视频）
IMAGE_SLOT_RE = re.compile(r"^__IMAGE(?:_\d+)?__$")
VIDEO_SLOT_RE = re.compile(r"^__VIDEO(?:_\d+)?__$")


def _slot_value(node: dict, key: str, ui_widgets: list | None = None) -> str | None:
    """取一个入口节点的槽位值（API 格式读 inputs[key]；UI 格式读 widgets_values 第一个）。"""
    if ui_widgets is not None:
        return str(ui_widgets[0]) if ui_widgets else None
    return (node.get("inputs") or {}).get(key)


def scan_inject_counts(wf: dict) -> dict:
    """统计工作流里约定入口节点的数量：{"images": n, "texts": n, "videos": n}（兼容 UI/API 两种格式）。

    图片入口 = ETN_LoadImageBase64 节点 + widget 值为 __IMAGE__ 占位的 LoadImage 节点；
    视频入口 = widget 值为 __VIDEO__ 占位的 VHS_LoadVideo 节点。
    """
    counts = {"images": 0, "texts": 0, "videos": 0}
    if isinstance(wf.get("nodes"), list):
        for node in wf["nodes"]:
            if not isinstance(node, dict):
                continue
            ctype = node.get("type") or ""
            widgets = node.get("widgets_values") or []
            if ctype in INJECT_IMAGE_TYPES:
                counts["images"] += 1
            elif ctype == "LoadImage" and widgets and IMAGE_SLOT_RE.match(str(widgets[0])):
                counts["images"] += 1
            elif ctype in INJECT_TEXT_TYPES:
                counts["texts"] += 1
            elif ctype in INJECT_VIDEO_TYPES and widgets and VIDEO_SLOT_RE.match(str(widgets[0])):
                counts["videos"] += 1
        return counts
    for _nid, node in (wf or {}).items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type") or ""
        if ctype in INJECT_IMAGE_TYPES:
            counts["images"] += 1
        elif ctype == "LoadImage" and IMAGE_SLOT_RE.match(str(_slot_value(node, "image") or "")):
            counts["images"] += 1
        elif ctype in INJECT_TEXT_TYPES:
            counts["texts"] += 1
        elif ctype in INJECT_VIDEO_TYPES and VIDEO_SLOT_RE.match(str(_slot_value(node, "video") or "")):
            counts["videos"] += 1
    return counts


def smart_merge_texts(texts: list, slots: int) -> list:
    """文本槽智能合并：LLM 给的段数多于槽位时合并，少于槽位时留空（避免硬报错）。"""
    if not texts or slots <= 0:
        return []
    if slots >= len(texts):
        return list(texts)
    if slots == 1:
        return [" ".join(texts)]
    result = list(texts[: slots - 1])
    result.append(" ".join(texts[slots - 1 :]))
    return result


def inject_texts(prompt: dict, texts: list) -> None:
    """把文本注入到 Simple String 入口节点（数量智能合并，少了留空）。"""
    txt_nodes = sorted(
        (int(k), v) for k, v in prompt.items()
        if isinstance(v, dict) and (v.get("class_type") or "") in INJECT_TEXT_TYPES
    )
    merged = smart_merge_texts(texts, len(txt_nodes))
    for (_nid, node), text in zip(txt_nodes, merged):
        if "string" in node.get("inputs", {}):
            node["inputs"]["string"] = text
        else:
            node["inputs"]["text"] = text


def randomize_negative_seeds(obj):
    """把工作流里所有负数 seed/noise_seed 换成随机正整数（显式指定的正 seed 尊重原值）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("seed", "noise_seed") and isinstance(v, (int, float)) and v < 0:
                out[k] = random.randint(1, 10**9)
            else:
                out[k] = randomize_negative_seeds(v)
        return out
    if isinstance(obj, list):
        return [randomize_negative_seeds(x) for x in obj]
    return obj


def parse_comfyui_400_summary(body: str) -> str | None:
    """把 ComfyUI /prompt 的 400 校验错误翻译成人类/LLM 可读的修复建议。

    参考社区插件 astrbot_plugin_comfyui 的 _parse_comfyui_400_summary 思路重写。
    """
    if not body or not body.strip():
        return None
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    node_errors = data.get("node_errors") if isinstance(data, dict) else None
    if not isinstance(node_errors, dict):
        return None
    parts = []
    for _node_id, node_data in node_errors.items():
        if not isinstance(node_data, dict):
            continue
        for err in (node_data.get("errors") or []):
            if not isinstance(err, dict):
                continue
            if err.get("type") == "value_not_in_list":
                extra = err.get("extra_info") or {}
                input_name = extra.get("input_name", "")
                received = extra.get("received_value", "")
                config = extra.get("input_config")
                allowed = []
                if isinstance(config, list) and config and isinstance(config[0], list):
                    allowed = config[0][:10]
                if input_name in ("ckpt_name", "model") and received:
                    allowed_str = "、".join(str(a) for a in allowed) or "（见本地模型目录）"
                    parts.append(
                        f"工作流使用的模型 '{received}' 在本地 ComfyUI 上不存在；"
                        f"本地可用：{allowed_str}。请换一个存在的模型名或改用其他工作流。"
                    )
                    break
                details = err.get("details") or ""
                if details:
                    parts.append(f"ComfyUI 校验失败: {details[:500]}")
            else:
                details = err.get("details") or ""
                if details and len(parts) < 3:
                    parts.append(f"ComfyUI 校验失败: {details[:400]}")
    return " ".join(parts) if parts else None


class _CallbackLogHandler(logging.Handler):
    """把日志行转发给 GUI 回调（线程安全：回调只做入队等轻量操作）。"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        try:
            self.callback(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class LocalAgent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ws = None  # 当前到云端插件的 WebSocket
        self.session = None  # 复用的 aiohttp 会话，用于代理本地 HTTP 请求
        self._started = time.time()
        self._connected_at = 0.0
        self._last_error = ""
        self._last_info: dict = {}  # 最近一次本机/服务状态采集（hello 与 info 服务、30s 周期刷新）
        self._current_progress: dict | None = None  # 当前执行任务的实时进度（本地看板/GUI 显示）
        self._history: deque = deque(maxlen=100)  # 生成历史（GUI 历史页展示）
        self._events: deque = deque(maxlen=300)  # (时间戳, 事件文本)，供本地看板展示
        self._object_info_cache = None
        self._object_info_ts = 0.0
        self._workflows_cache = None
        self._workflows_ts = 0.0
        self._capabilities_cache = None
        self._capabilities_ts = 0.0
        self._dashboard_task = None
        self._dashboard_runner = None
        self._info_task = None
        self._reconnect_flag = asyncio.Event()
        self._stop_requested = False  # GUI 请求停止
        self._loop = None
        self._sink_handlers: list = []

    def log_event(self, text: str):
        """记录一条事件：写日志 + 进本地看板的环形缓冲。"""
        logger.info(text)
        self._events.append((time.time(), text))

    # ---------------- GUI 接口（任意线程可调用） ----------------

    def add_log_sink(self, callback) -> None:
        """GUI 注册日志回调：所有 yunxin_agent 日志行都会推给 callback(text)。"""
        handler = _CallbackLogHandler(callback)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        self._sink_handlers.append(handler)

    def request_stop(self) -> None:
        """请求停止代理循环（线程安全）。"""
        self._stop_requested = True
        loop = self._loop
        if loop is not None and not loop.is_closed():

            def _close():
                ws = self.ws
                if ws is not None and not ws.closed:
                    asyncio.ensure_future(ws.close())

            try:
                loop.call_soon_threadsafe(_close)
            except RuntimeError:
                pass

    def request_reconnect(self) -> None:
        """强制断开当前隧道连接，触发重连（线程安全）。"""
        loop = self._loop
        if loop is not None and not loop.is_closed():

            def _close():
                ws = self.ws
                if ws is not None and not ws.closed:
                    asyncio.ensure_future(ws.close())

            try:
                loop.call_soon_threadsafe(_close)
            except RuntimeError:
                pass

    def snapshot(self) -> dict:
        """给 GUI 的线程安全状态快照。"""
        ws = self.ws
        connected = False
        try:
            connected = ws is not None and not ws.closed
        except Exception:  # noqa: BLE001
            connected = False
        info = dict(self._last_info or {})
        prog = self._current_progress
        if prog and time.time() - float(prog.get("updated_at") or 0) > 30:
            prog = None  # 超 30 秒未更新视为结束
        return {
            "connected": connected,
            "connected_at": self._connected_at,
            "started_at": self._started,
            "last_error": self._last_error,
            "server_url": str(self.cfg.get("server_url", "")),
            "info": info,
            "progress": prog,
            "history": list(self._history)[:30],
            "events": list(self._events)[-100:],
        }

    # ---------------- 主循环 ----------------

    async def _info_loop(self):
        """每 30 秒刷新一次本机/服务状态，供 GUI 状态区展示。"""
        while not self._stop_requested:
            await asyncio.sleep(30)
            if self._stop_requested:
                break
            try:
                self._last_info = await self.collect_info()
            except Exception:  # noqa: BLE001
                pass

    async def run(self):
        if self._stop_requested:
            return
        self._loop = asyncio.get_running_loop()
        reconnect = int(self.cfg.get("reconnect_seconds", 5))
        total = int(self.cfg.get("request_timeout", 600)) + 60
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total))
        if int(self.cfg.get("dashboard_port", 8899)) > 0:
            self._dashboard_task = asyncio.ensure_future(self._dashboard_main())
        self._info_task = asyncio.ensure_future(self._info_loop())
        try:
            while not self._stop_requested:
                self._reconnect_flag.clear()
                try:
                    await self._connect_and_serve()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._last_error = str(e)
                    self.log_event(f"连接中断：{e}，{reconnect}s 后重连…")
                await asyncio.sleep(reconnect)
        finally:
            await self.session.close()
            if self._dashboard_task is not None:
                self._dashboard_task.cancel()
            if self._info_task is not None:
                self._info_task.cancel()
            if self._dashboard_runner is not None:
                await self._dashboard_runner.cleanup()

    async def _connect_and_serve(self):
        url = str(self.cfg["server_url"]).rstrip("/")
        token = str(self.cfg.get("token", ""))
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.log_event(f"正在连接云端插件：{url}")
        async with self.session.ws_connect(
            url, headers=headers, heartbeat=20, max_msg_size=256 * 1024 * 1024
        ) as ws:
            self.ws = ws
            self._connected_at = time.time()
            info = await self.collect_info()
            self._last_info = info
            await self._send({"type": "hello", "data": info})
            self.log_event("已连接云端插件，等待请求…")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    asyncio.ensure_future(self._on_message(msg.data))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise ConnectionError(f"WebSocket 错误: {ws.exception()}")
        self.ws = None
        self._connected_at = 0.0

    async def _on_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("type") != "request":
            return
        rid = data.get("id")
        asyncio.ensure_future(self._dispatch(rid, data.get("service", ""), data.get("payload") or {}))

    async def _dispatch(self, rid: str, service: str, payload: dict):
        try:
            result = await self.handle_service(rid, service, payload)
            await self._send_response(rid, True, result)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"处理 {service} 请求失败")
            await self._send_response(rid, False, str(e))

    async def _send_response(self, rid: str, ok: bool, result):
        """发送任务响应；隧道暂断时等待重连补发（最多 30s），避免“生成了但发不回”。"""
        # 诊断：仅工作流响应（带 prompt_id）打印产物文件名；
        # info/capabilities 等面板轮询响应不打，避免每 10 秒刷一条“0 个产物”噪音。
        try:
            if ok and isinstance(result, dict) and "prompt_id" in result:
                files = result.get("files") or []
                logger.info(
                    f"[remote_link] 发送响应 {rid}: {len(files)} 个产物 "
                    f"({', '.join(f.get('filename', '?') for f in files) or '无'})"
                )
        except Exception:  # noqa: BLE001
            pass
        data = {"type": "response", "id": rid, "ok": ok, "result": result if ok else None,
                "error": None if ok else result}
        deadline = time.time() + 30
        while True:
            try:
                await self._send(data)
                return
            except Exception:  # noqa: BLE001
                if time.time() > deadline:
                    logger.warning(f"[remote_link] 响应 {rid} 发送失败（隧道 30s 内未恢复），丢弃")
                    return
                await asyncio.sleep(2)

    async def _send(self, data: dict):
        if self.ws is None:
            raise ConnectionError("WebSocket 未连接")
        await self.ws.send_str(json.dumps(data, ensure_ascii=False, default=str))

    async def _try_send(self, data: dict):
        """尽力发送（进度/通知类消息）：隧道断连时不抛错，任务继续执行。

        响应（response）仍用 _send 严格发送；只有进度这类“丢了也能重来”的消息走这里。
        """
        try:
            await self._send(data)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 服务分发 ----------------

    async def handle_service(self, rid: str, service: str, payload: dict):
        if service == "ping":
            return {"pong": True, "time": time.time(), "uptime": time.time() - self._started}
        if service == "info":
            info = await self.collect_info()
            self._last_info = info
            return info
        if service == "workflows":
            return await self.svc_workflows(payload)
        if service == "capabilities":
            return await self.svc_capabilities()
        if service == "comfyui":
            return await self.svc_comfyui(rid, payload)
        if service == "media_get":
            return await self.svc_media_get(rid, payload)
        if service == "openai":
            return await self.svc_openai(rid, payload)
        if service == "shell":
            return await self.svc_shell(payload)
        raise ValueError(f"未知服务: {service}")

    # ---------------- 工作流列表（自适应读取） ----------------

    def _workflows_root(self) -> Path | None:
        """本地 ComfyUI 工作流目录：优先用配置的 userdata_dir，兼容指向 user 或 workflows。"""
        d = str(self.cfg["comfyui"].get("userdata_dir") or "").strip()
        if not d:
            return None
        p = Path(d)
        if p.name.lower() == "workflows":
            return p
        return p / "default" / "workflows"

    def _list_workflows_fs(self) -> list[dict]:
        root = self._workflows_root()
        if root is None or not root.is_dir():
            return []
        items = []
        for f in sorted(root.rglob("*.json")):
            rel = str(f.relative_to(root)).replace("\\", "/")
            try:
                obj = json.loads(f.read_text(encoding="utf-8-sig", errors="ignore"))
                fmt = detect_workflow_format(obj)
            except Exception:  # noqa: BLE001
                fmt = "unknown"
            # 轻量能力扫描：占位符参数名 + 产物类型 + 入口节点（不依赖 object_info）
            tokens: list = []
            outputs: list = []
            injects = {"images": 0, "texts": 0, "videos": 0}
            if fmt == "ui":
                tokens = scan_ui_workflow_tokens(obj)
                injects = scan_inject_counts(obj)
            elif fmt == "api":
                _t, outputs, _n = scan_api_workflow(obj, None)
                tokens = sorted(_t.keys())
                injects = scan_inject_counts(obj)
            items.append(
                {
                    "name": f.stem,
                    "relpath": rel,
                    "format": fmt,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                    "tokens": tokens,
                    "outputs": outputs,
                    "injects": injects,
                }
            )
        return items

    async def _list_workflows_api(self) -> list[dict]:
        """兜底：部分 ComfyUI 变体提供 /workflows、/api/workflows 接口。"""
        timeout = aiohttp.ClientTimeout(total=10)
        for path in ("/workflows", "/api/workflows"):
            try:
                async with self.session.get(self._comfyui_url(path), timeout=timeout) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    if not isinstance(data, list):
                        continue
                    items = []
                    for w in data:
                        if not isinstance(w, dict):
                            continue
                        items.append(
                            {
                                "name": w.get("name") or w.get("id") or "?",
                                "relpath": w.get("path") or w.get("name") or "",
                                "format": "api",
                                "size": 0,
                                "modified": 0,
                            }
                        )
                    return items
            except Exception:  # noqa: BLE001
                continue
        return []

    async def svc_workflows(self, payload: dict) -> dict:
        """工作流服务：list（列表）/ read（读取）/ upload（上传保存）/ delete（删除）。"""
        action = payload.get("action") or "list"
        if action == "read":
            ref = str(payload.get("name") or "").strip()
            if not ref:
                raise ValueError("缺少工作流名称")
            content = self._read_workflow(ref)
            try:
                obj = json.loads(content)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"工作流文件不是合法 JSON: {e}") from None
            return {
                "name": ref,
                "format": detect_workflow_format(obj),
                "content": content,
            }
        if action == "upload":
            return await self._workflow_upload(payload)
        if action == "delete":
            return await self._workflow_delete(payload)
        return await self.svc_workflows_list()

    def _safe_workflow_path(self, name: str) -> Path:
        """把用户给的名字约束到工作流目录内（拒绝路径穿越/绝对路径）。"""
        root = self._workflows_root()
        if root is None:
            raise ValueError("未配置 comfyui.userdata_dir，无法管理工作流文件")
        name = str(name or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"非法的工作流文件名: {name}")
        if not name.endswith(".json"):
            name += ".json"
        return (root / name).resolve()

    async def _workflow_upload(self, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("缺少工作流内容")
        try:
            obj = json.loads(content)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"工作流内容不是合法 JSON: {e}") from None
        fmt = detect_workflow_format(obj)
        if fmt not in ("ui", "api"):
            raise ValueError("无法识别工作流格式（既不是 UI 格式也不是 API 格式）")
        p = self._safe_workflow_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if p.exists():
            # 覆盖前自动备份
            backup = p.with_suffix(f".bak.{int(time.time())}.json")
            try:
                p.replace(backup)
            except Exception:  # noqa: BLE001
                backup = None
        p.write_text(content, encoding="utf-8")
        self._workflows_cache = None  # 清缓存让列表立即生效
        self._workflows_ts = 0.0
        self.log_event(f"已上传工作流: {p.name}" + (f"（旧文件备份为 {backup.name}）" if backup else ""))
        return {"ok": True, "name": p.stem, "format": fmt}

    async def _workflow_delete(self, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()
        p = self._safe_workflow_path(name)
        if not p.exists():
            raise FileNotFoundError(f"找不到工作流: {name}")
        backup = p.with_suffix(f".del.{int(time.time())}.json")
        p.replace(backup)
        self._workflows_cache = None
        self._workflows_ts = 0.0
        self.log_event(f"已删除工作流: {p.name}（备份为 {backup.name}，可手动恢复）")
        return {"ok": True, "name": p.stem, "backup": backup.name}

    async def svc_workflows_list(self) -> dict:
        now = time.time()
        if self._workflows_cache is not None and now - self._workflows_ts < 60:
            return {"workflows": self._workflows_cache, "source": "cache"}
        items = self._list_workflows_fs()
        source = "filesystem"
        if not items:
            items = await self._list_workflows_api()
            source = "api"
        self._workflows_cache = items
        self._workflows_ts = now
        return {"workflows": items, "source": source, "root": str(self._workflows_root() or "")}

    # ---------------- 能力清单（LLM 智能调度的依据） ----------------

    async def svc_capabilities(self) -> dict:
        """返回本地能力清单：工作流（含节点/参数/产物）+ checkpoint + LLM 模型 + GPU。"""
        now = time.time()
        if self._capabilities_cache is not None and now - self._capabilities_ts < 60:
            return self._capabilities_cache
        caps = await self._compute_capabilities()
        self._capabilities_cache = caps
        self._capabilities_ts = now
        return caps

    async def _compute_capabilities(self) -> dict:
        workflows = []
        object_info = None
        items = self._list_workflows_fs()
        if not items:
            items = await self._list_workflows_api()
        for item in items:
            entry = {
                "name": item.get("name"),
                "relpath": item.get("relpath", ""),
                "format": item.get("format", "unknown"),
                "tokens": {},
                "outputs": item.get("outputs", []),
                "injects": item.get("injects") or {"images": 0, "texts": 0, "videos": 0},
                "nodes": [],
            }
            try:
                obj = json.loads(self._read_workflow(item.get("relpath") or item.get("name")))
                fmt = detect_workflow_format(obj)
                if fmt == "ui":
                    if object_info is None:
                        object_info = await self._get_object_info(
                            aiohttp.ClientTimeout(total=30)
                        )
                    api_wf = convert_ui_to_api(obj, object_info)
                elif fmt == "api":
                    api_wf = obj
                else:
                    api_wf = None
                if api_wf:
                    tokens, outputs, nodes = scan_api_workflow(api_wf, object_info or {})
                    entry["tokens"] = {t: {"type": tokens[t]} for t in tokens}
                    entry["outputs"] = outputs
                    entry["nodes"] = nodes
            except Exception as e:  # noqa: BLE001
                self.log_event(f"能力扫描 {item.get('name')} 失败: {e}")
            workflows.append(entry)

        checkpoints: list = []
        try:
            checkpoints = (await self.comfyui_checkpoints(aiohttp.ClientTimeout(total=20))).get(
                "checkpoints", []
            )
        except Exception:  # noqa: BLE001
            pass
        info = dict(self._last_info or {})
        llm_models = (info.get("openai") or {}).get("models") or []
        gpu = (info.get("comfyui") or {}).get("devices") or []
        queue = (info.get("comfyui") or {}).get("queue") or {"running": 0, "pending": 0}
        return {
            "workflows": workflows,
            "checkpoints": checkpoints,
            "llm_models": llm_models,
            "gpu": gpu,
            "queue": queue,
            "time": time.time(),
        }

    def _read_workflow(self, ref: str) -> str:
        """按名称/相对路径读取一个工作流文件的内容。"""
        root = self._workflows_root()
        if root is None:
            raise ValueError("未配置 comfyui.userdata_dir，无法读取工作流文件")
        p = Path(ref)
        if not p.is_absolute():
            p = root / p
            if not p.exists():
                # 允许只给名字，自动按文件名模糊匹配
                matches = list(root.rglob(Path(ref).name + ".json"))
                if matches:
                    p = matches[0]
        if not p.exists():
            raise FileNotFoundError(f"找不到工作流文件: {ref}")
        return p.read_text(encoding="utf-8-sig")

    async def _get_object_info(self, timeout: aiohttp.ClientTimeout) -> dict:
        """获取 ComfyUI 节点定义（带 10 分钟缓存），仅 UI 格式转换时需要。"""
        now = time.time()
        if self._object_info_cache is not None and now - self._object_info_ts < 600:
            return self._object_info_cache
        async with self.session.get(self._comfyui_url("/object_info"), timeout=timeout) as r:
            data = await r.json()
        self._object_info_cache = data
        self._object_info_ts = now
        return data

    # ---------------- ComfyUI ----------------

    def _comfyui_url(self, path: str) -> str:
        base = str(self.cfg["comfyui"]["base_url"]).rstrip("/")
        return base + path

    async def svc_comfyui(self, rid: str, payload: dict):
        action = payload.get("action", "proxy")
        timeout = aiohttp.ClientTimeout(total=int(self.cfg["comfyui"].get("timeout", 600)) + 120)
        if action == "run_workflow":
            return await self.run_workflow(rid, payload, timeout)
        if action == "checkpoints":
            return await self.comfyui_checkpoints(timeout)
        if action == "queue":
            async with self.session.get(self._comfyui_url("/queue"), timeout=timeout) as r:
                data = await r.json()
            qr, qp = data.get("queue_running", []), data.get("queue_pending", [])
            return {"running": len(qr), "pending": len(qp)}
        # 通用代理：method/path/body 原样转发给本地 ComfyUI
        method = str(payload.get("method") or "GET").upper()
        path = payload.get("path") or "/"
        body = payload.get("body")
        async with self.session.request(method, self._comfyui_url(path), json=body, timeout=timeout) as r:
            try:
                j = await r.json()
            except Exception:  # noqa: BLE001
                j = None
            return {"status": r.status, "json": j}

    async def svc_media_get(self, rid: str, payload: dict):
        """按文件名从本地 ComfyUI 拉取产物，base64 分块回传（每块 ≤400KB）。

        任务响应不再携带 base64（视频可达数 MB，跨公网单帧传输易被中间层截断）；
        云端需要发产物时，先调本服务把真实文件从本地分块拉走。
        """
        filename = str(payload.get("filename") or "").strip()
        if not filename:
            raise ValueError("缺少 filename")
        params = {
            "filename": filename,
            "subfolder": str(payload.get("subfolder") or ""),
            "type": str(payload.get("type") or "output"),
        }
        timeout = aiohttp.ClientTimeout(total=180, sock_read=120)
        async with self.session.get(self._comfyui_url("/view"), params=params, timeout=timeout) as r:
            if r.status != 200:
                raise RuntimeError(f"本地产物不存在（HTTP {r.status}）: {filename}")
            raw = await r.read()
        b64 = base64.b64encode(raw).decode("ascii")
        logger.info(f"[remote_link] media_get {filename}: {len(raw)} bytes, b64_len={len(b64)}")
        CHUNK = 400 * 1024  # 每帧 base64 约 400KB，远低于常见的 4MB WS 帧限制
        for i in range(0, len(b64), CHUNK):
            await self._send({"type": "stream_chunk", "id": rid, "data": b64[i : i + CHUNK]})
        return {
            "filename": filename,
            "bytes": len(raw),
            "b64_len": len(b64),
            "chunks": (len(b64) + CHUNK - 1) // CHUNK,
        }

    async def comfyui_checkpoints(self, timeout: aiohttp.ClientTimeout) -> dict:
        """列出本地 ComfyUI 的 checkpoint 模型（兼容新版 /models/checkpoints 与老版 /object_info）。"""
        try:
            async with self.session.get(self._comfyui_url("/models/checkpoints"), timeout=timeout) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        return {"checkpoints": data}
        except Exception:  # noqa: BLE001
            pass
        async with self.session.get(
            self._comfyui_url("/object_info/CheckpointLoaderSimple"), timeout=timeout
        ) as r:
            data = await r.json()
        info = (data.get("CheckpointLoaderSimple") or {}).get("input", {}).get("required", {})
        ckpts = (info.get("ckpt_name") or [[], {}])[0] if info else []
        return {"checkpoints": ckpts}

    def _default_workflow(self) -> str:
        """读取内置默认文生图工作流模板。"""
        p = self.cfg["comfyui"].get("default_workflow_file") or ""
        if p:
            p = Path(p)
            if not p.is_absolute():
                p = app_dir() / p
        else:
            p = bundled_file("default_workflow_api.json")
        return p.read_text(encoding="utf-8-sig")

    async def _comfy_upload(self, kind: str, data_b64: str, filename: str, timeout) -> str:
        """把 base64 数据上传到 ComfyUI 的 input 目录（/upload/image 或 /upload/video），返回服务器上的文件名。"""
        try:
            raw = base64.b64decode(data_b64)
        except Exception:  # noqa: BLE001
            raise ValueError(f"{kind} 上传失败：数据不是合法 base64") from None
        endpoint = "/upload/image" if kind == "image" else "/upload/video"
        field = "image" if kind == "image" else "video"
        mime = "application/octet-stream"
        if kind == "image" and raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
            if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                filename += ".png"
        if kind == "video" and not filename.lower().endswith((".mp4", ".webm", ".mov", ".avi", ".mkv")):
            filename += ".mp4"
        form = aiohttp.FormData()
        form.add_field(field, raw, filename=filename, content_type=mime)
        form.add_field("overwrite", "true")
        form.add_field("type", "input")
        async with self.session.post(
            self._comfyui_url(endpoint), data=form, timeout=aiohttp.ClientTimeout(total=120)
        ) as r:
            if r.status >= 400:
                raise RuntimeError(f"ComfyUI {kind} 上传失败（HTTP {r.status}）: {(await r.text())[:300]}")
            try:
                j = await r.json()
            except Exception:  # noqa: BLE001
                j = {}
        name = j.get("name") or filename
        self.log_event(f"已上传{('图片' if kind == 'image' else '视频')}: {name}")
        return str(name)

    async def apply_injects(self, prompt: dict, inject: dict, timeout) -> None:
        """按约定入口节点注入输入（提交 /prompt 前调用）：

        - 文本：Simple String 节点，智能合并；
        - 图片：优先 ETN_LoadImageBase64（base64 直接注入），
          其次 LoadImage 且 widget 值为 __IMAGE__ 占位的节点（先上传到 ComfyUI 再填文件名）；
        - 视频：VHS_LoadVideo 且 widget 值为 __VIDEO__ 占位的节点（先上传再填文件名）。

        数量严格匹配（错位是灾难）；只有显式占位标记的 LoadImage/VHS 才是注入目标。
        """
        images = list(inject.get("images") or [])
        texts = list(inject.get("texts") or [])
        videos = list(inject.get("videos") or [])

        inject_texts(prompt, texts)

        etn_nodes = sorted(
            (int(k), v) for k, v in prompt.items()
            if isinstance(v, dict) and (v.get("class_type") or "") in INJECT_IMAGE_TYPES
        )
        load_nodes = sorted(
            (int(k), v) for k, v in prompt.items()
            if isinstance(v, dict)
            and (v.get("class_type") or "") == "LoadImage"
            and IMAGE_SLOT_RE.match(str(_slot_value(v, "image") or ""))
        )
        vid_nodes = sorted(
            (int(k), v) for k, v in prompt.items()
            if isinstance(v, dict)
            and (v.get("class_type") or "") in INJECT_VIDEO_TYPES
            and VIDEO_SLOT_RE.match(str(_slot_value(v, "video") or ""))
        )

        if images and len(images) != len(etn_nodes) + len(load_nodes):
            raise ValueError(
                f"工作流有 {len(etn_nodes) + len(load_nodes)} 个图片入口，但传入了 {len(images)} 张图片"
            )
        if images and not (etn_nodes or load_nodes):
            raise ValueError("传入了图片，但工作流里没有图片入口（ETN_LoadImageBase64 或 __IMAGE__ 占位的 LoadImage）")
        if videos and len(videos) != len(vid_nodes):
            raise ValueError(f"工作流有 {len(vid_nodes)} 个视频入口，但传入了 {len(videos)} 段视频")
        if videos and not vid_nodes:
            raise ValueError("传入了视频，但工作流里没有 __VIDEO__ 占位的 VHS_LoadVideo 入口")

        # 图片：ETN 直接填 base64；多余/仅有 LoadImage 占位时走上传
        for (_nid, node), b64 in zip(etn_nodes, images):
            node["inputs"]["image"] = b64
        remaining = images[len(etn_nodes):]
        for (_nid, node), b64 in zip(load_nodes, remaining):
            ext = ".png"
            filename = f"yunxin_{uuid.uuid4().hex[:8]}{ext}"
            name = await self._comfy_upload("image", b64, filename, timeout)
            node["inputs"]["image"] = name

        # 视频：上传后填文件名
        for (_nid, node), b64 in zip(vid_nodes, videos):
            filename = f"yunxin_{uuid.uuid4().hex[:8]}.mp4"
            name = await self._comfy_upload("video", b64, filename, timeout)
            node["inputs"]["video"] = name

    async def run_workflow(self, rid: str, payload: dict, timeout: aiohttp.ClientTimeout) -> dict:
        """执行一个工作流：加载（含 UI→API 转换）→ 占位符替换 → 入口注入 → 提交
        → 原生 WS 推送等待完成（老版本回退轮询）→ 收集全部产物。"""
        self._current_progress = {
            "text": "正在提交工作流…",
            "percent": None,
            "updated_at": time.time(),
        }
        _run_started = time.time()
        params = payload.get("params") or {}
        inject = payload.get("inject") or {}
        wf_ref = str(payload.get("workflow") or "")
        run_timeout = int(payload.get("timeout") or self.cfg["comfyui"].get("timeout", 600))

        # 1. 加载工作流
        if wf_ref:
            obj = json.loads(self._read_workflow(wf_ref))
            fmt = detect_workflow_format(obj)
            if fmt == "ui":
                object_info = await self._get_object_info(timeout)
                prompt = convert_ui_to_api(obj, object_info)
            elif fmt == "api":
                prompt = obj
            else:
                raise ValueError(f"无法识别工作流格式: {wf_ref}")
        else:
            prompt = json.loads(self._default_workflow())

        # 2. 占位符替换：模板中 `"__TOKEN__"`（含引号）→ 参数的 JSON 编码
        wf_str = json.dumps(prompt, ensure_ascii=False)
        for key, value in params.items():
            if key == "texts":  # 文本入口槽位（Simple String），不走占位符替换
                continue
            if key == "checkpoint" and value in ("", None):
                value = self.cfg["comfyui"].get("default_checkpoint", "")
            token = '"__%s__"' % key.upper()
            wf_str = wf_str.replace(token, json.dumps(value, ensure_ascii=False))
        leftover = re.findall(r'"__[A-Z0-9_]+__"', wf_str)
        if leftover:
            names = [t.strip('"').strip("_").strip("__") for t in leftover]
            # 图片/视频入口占位（__IMAGE__/__VIDEO__）由 apply_injects 处理，不算缺失参数
            names = [
                n for n in names
                if not IMAGE_SLOT_RE.match(f"__{n}__") and not VIDEO_SLOT_RE.match(f"__{n}__")
            ]
            if names:
                raise ValueError(
                    f"工作流中存在未提供的参数: {', '.join(sorted(set(names)))}。"
                    "请在插件预设的 params 里为这些 token 定义参数"
                )
        prompt_data = json.loads(wf_str)
        # 兜底清理：内部约定 key 绝不允许提交给 ComfyUI（即使未触发注入）
        prompt_data.pop(ROLE_HINTS_KEY, None)

        # 2.5 约定入口节点注入（图片 base64 / 上传文件 / 文本 / 视频）+ 负种子随机化
        merged_inject = dict(inject)
        if isinstance(params.get("texts"), list):
            merged_inject["texts"] = params["texts"]
        await self.apply_injects(prompt_data, merged_inject, timeout)
        prompt_data = randomize_negative_seeds(prompt_data)
        # 2.6 无占位符工作流的自动提示词注入（识别正/负提示词框填入，占位符已在上面替换过则无副作用）
        auto_inject_prompt(prompt_data, params)
        # 2.7 filename_prefix 的 %date:格式% 替换（与 ComfyUI 前端提交时行为一致）
        substitute_prefix_dates(prompt_data)

        # 3. 先连 ComfyUI 原生 WS（带 clientId），再提交任务（与官方示例顺序一致，
        #    避免完成事件先于 WS 连接到达而丢失）
        client_id = "yunxin-" + uuid.uuid4().hex[:10]
        ws_url = (
            self._comfyui_url("/ws").replace("http://", "ws://").replace("https://", "wss://")
            + f"?clientId={client_id}"
        )
        comfy_ws = None
        try:
            comfy_ws = await self.session.ws_connect(
                ws_url, max_msg_size=64 * 1024 * 1024, timeout=aiohttp.ClientTimeout(total=60)
            )
        except Exception as e:  # noqa: BLE001
            self.log_event(f"ComfyUI WS 不可用（{e}），回退轮询模式")

        # 提交前释放显存：卸载上次任务残留的模型，避免大模型（视频）在 8GB 显存下
        # 因残留占用导致 HostBuffer.read_file_slice failed（图片→视频衔接时常见）
        try:
            async with self.session.post(
                self._comfyui_url("/free"),
                json={"unload_models": True, "free_memory": True},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as _r:
                pass
        except Exception:  # noqa: BLE001
            pass

        async with self.session.post(
            self._comfyui_url("/prompt"),
            json={"prompt": prompt_data, "client_id": client_id},
            timeout=timeout,
        ) as r:
            if r.status >= 400:
                raw = await r.text()
                summary = parse_comfyui_400_summary(raw)
                raise RuntimeError(
                    f"ComfyUI 拒绝任务（HTTP {r.status}）："
                    f"{summary or raw[:500]}"
                )
            resp = await r.json()
        if "prompt_id" not in resp:
            raise RuntimeError(f"ComfyUI 拒绝任务: {json.dumps(resp, ensure_ascii=False)[:500]}")
        prompt_id = resp["prompt_id"]

        # 任务失败/超时/中断时，主动 interrupt 释放 ComfyUI（避免空转占显存）
        async def _interrupt_task():
            try:
                await self.session.post(self._comfyui_url("/interrupt"), timeout=timeout)
                self.log_event(f"已中断 ComfyUI 任务 {prompt_id}（失败/超时清理）")
            except Exception:  # noqa: BLE001
                pass

        # 4. 优先用 ComfyUI 原生 WS 推送等待完成（实时进度）；失败回退轮询
        deadline = time.time() + run_timeout
        entry = None
        ws_completed = False
        try:
            if comfy_ws is not None:
                try:
                    await asyncio.wait_for(
                        self._comfy_ws_wait(comfy_ws, prompt_id, rid, deadline, prompt_data),
                        timeout=run_timeout,
                    )
                    ws_completed = True
                except asyncio.TimeoutError:
                    raise TimeoutError(f"ComfyUI 执行超时（>{run_timeout}s），任务仍在本地继续运行") from None
                except Exception as e:  # noqa: BLE001
                    self.log_event(f"WS 等待失败（{e}），回退轮询模式")
                finally:
                    try:
                        await comfy_ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            if not ws_completed:
                last_progress = 0.0
                while time.time() < deadline:
                    await asyncio.sleep(1.5)
                    async with self.session.get(
                        self._comfyui_url(f"/history/{prompt_id}"), timeout=timeout
                    ) as r:
                        hist = await r.json()
                    entry = hist.get(prompt_id)
                    if entry:
                        status = entry.get("status") or {}
                        if status.get("status_str") == "error":
                            await _interrupt_task()
                            raise RuntimeError(
                                f"ComfyUI 生成出错：{json.dumps(entry, ensure_ascii=False)[:800]}"
                            )
                        if status.get("completed"):
                            break
                    if time.time() - last_progress > 15:
                        last_progress = time.time()
                        self._current_progress = {
                            "text": f"ComfyUI 执行中…（{prompt_id}）",
                            "percent": None,
                            "updated_at": time.time(),
                        }
                        await self._try_send(
                            {"type": "progress", "id": rid, "data": f"ComfyUI 执行中…（{prompt_id}）"}
                        )
                else:
                    await _interrupt_task()
                    raise TimeoutError(f"ComfyUI 执行超时（>{run_timeout}s），任务仍在本地继续运行")

            # 5. 收集全部产物：图片 / 视频 / GIF / 音频 + ShowText 文本输出
            async with self.session.get(
                self._comfyui_url(f"/history/{prompt_id}"), timeout=timeout
            ) as r:
                hist = await r.json()
            entry = hist.get(prompt_id) or {}
            if (entry.get("status") or {}).get("status_str") == "error":
                await _interrupt_task()
                raise RuntimeError(f"ComfyUI 生成出错：{json.dumps(entry, ensure_ascii=False)[:800]}")
        except Exception:
            await _interrupt_task()
            raise
        files = []
        for node_id, node_out in (entry or {}).get("outputs", {}).items():
            node_meta = prompt_data.get(str(node_id)) or {}
            node_type = node_meta.get("class_type") or ""
            for field, kind in (
                ("images", "image"),
                ("videos", "video"),
                ("gifs", "video"),  # VHS 等视频节点把产物放 gifs 字段，按扩展名再细分
                ("audio", "audio"),
            ):
                for index, f in enumerate(node_out.get(field, [])):
                    filename = f["filename"]
                    # 按扩展名修正类型：.mp4/.webm/.mov/.mkv 是视频，.gif 是动图，.png/.jpg 是图片
                    ext = Path(filename).suffix.lower()
                    real_kind = kind
                    if ext in (".mp4", ".webm", ".mov", ".mkv", ".avi"):
                        real_kind = "video"
                    elif ext in (".png", ".jpg", ".jpeg", ".webp"):
                        real_kind = "image"
                    elif ext in (".mp3", ".wav", ".ogg", ".flac"):
                        real_kind = "audio"
                    elif ext == ".gif":
                        real_kind = "image"
                    # 注意：响应里不带 base64（视频可达数 MB，跨公网单帧传输易被中间层截断）。
                    # 产物文件按需通过 media_get 服务分块拉取（见 svc_media_get）。
                    files.append(
                        {
                            "kind": real_kind,
                            "filename": filename,
                            "mime": EXT_MIME.get(ext, "application/octet-stream"),
                            "subfolder": f.get("subfolder", ""),
                            "type": f.get("type", "output"),
                            # 产物元数据：来自哪个节点、第几个、是否主产物（文件名前缀 Final_ 约定）
                            "node": node_id,
                            "node_type": node_type,
                            "index": index,
                            "main": filename.lower().startswith("final"),
                        }
                    )
        # 主产物排最前（约定：主输出节点的 filename_prefix 用 Final_ 开头）
        files.sort(key=lambda x: (0 if x.get("main") else 1, x.get("node") or "", x.get("index") or 0))
        # 去重：有正式产物时，跳过 PreviewImage 的临时预览文件（ComfyUI_temp_*）
        if files:
            formal = [f for f in files if not str(f.get("filename") or "").startswith("ComfyUI_temp_")]
            if formal:
                files = formal
        texts = self._extract_text_outputs(prompt_data, hist, prompt_id)
        self._current_progress = None  # 任务完成，清空进度
        # 记录生成历史（GUI 历史页展示）
        try:
            self._history.appendleft({
                "t": time.time(),
                "workflow": wf_ref or "内置模板",
                "prompt": str(params.get("PROMPT") or params.get("prompt") or "")[:200],
                "duration": round(time.time() - _run_started, 1),
                "files": [f.get("filename") for f in files][:8],
            })
        except Exception:  # noqa: BLE001
            pass
        return {"prompt_id": prompt_id, "files": files, "texts": texts}

    def _extract_text_outputs(self, workflow_data: dict, history_data: dict, prompt_id: str) -> list[str]:
        """提取 ShowText（pythongosssss 自定义脚本）节点的文本输出。

        参考社区插件 astrbot_plugin_comfyui 的做法：文本在 history 的 prompts 里按节点 id 取 inputs.text_0。
        """
        showtext_nodes = {
            nid: nd
            for nid, nd in workflow_data.items()
            if isinstance(nd, dict) and nd.get("class_type") == "ShowText|pysssss"
        }
        if not showtext_nodes:
            return []
        history_entry = history_data.get(prompt_id) if isinstance(history_data, dict) else None
        out = []
        for node_id, nd in showtext_nodes.items():
            text_content = None
            if history_entry and isinstance(history_entry, dict) and "prompts" in history_entry:
                for item in history_entry["prompts"]:
                    if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]) == node_id:
                        inp = item[1].get("inputs", {}) if isinstance(item[1], dict) else {}
                        text_content = inp.get("text_0")
                        break
            if text_content is None:
                text_content = (nd.get("inputs") or {}).get("text_0")
            if isinstance(text_content, str):
                if "</think>" in text_content:
                    text_content = text_content.split("</think>", 1)[1]
                t = text_content.strip()
                if t:
                    out.append(t)
        return out

    async def _comfy_ws_wait(self, ws, prompt_id: str, rid: str, deadline: float, prompt_data: dict | None = None):
        """在已建立的 ComfyUI WS 连接上等待任务完成（executing 事件 node=None）。

        同时把 progress/executing 事件转发为隧道进度消息（含当前执行节点，云端可做流程可视化）。
        """
        prompt_data = prompt_data or {}
        last_pct = -1
        last_node: dict | None = None
        async for msg in ws:
            if time.time() > deadline:
                raise asyncio.TimeoutError
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "executing":
                    d = data.get("data") or {}
                    if d.get("node") is None and d.get("prompt_id") == prompt_id:
                        return
                    node_id = d.get("node")
                    if node_id is not None and d.get("prompt_id") == prompt_id:
                        node_type = (prompt_data.get(str(node_id)) or {}).get("class_type") or ""
                        last_node = {"node": str(node_id), "node_type": node_type}
                        self._current_progress = {
                            "text": f"正在执行节点 {node_type or node_id}…",
                            "percent": None,
                            "node": str(node_id),
                            "node_type": node_type,
                            "updated_at": time.time(),
                        }
                        await self._try_send(
                            {
                                "type": "progress",
                                "id": rid,
                                "data": {
                                    "text": f"正在执行节点 {node_type or node_id}…",
                                    "percent": None,
                                    "node": str(node_id),
                                    "node_type": node_type,
                                },
                            }
                        )
                elif mtype == "execution_error":
                    raise RuntimeError(f"ComfyUI 执行出错：{json.dumps(data, ensure_ascii=False)[:500]}")
                elif mtype == "progress":
                    d = data.get("data") or {}
                    val, maxv = d.get("value"), d.get("max")
                    if val and maxv:
                        pct = int(val / maxv * 100)
                        if pct != last_pct:
                            last_pct = pct
                            node_info = last_node or {}
                            self._current_progress = {
                                "text": f"ComfyUI 进度 {pct}%（{prompt_id}）",
                                "percent": pct,
                                "node": node_info.get("node"),
                                "node_type": node_info.get("node_type"),
                                "updated_at": time.time(),
                            }
                            await self._try_send(
                                {
                                    "type": "progress",
                                    "id": rid,
                                    "data": {
                                        "text": f"ComfyUI 进度 {pct}%（{prompt_id}）",
                                        "percent": pct,
                                        "node": node_info.get("node"),
                                        "node_type": node_info.get("node_type"),
                                    },
                                }
                            )
            elif msg.type == aiohttp.WSMsgType.BINARY:
                continue  # 潜在预览帧，暂不转发
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError(f"ComfyUI WS 错误: {ws.exception()}")

    # ---------------- OpenAI 兼容接口 ----------------

    def _openai_url(self, path: str) -> str:
        base = str(self.cfg["openai"]["base_url"]).rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base + path

    async def svc_openai(self, rid: str, payload: dict):
        method = str(payload.get("method") or "POST").upper()
        path = payload.get("path") or "/v1/chat/completions"
        body = payload.get("body") or {}
        headers = {"Content-Type": "application/json"}
        api_key = str(self.cfg["openai"].get("api_key", ""))
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = aiohttp.ClientTimeout(
            total=int(self.cfg["openai"].get("timeout", 300)) + 60, sock_read=300
        )

        if body.get("stream"):
            # SSE 流式：把本地接口的每个分块原样转发回云端插件
            async with self.session.request(
                method, self._openai_url(path), json=body, headers=headers, timeout=timeout
            ) as r:
                if r.status != 200:
                    try:
                        j = await r.json()
                    except Exception:  # noqa: BLE001
                        j = {"raw": (await r.text())[:500]}
                    raise RuntimeError(
                        f"本地 LLM 返回 {r.status}: {json.dumps(j, ensure_ascii=False)[:400]}"
                    )
                async for chunk in r.content.iter_chunked(4096):
                    if not chunk:
                        continue
                    text = chunk.decode("utf-8", errors="ignore")
                    await self._send({"type": "stream_chunk", "id": rid, "data": text})
            return {"status": 200, "streamed": True, "json": {}}

        async with self.session.request(
            method, self._openai_url(path), json=body, headers=headers, timeout=timeout
        ) as r:
            try:
                j = await r.json()
            except Exception:  # noqa: BLE001
                raise RuntimeError(
                    f"本地 LLM 返回非 JSON（HTTP {r.status}）: {(await r.text())[:300]}"
                ) from None
            return {"status": r.status, "json": j}

    # ---------------- Shell ----------------

    async def svc_shell(self, payload: dict):
        shell_cfg = self.cfg.get("shell") or {}
        if not shell_cfg.get("enabled"):
            raise PermissionError("shell 未在本地代理配置中启用（shell.enabled）")
        command = payload.get("command", "")
        if not command:
            raise ValueError("命令不能为空")
        timeout = int(shell_cfg.get("timeout", 60))
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"命令执行超时（>{timeout}s）") from None
        return {
            "exit_code": proc.returncode,
            "stdout": (out or b"").decode("utf-8", errors="ignore"),
            "stderr": (err or b"").decode("utf-8", errors="ignore"),
        }

    # ---------------- 机器信息 ----------------

    async def collect_info(self) -> dict:
        """采集本机信息与本地服务可达性（供云端 /remote status 与本地看板展示）。"""
        info = {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "version": AGENT_VERSION,
            "time": time.time(),
            "uptime": time.time() - self._started,
        }
        info["comfyui"] = await self._check_comfyui()
        info["openai"] = await self._check_openai()
        info["openai_services"] = await self._check_openai_services()
        return info

    async def _check_openai_services(self) -> list[dict]:
        """探测配置的多个本地 LLM 服务（GUI 多服务状态总览用）。"""
        services = self.cfg.get("openai_services") or []
        out = []
        for svc in services[:8]:
            name = str(svc.get("name") or "服务")
            base = str(svc.get("base_url") or "").rstrip("/")
            if not base:
                continue
            entry = {"name": name, "ok": False, "error": "", "base_url": base, "models": []}
            headers = {}
            if svc.get("api_key"):
                headers["Authorization"] = f"Bearer {svc['api_key']}"
            try:
                async with self.session.get(base + "/v1/models", headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    data = await r.json()
                if r.status == 200:
                    entry["ok"] = True
                    models_raw = data.get("data") or data.get("models") or []
                    entry["models"] = [
                        m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
                        for m in models_raw
                    ][:20]
                else:
                    entry["error"] = f"HTTP {r.status}"
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"无法访问 {base}：{type(e).__name__}"
            out.append(entry)
        return out

    async def _check_comfyui(self) -> dict:
        base = str(self.cfg["comfyui"]["base_url"]).rstrip("/")
        out = {"ok": False, "error": "", "base_url": base, "devices": [], "queue": {"running": 0, "pending": 0}}
        timeout = aiohttp.ClientTimeout(total=6)
        try:
            async with self.session.get(base + "/system_stats", timeout=timeout) as r:
                data = await r.json()
            out["ok"] = True
            for d in data.get("devices", []):
                out["devices"].append(
                    {
                        "name": d.get("name", "?"),
                        "vram_total_gb": round((d.get("vram_total") or 0) / 1024**3, 1),
                        "vram_free_gb": round((d.get("vram_free") or 0) / 1024**3, 1),
                    }
                )
        except Exception as e:  # noqa: BLE001
            out["error"] = f"无法访问 {base}：{type(e).__name__}"
        # 顺带取队列状态（供概览页展示）
        try:
            async with self.session.get(base + "/queue", timeout=aiohttp.ClientTimeout(total=4)) as r:
                q = await r.json()
            out["queue"] = {
                "running": len(q.get("queue_running") or []),
                "pending": len(q.get("queue_pending") or []),
            }
        except Exception:  # noqa: BLE001
            pass
        return out

    async def _check_openai(self) -> dict:
        base = str(self.cfg["openai"]["base_url"]).rstrip("/")
        out = {"ok": False, "error": "", "base_url": base, "models": []}
        timeout = aiohttp.ClientTimeout(total=6)
        headers = {}
        if self.cfg["openai"].get("api_key"):
            headers["Authorization"] = f"Bearer {self.cfg['openai']['api_key']}"
        try:
            async with self.session.get(self._openai_url("/v1/models"), headers=headers, timeout=timeout) as r:
                data = await r.json()
            if r.status == 200:
                out["ok"] = True
                # OpenAI 格式 {"data": [...]}；Ollama 格式 {"models": [...]}，兼容两种
                models_raw = data.get("data") or data.get("models") or []
                out["models"] = [
                    m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
                    for m in models_raw
                ][:20]
            else:
                out["error"] = f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001
            out["error"] = f"无法访问 {base}：{type(e).__name__}"
        return out

    # ---------------- 本地看板 ----------------

    async def _dashboard_main(self):
        """本地状态看板：只绑定 127.0.0.1，供本机浏览器查看连接状态/工作流/日志。"""
        port = int(self.cfg.get("dashboard_port", 8899))
        host = str(self.cfg.get("dashboard_host", "127.0.0.1"))
        app = web.Application()
        app.router.add_get("/", self._dashboard_page)
        app.router.add_get("/api/status", self._dashboard_status)
        app.router.add_get("/api/workflows", self._dashboard_workflows)
        app.router.add_post("/api/reconnect", self._dashboard_reconnect)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
            self._dashboard_runner = runner
            self.log_event(f"本地看板已启动: http://{host}:{port}")
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            self.log_event(f"本地看板启动失败: {e}")
        finally:
            await runner.cleanup()

    async def _dashboard_status(self, request):
        connected = self.ws is not None and not self.ws.closed
        prog = self._current_progress
        if prog and time.time() - float(prog.get("updated_at") or 0) > 30:
            prog = None  # 超 30 秒未更新视为结束
        return web.json_response(
            {
                "connected": connected,
                "server_url": self.cfg.get("server_url", ""),
                "connected_seconds": int(time.time() - self._connected_at) if connected else 0,
                "uptime": time.time() - self._started,
                "last_error": self._last_error,
                "progress": prog,
                "events": [{"time": t, "text": x} for t, x in list(self._events)[-50:]],
            }
        )

    async def _dashboard_workflows(self, request):
        result = await self.svc_workflows_list()
        return web.json_response(result)

    async def _dashboard_reconnect(self, request):
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        return web.json_response({"ok": True, "message": "已请求重连"})

    async def _dashboard_page(self, request):
        return web.Response(
            text=_DASHBOARD_HTML,
            content_type="text/html",
            charset="utf-8",
        )


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>云信互联 本地代理看板</title>
<style>
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px; background: #111; color: #ddd; }
  h1 { font-size: 20px; }
  .card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .ok { color: #4caf50; } .bad { color: #f44336; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #333; padding: 6px 10px; text-align: left; font-size: 14px; }
  pre { white-space: pre-wrap; font-size: 12px; color: #9e9e9e; max-height: 260px; overflow: auto; }
  button { background: #2d6cdf; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; }
</style>
</head>
<body>
<h1>云信互联 本地代理看板</h1>
<div class="card" id="status">加载中…</div>
<div class="card">
  <b>操作</b><br>
  <button onclick="fetch('/api/reconnect',{method:'POST'}).then(()=>refresh())">强制重连</button>
  <button onclick="loadWorkflows()">刷新工作流</button>
</div>
<div class="card"><b>工作流</b><div id="wfs">未加载</div></div>
<div class="card"><b>最近事件</b><pre id="events"></pre></div>
<script>
async function refresh() {
  const r = await fetch('/api/status');
  const s = await r.json();
  document.getElementById('status').innerHTML =
    '<b>连接状态:</b> ' + (s.connected
      ? '<span class="ok">已连接</span>（' + s.connected_seconds + 's）'
      : '<span class="bad">未连接</span>') +
    '<br><b>云端:</b> ' + s.server_url +
    '<br><b>运行时长:</b> ' + Math.round(s.uptime) + 's' +
    (s.last_error ? '<br><b>最近错误:</b> <span class="bad">' + s.last_error + '</span>' : '');
  document.getElementById('events').textContent =
    (s.events || []).map(e => new Date(e.time * 1000).toLocaleTimeString() + ' ' + e.text).join('\\n');
}
async function loadWorkflows() {
  const r = await fetch('/api/workflows');
  const d = await r.json();
  const ws = d.workflows || [];
  document.getElementById('wfs').innerHTML = ws.length
    ? '<table><tr><th>名称</th><th>路径</th><th>格式</th><th>大小</th></tr>' +
      ws.map(w => '<tr><td>' + w.name + '</td><td>' + w.relpath + '</td><td>' +
        (w.format === 'api' ? 'API' : w.format === 'ui' ? 'UI(自动转换)' : w.format) +
        '</td><td>' + w.size + '</td></tr>').join('') + '</table>'
    : '（未配置 comfyui.userdata_dir，或目录为空）';
}
refresh(); loadWorkflows(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="云信互联 本地代理（连接云端 AstrBot 插件）")
    parser.add_argument(
        "-c", "--config", default=None, help="配置文件路径，默认同目录 agent_config.json"
    )
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        logger.error(
            f"找不到配置文件：{args.config or DEFAULT_CONFIG}。"
            "请复制 agent_config.example.json 为 agent_config.json 并填写。"
        )
        sys.exit(1)
    agent = LocalAgent(cfg)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        logger.info("已退出")


if __name__ == "__main__":
    main()
