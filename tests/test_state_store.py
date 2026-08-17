"""统一状态存储 (StateStore) 测试.

覆盖:
  - save/restore 往返: 角色档案 / 任务历史 / 队列待办 / 时间进度
  - 角色状态与字段恢复
  - 电脑/容器信息恢复 (local 电脑, 不建容器)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.core.agent_system import AgentSystem
from src.core.roles import Task, Urgency
from src.core.state_store import StateStore
from src.core.types import AgentState


def _make(tmp_path, monkeypatch, rid: str) -> AgentSystem:
    """构造不装配电脑/MCP 的轻量系统 (测试专用)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    return AgentSystem(role_ids=[rid], auto_toolkits=False)


def test_save_restore_roundtrip(tmp_path, monkeypatch):
    """任务历史 / 队列待办 / 时间进度 完整往返."""
    store = StateStore(tmp_path / "state.json")
    s1 = _make(tmp_path, monkeypatch, "CEO")
    ceo = s1.get_role("CEO")
    ceo._task_history.append(Task(
        urgency=Urgency.HIGH, description="完成登录页开发", source="github",
        status="done", result="已交付", tokens_consumed=456))
    ceo.add_task(Task(urgency=Urgency.NORMAL, description="待办: 写周报"))
    # Tick 前移到 30 → 第 1 天 Tick 30
    s1.time_manager._tick = 30
    store.save(s1)

    s2 = _make(tmp_path, monkeypatch, "CEO")
    assert store.restore(s2) == 1
    ceo2 = s2.get_role("CEO")
    assert ceo2._task_history[0].description == "完成登录页开发"
    assert ceo2._task_history[0].tokens_consumed == 456
    assert ceo2.queue_depth == 1
    assert ceo2.peek_next_urgency() == Urgency.NORMAL

    # 时间恢复: start() 应用进度 → 第 1 天 Tick 30
    s2.time_manager.start()
    try:
        assert s2.time_manager.day_number() == 1
        assert s2.time_manager.tick_of_day() == 30
    finally:
        s2.time_manager.stop()


def test_save_restore_state_and_fields(tmp_path, monkeypatch):
    """角色状态与档案字段恢复 (模板角色以存档为准)."""
    store = StateStore(tmp_path / "state.json")
    s1 = _make(tmp_path, monkeypatch, "HR")
    hr = s1.get_role("HR")
    hr.state = AgentState.OFF_DUTY
    hr.personality = "存档覆盖的性格"
    hr.skills = ["存档技能A", "存档技能B"]
    store.save(s1)

    s2 = _make(tmp_path, monkeypatch, "HR")
    store.restore(s2)
    hr2 = s2.get_role("HR")
    assert hr2.state == AgentState.OFF_DUTY
    assert hr2.personality == "存档覆盖的性格"
    assert hr2.skills == ["存档技能A", "存档技能B"]


def test_computer_restore_local(tmp_path, monkeypatch):
    """电脑/容器信息恢复: 存档的电脑重启后重建并绑定 (local, 不建容器)."""
    from src.core.computer import _COMPUTER_MANAGER, create_computer
    from src.core.computer import LocalComputer

    store = StateStore(tmp_path / "state.json")
    s1 = _make(tmp_path, monkeypatch, "architect")
    role = s1.get_role("architect")
    comp = create_computer("local", role_id="architect")
    _COMPUTER_MANAGER.register(comp, name=role.name)
    role._computer = comp
    store.save(s1)

    s2 = _make(tmp_path, monkeypatch, "architect")
    store.restore(s2)
    role2 = s2.get_role("architect")
    assert role2._computer is not None
    assert isinstance(role2._computer, LocalComputer)
    assert role2._computer.role_id == "architect"


def test_restore_no_state(tmp_path, monkeypatch):
    """无存档时 restore 返回 0, 不报错."""
    s = _make(tmp_path, monkeypatch, "CEO")
    store = StateStore(tmp_path / "state.json")
    assert store.restore(s) == 0
