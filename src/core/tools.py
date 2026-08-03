"""MCP + Python Tool System.

Two-tier architecture:
  ToolKit     — a named collection of related tools (can be MCP or Python)
  ToolRegistry — per-role tool registry that manages ToolKits

Supports:
  - Python-native tools: handler = callable(dict) → str
  - MCP tools: compatible with mcp.types.Tool + stdio_client
  - Duplicate detection: warns when two toolkits register the same tool name
  - Role can add a full toolkit at once via AgentRole.add_toolkit()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from mcp.types import CallToolResult, TextContent, Tool as MCPTool
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

    @dataclass
    class MCPTool:
        name: str
        description: str | None = None
        inputSchema: dict[str, Any] = field(default_factory=dict)
        title: str | None = None

    @dataclass
    class TextContent:
        type: str = "text"
        text: str = ""

    @dataclass
    class CallToolResult:
        content: list[TextContent] = field(default_factory=list)
        is_error: bool = False

logger = logging.getLogger(__name__)

# ── Tool Handler ──────────────────────────────────────────

ToolHandler = Callable[[dict[str, Any]], str]
"""A tool handler receives arguments dict and returns a string result."""


# ── ToolDef (unified) ─────────────────────────────────────

@dataclass
class ToolDef:
    """Unified tool definition — works for both MCP and Python-native tools."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler: Optional[ToolHandler] = None          # Python-native handler
    source: str = "python"                         # "python" | "mcp" | "talk"
    mcp_tool: Any = None                           # Original MCP Tool object if applicable


# ── ToolKit ───────────────────────────────────────────────

