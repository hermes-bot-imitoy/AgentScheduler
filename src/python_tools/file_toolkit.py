"""文件操作工具类 (File ToolKit) — 操作各角色自己电脑上的文件.

包含 (与官方 filesystem MCP 服务器工具同名, 行为对齐):
  - read_file:        读取文件
  - write_file:       写入文件
  - edit_file:        编辑文件 (替换文本)
  - list_directory:   列出目录
  - search_files:     按 glob 搜索文件
  - create_directory: 创建目录
  - move_file:        移动/重命名
  - get_file_info:    查看文件信息

与 MCP filesystem 服务器的区别: 本工具类直接操作角色个人电脑
(computer.read_file/write_file/run_command), 文件落在各角色自己的
工作目录里, 互不干扰. 作为默认工具自动加载, 无需 mcp_search/mcp_add.

用法:
    from src.python_tools.file_toolkit import create_file_toolkit
    role.add_toolkit(create_file_toolkit())   # add_toolkit 自动绑定当前角色
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_file_toolkit() -> ToolKit:
    """创建文件操作工具类 (操作角色个人电脑上的文件).

    返回:
        包含 read_file / write_file / edit_file / list_directory /
        search_files / create_directory / move_file / get_file_info 的 ToolKit.
        角色 add_toolkit 后自动绑定该角色 (经其 computer 操作文件).
    """
    tk = ToolKit(name="file", description="文件操作工具: 读写编辑自己电脑上的文件")
    # 持有当前角色引用 (由 AgentRole.add_toolkit 绑定)
    tk._file_holder = {"role": None}  # type: ignore[attr-defined]

    def _computer() -> Any:
        r = tk._file_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("file 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r.computer

    def _resolve(role: Any, path: str) -> str:
        """把相对路径解析到角色电脑工作目录下."""
        comp = role.computer
        if path.startswith("/") or path.startswith("~"):
            return path
        return f"{comp.workdir}/{path}"

    def _read_file(args: dict[str, Any]) -> str:
        """读取文件内容."""
        path = args.get("path", "").strip()
        if not path:
            return "错误: 'path' 为必填参数."
        comp = _computer()
        return comp.read_file(_resolve(_holder_role(), path))

    def _holder_role() -> Any:
        return tk._file_holder["role"]  # type: ignore[attr-defined]

    def _write_file(args: dict[str, Any]) -> str:
        """写入文件 (覆盖已有内容, 自动创建父目录)."""
        path = args.get("path", "").strip()
        content = args.get("content", "")
        if not path:
            return "错误: 'path' 为必填参数."
        comp = _computer()
        return comp.write_file(_resolve(_holder_role(), path), content)

    def _edit_file(args: dict[str, Any]) -> str:
        """编辑文件: 将 old_text 替换为 new_text (只替换第一处)."""
        path = args.get("path", "").strip()
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        if not path or not old_text:
            return "错误: 'path' 和 'old_text' 为必填参数."
        comp = _computer()
        full = _resolve(_holder_role(), path)
        content = comp.read_file(full)
        if content.startswith("文件不存在") or content.startswith("错误:"):
            return content
        if old_text not in content:
            return f"错误: 在 {path} 中未找到要替换的文本."
        updated = content.replace(old_text, new_text, 1)
        comp.write_file(full, updated)
        return f"文件已编辑: {path}"

    def _list_directory(args: dict[str, Any]) -> str:
        """列出目录内容 (默认列出电脑工作目录)."""
        path = args.get("path", "").strip()
        comp = _computer()
        if not path:
            return comp.list_dir(comp.workdir)
        return comp.list_dir(_resolve(_holder_role(), path))

    def _search_files(args: dict[str, Any]) -> str:
        """按 glob 模式搜索文件 (如 *.md, **/*.py)."""
        path = args.get("path", "").strip()
        pattern = args.get("pattern", "").strip()
        if not pattern:
            return "错误: 'pattern' 为必填参数."
        comp = _computer()
        base = _resolve(_holder_role(), path) if path else comp.workdir
        # 用 find 实现 glob 搜索
        if "**" in pattern:
            cmd = f"find '{base}' -name '{pattern.split('/')[-1]}' 2>/dev/null"
        else:
            cmd = f"find '{base}' -maxdepth 3 -name '{pattern}' 2>/dev/null"
        r = comp.run_command(cmd)
        if r.startswith("错误:") or r.startswith("[exit"):
            return f"(搜索无结果或目录不存在: {r[:100]})"
        return r or "(无匹配文件)"

    def _create_directory(args: dict[str, Any]) -> str:
        """创建目录 (含父目录)."""
        path = args.get("path", "").strip()
        if not path:
            return "错误: 'path' 为必填参数."
        comp = _computer()
        r = comp.run_command(f"mkdir -p '{_resolve(_holder_role(), path)}'")
        if r.startswith("错误:") or r.startswith("[exit"):
            return f"错误: 创建目录失败 - {r}"
        return f"目录已创建: {path}"

    def _move_file(args: dict[str, Any]) -> str:
        """移动或重命名文件."""
        src = args.get("source", "").strip()
        dst = args.get("destination", "").strip()
        if not src or not dst:
            return "错误: 'source' 和 'destination' 为必填参数."
        comp = _computer()
        r = comp.run_command(
            f"mv '{_resolve(_holder_role(), src)}' '{_resolve(_holder_role(), dst)}'")
        if r.startswith("错误:") or r.startswith("[exit"):
            return f"错误: 移动失败 - {r}"
        return f"已移动: {src} → {dst}"

    def _get_file_info(args: dict[str, Any]) -> str:
        """查看文件/目录信息 (大小, 修改时间, 权限)."""
        path = args.get("path", "").strip()
        if not path:
            return "错误: 'path' 为必填参数."
        comp = _computer()
        r = comp.run_command(f"ls -la '{_resolve(_holder_role(), path)}'")
        if r.startswith("错误:") or r.startswith("[exit"):
            return f"文件不存在或无法访问: {path}"
        return r

    tk.add_python_tool(
        "read_file",
        "读取你电脑上指定路径的文件内容. path 相对路径基于你的工作目录.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径 (相对工作目录或绝对路径)"},
        }, "required": ["path"]},
        _read_file,
    )
    tk.add_python_tool(
        "write_file",
        "写入文件到你的电脑 (覆盖已有内容, 自动创建父目录). 适合保存文档、代码、配置.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径 (相对工作目录或绝对路径)"},
            "content": {"type": "string", "description": "文件内容"},
        }, "required": ["path", "content"]},
        _write_file,
    )
    tk.add_python_tool(
        "edit_file",
        "编辑你电脑上的文件: 把 old_text 替换为 new_text (只替换第一处). 适合局部修改.",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_text": {"type": "string", "description": "要查找替换的原文"},
            "new_text": {"type": "string", "description": "替换后的新文本"},
        }, "required": ["path", "old_text"]},
        _edit_file,
    )
    tk.add_python_tool(
        "list_directory",
        "列出你电脑上指定目录的内容 (默认你的工作目录).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径 (可选, 默认工作目录)"},
        }},
        _list_directory,
    )
    tk.add_python_tool(
        "search_files",
        "按 glob 模式在你电脑上搜索文件 (如 *.md, *.py, 支持 ** 递归).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "搜索起始目录 (可选, 默认工作目录)"},
            "pattern": {"type": "string", "description": "glob 模式, 如 *.md"},
        }, "required": ["pattern"]},
        _search_files,
    )
    tk.add_python_tool(
        "create_directory",
        "在你电脑上创建目录 (自动创建父目录).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径"},
        }, "required": ["path"]},
        _create_directory,
    )
    tk.add_python_tool(
        "move_file",
        "在你电脑上移动或重命名文件.",
        {"type": "object", "properties": {
            "source": {"type": "string", "description": "源路径"},
            "destination": {"type": "string", "description": "目标路径"},
        }, "required": ["source", "destination"]},
        _move_file,
    )
    tk.add_python_tool(
        "get_file_info",
        "查看你电脑上文件/目录的信息 (大小, 修改时间, 权限).",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件或目录路径"},
        }, "required": ["path"]},
        _get_file_info,
    )
    return tk


def bind_file_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 file 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: file 工具类实例.
        role:    绑定的 AgentRole (其 computer 属性提供文件操作目标).
    """
    toolkit._file_holder["role"] = role  # type: ignore[attr-defined]
