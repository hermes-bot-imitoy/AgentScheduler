"""EventBus 收敛后单元测试 (2026-08 清理: 纯调度表, 无过滤管线).

覆盖:
  - register_event(tick=None) 必须抛 ValueError (裸 EventBus 无发送回调)
  - tick=N 注册 → list_scheduled_events / _check_due_events / cancel_event
  - TimeEventBus 覆写 register_event: tick=None 走 _dispatch → 发送回调

运行: cd 项目根 && .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from src.core.event_bus import EventBus
from src.core.time_manager import TimeEventBus
from src.core.types import Event, Priority


def _ev(event_type: str = "TEST") -> Event:
    return Event(source="test", event_type=event_type, priority=Priority.NORMAL)


class EventBusScheduleTest(unittest.TestCase):
    """裸 EventBus 只做定时调度表."""

    def setUp(self) -> None:
        self.bus = EventBus()

    def test_immediate_register_raises(self) -> None:
        # tick=None (立即触发) 已无意义: 裸 EventBus 没有发送回调
        with self.assertRaises(ValueError):
            self.bus.register_event(_ev())

    def test_scheduled_register_and_due(self) -> None:
        eid = self.bus.register_event(_ev("A"), tick=5)
        self.bus.register_event(_ev("B"), tick=3)
        # 按 tick 排序列出
        ticks = [s["tick"] for s in self.bus.list_scheduled_events()]
        self.assertEqual(ticks, [3, 5])
        # 到期取出 (已从调度表移除)
        due = self.bus._check_due_events(4)
        self.assertEqual([d.event_type for d in due], ["B"])
        # 未到期事件仍在表中 → 可取消
        self.assertTrue(self.bus.cancel_event(eid))

    def test_cancel_event(self) -> None:
        eid = self.bus.register_event(_ev(), tick=7)
        self.assertTrue(self.bus.cancel_event(eid))
        self.assertFalse(self.bus.cancel_event(eid))
        self.assertEqual(self.bus.list_scheduled_events(), [])


class TimeEventBusImmediateTest(unittest.TestCase):
    """TimeEventBus 覆写 register_event: tick=None 立即走 _dispatch."""

    def setUp(self) -> None:
        self.bus = TimeEventBus(check_interval=0.05)
        self.sent: list[str] = []
        self.bus.set_event_sender(lambda ev: self.sent.append(ev.event_type))

    def test_immediate_dispatches(self) -> None:
        self.bus.register_event(_ev("IMMEDIATE"), tick=None)
        self.assertEqual(self.sent, ["IMMEDIATE"])

    def test_scheduled_not_dispatched_immediately(self) -> None:
        self.bus.register_event(_ev("LATER"), tick=100)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.bus.list_scheduled_events()), 1)


if __name__ == "__main__":
    unittest.main()
