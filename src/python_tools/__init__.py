"""Python 原生工具集.

本文件夹存放所有 Python 实现的工具类 (ToolKit)。
每个文件定义一个或多个工具类，角色通过 AgentRole.add_toolkit() 一次性导入。

默认工具 (DEFAULT_TOOLKITS): 角色被添加进 AgentSystem 时自动加载,
不需要额外配置。除 hr / client 之外的工具类均为默认:
  - memory: summary / write_note / edit_note / list_notes / read_note
  - time:   get_time / take_rest
  - task:   create_task / list_tasks / edit_task / delete_task
  - mcp_manager: mcp_search / mcp_list / mcp_add / mcp_remove / mcp_my_tools
    (MCP 工具自助管理, 共享全局 MCPManager, 懒加载服务器)
  - communication (talk / list_roles): 由 RolePool.start() 自动注入
    (需要 pool 引用, 见 roles.py _register_talk_tool)

需手动添加的工具类:
  - hr:     post_job_posting / list_candidates (招聘即入职)
  - client: talk_to_client (与甲方交流, 通常只给 CEO)

用法:
    from src.python_tools import DEFAULT_TOOLKITS
    for factory in DEFAULT_TOOLKITS.values():
        role.add_toolkit(factory())
"""

from typing import Callable

from src.python_tools.memory_toolkit import create_memory_toolkit
from src.python_tools.task_toolkit import create_task_toolkit
from src.python_tools.time_toolkit import create_time_toolkit
from src.python_tools.mcp_manager import MCPManager, create_mcp_manager_toolkit
from src.python_tools.computer_toolkit import create_computer_toolkit

# 全局共享 MCP 管理器 (懒加载: 首次调用 mcp_* 工具时才连接服务器).
# 所有角色共享同一份工具池, 但每个角色 add_toolkit 时拿到独立的工具类实例
# (角色引用由 AgentRole.add_toolkit 自动绑定, 互不干扰).
_MCP_MANAGER = MCPManager()

# 默认工具类注册表: {名称: 工厂函数}
# 角色被添加进 AgentSystem 时 (auto_toolkits=True) 自动逐个加载.
DEFAULT_TOOLKITS: dict[str, Callable[[], object]] = {
    "memory": create_memory_toolkit,
    "time": create_time_toolkit,
    "task": create_task_toolkit,
    "computer": create_computer_toolkit,          # 个人电脑 (默认 Podman, 角色添加时自动创建)
    "mcp_manager": lambda: create_mcp_manager_toolkit(_MCP_MANAGER),
}
