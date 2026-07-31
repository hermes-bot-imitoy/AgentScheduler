"""MCP 工具加载器 (MCP Tool Loader).

负责:
  1. 连接配置文件中定义的所有 MCP 服务器
  2. 加载每个服务器暴露的工具
  3. 根据分组规则 (mcp_group_rules.json) 将工具分组成多个 ToolKit
  4. 角色可以一次导入某个分组的 ToolKit

用法:
    from src.python_tools.mcp_toolkit import load_mcp_toolkits
    toolkits = load_mcp_toolkits()          # 返回 {组名: ToolKit}
    role.add_toolkit(toolkits["file_ops"])  # 角色导入文件操作工具组
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from src.core.tools import ToolDef, ToolKit

logger = logging.getLogger(__name__)

# 分组规则文件路径 (可被环境变量覆盖)
RULES_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp_group_rules.json"


def load_rules(rules_file: str | Path | None = None) -> dict[str, Any]:
    """加载 MCP 分组规则 JSON.

    参数:
        rules_file: 规则文件路径 (默认: src/config/mcp_group_rules.json).

    返回:
        规则字典: {"servers": [...], "groups": [...], "default_group": "..."}
    """
    path = Path(rules_file) if rules_file else RULES_FILE
    if not path.exists():
        raise FileNotFoundError(f"MCP 分组规则文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════
#  MCP 服务器连接管理
# ═══════════════════════════════════════════════════════════

class MCPServer:
    """单个 MCP 服务器连接.

    在后台线程中维护一个事件循环, 保持 session 存活,
    工具调用通过 run_coroutine_threadsafe 提交到该循环执行.
    """

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None, description: str = ""):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self.description = description

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._ready = threading.Event()

    # ── 生命周期 ──────────────────────────────────────────

    def connect(self) -> None:
        """启动后台线程连接服务器."""
        self._thread = threading.Thread(
            target=self._run_loop, name=f"mcp-{self.name}", daemon=True,
        )
        self._thread.start()
        # 等待连接就绪 (最多 15 秒)
        self._ready.wait(15)

    def _run_loop(self) -> None:
        """后台线程入口: 运行事件循环, 建立 session."""
        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _connect():
                server_params = StdioServerParameters(
                    command=self.command,
                    args=self.args,
                    env=self.env or None,
                )
                async with stdio_client(server_params) as (read, write):
                    from mcp import ClientSession
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set()
                        logger.info("MCP server '%s' connected (%s)", self.name, self.command)
                        # 保持连接, 循环永远运行
                        await asyncio.Event().wait()

            self._loop.run_until_complete(_connect())
        except Exception as exc:
            logger.error("MCP server '%s' connect failed: %s", self.name, exc)
            self._ready.set()  # 即使失败也唤醒, 避免卡死

    def close(self) -> None:
        """关闭服务器连接."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

    # ── 工具操作 ──────────────────────────────────────────

    def list_tools(self) -> list[Any]:
        """列出服务器暴露的所有工具."""
        if self._session is None or self._loop is None:
            return []
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.list_tools(), self._loop,
            )
            result = future.result(timeout=10)
            return list(result.tools)
        except Exception as exc:
            logger.error("MCP '%s' list_tools failed: %s", self.name, exc)
            return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用服务器上的工具. 返回结果文本."""
        if self._session is None or self._loop is None:
            return f"错误: MCP 服务器 '{self.name}' 未连接"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, arguments), self._loop,
            )
            result = future.result(timeout=60)
            # 提取文本内容
            parts = []
            for content in getattr(result, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    parts.append(text)
                elif hasattr(content, "type") and content.type == "text":
                    parts.append(str(content))
            if getattr(result, "is_error", False):
                return f"[MCP 错误] {''.join(parts)}"
            return "\n".join(parts) if parts else str(result)
        except Exception as exc:
            logger.error("MCP '%s' call %s failed: %s", self.name, name, exc)
            return f"错误: 调用 {name} 失败 - {exc}"


# ═══════════════════════════════════════════════════════════
#  分组加载器
# ═══════════════════════════════════════════════════════════

def _match_group(tool_name: str, patterns: list[str]) -> bool:
    """判断工具名是否匹配分组规则 (支持通配符)."""
    for pat in patterns:
        if fnmatch.fnmatch(tool_name, pat):
            return True
    return False


class MCPToolLoader:
    """MCP 工具加载器: 连接所有服务器, 按规则分组.

    参数:
        rules_file: 分组规则 JSON 路径.

    用法:
        loader = MCPToolLoader()
        toolkits = loader.load()          # {组名: ToolKit}
        loader.close()                     # 关闭所有服务器连接
    """

    def __init__(self, rules_file: str | Path | None = None):
        self.rules = load_rules(rules_file)
        self.default_group = self.rules.get("default_group", "default")
        self._servers: dict[str, MCPServer] = {}
        self._tool_owner: dict[str, str] = {}   # 工具名 -> 服务器名
        self._loaded = False
        self._result: dict[str, ToolKit] = {}   # 缓存上次加载结果

    # ── 加载流程 ──────────────────────────────────────────

    def load(self) -> dict[str, ToolKit]:
        """加载所有 MCP 工具并分组.

        返回:
            {组名: ToolKit} 字典. 每个 ToolKit 包含该组匹配到的所有 MCP 工具.
        """
        if self._loaded:
            # 返回上次加载结果 (避免重复连接)
            return self._result

        # 1. 连接所有配置的服务器
        for server_cfg in self.rules.get("servers", []):
            name = server_cfg["name"]
            server = MCPServer(
                name=name,
                command=server_cfg["command"],
                args=server_cfg.get("args", []),
                env=server_cfg.get("env"),
                description=server_cfg.get("description", ""),
            )
            server.connect()
            self._servers[name] = server

        # 2. 收集所有工具
        all_tools: dict[str, Any] = {}
        for sname, server in self._servers.items():
            for tool in server.list_tools():
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                if tname in all_tools:
                    logger.warning("工具名冲突: '%s' 来自服务器 '%s' 和 '%s', 保留第一个",
                                   tname, self._tool_owner.get(tname), sname)
                    continue
                all_tools[tname] = tool
                self._tool_owner[tname] = sname
                logger.info("MCP 加载工具: %s (来自 %s)", tname, sname)

        self._loaded = True
        self._result = self._build_toolkits(all_tools)
        return self._result

    def _build_toolkits(self, all_tools: dict[str, Any]) -> dict[str, ToolKit]:
        """将工具按分组规则分配到各个 ToolKit."""
        groups = self.rules.get("groups", [])
        # 创建每个组的 ToolKit
        toolkits: dict[str, ToolKit] = {}
        group_tools: dict[str, dict[str, Any]] = {}

        for g in groups:
            gname = g["name"]
            toolkits[gname] = ToolKit(name=gname, description=g.get("description", ""))
            group_tools[gname] = {}

        # 分配工具到组
        default_tk = toolkits.get(self.default_group)
        default_gt = group_tools.get(self.default_group, {})

        for tname, tool in all_tools.items():
            assigned = False
            for g in groups:
                if g["name"] == self.default_group:
                    continue  # default 组最后兜底
                if _match_group(tname, g.get("match", [])):
                    group_tools[g["name"]][tname] = tool
                    assigned = True
                    break
            if not assigned:
                default_gt[tname] = tool

        # 为每个组构建 ToolDef + 绑定服务器调用 handler
        for g in groups:
            gname = g["name"]
            for tname, tool in group_tools.get(gname, {}).items():
                owner = self._tool_owner.get(tname)
                server = self._servers.get(owner) if owner else None
                if server is None:
                    continue

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                td = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{server.name}",
                    mcp_tool=tool,
                )
                toolkits[gname]._tools[tname] = td

        return toolkits

    # ── 查询 ──────────────────────────────────────────────

    def list_loaded_tools(self) -> list[dict[str, str]]:
        """列出所有已加载工具及其来源服务器."""
        result = []
        for tname, sname in self._tool_owner.items():
            result.append({"tool": tname, "server": sname})
        return result

    def close(self) -> None:
        """关闭所有 MCP 服务器连接."""
        for server in self._servers.values():
            server.close()
        self._servers.clear()
        self._loaded = False


# ── 便捷函数 ──────────────────────────────────────────────

def load_mcp_toolkits(rules_file: str | Path | None = None) -> dict[str, ToolKit]:
    """一键加载所有 MCP 工具并分组.

    参数:
        rules_file: 分组规则 JSON 路径 (可选).

    返回:
        {组名: ToolKit} 字典.
    """
    loader = MCPToolLoader(rules_file)
    return loader.load()
