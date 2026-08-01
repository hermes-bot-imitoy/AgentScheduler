"""时间工具类 (Time ToolKit) — 作息系统的时间查询工具.

包含:
  - get_time:   查看当前 Tick 与作息状态

时间规则: 1 Tick = 10 分钟. Tick 0 = 上班 (09:00), Tick 60 = 下班 (19:00).

用法:
    from src.python_tools.time_toolkit import create_time_toolkit
    role.add_toolkit(create_time_toolkit())
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_time_toolkit() -> ToolKit:
    """创建时间工具类.

    返回:
        包含 get_time 工具的 ToolKit.
    """

    tk = ToolKit(name="time", description="时间与作息工具类")

    # 工具类持有 time_manager 引用 (由 AgentRole.add_toolkit 注入)
    tk._time_holder = {"manager": None}  # type: ignore[attr-defined]

    def _get_time(args: dict[str, Any]) -> str:
        """查看当前作息时间.

        参数:
            args: 无.

        返回:
            当前 Tick 数与作息状态描述.
        """
        manager = tk._time_holder["manager"]  # type: ignore[attr-defined]
        if manager is None:
            raise RuntimeError("时间工具类尚未绑定 TimeManager, 请通过 role.add_toolkit() 注册")

        tick = manager.current_tick()
        return manager.describe() + f"\n当前 Tick 数: {tick}"

    tk.add_python_tool(
        name="get_time",
        description=(
            "查看当前作息时间. 返回当前 Tick 数和作息状态. "
            "时间规则: 1 Tick = 10 分钟, 系统启动 = Tick 0, 每天第 60 Tick 下班. "
            "用于判断现在是上班时间还是下班时间, 或距离下班还有多久."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_get_time,
    )

    return tk


def bind_time_to_toolkit(toolkit: ToolKit, manager: Any) -> None:
    """将 TimeManager 绑定到时间工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit:  时间工具类实例
        manager:  TimeManager 实例
    """
    toolkit._time_holder["manager"] = manager  # type: ignore[attr-defined]
