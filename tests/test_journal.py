"""角色活动日志 (journal) 测试.

验证:
  - AgentRole.journal 写入 data/journals/<role_id>.md (测试时指向临时目录)
  - add_task 触发"收到任务"日志
  - RolePool.journal_all 全局通知写入每个角色
"""

from __future__ import annotations

from src.core.roles import AgentRole, RolePool, Task, Urgency

ROLE_ID = "test_journal_role"


def _journal_path(tmp_path, role_id: str) -> str:
    return str(tmp_path / f"{role_id}.md")


def test_agent_role_journal_writes_file(tmp_path, monkeypatch):
    """journal() 应把带时间前缀的内容追加到角色自己的日志文件."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    role = AgentRole(name="测试", role_id=ROLE_ID)

    role.journal("hello journal")

    content = open(_journal_path(tmp_path, ROLE_ID), encoding="utf-8").read()
    assert "hello journal" in content
    assert content.startswith("[D1 T0 ")  # 时间前缀: 第几天/Tick/时分秒


def test_add_task_writes_journal(tmp_path, monkeypatch):
    """任务入队 (上下文更新) 应写入角色活动日志."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    role = AgentRole(name="测试", role_id=ROLE_ID)

    role.add_task(Task(urgency=Urgency.NORMAL, description="写一篇技术文档"))

    content = open(_journal_path(tmp_path, ROLE_ID), encoding="utf-8").read()
    assert "收到任务" in content
    assert "写一篇技术文档" in content
    assert "NORMAL" in content


def test_journal_all_writes_every_role(tmp_path, monkeypatch):
    """全局通知: journal_all 应写入每个角色的日志文件."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    pool = RolePool()
    pool.add_role(AgentRole(name="甲", role_id="role_a"))
    pool.add_role(AgentRole(name="乙", role_id="role_b"))

    pool.journal_all("全局通知: 测试广播")

    for rid in ("role_a", "role_b"):
        content = open(_journal_path(tmp_path, rid), encoding="utf-8").read()
        assert "全局通知: 测试广播" in content


def test_add_role_creates_journal_immediately(tmp_path, monkeypatch):
    """角色注册进池时立即创建专属日志文件 (不等第一次活动)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    pool = RolePool()
    pool.add_role(AgentRole(name="张三", role_id="dev_1", title="前端开发"))
    pool.add_role(AgentRole(name="李四", role_id="dev_2", title="后端开发"))

    # 注册即生成文件, 且带"角色就位"记录
    for rid, name in (("dev_1", "张三"), ("dev_2", "李四")):
        content = open(_journal_path(tmp_path, rid), encoding="utf-8").read()
        assert "角色就位" in content
        assert name in content
