"""示例 ToolKit 工厂 — "如何写一个 ToolKit" 教学用例.

这两个工厂曾是 src/core/tools.py 的内置注册项, 现在运行时没有任何调用方
(2026-08 复查清理: 默认工具由 python_tools.DEFAULT_TOOLKITS 提供).
保留在这里作为完整示例: 展示 ToolKit 的创建 / add_python_tool /
input_schema 声明 / handler 编写.

用法 (照抄这个模式写自己的工具类):
    from src.core.tools import ToolKit

    def create_my_toolkit() -> ToolKit:
        tk = ToolKit(name="my_tools", description="我的工具")
        def _handler(args: dict) -> str:
            return "结果"
        tk.add_python_tool("my_tool", "工具描述",
                           {"type": "object", "properties": {}}, _handler)
        return tk

注册到系统: 在 src/python_tools/__init__.py 的 DEFAULT_TOOLKITS 加一行工厂,
角色 (含新入职) 即自动获得.
"""

from __future__ import annotations

from src.core.tools import ToolKit


def create_coding_toolkit() -> ToolKit:
    """Coding toolkit: file operations, code editing, command execution."""
    import subprocess
    from pathlib import Path

    tk = ToolKit(name="coding", description="File and code operations")

    def _read_file(args: dict) -> str:
        path = Path(args["path"])
        if not path.exists():
            return f"Error: file not found: {path}"
        limit = args.get("limit", 500)
        content = path.read_text(encoding="utf-8")
        lines = content.split("\n")
        if len(lines) > limit:
            return "\n".join(lines[:limit]) + f"\n... (truncated, {len(lines)} total lines)"
        return content

    def _edit_file(args: dict) -> str:
        path = Path(args["path"])
        old_text = args["old_text"]
        new_text = args.get("new_text", "")
        if not path.exists():
            return f"Error: file not found: {path}"
        content = path.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: old_text not found in {path}"
        content = content.replace(old_text, new_text, 1)
        path.write_text(content, encoding="utf-8")
        return f"File edited: {path}"

    def _run_cmd(args: dict) -> str:
        cmd = args["command"]
        timeout = args.get("timeout", 30)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout or result.stderr
            if result.returncode != 0:
                return f"[exit {result.returncode}] {output[:2000]}"
            return output[:2000]
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"

    tk.add_python_tool(
        "read_file", "Read a file from disk",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path to read"},
            "limit": {"type": "integer", "description": "Max lines to return (default 500)"},
        }, "required": ["path"]},
        _read_file,
    )

    tk.add_python_tool(
        "edit_file", "Edit a file by replacing old text with new text",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_text": {"type": "string", "description": "Text to find and replace"},
            "new_text": {"type": "string", "description": "Replacement text (empty to delete)"},
        }, "required": ["path", "old_text"]},
        _edit_file,
    )

    tk.add_python_tool(
        "run_command", "Execute a shell command",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
        }, "required": ["command"]},
        _run_cmd,
    )

    return tk


def create_web_toolkit() -> ToolKit:
    """Web toolkit: HTTP requests, web scraping."""
    tk = ToolKit(name="web", description="Web and HTTP operations")

    def _http_get(args: dict) -> str:
        import requests as req
        url = args["url"]
        try:
            resp = req.get(url, timeout=10)
            return resp.text[:3000]
        except Exception as e:
            return f"Error: {e}"

    def _http_post(args: dict) -> str:
        import requests as req
        url = args["url"]
        body = args.get("body", "{}")
        try:
            resp = req.post(url, data=body, timeout=10, headers={"Content-Type": "application/json"})
            return f"Status {resp.status_code}: {resp.text[:2000]}"
        except Exception as e:
            return f"Error: {e}"

    tk.add_python_tool(
        "http_get", "Send HTTP GET request",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
        }, "required": ["url"]},
        _http_get,
    )

    tk.add_python_tool(
        "http_post", "Send HTTP POST request",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to post to"},
            "body": {"type": "string", "description": "Request body (JSON string)"},
        }, "required": ["url"]},
        _http_post,
    )

    return tk