class ToolKit:
    """A named collection of related tools.

    Can contain:
      - Python-native tools (handler = callable)
      - MCP-based tools (loaded from an MCP server)
      - Mixed (some Python, some MCP)

    Usage:
        # Python toolkit
        coding = ToolKit(name="coding", description="File and code operations")
        coding.add_python_tool("read_file", "Read a file", {...}, handler)

        # Role imports the whole toolkit
        role.add_toolkit(coding)
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._tools: dict[str, ToolDef] = {}

    # ── Python tool management ────────────────────────────

    def add_python_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> ToolDef:
        """Add a Python-native tool to this toolkit."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already exists in toolkit '{self.name}'")
        td = ToolDef(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            source="python",
        )
        self._tools[name] = td
        logger.info("ToolKit[%s] Python tool: %s", self.name, name)
        return td

    # ── Properties ────────────────────────────────────────

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def get_tool(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def __iter__(self):
        return iter(self._tools.values())

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ═══════════════════════════════════════════════════════════
#  Built-in Python ToolKits
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Built-in ToolKits registry
# ═══════════════════════════════════════════════════════════

BUILTIN_TOOLKITS: dict[str, Callable[..., ToolKit]] = {
    "coding": create_coding_toolkit,
    "web": create_web_toolkit,
}


# ── ToolRegistry (updated for ToolKit support) ────────────

class ToolRegistry:
    """Per-role tool registry that manages ToolKits.

    Supports:
      - Adding individual Python tools (backward-compatible)
      - Adding entire ToolKits at once (with duplicate detection)
      - Listing all tools across all loaded toolkits for LLM context
      - Executing tools by name (searches all toolkits)

    Usage:
        reg = ToolRegistry()
        reg.add_toolkit(create_coding_toolkit())
        reg.add_toolkit(create_web_toolkit())
        # Duplicate detection: warns if two toolkits register same name
        reg.call_tool("read_file", {"path": "/tmp/test.txt"})
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}          # unified registry
        self._toolkits: dict[str, ToolKit] = {}       # loaded toolkits by name
        # Track which toolkit each tool came from
        self._tool_source: dict[str, str] = {}        # tool_name → toolkit_name

    # ── Toolkit management ────────────────────────────────

    def add_toolkit(self, toolkit: ToolKit) -> int:
        """Import an entire toolkit. Returns number of new tools added.

        If a tool with the same name already exists, it is skipped with a warning.
        """
        if toolkit.name in self._toolkits:
            logger.warning("Toolkit '%s' already loaded, skipping", toolkit.name)
            return 0

        self._toolkits[toolkit.name] = toolkit
        added = 0

        for td in toolkit:
            if td.name in self._tools:
                existing = self._tool_source.get(td.name, "unknown")
                logger.warning(
                    "Tool '%s' from toolkit '%s' conflicts with existing tool from '%s' — keeping original",
                    td.name, toolkit.name, existing,
                )
                continue
            self._tools[td.name] = td
            self._tool_source[td.name] = toolkit.name
            added += 1

        logger.info(
            "ToolRegistry: loaded toolkit '%s' — %d tools (%d new, %d skipped)",
            toolkit.name, toolkit.tool_count, added, toolkit.tool_count - added,
        )
        return added

    def remove_toolkit(self, name: str) -> int:
        """Remove a toolkit and all its tools. Returns number of tools removed."""
        if name not in self._toolkits:
            return 0
        tk = self._toolkits.pop(name)
        removed = 0
        for td in tk:
            if self._tool_source.get(td.name) == name:
                self._tools.pop(td.name, None)
                self._tool_source.pop(td.name, None)
                removed += 1
        return removed

    # ── Individual tool management (backward-compat) ──────

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
        source: str = "inline",
    ) -> None:
        """Register a single Python tool (backward-compatible API)."""
        if name in self._tools:
            logger.warning("Tool '%s' already registered, overwriting", name)
        td = ToolDef(name=name, description=description, input_schema=input_schema,
                     handler=handler, source=source)
        self._tools[name] = td
        self._tool_source[name] = source
        logger.info("Tool registered: %s — %s", name, description[:60])

    def remove_tool(self, name: str) -> None:
        self._tools.pop(name, None)
        self._tool_source.pop(name, None)

    # ── MCP Protocol Methods ──────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tools in LLM-compatible format."""
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for td in self._tools.values()
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a tool by name. Searches all loaded toolkits."""
        td = self._tools.get(name)
        if td is None:
            return CallToolResult(
                content=[TextContent(text=f"Error: tool '{name}' not found. Available: {list(self._tools)}")],
                is_error=True,
            )

        if td.handler is None:
            # MCP tool — would need session.call_tool(), but we don't keep sessions
            return CallToolResult(
                content=[TextContent(text=f"Error: tool '{name}' is MCP-based and requires an active server connection")],
                is_error=True,
            )

        try:
            result_text = td.handler(arguments)
            return CallToolResult(
                content=[TextContent(text=str(result_text))],
                is_error=False,
            )
        except Exception as exc:
            logger.exception("Tool '%s' execution failed", name)
            return CallToolResult(
                content=[TextContent(text=f"Tool error: {exc}")],
                is_error=True,
            )

    def get_tools_prompt(self) -> str:
        """Generate a prompt snippet describing available tools for the LLM."""
        tools = self.list_tools()
        if not tools:
            return ""

        # Group by toolkit
        by_toolkit: dict[str, list[dict]] = {}
        for t in tools:
            src = self._tool_source.get(t["name"], "other")
            by_toolkit.setdefault(src, []).append(t)

        lines = ["You have access to the following tools. To use a tool, respond with:"]
        lines.append('```tool_call')
        lines.append('{"tool": "<tool_name>", "<参数名>": <值>, ...}')
        lines.append('```')
        lines.append('例如: {"tool": "write_note", "title": "会议记录", "content": "..."}')
        lines.append("")

        for tk_name, tk_tools in by_toolkit.items():
            lines.append(f"### {tk_name}")
            for t in tk_tools:
                schema_str = json.dumps(t["input_schema"], ensure_ascii=False)
                lines.append(f"- **{t['name']}**: {t['description']}")
                lines.append(f"  Input: {schema_str}")
            lines.append("")

        return "\n".join(lines)

    # ── Properties ────────────────────────────────────────

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    @property
    def toolkit_names(self) -> list[str]:
        return list(self._toolkits)

    @property
    def tool_count(self) -> int:
        return len(self._tools)
