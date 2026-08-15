"""系统管理类 (AgentSystem) — 统一管理 TimeEventBus + RolePool + 事件总线.

职责:
  - 创建唯一的共享 TimeEventBus (单一时间源) 并绑定到所有角色
  - 创建 RolePool, 自动为角色注册 memory / time 工具类
  - 将 TimeEventBus 的作息事件 (SHIFT_START / SHIFT_END) 接入事件分发器
  - 统一 start / stop 生命周期
  - 对外提供事件投递 / 任务分配 / 状态查询

用法:
    from src.core.agent_system import AgentSystem

    system = AgentSystem(role_ids=["CEO", "COO", "HR", "CFO"])
    system.start()                     # 启动角色线程 + 时间线程 (Tick 0 = 启动)
    system.trigger(event)              # 投递外部事件
    system.assign_task("coder", task)  # 直接分配任务
    status = system.get_status()       # 角色状态快照
    print(system.describe())           # 第 X 天, Tick Y
    system.stop()                      # 停止一切
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.dispatcher import EventDispatcher
from src.core.roles import RolePool
from src.core.role_templates import DEFAULT_ROLES, get_template
from src.core.time_manager import EVENT_SHIFT_END, EVENT_SHIFT_START, TimeEventBus
from src.core.types import AgentState, Event

logger = logging.getLogger(__name__)


class AgentSystem:
    """统一管理 TimeEventBus 与 RolePool 的系统管理类.

    参数:
        roles:          预构建的 AgentRole 列表 (可选).
        role_ids:       角色模板 id 列表, 自动从模板创建 (可选).
        check_interval: 时间线程检查间隔秒数 (默认 30).
        auto_toolkits:  是否自动注册 memory/time 工具类 (默认 True).
    """

    def __init__(
        self,
        roles: Optional[list[Any]] = None,
        role_ids: Optional[list[str]] = None,
        check_interval: int = 30,
        auto_toolkits: bool = True,
    ):
        # 共享时间源: 所有角色绑定同一个 TimeEventBus
        self.time_manager = TimeEventBus(check_interval=check_interval)

        # 角色池 (注入共享时间源: 招聘入职的新角色也会绑定同一个时钟)
        self.pool = RolePool(time_manager=self.time_manager)
        self.dispatcher = EventDispatcher(self.pool)
        self.auto_toolkits = auto_toolkits

        # 时间线程的事件 → 事件分发器 (作息事件统一入口)
        self.time_manager.set_event_sender(self._on_time_event)

        # 快进: 全部角色空闲时自动跳到下一个事件 Tick
        self.time_manager.set_idle_checker(self._all_roles_idle)

        # 注册角色
        for role in roles or []:
            self.add_role(role)
        for rid in role_ids or []:
            self.add_role(get_template(rid))

    # ── 角色管理 ──────────────────────────────────────────

    def add_role(self, role: Any) -> Any:
        """注册角色: 绑定共享 TimeEventBus + 自动注册工具类.

        参数:
            role: AgentRole 实例.

        返回:
            传入的角色 (便于链式调用).
        """
        # 绑定共享时间源 (角色 get_time / summary 取到同一时间)
        role.bind_time_manager(self.time_manager)

        # 自动装配默认工具 + MCP 组 — 统一走 pool._setup_role (唯一入口,
        # 与招聘入职 add_role_and_start 同一条路径, 防止装配逻辑漂移)
        if self.auto_toolkits:
            self.pool._setup_role(role)

        self.pool.add_role(role)
        logger.info("AgentSystem: 角色已注册 %s (%s)", role.role_id, role.name)
        return role

    def add_default_roles(self) -> list[Any]:
        """注册全部默认管理角色 (CEO/COO/HR/CFO). 返回角色列表."""
        roles = []
        for rid in DEFAULT_ROLES:
            roles.append(self.add_role(get_template(rid)))
        return roles

    def get_role(self, role_id: str) -> Any:
        """按 role_id 获取角色."""
        return self.pool.get_role(role_id)

    def get_status(self) -> dict[str, dict[str, Any]]:
        """获取所有角色状态快照."""
        return self.pool.get_status()

    # ── 事件与任务 ────────────────────────────────────────

    def _on_time_event(self, event: Event) -> None:
        """时间线程作息事件的统一入口.

        - SHIFT_START: 将所有角色状态置为 ON_DUTY_IDLE (上班唤醒)
        - SHIFT_END:   交由角色处理 (instruction 指示调用 summary → OFF_DUTY)
        随后广播给所有角色 (EMERGENCY 穿透过滤).

        参数:
            event: 时间线程产生的作息事件.
        """
        if event.event_type == EVENT_SHIFT_START:
            for role in self.pool.all_roles():
                if role.state != AgentState.ON_DUTY_IDLE:
                    role.state = AgentState.ON_DUTY_IDLE
                    logger.info("AgentSystem: SHIFT_START → %s 上班 (ON_DUTY_IDLE)", role.role_id)
                # 上班自动开机 (下班后 summary 已自动关机)
                try:
                    if role._computer is not None and not role._computer.is_on:
                        role._computer.power_on()
                        logger.info("AgentSystem: SHIFT_START → %s 电脑已自动开机",
                                    role.role_id)
                except Exception:
                    logger.exception("AgentSystem: %s 上班开机失败", role.role_id)
            # 全局通知: 上班 → 每个角色的活动日志都写一条
            self.pool.journal_all(f"全局通知: 上班 (SHIFT_START, 第 {self.day} 天)")
        elif event.event_type == EVENT_SHIFT_END:
            # 全局通知: 下班 → 每个角色的活动日志都写一条
            self.pool.journal_all("全局通知: 下班时间到 (SHIFT_END), 各角色总结后休息")
        self.dispatcher.trigger(event)

    def _all_roles_idle(self) -> bool:
        """全部角色是否空闲 (供快进功能判定).

        空闲 = 无正在处理的任务 且 任务队列为空. 角色池为空视为不空闲
        (避免系统还没加角色时就快进).

        返回:
            True 表示全部角色空闲.
        """
        roles = self.pool.all_roles()
        if not roles:
            return False
        return all(not r.is_busy and r.queue_depth == 0 for r in roles)

    def trigger(self, event: Event) -> dict[str, dict[str, Any]]:
        """向事件总线投递事件, 广播给所有角色.

        参数:
            event: 要投递的事件.

        返回:
            各角色的过滤结果 {role_id: {"accepted": bool, ...}}.
        """
        return self.dispatcher.trigger(event)

    def assign_task(self, role_id: str, task: Any) -> None:
        """直接给指定角色分配任务.

        参数:
            role_id: 角色标识.
            task:    Task 实例.
        """
        self.pool.assign_task(role_id, task)

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        """启动系统: 角色池线程 + 时间线程.

        启动时刻 = Tick 0 / 第 1 天, 时间线程首次检查即触发 SHIFT_START.
        """
        self.pool.start()
        self.time_manager.start()
        logger.info("AgentSystem 已启动: %s", self.describe())

    def stop(self) -> None:
        """停止系统: 时间线程 + 角色池."""
        self.time_manager.stop()
        self.pool.shutdown(wait=False)
        logger.info("AgentSystem 已停止")

    # ── 时间查询 (转发共享 TimeEventBus) ───────────────────

    @property
    def tick(self) -> int:
        """当前 Tick 数 (系统启动 = 0)."""
        return self.time_manager.current_tick()

    @property
    def day(self) -> int:
        """当前第几天 (启动当天 = 1)."""
        return self.time_manager.day_number()

    def describe(self) -> str:
        """当前作息状态描述 (第几天 / Tick / 上班或下班)."""
        return self.time_manager.describe()
