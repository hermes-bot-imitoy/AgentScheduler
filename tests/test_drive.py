"""企业云盘 (DriveStore + drive 工具 + talk attachment) 测试.

覆盖:
  - 权限模型: 本人读写 / 他人只读 / ACL 授权写 / 越权拒绝
  - 文件操作: 上传/读取/列表/删除/重命名/复制/移动/查找
  - 路径安全: ../ 穿越 / 非法路径拒绝
  - 工具层: drive_* handler 调用
  - talk attachment: 附件校验 + 消息携带附件提示
"""

from __future__ import annotations

import pytest

from src.core.drive_store import DriveStore
from src.core.roles import AgentRole, RolePool
from src.python_tools.drive_toolkit import create_drive_toolkit
from src.python_tools.talk_toolkit import create_talk_toolkit


@pytest.fixture()
def drive(tmp_path) -> DriveStore:
    """独立云盘实例 (测试隔离)."""
    d = DriveStore(base_dir=str(tmp_path / "drive"))
    d.ensure_role_dir("郭晓东")
    d.ensure_role_dir("王建国")
    return d


# ── 权限模型 ──────────────────────────────────────────────


def test_owner_read_write_others_readonly(drive):
    """本人读写, 他人只读 (默认)."""
    # 郭晓东上传到自己目录
    drive.upload("郭晓东", "郭晓东/设计稿.md", "设计内容 v1")
    # 本人可读可改
    assert "设计内容" in drive.read("郭晓东", "郭晓东/设计稿.md")
    drive.upload("郭晓东", "郭晓东/设计稿.md", "v2")
    # 王建国只读: 可读
    assert "v2" in drive.read("王建国", "郭晓东/设计稿.md")
    # 王建国不能写郭晓东目录
    with pytest.raises(PermissionError):
        drive.upload("王建国", "郭晓东/被篡改.md", "x")
    with pytest.raises(PermissionError):
        drive.delete("王建国", "郭晓东/设计稿.md")


def test_acl_grant_write(drive):
    """ACL 授权后他人可写 (set_permission)."""
    assert drive.set_permission("郭晓东", "王建国", True) is True
    drive.upload("王建国", "郭晓东/协助文档.md", "王建国代写")
    assert "王建国代写" in drive.read("郭晓东", "郭晓东/协助文档.md")
    # 撤销后不能再写
    drive.set_permission("郭晓东", "王建国", False)
    with pytest.raises(PermissionError):
        drive.upload("王建国", "郭晓东/又写一个.md", "x")


def test_set_permission_affects_actor_own_dir(drive):
    """set_permission 只作用于操作者自己的目录 (授权他人写自己目录)."""
    # 王建国授权郭晓东写王建国目录
    assert drive.set_permission("王建国", "郭晓东", True) is True
    drive.upload("郭晓东", "王建国/协作文件.md", "郭晓东写入")
    assert "郭晓东写入" in drive.read("王建国", "王建国/协作文件.md")
    # 目标角色必须有云盘目录才可被授权
    assert drive.set_permission("郭晓东", "不存在的人", True) is False


# ── 文件操作 ──────────────────────────────────────────────


def test_file_ops(drive):
    """上传/读取/列表/重命名/复制/移动/删除."""
    drive.upload("郭晓东", "郭晓东/项目/方案.md", "方案内容")
    # 列表
    root = drive.list_dir("王建国")
    assert {"name": "郭晓东", "type": "dir", "path": "郭晓东"} in root
    sub = drive.list_dir("郭晓东", "郭晓东/项目")
    assert sub == [{"name": "方案.md", "type": "file", "path": "郭晓东/项目/方案.md"}]
    # 重命名
    new = drive.rename("郭晓东", "郭晓东/项目/方案.md", "方案v2.md")
    assert new == "郭晓东/项目/方案v2.md"
    # 复制 (读他人 + 写自己)
    dst = drive.copy("王建国", "郭晓东/项目/方案v2.md", "王建国/备份方案.md")
    assert "方案内容" in drive.read("王建国", "王建国/备份方案.md")
    # 移动 (自己目录内)
    moved = drive.move("王建国", "王建国/备份方案.md", "王建国/归档/备份方案.md")
    assert "方案内容" in drive.read("王建国", moved)
    # 删除
    assert drive.delete("郭晓东", "郭晓东/项目") is True
    assert drive.list_dir("郭晓东", "郭晓东/项目") == []


