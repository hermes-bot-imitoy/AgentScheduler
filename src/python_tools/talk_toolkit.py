"""通信工具类 (Communication ToolKit).

包含:
  - talk:       角色之间的消息传递与任务委托
  - list_roles: 获取当前团队角色列表 (与 talk 花名册格式一致)

团队花名册格式固定, 由 build_team_roster() 统一生成.

用法:
    from src.python_tools.talk_toolkit import create_talk_toolkit
    tk = create_talk_toolkit(pool)       # pool = RolePool 实例
    role.add_toolkit(tk)                  # 角色导入整个工具类
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def build_team_roster(pool: Any) -> str:
    """构建团队花名册 (固定格式, 供 talk 描述与 list_roles 工具复用).

    格式:
        - **姓名** (role_id: `xxx`) -- 职责  Skills: 技能列表

    参数:
        pool: RolePool 实例.

    返回:
        花名册字符串 (每行一个成员).
    """
    roster_lines: list[str] = []
    for rid, r in pool._roles.items():
        resp = r.responsibilities or r.title
        roster_lines.append(
            f"  - **{r.name}** (role_id: `{rid}`) -- {resp}  "
            f"Skills: {', '.join(r.skills[:4])}"
        )
    return "\n".join(roster_lines)


def create_talk_toolkit(pool: Any) -> ToolKit:
    """创建通信工具类 (talk + list_roles).

    参数:
        pool: RolePool 实例, 用于查找目标角色并投递任务.

    返回:
        包含 talk / list_roles 工具的 ToolKit 实例.
    """
    from src.core.roles import Task, Urgency

    tk = ToolKit(name="communication", description="角色间通信工具类")

    def _talk_handler(args: dict[str, Any]) -> str:
        """talk 工具处理函数: 发送消息给指定角色.

        参数:
            args: {"target": 目标role_id, "message": 消息内容, "urgency": 紧急度}

        返回:
            发送结果字符串 (包含目标角色的队列深度).
        """
        target = args.get("target", "")
        message = args.get("message", "")
        urgency_str = args.get("urgency", "NORMAL")

        if not target or not message:
            return "错误: 'target' 和 'message' 为必填参数."

        target_role = pool.get_role(target)
        urgency = getattr(Urgency, urgency_str.upper(), Urgency.NORMAL)

        task = Task(
            urgency=urgency,
            description=f"[FROM talk] {message}",
            source="talk",
            context={"message": message},
        )
        target_role.add_task(task)
        return (
            f"消息已发送给 {target_role.name} ({target}), 紧急度={urgency.name}, "
            f"对方队列现有 {target_role.queue_depth} 个任务."
        )

    def _list_roles_handler(args: dict[str, Any]) -> str:
        """list_roles 工具处理函数: 实时获取当前团队角色列表.

        参数:
            args: 无.

        返回:
            当前角色花名册 (姓名/role_id/职责/技能).
        """
        roster = build_team_roster(pool)  # 动态构建, 包含新入职角色
        if not roster:
            return "(当前无团队成员)"
        return f"当前团队成员:\n{roster}"

    tk.add_python_tool(
        name="talk",
        description=(
            "给团队成员发送消息或委托任务. "
            "团队当前有哪些成员请先调用 list_roles 获取 (名单是动态的, 可能有新入职). "
            "根据每个人的职责选择合适的人选后, 用 target 发送.\n"
            "target 参数使用 role_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标人员的 role_id (团队名单请先通过 list_roles 获取)",
                },
                "message": {
                    "type": "string",
                    "description": "要发送的消息或委托的任务, 描述要具体.",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["LOW", "NORMAL", "HIGH", "CRITICAL"],
                    "description": "紧急程度, 生产事故用 CRITICAL.",
                },
            },
            "required": ["target", "message"],
        },
        handler=_talk_handler,
    )

    tk.add_python_tool(
        name="list_roles",
        description=(
            "获取当前团队都有哪些角色 (姓名/role_id/职责/技能). "
            "在向同事发消息前, 或不确定该找谁处理某件事时, 先调用此工具查看团队成员."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_list_roles_handler,
    )

    return tk
