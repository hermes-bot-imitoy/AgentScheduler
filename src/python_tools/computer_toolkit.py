"""个人电脑工具类 (Computer ToolKit) — 让 LLM 在自己电脑上工作.

包含:
  - run_command:     在个人电脑上运行命令
  - computer_status: 查看电脑状态 (开机/关机/工作目录)
  - power_off:       关机
  - run_mcp_tool:    在个人电脑上运行 MCP 工具

每个角色有独立电脑 (默认 Podman 虚拟电脑, 见 src/core/computer.py).
笔记/任务/总结等数据存放在电脑工作目录的指定子目录 (notes/ tasks/ 等).

用法:
    from src.python_tools.computer_toolkit import create_computer_toolkit
    role.add_toolkit(create_computer_toolkit())   # add_toolkit 自动绑定当前角色
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_computer_toolkit() -> ToolKit:
    """创建个人电脑工具类.

    返回:
        包含 run_command / computer_status / power_off / run_mcp_tool 的 ToolKit.
        角色 add_toolkit 后自动绑定该角色 (获取其个人电脑).
    """
    tk = ToolKit(name="computer", description="个人电脑工具: 运行命令, 运行 MCP 工具")
    # 持有当前角色引用 (由 AgentRole.add_toolkit 绑定)
    tk._computer_holder = {"role": None}  # type: ignore[attr-defined]

    def _computer() -> Any:
        r = tk._computer_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("computer 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r.computer  # 角色添加时自动创建 (默认 Podman)

    def _run_command(args: dict[str, Any]) -> str:
        """在个人电脑上运行命令."""
        cmd = args.get("command", "").strip()
        if not cmd:
            return "错误: 'command' 为必填参数."
        return _computer().run_command(cmd)

    def _computer_status(args: dict[str, Any]) -> str:
        """查看个人电脑状态."""
        comp = _computer()
        return comp.describe()

    def _power_off(args: dict[str, Any]) -> str:
        """关闭个人电脑."""
        return _computer().power_off()

    def _run_mcp_tool(args: dict[str, Any]) -> str:
        """在个人电脑上运行一个 MCP 工具."""
        name = args.get("tool_name", "").strip()
        if not name:
            return "错误: 'tool_name' 为必填参数."
        tool_args = args.get("arguments", {}) or {}
        if not isinstance(tool_args, dict):
            return "错误: 'arguments' 必须是对象."
        return _computer().run_mcp_tool(name, tool_args)

    tk.add_python_tool(
        "run_command",
        "在你自己个人的电脑上运行一条命令 (如 ls, cat, python, git 等), 返回命令输出. "
        "适合查看电脑上的文件、执行脚本、检查项目状态.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "要运行的命令"},
        }, "required": ["command"]},
        _run_command,
    )
    tk.add_python_tool(
        "computer_status",
        "查看你个人电脑的状态: 是否开机, 工作目录在哪里, 电脑类型.",
        {"type": "object", "properties": {}},
        _computer_status,
    )
    tk.add_python_tool(
        "power_off",
        "关闭你的个人电脑. 关机后无法运行命令, 直到下次上班自动开机.",
        {"type": "object", "properties": {}},
        _power_off,
    )
    tk.add_python_tool(
        "run_mcp_tool",
        "在你个人电脑上运行一个 MCP 工具 (如文件读写、GitHub 操作). "
        "先用 mcp_list 查看有哪些 MCP 工具可用, 再调用.",
        {"type": "object", "properties": {
            "tool_name": {"type": "string", "description": "MCP 工具名"},
            "arguments": {"type": "object", "description": "工具参数 (可选)"},
        }, "required": ["tool_name"]},
        _run_mcp_tool,
    )
    return tk


def bind_computer_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 computer 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: computer 工具类实例.
        role:    绑定的 AgentRole (其 computer 属性提供个人电脑).
    """
    toolkit._computer_holder["role"] = role  # type: ignore[attr-defined]
