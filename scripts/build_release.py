#!/usr/bin/env python3
"""构建云信互联发布包（插件 zip / 源码 zip）。

用法（在项目根目录下）：
    python scripts/build_release.py          # 构建全部
    python scripts/build_release.py plugin   # 仅插件 zip
    python scripts/build_release.py src      # 仅源码 zip
    python scripts/build_release.py local    # 仅本地代理 exe 打包目录

产物输出到 release/ 目录。
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
RELEASE.mkdir(exist_ok=True)

VERSION = "v0.1.1"

# ---- 插件 zip：只含云端 AstrBot 侧运行所需 ----
PLUGIN_INCLUDE_TOP = {
    "CHANGELOG.md", "LICENSE", "README.md", "_conf_schema.json",
    "docs/DESIGN.md", "logo.png", "main.py", "metadata.yaml",
    "__init__.py",
}
PLUGIN_INCLUDE_PREFIX = ("pages/console/", "tools/")
# 注：docs/screenshots 不打进插件包（约 14MB，运行时不需要；GitHub 仓库里 README 直接读仓库图）
PLUGIN_SKIP_DIRS = {"__pycache__", "test", "preview", "release", "agent", "scripts", "build", "dist", ".git"}

# ---- 源码 zip：完整源码（不含构建产物/运行数据） ----
SRC_SKIP_DIRS = {"__pycache__", "release", ".git", ".idea", ".vscode", "dist", "build", "data"}


def walk_rel(include_top, include_prefix, skip_dirs):
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        dirnames[:] = [d for d in dirnames
                       if d not in skip_dirs and not (rel_dir == "." and d == "assets")]
        for fn in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, fn)).replace("\\", "/")
            if rel.endswith((".pyc", ".zip", ".pyz")):
                continue
            if rel in include_top or rel.startswith(include_prefix):
                files.append(rel)
    return sorted(set(files))


def build_plugin():
    files = walk_rel(PLUGIN_INCLUDE_TOP, PLUGIN_INCLUDE_PREFIX, PLUGIN_SKIP_DIRS)
    out = RELEASE / f"yunxin-plugin-{VERSION}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(ROOT / rel, rel)
    print(f"[plugin] {len(files)} files -> {out.name} ({os.path.getsize(out)//1024} KB)")
    return out


def build_src():
    files = walk_rel(set(), "", SRC_SKIP_DIRS)
    out = RELEASE / f"yunxin-src-{VERSION}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(ROOT / rel, rel)
    print(f"[src] {len(files)} files -> {out.name} ({os.path.getsize(out)//1024} KB)")
    return out


def build_local():
    """本地端发行版：exe + 配置模板 + 启动脚本 + 说明，打包成 zip（开箱即用）。

    安全：只放 agent_config.example.json 占位模板，绝不打包本机真实配置。
    需要先执行 PyInstaller：python -m PyInstaller --noconfirm YunxinAgent.spec
    """
    import shutil

    dist_exe = ROOT / "dist" / "YunxinAgent.exe"
    if not dist_exe.exists():
        print("[local] 缺少 dist/YunxinAgent.exe，请先运行："
              "python -m PyInstaller --noconfirm YunxinAgent.spec")
        return None

    stage = RELEASE / "_stage_local"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # 1) 主程序
    shutil.copy2(dist_exe, stage / "YunxinAgent.exe")
    # 2) 配置模板（占位，不含任何真实地址/token）
    shutil.copy2(ROOT / "agent" / "agent_config.example.json",
                 stage / "agent_config.example.json")
    # 3) 启动脚本
    (stage / "启动本地代理.bat").write_text(
        "@echo off\r\n"
        "rem 云信互联 本地代理（双击运行）\r\n"
        "cd /d \"%~dp0\"\r\n"
        "echo ============================================\r\n"
        "echo   云信互联 本地代理\r\n"
        "echo   首次运行会自动生成 agent_config.json\r\n"
        "echo   在界面里填云端地址和 token 即可\r\n"
        "echo ============================================\r\n"
        "\"%~dp0YunxinAgent.exe\"\r\n",
        encoding="gbk",
    )
    # 4) 使用说明
    (stage / "使用说明.txt").write_text(
        """云信互联 · 本地代理（Windows x64）
================================================

【快速开始】
1. 双击「YunxinAgent.exe」（或「启动本地代理.bat」）
2. 首次运行会自动生成 agent_config.json 并打开界面
3. 在「配置」页填三样：
   - 云端地址 server_url：ws://你的服务器IP:8468/ws
   - 共享密钥 token：与云端 AstrBot 插件的 auth_token 完全一致
   - ComfyUI 的 user 目录：如 D:/ComfyUI/user
4. 点「保存并重启代理」，状态灯变绿 ●已连接 就好了

【免装环境】
本程序已打包 Python 运行时，无需安装 Python。
需要本机自行运行 ComfyUI（默认 127.0.0.1:8188）
和可选的本地 LLM（Ollama 默认 127.0.0.1:11434）。

【常驻后台】
点窗口右上角 ✕ 会最小化到系统托盘，代理继续运行。
设置页可开启「开机自启」。

【本地看板】
浏览器打开 http://127.0.0.1:8899 查看连接状态、工作流列表、日志。
只绑定本机回环地址，外部访问不到。

【常见问题】
· 连不上：确认云端插件已启动、服务器防火墙放行了 8468、两端 token 一致
· 工作流列表为空：ComfyUI 的 user 目录没配对
· 提示端口 8899 被占用：已有一个实例在运行，先在任务管理器结束多余进程
· 更多排查见项目 README 的「故障排查大全」

【项目地址】
https://github.com/huy878007-pixel/astrbot_plugin_remote_link
""",
        encoding="utf-8",
    )

    out = RELEASE / f"YunxinAgent-{VERSION}-win64.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(stage))
    shutil.rmtree(stage)
    print(f"[local] -> {out.name} ({os.path.getsize(out)//1024//1024} MB)")
    return out


if __name__ == "__main__":
    args = sys.argv[1:] or ["plugin", "src"]
    for a in args:
        a = a.lower()
        if a in ("plugin", "p"):
            build_plugin()
        elif a in ("src", "s"):
            build_src()
        elif a in ("local", "l"):
            build_local()
        else:
            print(f"未知目标: {a}")
