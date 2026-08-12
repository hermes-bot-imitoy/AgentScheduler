"""定时任务工具类 (Task ToolKit) — 在指定 Tick 提醒角色处理任务.

包含:
  - create_task: 新建定时任务 (指定 Tick 触发, 到达后提醒当前角色)
  - list_tasks:  读取任务列表
  - edit_task:   编辑任务 (改时间/改内容)
  - delete_task: 删除任务

时间单位是 Tick, 范围 0~60 (一天内, 0 = 上班, 60 = 下班).
任务注册到共享 TimeEventBus, 到达指定 Tick 时通过事件总线
定向提醒创建该任务的角色 (其他角色不会收到).

用法:
    from src.python_tools.task_toolkit import create_task_toolkit
    role.add_toolkit(create_task_toolkit())
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.time_manager import TASK_TICK_MAX, TASK_TICK_MIN
from src.core.tools import ToolKit

logger = logging.getLogger(__name__)

TICK_RANGE_DESC = f"单位: Tick, 范围 {TASK_TICK_MIN}~{TASK_TICK_MAX} (一天内, 0=上班 60=下班)"


def _persist_task(role: Any, task: Any) -> None:
    """将任务同步到角色的个人电脑 (tasks/<task_id>.md).

    参数:
        role: AgentRole (提供 computer).
        task: TimeEventBus 返回的 Task 对象.
    """
    try:
        comp = role.computer
        content = (f"# 任务 {task.task_id}\n\n"
                   f"- 内容: {task.description}\n"
                   f"- 触发: 第 {task.day} 天 Tick {task.target_tick}\n")
        comp.write_file(f"{comp.workdir}/tasks/{task.task_id}.md", content)
    except Exception:
        logger.warning("任务持久化到电脑失败 (不影响任务调度)", exc_info=True)


def create_task_toolkit() -> ToolKit:
    """创建定时任务工具类.

    返回:
        包含 create_task / list_tasks / edit_task / delete_task 的 ToolKit.
    """

    tk = ToolKit(name="task", description="定时任务工具类: 在指定 Tick 提醒自己处理任务")

    # 工具类持有 role 引用 (由 AgentRole.add_toolkit 注入)
    tk._role_holder = {"role": None}  # type: ignore[attr-defined]

    def _get_role() -> Any:
        role = tk._role_holder["role"]  # type: ignore[attr-defined]
        if role is None:
            raise RuntimeError("任务工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return role

    def _create_task(args: dict[str, Any]) -> str:
        """新建定时任务.

        参数:
            args: {"description": 任务内容, "tick": 目标Tick(0~60), "day": 第几天(可选)}

        返回:
            创建结果与任务 ID.
        """
        description = args.get("description", "").strip()
        tick = args.get("tick")
        if not description:
            return "错误: 'description' (任务内容) 为必填参数."
        if tick is None:
            return "错误: 'tick' (触发时间) 为必填参数."

        role = _get_role()
        try:
            task = role.time_manager.schedule_task(
                description=description,
                owner_role=role.role_id,
                target_tick=int(tick),
                day=args.get("day"),
            )
        except ValueError as exc:
            return f"错误: {exc}"

        # 同步持久化到个人电脑 (tasks/<task_id>.md), 供电脑上查看
        _persist_task(role, task)
        return (f"定时任务已创建 [ID={task.task_id}]: '{task.description}' "
                f"将在第 {task.day} 天 Tick {task.target_tick} 提醒你.")

    def _list_tasks(args: dict[str, Any]) -> str:
        """读取任务列表.

        参数:
            args: 无 (只列出当前角色的任务).

        返回:
            未触发的任务列表 (从个人电脑 tasks/ 目录读取).
        """
        role = _get_role()
        tasks = role.time_manager.list_tasks(owner_role=role.role_id)
        if not tasks:
            return "(暂无未触发的定时任务)"
        lines = [f"- [ID={t.task_id}] Tick {t.target_tick} (第 {t.day} 天): {t.description}"
                 for t in tasks]
        return "\n".join(lines)

    def _edit_task(args: dict[str, Any]) -> str:
        """编辑任务.

        参数:
            args: {"task_id": 任务ID, "description": 新内容(可选), "tick": 新Tick(可选)}

        返回:
            编辑结果.
        """
        task_id = args.get("task_id", "").strip()
        if not task_id:
            return "错误: 'task_id' (任务 ID) 为必填参数."

        role = _get_role()
        try:
            task = role.time_manager.edit_task(
                task_id=task_id,
                description=args.get("description"),
                target_tick=args.get("tick"),
                day=args.get("day"),
            )
        except ValueError as exc:
            return f"错误: {exc}"
        if task is None:
            return f"任务不存在: {task_id}"
        _persist_task(_get_role(), task)  # 同步更新电脑上的任务文件
        return (f"任务已更新 [ID={task.task_id}]: 第 {task.day} 天 Tick {task.target_tick}, "
                f"内容: {task.description}")

    def _delete_task(args: dict[str, Any]) -> str:
        """删除任务.

        参数:
            args: {"task_id": 任务ID}

        返回:
            删除结果.
        """
        task_id = args.get("task_id", "").strip()
        if not task_id:
            return "错误: 'task_id' (任务 ID) 为必填参数."

        role = _get_role()
        if role.time_manager.cancel_task(task_id):
            # 同步删除电脑上的任务文件
            try:
                comp = role.computer
                comp.write_file(f"{comp.workdir}/tasks/{task_id}.md", "")  # 置空
            except Exception:
                logger.warning("任务文件清理失败 (不影响任务调度)", exc_info=True)
            return f"任务已删除: {task_id}"
        return f"任务不存在: {task_id}"

    tk.add_python_tool(
        name="create_task",
        description=(
            "新建一个定时任务, 到达指定 Tick 时系统会提醒你处理. "
            f"时间参数 tick 的单位: {TICK_RANGE_DESC}. "
            "day 参数可指定任意第几天 (默认今天), 支持安排未来任何一天的任务. "
            "适合安排稍后要做的工作, 比如 'Tick 45 时开始写周报'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "任务内容"},
                "tick": {"type": "integer", "description": f"触发 Tick ({TICK_RANGE_DESC})"},
                "day": {"type": "integer", "description": "第几天 (可选, 默认今天, 可设任意未来天)"},
            },
            "required": ["description", "tick"],
        },
        handler=_create_task,
    )

    tk.add_python_tool(
        name="list_tasks",
        description="列出当前所有未触发的定时任务 (含任务 ID / Tick / 内容).",
        input_schema={"type": "object", "properties": {}},
        handler=_list_tasks,
    )

    tk.add_python_tool(
        name="edit_task",
        description=(
            "编辑已有的定时任务: 修改内容或触发时间. "
            f"时间参数 tick 的单位: {TICK_RANGE_DESC}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID (从 list_tasks 获取)"},
                "description": {"type": "string", "description": "新的任务内容 (可选)"},
                "tick": {"type": "integer", "description": f"新的触发 Tick ({TICK_RANGE_DESC}, 可选)"},
                "day": {"type": "integer", "description": "新的触发天 (可选)"},
            },
            "required": ["task_id"],
        },
        handler=_edit_task,
    )

    tk.add_python_tool(
        name="delete_task",
        description="删除一个定时任务 (不再提醒).",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID (从 list_tasks 获取)"},
            },
            "required": ["task_id"],
        },
        handler=_delete_task,
    )

    return tk


def bind_role_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将角色绑定到任务工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: 任务工具类实例
        role:    AgentRole 实例 (提供 role_id 与共享 TimeEventBus)
    """
    toolkit._role_holder["role"] = role  # type: ignore[attr-defined]
