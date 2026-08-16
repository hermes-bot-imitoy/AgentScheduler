"""笔记与任务统一 (笔记 = 内容 + 可选提醒时间) 测试.

覆盖:
  - write_note 带 remind_tick → 注册提醒 (到点发事件)
  - 不带 remind_tick → 普通笔记 (无提醒)
  - delete_note → 取消关联提醒
  - edit_note 提供 remind_tick → 重置提醒
  - get_reminder / list_notes 提醒信息
  - 提醒到点 → TASK_DUE 事件触发 (可控时钟)
"""

from __future__ import annotations

import threading
import time as time_module
from datetime import datetime, timedelta

from src.core.note_store import NoteStore
from src.core.time_manager import TimeEventBus


class _FakeClock:
    """可推进的假时钟 (TimeEventBus 测试用)."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 1, 1, 9, 0)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


def _make_tm(clock: _FakeClock) -> TimeEventBus:
    tm = TimeEventBus(check_interval=0.05)
    tm.set_clock(clock)
    return tm


def test_write_note_with_reminder(tmp_path):
    """write_note 带 remind_tick → 注册提醒 (payload 携带笔记标题)."""
    tm = _make_tm(_FakeClock())
    store = NoteStore(role_id="tester_1", time_manager=tm)
    store.write_note("写周报", "本周工作小结", remind_tick=50)

    tasks = tm.list_tasks(owner_role="tester_1")
    assert len(tasks) == 1
    assert tasks[0].target_tick == 50
    assert tasks[0].payload.get("note_title") == "写周报"
    assert "[笔记提醒]" in tasks[0].description
    # get_reminder 查询
    assert store.get_reminder("写周报") == {"day": 1, "tick": 50}


def test_write_note_without_reminder(tmp_path):
    """不带 remind_tick → 普通笔记, 无提醒."""
    tm = _make_tm(_FakeClock())
    store = NoteStore(role_id="tester_1", time_manager=tm)
    store.write_note("普通笔记", "没有提醒")
    assert tm.list_tasks(owner_role="tester_1") == []
    assert store.get_reminder("普通笔记") is None


def test_delete_note_cancels_reminder(tmp_path):
    """删除带提醒的笔记 → 提醒一并取消."""
    tm = _make_tm(_FakeClock())
    store = NoteStore(role_id="tester_1", time_manager=tm)
    store.write_note("待办", "删掉", remind_tick=10)
    assert len(tm.list_tasks(owner_role="tester_1")) == 1
    assert store.delete_note("待办") is True
    assert tm.list_tasks(owner_role="tester_1") == []


def test_edit_note_resets_reminder(tmp_path):
    """edit_note 提供 remind_tick → 旧提醒取消, 注册新提醒."""
    tm = _make_tm(_FakeClock())
    store = NoteStore(role_id="tester_1", time_manager=tm)
    store.write_note("计划", "v1", remind_tick=5)
    store.edit_note("计划", "v2", remind_tick=30)

    tasks = tm.list_tasks(owner_role="tester_1")
    assert len(tasks) == 1  # 旧提醒已取消, 不重复
    assert tasks[0].target_tick == 30
    # 不带 remind_tick 编辑 → 保持原提醒
    store.edit_note("计划", "v3")
    tasks = tm.list_tasks(owner_role="tester_1")
    assert len(tasks) == 1 and tasks[0].target_tick == 30


def test_reminder_fires_task_due_event(tmp_path):
    """提醒到点 → TASK_DUE 事件触发 (可控时钟推进)."""
    clock = _FakeClock()
    tm = _make_tm(clock)
    captured: list = []
    tm.set_event_sender(lambda ev: captured.append(ev))
    store = NoteStore(role_id="tester_1", time_manager=tm)

    store.write_note("下午开会", "记得准备材料", remind_tick=3)
    tm.start()
    try:
        # 推进到 Tick 3 之后 (10 分钟/Tick); 等到 TASK_DUE 或超时
        # (SHIFT_START 与 TASK_DUE 同迭代相继投递, 不能以 captured 非空为完成条件)
        clock.advance(3 * 10 + 1)
        deadline = time_module.time() + 3
        while (time_module.time() < deadline
               and not any(ev.event_type == "TASK_DUE" for ev in captured)):
            time_module.sleep(0.05)
        assert captured, "TASK_DUE 事件未触发"
        # captured 里可能有 SHIFT_START (上班事件) — 找 TASK_DUE 提醒事件
        due = [ev for ev in captured if ev.event_type == "TASK_DUE"]
        assert due, f"未捕获 TASK_DUE 事件 (收到: {[ev.event_type for ev in captured]})"
        ev = due[0]
        assert ev.target_role == "tester_1"
        assert ev.payload.get("note_title") == "下午开会"
        assert len(due) == 1  # 只注册一次 → 只触发一次
    finally:
        tm.stop()


# ── 任务只注册一次 (创建时注册; 上班加载只补隔天到期任务) ──


def test_task_registered_once_on_creation(tmp_path):
    """当天任务: 创建时注册一次, 上班加载不重复注册."""
    clock = _FakeClock()
    tm = _make_tm(clock)
    tm.start()
    try:
        tm.schedule_task("今天的事", owner_role="r1", target_tick=5)
        assert len(tm._tick_schedule) == 1  # 创建时已注册
        # 上班加载: 当天任务已注册 (event_id 非空), 绝不重复
        tm._load_today_tasks_to_bus()
        assert len(tm._tick_schedule) == 1
        due = tm._check_due_events(999)
        assert len(due) == 1 and due[0].event_type == "TASK_DUE"
    finally:
        tm.stop()


def test_future_task_loaded_on_target_day(tmp_path):
    """隔天任务: 创建时不注册 (仅保存), 目标天上班加载时补注册一次."""
    clock = _FakeClock()
    tm = _make_tm(clock)
    tm.start()
    try:
        task = tm.schedule_task("明天的事", owner_role="r1", target_tick=5, day=2)
        assert task.event_id == ""            # 创建时是隔天 → 仅保存
        assert len(tm._tick_schedule) == 0
        # 推进到第 2 天 (1 天 = 144 Tick × 10 分钟), 上班加载
        clock.advance(144 * 10 + 1)
        tm._load_today_tasks_to_bus()
        assert task.event_id                   # 现在补注册了
        assert len(tm._tick_schedule) == 1
        # 再加载一次不重复
        tm._load_today_tasks_to_bus()
        assert len(tm._tick_schedule) == 1
    finally:
        tm.stop()


def test_duplicate_register_warns(tmp_path, caplog):
    """兜底: 已注册任务被再次注册 → WARNING (运行异常) 且不重复注册."""
    import logging
    clock = _FakeClock()
    tm = _make_tm(clock)
    tm.start()
    try:
        task = tm.schedule_task("只注册一次", owner_role="r1", target_tick=5)
        assert len(tm._tick_schedule) == 1
        with caplog.at_level(logging.WARNING, logger="src.core.time_manager"):
            tm._register_task_event_if_today(task)  # 异常路径: 重复注册
        assert len(tm._tick_schedule) == 1    # 未重复
        assert any("重复注册" in r.message for r in caplog.records)
    finally:
        tm.stop()
