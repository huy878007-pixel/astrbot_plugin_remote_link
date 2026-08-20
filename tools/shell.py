"""LLM 工具构建器：在用户本地电脑上执行 shell 命令（危险功能，双端开关默认关闭）。"""

from astrbot.api import FunctionTool


def build_shell_tool(plugin) -> FunctionTool:
    async def handler(event, command: str):
        yield event.plain_result("⚙️ 正在本地执行…")
        try:
            result = await plugin.local_shell(command)
        except Exception as e:  # noqa: BLE001
            msg = f"❌ 本地 shell 执行失败：{e}"
            yield event.plain_result(msg)
            yield msg
            return
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        text = f"退出码 {result.get('exit_code')}"
        if out:
            text += f"\nstdout:\n{out}"
        if err:
            text += f"\nstderr:\n{err}"
        yield event.plain_result(text[:4000])
        yield text

    return FunctionTool(
        name="remote_shell",
        description=(
            "在用户本地电脑上执行 shell 命令（例如查看 GPU 状态 nvidia-smi、查看 ComfyUI 进程等）。"
            "仅执行只读、安全的诊断类命令，绝不执行删除、下载执行文件等破坏性操作。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要在本地电脑执行的命令"},
            },
            "required": ["command"],
        },
        handler=handler,
    )
