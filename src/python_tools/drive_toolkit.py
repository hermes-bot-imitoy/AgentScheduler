"""企业云盘工具类 (Drive ToolKit) — 公共资源共享.

云盘 = 目录树 (data/drive/<角色名>/...), 根目录第一级文件夹为各角色名字:
  - 自己的目录: 读写; 其他角色目录: 只读 (默认)
  - 可为自己目录授权其他角色写 (drive_set_permission)

包含:
  - drive_list:   列出目录内容 (空路径 = 云盘根, 显示各角色目录)
  - drive_upload: 上传文件 (写入自己或有写权限的目录)
  - drive_read:   读取文件 (所有角色默认可读)
  - drive_delete: 删除文件/目录 (需写权限)
  - drive_rename: 重命名 (需写权限)
  - drive_copy:   复制 (读源 + 写目标)
  - drive_move:   移动 (源/目标都要写权限)
  - drive_search: 全盘查找文件名
  - drive_set_permission: 为其他角色设置对我目录的写权限

路径格式: "角色名/子路径/文件名", 如 "郭晓东/设计稿.md" (根 = 角色目录).
路径第一级必须是角色目录名; 非法路径/越权会返回错误.

用法:
    from src.python_tools.drive_toolkit import create_drive_toolkit
    role.add_toolkit(create_drive_toolkit(drive_store))
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_drive_toolkit(drive: Any) -> ToolKit:
    """创建企业云盘工具类.

    参数:
        drive: DriveStore 实例 (全局共享一份).

    返回:
        包含 drive_* 系列工具的 ToolKit.
    """

    tk = ToolKit(name="drive", description="企业云盘工具类: 公共资源文件管理")

    # 工具类持有 drive / role 引用 (由 AgentRole.add_toolkit 注入)
    tk._drive_holder = {"drive": drive, "role": None}  # type: ignore[attr-defined]

    def _get_drive() -> Any:
        return getattr(tk, "_drive_holder", {}).get("drive")

    def _get_role() -> Any:
        return getattr(tk, "_drive_holder", {}).get("role")

    def _actor() -> str:
        """当前操作者 (角色名, 供权限判断)."""
        role = _get_role()
        if role is None:
            raise RuntimeError("云盘工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return role.name

    def _err(exc: Exception) -> str:
        return f"错误: {exc}"

    def _fmt_entries(entries: list[dict[str, Any]]) -> str:
        if not entries:
            return "(空)"
        return "\n".join(
            f"- {'📁' if e['type'] == 'dir' else '📄'} {e['name']}  ({e['path']})"
            for e in entries)

    def _drive_list(args: dict[str, Any]) -> str:
        """列出目录内容.

        参数:
            args: {"path": 目录路径 (可选, 空 = 云盘根)}

        返回:
            目录条目列表.
        """
        drive = _get_drive()
        try:
            entries = drive.list_dir(_actor(), args.get("path") or "")
        except ValueError as exc:
            return _err(exc)
        return _fmt_entries(entries)

    def _drive_upload(args: dict[str, Any]) -> str:
        """上传文件.

        参数:
            args: {"path": 目标路径(须在本人或有写权限的目录下),
                   "content": 文件内容}

        返回:
            保存路径.
        """
        path = args.get("path", "").strip()
        content = args.get("content", "")
        if not path:
            return "错误: 'path' (目标路径) 为必填参数."
        drive = _get_drive()
        try:
            saved = drive.upload(_actor(), path, content)
        except (ValueError, PermissionError) as exc:
            return _err(exc)
        return f"已上传: {saved}"

    def _drive_read(args: dict[str, Any]) -> str:
        """读取文件内容.

        参数:
            args: {"path": 文件路径}

        返回:
            文件内容.
        """
        path = args.get("path", "").strip()
        if not path:
            return "错误: 'path' (文件路径) 为必填参数."
        drive = _get_drive()
        try:
            return drive.read(_actor(), path)
        except ValueError as exc:
            return _err(exc)

    def _drive_delete(args: dict[str, Any]) -> str:
        """删除文件/目录.

        参数:
            args: {"path": 目标路径}

        返回:
            删除结果.
        """
        path = args.get("path", "").strip()
        if not path:
            return "错误: 'path' (目标路径) 为必填参数."
        drive = _get_drive()
        try:
            ok = drive.delete(_actor(), path)
        except (ValueError, PermissionError) as exc:
            return _err(exc)
        return f"已删除: {path}" if ok else f"文件不存在: {path}"

    def _drive_rename(args: dict[str, Any]) -> str:
        """重命名文件/目录.

        参数:
            args: {"path": 源路径, "new_name": 新文件名}

        返回:
            新路径.
        """
        path = args.get("path", "").strip()
        new_name = args.get("new_name", "").strip()
        if not path or not new_name:
            return "错误: 'path' 和 'new_name' 为必填参数."
        drive = _get_drive()
        try:
            new_path = drive.rename(_actor(), path, new_name)
        except (ValueError, PermissionError) as exc:
            return _err(exc)
        return f"已重命名: {path} → {new_path}"

    def _drive_copy(args: dict[str, Any]) -> str:
        """复制文件/目录.

        参数:
            args: {"src": 源路径, "dst": 目标路径}

        返回:
            目标路径.
        """
        src = args.get("src", "").strip()
        dst = args.get("dst", "").strip()
        if not src or not dst:
            return "错误: 'src' 和 'dst' 为必填参数."
        drive = _get_drive()
        try:
            saved = drive.copy(_actor(), src, dst)
        except (ValueError, PermissionError) as exc:
            return _err(exc)
        return f"已复制: {src} → {saved}"

    def _drive_move(args: dict[str, Any]) -> str:
        """移动文件/目录.

        参数:
            args: {"src": 源路径, "dst": 目标路径}

        返回:
            目标路径.
        """
        src = args.get("src", "").strip()
        dst = args.get("dst", "").strip()
        if not src or not dst:
            return "错误: 'src' 和 'dst' 为必填参数."
        drive = _get_drive()
        try:
            saved = drive.move(_actor(), src, dst)
        except (ValueError, PermissionError) as exc:
            return _err(exc)
        return f"已移动: {src} → {saved}"

    def _drive_search(args: dict[str, Any]) -> str:
        """全盘查找文件.

        参数:
            args: {"keyword": 文件名关键词}

        返回:
            匹配路径列表.
        """
        keyword = args.get("keyword", "").strip()
        if not keyword:
            return "错误: 'keyword' (关键词) 为必填参数."
        drive = _get_drive()
        hits = drive.search(_actor(), keyword)
        if not hits:
            return f"(未找到包含 '{keyword}' 的文件)"
        return "\n".join(f"- {h}" for h in hits)

    def _drive_set_permission(args: dict[str, Any]) -> str:
        """为其他角色设置权限.

        参数:
            args: {"target_name": 目标角色名, "writable": 是否授予写权限}

        返回:
            设置结果.
        """
        target = args.get("target_name", "").strip()
        writable = args.get("writable")
        if not target:
            return "错误: 'target_name' (目标角色名) 为必填参数."
        if writable is None:
            return "错误: 'writable' (是否授予写权限) 为必填参数 (true/false)."
        if isinstance(writable, str):
            writable = writable.lower() in ("1", "true", "yes", "on")
        drive = _get_drive()
        if not drive.set_permission(_actor(), target, bool(writable)):
            return (f"错误: 设置失败 (你只能管理自己的目录, 且 '{target}' "
                    f"必须有云盘目录)")
        verb = "授予" if writable else "撤销"
        return f"已{verb} '{target}' 对你目录的写权限。"

    tk.add_python_tool(name="drive_list", description=(
        "列出企业云盘目录内容. path 为空 = 云盘根 (各角色目录); "
        "path 填 '角色名/子目录' 查看具体目录. 所有角色目录默认只读可看."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径 (可选, 空 = 云盘根)"},
        }}, handler=_drive_list)
    tk.add_python_tool(name="drive_upload", description=(
        "上传文件到企业云盘. path 必须是本人目录 (自己名字) 下, 或已授权给"
        "你的目录下. 例: drive_upload('郭晓东/设计稿.md', '内容')."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目标路径 (角色名/子路径/文件名)"},
            "content": {"type": "string", "description": "文件内容"},
        }, "required": ["path", "content"]}, handler=_drive_upload)
    tk.add_python_tool(name="drive_read", description=(
        "读取企业云盘文件内容. 所有角色目录默认只读, 可读任何人的文件."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径 (角色名/子路径/文件名)"},
        }, "required": ["path"]}, handler=_drive_read)
    tk.add_python_tool(name="drive_delete", description=(
        "删除企业云盘文件或目录. 需要对该目录有写权限 (本人目录或已授权)."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目标路径"},
        }, "required": ["path"]}, handler=_drive_delete)
    tk.add_python_tool(name="drive_rename", description=(
        "重命名企业云盘文件/目录. 需要写权限."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "源路径"},
            "new_name": {"type": "string", "description": "新文件名"},
        }, "required": ["path", "new_name"]}, handler=_drive_rename)
    tk.add_python_tool(name="drive_copy", description=(
        "复制企业云盘文件/目录: 源可读即可, 目标处需要写权限. "
        "例: drive_copy('王建国/方案.md', '郭晓东/方案副本.md')."),
        input_schema={"type": "object", "properties": {
            "src": {"type": "string", "description": "源路径"},
            "dst": {"type": "string", "description": "目标路径"},
        }, "required": ["src", "dst"]}, handler=_drive_copy)
    tk.add_python_tool(name="drive_move", description=(
        "移动企业云盘文件/目录 (源删除+目标写入, 两处都要写权限)."),
        input_schema={"type": "object", "properties": {
            "src": {"type": "string", "description": "源路径"},
            "dst": {"type": "string", "description": "目标路径"},
        }, "required": ["src", "dst"]}, handler=_drive_move)
    tk.add_python_tool(name="drive_search", description=(
        "在全盘搜索文件名包含关键词的文件 (所有角色目录默认可读)."),
        input_schema={"type": "object", "properties": {
            "keyword": {"type": "string", "description": "文件名关键词"},
        }, "required": ["keyword"]}, handler=_drive_search)
    tk.add_python_tool(name="drive_set_permission", description=(
        "为其他角色设置对你目录的权限: writable=true 授予对方写你目录的权限, "
        "false 撤销 (默认其他角色只读你的目录). 只有你能管理自己目录的权限."),
        input_schema={"type": "object", "properties": {
            "target_name": {"type": "string", "description": "目标角色名 (花名册里的名字)"},
            "writable": {"type": "boolean", "description": "true=授予写权限, false=撤销"},
        }, "required": ["target_name", "writable"]}, handler=_drive_set_permission)

    return tk


def bind_drive_to_toolkit(toolkit: ToolKit, drive: Any, role: Any) -> None:
    """将 DriveStore 与角色绑定到工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: drive 工具类实例.
        drive:   全局 DriveStore 实例.
        role:    绑定的 AgentRole (提供角色名用于权限判断).
    """
    toolkit._drive_holder["drive"] = drive  # type: ignore[attr-defined]
    toolkit._drive_holder["role"] = role    # type: ignore[attr-defined]