def test_search(drive):
    """全盘查找文件名."""
    drive.upload("郭晓东", "郭晓东/周报-第3周.md", "周报")
    drive.upload("王建国", "王建国/架构评审记录.md", "评审")
    hits = drive.search("郭晓东", "周报")
    assert hits == ["郭晓东/周报-第3周.md"]
    hits2 = drive.search("王建国", "评审")
    assert hits2 == ["王建国/架构评审记录.md"]


def test_path_traversal_rejected(drive):
    """路径穿越/非法路径拒绝."""
    with pytest.raises(ValueError):
        drive.read("郭晓东", "../秘密.md")
    with pytest.raises(ValueError):
        drive.read("郭晓东", "郭晓东/../../etc/passwd")
    with pytest.raises(ValueError):
        drive.upload("郭晓东", "/绝对路径.md", "x")
    with pytest.raises(ValueError):
        drive.read("郭晓东", "不存在的角色/文件.md")
    with pytest.raises(ValueError):
        drive.read("郭晓东", ".permissions.json")  # ACL 隐藏文件不可访问


# ── 工具层 ────────────────────────────────────────────────


def _bind_drive_tool(role: AgentRole, drive: DriveStore):
    tk = create_drive_toolkit(drive)
    tk._drive_holder["role"] = role  # type: ignore[attr-defined]
    return tk


def test_drive_tools_via_handler(drive, tmp_path, monkeypatch):
    """drive_* 工具全链路 (upload/read/list/set_permission)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1")
    b = AgentRole(name="王建国", role_id="architect")
    pool.add_role(a)
    pool.add_role(b)
    tk_a, tk_b = _bind_drive_tool(a, drive), _bind_drive_tool(b, drive)

    # 郭晓东上传
    r1 = tk_a._tools["drive_upload"].handler(
        {"path": "郭晓东/设计稿.md", "content": "内容"})
    assert "已上传" in r1
    # 王建国只读
    assert "内容" in tk_b._tools["drive_read"].handler({"path": "郭晓东/设计稿.md"})
    # 王建国越权上传被拒
    r2 = tk_b._tools["drive_upload"].handler({"path": "郭晓东/越权.md", "content": "x"})
    assert "无写权限" in r2
    # 授权后可写
    tk_a._tools["drive_set_permission"].handler(
        {"target_name": "王建国", "writable": True})
    r3 = tk_b._tools["drive_upload"].handler({"path": "郭晓东/协助.md", "content": "ok"})
    assert "已上传" in r3
    # 列表 / 查找
    assert "设计稿.md" in tk_a._tools["drive_list"].handler({"path": "郭晓东"})
    assert "设计稿.md" in tk_a._tools["drive_search"].handler({"keyword": "设计稿"})


# ── talk attachment ───────────────────────────────────────


def test_talk_attachment_validated_and_carried(drive, tmp_path, monkeypatch):
    """talk attachment: 无效附件拒绝; 有效附件随消息携带并提示对方."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1")
    b = AgentRole(name="王建国", role_id="architect")
    pool.add_role(a)
    pool.add_role(b)
    drive.upload("郭晓东", "郭晓东/设计稿.md", "附件内容")

    tk_a = create_talk_toolkit(pool, drive)
    tk_a._role_holder = {"role": a}  # type: ignore[attr-defined]
    tk_b = create_talk_toolkit(pool, drive)
    tk_b._role_holder = {"role": b}  # type: ignore[attr-defined]

    # 无效附件 (不存在) → 拒绝
    r = tk_a._tools["talk"].handler(
        {"target": "王建国", "message": "看下", "attachment": "郭晓东/不存在.md"})
    assert "附件无效" in r
    assert b.queue_depth == 0

    # 有效附件 → 消息送达, B 的任务描述带附件提示
    r2 = tk_a._tools["talk"].handler(
        {"target": "王建国", "message": "看下设计稿", "attachment": "郭晓东/设计稿.md"})
    assert "消息已发送给 王建国" in r2
    task = b.pop_task()
    assert task is not None
    assert "[附件: 郭晓东/设计稿.md]" in task.description
    assert "drive_read" in task.description
    assert task.context.get("attachment") == "郭晓东/设计稿.md"
