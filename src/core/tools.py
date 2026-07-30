"""MCP Tool Registry — MCP-compatible tool definitions and execution.

Integrates with the Model Context Protocol (MCP) Python SDK:
  - Tools are defined as MCP `Tool` objects with JSON Schema input
  - Handlers are callables: (arguments: dict) -> str
  - Tool execution loop: LLM decides tool → execute → feed result back → repeat
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
    # Fallback: define minimal compatible types
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

# ── Tool Handler Type ─────────────────────────────────────

ToolHandler = Callable[[dict[str, Any]], str]
"""A tool handler receives arguments dict and returns a string result."""


# ── Tool Registry ─────────────────────────────────────────

class ToolRegistry:
    """Registry of MCP-compatible tools with execution handlers.

    Each tool has:
      - name, description, inputSchema (MCP Tool spec)
      - handler: callable that executes the tool locally

    Usage:
        registry = ToolRegistry()
        registry.add_tool(
            name="read_file",
            description="Read a file from disk",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path"}},
                "required": ["path"],
            },
            handler=lambda args: Path(args["path"]).read_text(),
        )
        result = registry.call_tool("read_file", {"path": "/tmp/test.txt"})
    """

    def __init__(self):
        self._tools: dict[str, MCPTool] = {}
        self._handlers: dict[str, ToolHandler] = {}

    # ── Tool Management ──────────────────────────────────

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        """Register a new MCP tool."""
        tool = MCPTool(
            name=name,
            description=description,
            inputSchema=input_schema,
        )
        self._tools[name] = tool
        self._handlers[name] = handler
        logger.info("Tool registered: %s — %s", name, description[:60])

    def remove_tool(self, name: str) -> None:
        self._tools.pop(name, None)
        self._handlers.pop(name, None)

    # ── MCP Protocol Methods ─────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tools in MCP-compatible format (for LLM context)."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema if hasattr(t, 'input_schema') else t.inputSchema,
            }
            for t in self._tools.values()
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Execute a tool by name with arguments. Returns MCP CallToolResult."""
        if name not in self._handlers:
            return CallToolResult(
                content=[TextContent(text=f"Error: tool '{name}' not found")],
                is_error=True,
            )

        handler = self._handlers[name]
        try:
            result_text = handler(arguments)
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

        lines = ["You have access to the following tools. To use a tool, respond with:"]
        lines.append("```tool_call")
        lines.append('{"tool": "<tool_name>", "arguments": {...}}')
        lines.append("```")
        lines.append("")

        for t in tools:
            schema_str = json.dumps(t["input_schema"], ensure_ascii=False)
            lines.append(f"- **{t['name']}**: {t['description']}")
            lines.append(f"  Input: {schema_str}")
            lines.append("")

        return "\n".join(lines)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def tool_count(self) -> int:
        return len(self._tools)
