"""时间工具类 (Time ToolKit) — 作息系统的时间工具.

包含:
  - get_time: 查看当前 Tick 与作息状态
  - take_rest: 休息工具. 休息指定 Tick 数, 期间状态为 ON_DUTY_IDLE

时间规则: 1 Tick = 10 分钟. 系统启动 = Tick 0, 每天第 60 Tick 下班.

用法:
    from src.python_tools.time_toolkit import create_time_toolkit
    role.add_toolkit(create_time_toolkit())
"""

from __future__ import annotations

import logging
import time as time_module
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)

# 休息等待的最大真实秒数 (防止真实时钟下长时间阻塞)
REST_MAX_WAIT_SECONDS = 120


def create_time_toolkit() -> ToolKit:
    """创建时间工具类.

    返回:
        包含 get_time / take_rest 工具的 ToolKit.
    """

    tk = ToolKit(name="time", description="时间与作息工具类")

    # 工具类持有 time_manager / role 引用 (由 AgentRole.add_toolkit 注入)
    tk._time_holder = {"manager": None, "role": None}  # type: ignore[attr-defined]

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

    def _take_rest(args: dict[str, Any]) -> str:
        """休息: 休息指定 Tick 数, 期间角色状态为 ON_DUTY_IDLE.

        参数:
            args: {"ticks": 休息的 Tick 数 (1~60)}

        返回:
            休息结果说明.
        """
        manager = tk._time_holder["manager"]  # type: ignore[attr-defined]
        role = tk._time_holder["role"]  # type: ignore[attr-defined]
        if manager is None:
            raise RuntimeError("时间工具类尚未绑定 TimeManager, 请通过 role.add_toolkit() 注册")

        try:
            ticks = int(args.get("ticks", 1))
        except (TypeError, ValueError):
            return "错误: 'ticks' 必须是整数 (休息的 Tick 数)."
        if not (1 <= ticks <= 60):
            return "错误: 'ticks' 必须在 1~60 范围内."

        # 休息期间状态为 ON_DUTY_IDLE
        from src.core.types import AgentState
        if role is not None and role.state != AgentState.ON_DUTY_IDLE:
            role.state = AgentState.ON_DUTY_IDLE
            logger.info("[%s] 开始休息 %d Ticks (状态 ON_DUTY_IDLE)", role.role_id, ticks)

        start_tick = manager.current_tick()
        target_tick = start_tick + ticks

        # 等待时间推进到目标 Tick (模拟时钟由外部推进, 真实时钟 1 Tick = 10 分钟)
        waited = 0.0
        while manager.current_tick() < target_tick:
            if waited >= REST_MAX_WAIT_SECONDS:
                return (f"休息中断: 等待超过 {REST_MAX_WAIT_SECONDS} 秒仍未到达目标 Tick "
                        f"(start={start_tick}, target={target_tick}, now={manager.current_tick()}). "
                        "已恢复工作.")
            time_module.sleep(1)
            waited += 1.0

        return f"休息结束: 从 Tick {start_tick} 休息到 Tick {target_tick} (共 {ticks} Ticks). 已恢复工作."

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

    tk.add_python_tool(
        name="take_rest",
        description=(
            "休息工具. 休息指定的 Tick 数 (1 Tick = 10 分钟), 休息期间你的状态为 ON_DUTY_IDLE. "
            "适合在高强度工作之间安排休息, 或在等待他人工作时短暂放松. "
            "休息结束后自动恢复工作."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticks": {"type": "integer", "description": "休息的 Tick 数 (1~60, 1 Tick = 10 分钟)"},
            },
            "required": ["ticks"],
        },
        handler=_take_rest,
    )

    return tk


def bind_time_to_toolkit(toolkit: ToolKit, manager: Any, role: Any = None) -> None:
    """将 TimeManager 绑定到时间工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit:  时间工具类实例
        manager:  TimeManager 实例
        role:     绑定的 AgentRole (可选, 用于休息时设置状态)
    """
    toolkit._time_holder["manager"] = manager  # type: ignore[attr-defined]
    toolkit._time_holder["role"] = role        # type: ignore[attr-defined]
