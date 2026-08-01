"""记忆工具类 (Memory ToolKit) — 作息系统的存储工具.

包含:
  - summary:    总结这一天. LLM 输出的总结会被保存, 下一天自动注入提示词.
  - write_note: 写笔记 (标题 + 内容)
  - edit_note:  编辑已有笔记
  - list_notes: 列出笔记标题
  - read_note:  读取笔记内容

所有工具的数据都存到角色绑定的 NoteStore (data/notes/<role_id>/),
各角色内容完全独立.

用法:
    from src.python_tools.memory_toolkit import create_memory_toolkit
    role.add_toolkit(create_memory_toolkit())
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_memory_toolkit() -> ToolKit:
    """创建记忆工具类.

    返回:
        包含 summary / write_note / edit_note / list_notes / read_note 的 ToolKit.
    """

    tk = ToolKit(name="memory", description="记忆与笔记工具类: 每日总结, 笔记管理")

    def _summary(args: dict[str, Any]) -> str:
        """总结这一天. 保存后下一天自动注入提示词.

        参数:
            args: {"content": 今日总结内容, "date": 日期(可选, 默认今天)}

        返回:
            保存确认信息.
        """
        content = args.get("content", "").strip()
        if not content:
            return "错误: 'content' (总结内容) 为必填参数."

        # 通过角色绑定 NoteStore 保存总结
        store = _get_store()
        path = store.save_summary(content, args.get("date"))
        return f"当日总结已保存: {path}"

    def _write_note(args: dict[str, Any]) -> str:
        """写笔记.

        参数:
            args: {"title": 笔记标题, "content": 笔记内容}

        返回:
            保存路径.
        """
        title = args.get("title", "").strip()
        content = args.get("content", "")
        if not title:
            return "错误: 'title' (笔记标题) 为必填参数."

        store = _get_store()
        path = store.write_note(title, content)
        return f"笔记已保存: {path}"

    def _edit_note(args: dict[str, Any]) -> str:
        """编辑已有笔记 (覆盖内容). 不存在则创建.

        参数:
            args: {"title": 笔记标题, "content": 新内容}

        返回:
            保存路径.
        """
        title = args.get("title", "").strip()
        content = args.get("content", "")
        if not title:
            return "错误: 'title' (笔记标题) 为必填参数."

        store = _get_store()
        path = store.edit_note(title, content)
        return f"笔记已更新: {path}"

    def _list_notes(args: dict[str, Any]) -> str:
        """列出所有笔记标题.

        参数:
            args: 无.

        返回:
            笔记标题列表 (每行一个).
        """
        store = _get_store()
        titles = store.list_notes()
        if not titles:
            return "(暂无笔记)"
        return "\n".join(f"- {t}" for t in titles)

    def _read_note(args: dict[str, Any]) -> str:
        """读取笔记内容.

        参数:
            args: {"title": 笔记标题}

        返回:
            笔记内容, 不存在则返回错误提示.
        """
        title = args.get("title", "").strip()
        if not title:
            return "错误: 'title' (笔记标题) 为必填参数."

        store = _get_store()
        content = store.read_note(title)
        if content is None:
            return f"笔记不存在: {title}"
        return content

    # 通过 Toolkit 持有 store 引用 (在 add_toolkit 时注入)
    # 方案: 使用线程局部变量在角色执行时设置 store
    tk._store_holder = {"store": None}  # type: ignore[attr-defined]

    def _get_store() -> Any:
        store = tk._store_holder["store"]  # type: ignore[attr-defined]
        if store is None:
            raise RuntimeError("记忆工具类尚未绑定 NoteStore, 请通过 role.add_toolkit() 注册")
        return store

    tk.add_python_tool(
        name="summary",
        description=(
            "总结今天的工作. 调用此工具后, 总结内容会被保存, 并在下一天自动注入到你的系统提示词中, "
            "帮助你记住昨天做了什么. content 应包含: 今日完成的工作, 关键决策, 未完成事项."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "今日总结内容"},
                "date": {"type": "string", "description": "日期 (ISO 格式, 可选, 默认今天)"},
            },
            "required": ["content"],
        },
        handler=_summary,
    )

    tk.add_python_tool(
        name="write_note",
        description="写一篇笔记. 用于记录重要信息, 决策依据, 或需要长期保存的内容.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题"},
                "content": {"type": "string", "description": "笔记内容"},
            },
            "required": ["title", "content"],
        },
        handler=_write_note,
    )

    tk.add_python_tool(
        name="edit_note",
        description="编辑已有笔记 (覆盖原内容). 笔记不存在时会自动创建.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题"},
                "content": {"type": "string", "description": "新的笔记内容"},
            },
            "required": ["title", "content"],
        },
        handler=_edit_note,
    )

    tk.add_python_tool(
        name="list_notes",
        description="列出当前所有笔记的标题列表.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_notes,
    )

    tk.add_python_tool(
        name="read_note",
        description="读取指定标题的笔记内容.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题"},
            },
            "required": ["title"],
        },
        handler=_read_note,
    )

    return tk


def bind_store_to_toolkit(toolkit: ToolKit, store: Any) -> None:
    """将 NoteStore 绑定到工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: 记忆工具类实例
        store:   NoteStore 实例
    """
    toolkit._store_holder["store"] = store  # type: ignore[attr-defined]
