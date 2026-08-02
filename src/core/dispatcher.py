"""Event Dispatcher — 事件广播到所有角色的 Layer 1-3 过滤.

Bridges EventBus → RolePool:
  1. trigger(event) fans out to all roles
  2. Each role independently runs Layer 1-3 via AgentRole.evaluate_event()
  3. If PASS: converts event → Task, inserts into role's priority queue
  4. If BLOCKED/AMBIENT: logs reason, no task created for that role
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.roles import AgentRole, RolePool, Task
from src.core.types import Event, FilterDecision, Priority

logger = logging.getLogger(__name__)


# # 事件分发器: 将事件广播给所有角色, 每个角色独立运行3层过滤. PASS事件自动转Task插入队列
class EventDispatcher:
    """Broadcasts events to all roles with per-role filtering.

    Usage:
        pool = RolePool()
        pool.add_role(coder)
        pool.add_role(reviewer)
        pool.start()

        dispatcher = EventDispatcher(pool)
        results = dispatcher.trigger(event)
        # results: {"coder": (True, "PASS..."), "reviewer": (False, "Salience 0.2 < 0.4")}
    """

    def __init__(self, pool: RolePool):
        self._pool = pool
        self.stats: dict[str, int] = {
            "total_events": 0,
            "total_tasks_created": 0,
            "roles_notified": 0,
            "roles_activated": 0,
            "roles_skipped": 0,
        }

    # ── Public API ─────────────────────────────────────────

# # 触发事件广播. 返回{role_id: {accepted, reason, task_id}}. 每个角色调用evaluate_event()
    def trigger(self, event: Event) -> dict[str, dict[str, Any]]:
        """Fan out an event to roles.

        - 广播事件 (target_role=None): 所有角色各自运行 Layer 1-3 过滤.
        - 定向事件 (target_role=xxx):  只投递给指定角色, 直接接受 (跳过过滤).
          用于定时任务提醒等"只有本人该收到"的事件.

        Returns per-role result dict:
            {"role_name": {"accepted": bool, "reason": str, "task_id": str|None}}
        """
        self.stats["total_events"] += 1
        results: dict[str, dict[str, Any]] = {}

        logger.info(
            "EventDispatcher trigger: id=%s type=%s/%s priority=%s target=%s",
            event.id, event.source, event.event_type, event.priority.name,
            event.target_role or "(广播)",
        )

        # 定向事件: 只投递给 target_role, 其他角色跳过
        if event.target_role is not None:
            for role_name in self._pool._roles:
                if role_name == event.target_role:
                    continue
                self.stats["roles_notified"] += 1
                self.stats["roles_skipped"] += 1
                results[role_name] = {
                    "accepted": False,
                    "reason": f"定向事件, 目标: {event.target_role}",
                    "task_id": None,
                }

        for role_name, role in self._pool._roles.items():
            # 定向事件: 只处理目标角色, 直接接受 (任务是它自己创建的提醒)
            if event.target_role is not None:
                if role_name != event.target_role:
                    continue
                accepted, reason = True, f"定向任务提醒 (target_role={role_name})"
            else:
                self.stats["roles_notified"] += 1
                accepted, reason = role.evaluate_event(event)

            task_id = None
            if accepted:
                task = role.event_to_task(event)
                role.add_task(task)
                task_id = task.task_id
                self.stats["roles_activated"] += 1
                self.stats["total_tasks_created"] += 1
                logger.info(
                    "  → [%s] ACCEPTED: %s → Task %s (urgency=%s)",
                    role_name, reason, task_id, task.urgency,
                )
            else:
                self.stats["roles_skipped"] += 1
                logger.info("  → [%s] SKIPPED: %s", role_name, reason)

            results[role_name] = {
                "accepted": accepted,
                "reason": reason,
                "task_id": task_id,
            }

        return results

    def get_stats(self) -> dict[str, int]:
        return dict(self.stats)
