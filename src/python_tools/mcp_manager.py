"""MCP 工具管理类 (MCPManager) — 为每个角色管理 MCP 工具.

功能:
  - 懒加载本地配置的 MCP 服务器 (默认 filesystem + github, 见 mcp_group_rules.json)
  - 搜索本地已有的 MCP 工具 (按名称/描述关键词)
  - 为指定角色添加/删除 MCP 工具 (注册进角色的 ToolRegistry)
  - 查询角色已添加的 MCP 工具

同时把管理操作打包成 LLM tool-call 工具 (mcp_manager 工具类):
  - mcp_search / mcp_list      — 搜索、列出可用 MCP 工具
  - mcp_add / mcp_remove       — 为当前角色添加/移除工具
  - mcp_my_tools               — 查看当前角色已添加的工具
角色获得该工具类后, 即可自主寻找并使用自己所需的 MCP 工具.

用法:
    from src.python_tools.mcp_manager import MCPManager, create_mcp_manager_toolkit

    mgr = MCPManager()                        # 全局一个实例即可
    tk = create_mcp_manager_toolkit(mgr)      # 打包成 LLM 工具类
    role.add_toolkit(tk)                      # add_toolkit 自动绑定当前角色
    # 或编程式:
    mgr.search_tools("file")                  # 搜索
    mgr.add_tool(role, "read_file")           # 给角色添加工具
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from src.core.tools import ToolDef, ToolKit

logger = logging.getLogger(__name__)

# 默认分组规则文件 (相对项目根)
DEFAULT_RULES_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp_group_rules.json"

# filesystem 服务器需要授权目录参数, 默认授权项目根
DEFAULT_SERVER_ARGS: dict[str, list[str]] = {
    "@modelcontextprotocol/server-filesystem": ["."],
}


class MCPManager:
    """全局 MCP 工具管理器: 加载工具池, 为每个角色登记/撤销工具.

    参数:
        rules_file:   分组规则 JSON 路径 (默认读取 src/config/mcp_group_rules.json).
        server_args:  服务器附加参数, 如 filesystem 的授权目录.
        loader:       可注入自定义 MCPToolLoader (测试用).
    """

    def __init__(
        self,
        rules_file: str | Path | None = None,
        server_args: dict[str, list[str]] | None = None,
        loader: Any = None,
    ):
        self.rules_file = Path(rules_file) if rules_file else DEFAULT_RULES_FILE
        self.server_args = server_args if server_args is not None else DEFAULT_SERVER_ARGS
        self._loader = loader  # 惰性创建
        self._toolkit_groups: dict[str, ToolKit] = {}   # 组名 → ToolKit (加载后)
        self._tool_pool: dict[str, ToolDef] = {}        # 工具名 → ToolDef (全量平铺)
        self._role_tools: dict[str, set[str]] = {}      # role_id → {已添加的工具名}
        self._loaded = False

    # ── 加载 ──────────────────────────────────────────────

    def _ensure_loader(self) -> Any:
        """惰性创建 MCPToolLoader 实例."""
        if self._loader is None:
            from src.python_tools.mcp_toolkit import MCPToolLoader
            self._loader = MCPToolLoader(rules_file=str(self.rules_file),
                                         server_args=self.server_args)
        return self._loader

    def ensure_loaded(self) -> dict[str, ToolKit]:
        """加载全部配置的 MCP 服务器工具并平铺到工具池. 幂等, 可重复调用.

        返回:
            分组后的 {组名: ToolKit}.
        """
        if self._loaded:
            return self._toolkit_groups
        loader = self._ensure_loader()
        self._toolkit_groups = loader.load()
        self._tool_pool.clear()
        for gname, tk in self._toolkit_groups.items():
            for td in tk:
                self._tool_pool[td.name] = td
        self._loaded = True
        logger.info("MCPManager: 已加载 %d 个 MCP 工具 (来自 %d 组)",
                    len(self._tool_pool), len(self._toolkit_groups))
        return self._toolkit_groups

    def close(self) -> None:
        """关闭全部 MCP 服务器连接 (进程退出时调用)."""
        if self._loader is not None:
            try:
                self._loader.close()
            except Exception:
                logger.exception("MCPManager: 关闭服务器连接失败")
        self._loaded = False

    # ── 查询 ──────────────────────────────────────────────

    def list_available(self) -> list[dict[str, str]]:
        """列出工具池中全部可用 MCP 工具 (名称 + 简述 + 来源组)."""
        self.ensure_loaded()
        result = []
        for name, td in sorted(self._tool_pool.items()):
            result.append({
                "name": name,
                "description": (td.description or "")[:120],
                "source": td.source,
            })
        return result

    def search_tools(self, keyword: str) -> list[dict[str, str]]:
        """按关键词搜索本地已有的 MCP 工具 (匹配名称或描述).

        参数:
            keyword: 搜索词, 如 "file" / "git" / "issue".

        返回:
            匹配的工具列表 [{name, description, source}].
        """
        self.ensure_loaded()
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        hits = []
        for name, td in sorted(self._tool_pool.items()):
            haystack = f"{name} {td.description or ''}".lower()
            if kw in haystack:
                hits.append({
                    "name": name,
                    "description": (td.description or "")[:120],
                    "source": td.source,
                })
        return hits

    # ── 角色工具管理 ──────────────────────────────────────

    def add_tool(self, role: Any, tool_name: str) -> str:
        """为角色安装一个本地已有的 MCP 工具 (安装到该角色的个人电脑).

        安装语义:
          1. 工具安装到角色电脑 (computer.install_mcp_tool), 归属该电脑
          2. 同时在角色 ToolRegistry 注册一个代理 handler — 调用时转发到
             computer.run_mcp_tool 在电脑上执行

        参数:
            role:      AgentRole 实例.
            tool_name: 工具池中的工具名 (如 "read_file").

        返回:
            操作结果说明 (成功/已存在/不存在).
        """
        self.ensure_loaded()
        td = self._tool_pool.get(tool_name)
        if td is None:
            return f"错误: 本地没有名为 '{tool_name}' 的 MCP 工具. 可用: {sorted(self._tool_pool)[:20]}"
        role_id = role.role_id
        mine = self._role_tools.setdefault(role_id, set())
        if tool_name in mine:
            return f"工具 '{tool_name}' 已添加给 {role_id}, 无需重复添加."

        # 1) 安装到角色的个人电脑
        computer = role.computer
        computer.install_mcp_tool(td)
        assert td.handler is not None, f"工具 {tool_name} 缺少 handler"

        # 2) 角色注册代理 handler → 转发到电脑上执行
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()
        role._tools.add_tool(
            name=td.name,
            description=td.description,
            input_schema=td.input_schema,
            handler=lambda args, _n=tool_name: computer.run_mcp_tool(_n, args),
            source=td.source,
        )
        mine.add(tool_name)
        logger.info("[%s] MCP 工具已安装到电脑: %s (来源 %s)", role_id, tool_name, td.source)
        return f"成功: 工具 '{tool_name}' 已安装到 {role_id} 的电脑 ({td.description[:60]})"

    def remove_tool(self, role: Any, tool_name: str) -> str:
        """从角色电脑卸载一个 MCP 工具.

        参数:
            role:      AgentRole 实例.
            tool_name: 工具名.

        返回:
            操作结果说明 (成功/未添加/不存在).
        """
        role_id = role.role_id
        mine = self._role_tools.get(role_id, set())
        if tool_name not in mine:
            return f"工具 '{tool_name}' 尚未添加给 {role_id}, 无需移除."

        # 1) 从角色电脑卸载
        computer = role.computer
        computer.uninstall_mcp_tool(tool_name)

        # 2) 从角色 ToolRegistry 移除
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()
        role._tools.remove_tool(tool_name)
        mine.discard(tool_name)
        logger.info("[%s] MCP 工具已从电脑卸载: %s", role_id, tool_name)
        return f"成功: 工具 '{tool_name}' 已从 {role_id} 的电脑卸载."

    def list_role_tools(self, role: Any) -> list[dict[str, str]]:
        """列出角色电脑上已安装的 MCP 工具."""
        computer = role.computer
        return [
            {"name": n, "description": (self._tool_pool[n].description or "")[:120]}
            for n in computer.list_installed_mcp_tools() if n in self._tool_pool
        ]

    # ── 角色工具包 (MCP 工具集合, 按组) ──────────────────

    def role_toolkit(self, role: Any) -> ToolKit:
        """将角色已添加的 MCP 工具打包成一个 ToolKit (供 get_tools_prompt 展示)."""
        tk = ToolKit(name=f"mcp[{role.role_id}]", description="该角色已启用的 MCP 工具")
        for n in sorted(self._role_tools.get(role.role_id, set())):
            td = self._tool_pool.get(n)
            if td is not None:
                tk._tools[n] = td
        return tk


# ── LLM 管理工具 (打包成 tool_call 工具类) ────────────────

def create_mcp_manager_toolkit(manager: MCPManager) -> ToolKit:
    """把 MCP 管理操作打包成 LLM 可调用的工具类.

    参数:
        manager: MCPManager 实例 (全局共享).

    返回:
        包含 mcp_search / mcp_list / mcp_add / mcp_remove / mcp_my_tools 的工具类.
        角色 add_toolkit 后, LLM 即可自主搜索/添加/移除 MCP 工具.
    """
    tk = ToolKit(name="mcp_manager", description="MCP 工具管理: 搜索/添加/移除本地 MCP 工具")
    # 持有 manager 与当前角色引用 (由 AgentRole.add_toolkit 绑定)
    tk._mcp_holder = {"manager": manager, "role": None}  # type: ignore[attr-defined]

    def _role() -> Any:
        r = tk._mcp_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("mcp_manager 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r

    def _mcp_search(args: dict[str, Any]) -> str:
        """搜索本地可用的 MCP 工具."""
        kw = args.get("keyword", "").strip()
        if not kw:
            return "请提供 keyword 搜索词."
        hits = manager.search_tools(kw)
        if not hits:
            return f"没有匹配 '{kw}' 的 MCP 工具. 可用 mcp_list 查看全部."
        lines = [f"搜索 '{kw}' 找到 {len(hits)} 个工具:"]
        for h in hits:
            lines.append(f"- {h['name']}: {h['description']}")
        return "\n".join(lines)

    def _mcp_list(args: dict[str, Any]) -> str:
        """列出全部可用的本地 MCP 工具."""
        avail = manager.list_available()
        if not avail:
            return "暂无可用 MCP 工具 (服务器可能未连接)."
        lines = [f"本地共有 {len(avail)} 个 MCP 工具:"]
        for a in avail:
            lines.append(f"- {a['name']}: {a['description']}")
        return "\n".join(lines)

    def _mcp_add(args: dict[str, Any]) -> str:
        """为当前角色添加一个 MCP 工具."""
        name = args.get("tool_name", "").strip()
        if not name:
            return "请提供 tool_name."
        return manager.add_tool(_role(), name)

    def _mcp_remove(args: dict[str, Any]) -> str:
        """从当前角色移除一个 MCP 工具."""
        name = args.get("tool_name", "").strip()
        if not name:
            return "请提供 tool_name."
        return manager.remove_tool(_role(), name)

    def _mcp_my_tools(args: dict[str, Any]) -> str:
        """查看当前角色已添加的 MCP 工具."""
        mine = manager.list_role_tools(_role())
        if not mine:
            return "你还没有添加任何 MCP 工具. 可用 mcp_search / mcp_list 寻找, 用 mcp_add 添加."
        lines = [f"你已添加 {len(mine)} 个 MCP 工具:"]
        for m in mine:
            lines.append(f"- {m['name']}: {m['description']}")
        return "\n".join(lines)

    tk.add_python_tool(
        "mcp_search",
        "搜索本地已有的 MCP 工具 (按名称或描述关键词). 先搜索找到合适的工具, 再用 mcp_add 添加给自己.",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词, 如 file/git/issue/read"},
        }, "required": ["keyword"]},
        _mcp_search,
    )
    tk.add_python_tool(
        "mcp_list",
        "列出本地全部可用的 MCP 工具 (名称+简述). 查看有哪些工具可用.",
        {"type": "object", "properties": {}},
        _mcp_list,
    )
    tk.add_python_tool(
        "mcp_add",
        "为当前角色添加一个本地已有的 MCP 工具. 添加后即可在后续任务中直接调用该工具.",
        {"type": "object", "properties": {
            "tool_name": {"type": "string", "description": "要添加的工具名, 如 read_file"},
        }, "required": ["tool_name"]},
        _mcp_add,
    )
    tk.add_python_tool(
        "mcp_remove",
        "从当前角色移除一个已添加的 MCP 工具.",
        {"type": "object", "properties": {
            "tool_name": {"type": "string", "description": "要移除的工具名"},
        }, "required": ["tool_name"]},
        _mcp_remove,
    )
    tk.add_python_tool(
        "mcp_my_tools",
        "查看当前角色已添加的 MCP 工具列表.",
        {"type": "object", "properties": {}},
        _mcp_my_tools,
    )
    return tk


def bind_mcp_manager_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 mcp_manager 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: mcp_manager 工具类实例.
        role:    绑定的 AgentRole.
    """
    toolkit._mcp_holder["role"] = role  # type: ignore[attr-defined]
