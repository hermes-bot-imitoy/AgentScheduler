"""通信工具类 (Communication ToolKit).

包含 talk 工具: 角色之间的消息传递与任务委托。
这是一个 Python 原生工具类 (ToolKit) 的示例实现。

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


def create_talk_toolkit(pool: Any) -> ToolKit:
    """创建通信工具类 (talk 工具).

    参数:
        pool: RolePool 实例, 用于查找目标角色并投递任务.

    返回:
        包含 talk 工具的 ToolKit 实例.
    """
    from src.core.roles import Task, Urgency

    tk = ToolKit(name="communication", description="角色间通信工具类")

    # 构建团队花名册: 每个成员的姓名, 职能, 职责, 技能
    roster_lines: list[str] = []
    for rid, r in pool._roles.items():
        resp = r.responsibilities or r.title
        roster_lines.append(
            f"  - **{r.name}** (role_id: `{rid}`) -- {resp}  "
            f"Skills: {', '.join(r.skills[:4])}"
        )
    team_roster = "\n".join(roster_lines)

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

    tk.add_python_tool(
        name="talk",
        description=(
            "给团队成员发送消息或委托任务. 根据每个人的职责选择合适的人选.\n\n"
            f"**团队花名册:**\n{team_roster}\n\n"
            "target 参数使用 role_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": f"目标人员的 role_id. 可选: {', '.join(pool.list_roles())}",
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

    return tk
