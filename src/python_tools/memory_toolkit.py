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

    # 工具类持有 store / role 引用 (由 AgentRole.add_toolkit 注入)
    tk._store_holder = {"store": None}  # type: ignore[attr-defined]
    tk._role_holder = {"role": None}    # type: ignore[attr-defined]

    def _get_store() -> Any:
        store = tk._store_holder["store"]  # type: ignore[attr-defined]
        if store is None:
            raise RuntimeError("记忆工具类尚未绑定 NoteStore, 请通过 role.add_toolkit() 注册")
        return store

    def _get_role() -> Any:
        return tk._role_holder["role"]  # type: ignore[attr-defined]

    def _summary(args: dict[str, Any]) -> str:
        """总结这一天. 保存后下一天自动注入提示词, 并将角色切换为 OFF_DUTY.

        参数:
            args: {"content": 今日总结内容, "day": 第几天(可选, 默认取角色当前天)}

        返回:
            保存确认信息.
        """
        content = args.get("content", "").strip()
        if not content:
            return "错误: 'content' (总结内容) 为必填参数."

        store = _get_store()
        role = _get_role()

        # 天序号: 显式传入或取角色当前天
        day = args.get("day")
        if day is None and role is not None:
            day = role.time_manager.day_number()
        if day is None:
            day = 1

        path = store.save_summary(content, day=day)

        # 上下文更新: 总结保存 → 写入角色活动日志
        if role is not None:
            role.journal(f"保存第 {day} 天总结 ({len(content)} 字符)")

        # 总结完成 → 角色下班 (OFF_DUTY) + 一天结束自动关电脑
        if role is not None:
            from src.core.types import AgentState
            if role.state != AgentState.OFF_DUTY:
                role.state = AgentState.OFF_DUTY
                logger.info("[%s] 总结完成, 角色已切换为 OFF_DUTY", role.role_id)
            # 一天结束: 自动关闭个人电脑 (下次上班/事件自动开机)
            try:
                if role._computer is not None and role._computer.is_on:
                    role._computer.power_off()
                    logger.info("[%s] 一天结束, 电脑已自动关机", role.role_id)
            except Exception:
                logger.warning("[%s] 电脑自动关机失败", role.role_id, exc_info=True)
            return f"第 {day} 天总结已保存: {path}. 你已下班 (OFF_DUTY), 电脑已关闭."
        return f"第 {day} 天总结已保存: {path}"

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
        role = _get_role()
        path = store.write_note(title, content)
        # 上下文更新: 新笔记 → 写入角色活动日志
        if role is not None:
            role.journal(f"写入笔记: {title}")
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
        role = _get_role()
        path = store.edit_note(title, content)
        # 上下文更新: 笔记被编辑 → 写入角色活动日志
        if role is not None:
            role.journal(f"更新笔记: {title}")
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

    tk.add_python_tool(
        name="summary",
        description=(
            "总结今天的工作. 调用此工具后, 总结内容会被保存, 并在下一天自动注入到你的系统提示词中. "
            "同时你会切换为 OFF_DUTY (下班) 状态. content 应包含: 今日完成的工作, 关键决策, 未完成事项."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "今日总结内容"},
                "day": {"type": "integer", "description": "第几天 (可选, 默认当前天)"},
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


def bind_store_to_toolkit(toolkit: ToolKit, store: Any, role: Any = None) -> None:
    """将 NoteStore 绑定到工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: 记忆工具类实例
        store:   NoteStore 实例
        role:    绑定的 AgentRole (可选, 用于 summary 后切换 OFF_DUTY)
    """
    toolkit._store_holder["store"] = store  # type: ignore[attr-defined]
    toolkit._role_holder["role"] = role     # type: ignore[attr-defined]
