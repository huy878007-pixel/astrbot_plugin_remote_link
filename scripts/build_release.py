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

VERSION = "v0.1.0"

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
    """本地代理发布目录：exe + 配置模板 + 启动脚本 + 说明。
    需要先执行 PyInstaller 打包生成 dist/YunxinAgent.exe。"""
    dist_exe = ROOT / "dist" / "YunxinAgent.exe"
    outdir = RELEASE / "yunxin-local"
    outdir.mkdir(exist_ok=True)
    if dist_exe.exists():
        import shutil
        shutil.copy2(dist_exe, outdir / "YunxinAgent.exe")
    shutil.copy2(ROOT / "agent" / "agent_config.example.json", outdir / "agent_config.example.json")
    shutil.copy2(ROOT / "agent" / "start_agent.bat", outdir / "启动本地代理.bat")
    print(f"[local] -> {outdir} (exe 存在: {dist_exe.exists()})")
    return outdir


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
