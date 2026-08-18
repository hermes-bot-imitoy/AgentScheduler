"""企业云盘存储 (DriveStore) — 公共资源共享 (目录树 + 权限模型).

目录结构: data/drive/<角色名>/... (data/ 整体 gitignored)
  - 根目录第一级文件夹 = 各角色名字 (如 郭晓东/, 王建国/)
  - 每个角色有自己名字的目录, 默认权限:
      * 自己的目录: 读写
      * 其他角色的目录: 只读
  - ACL 授权: owner 可授予其他角色对自己目录的写权限 (data/drive/.permissions.json)

权限模型:
  - 写操作 (上传/删除/重命名/复制目标/移动) 需对目标目录有写权限
  - 读操作 (读取/列出/搜索) 所有角色默认可读 (只读)
  - 路径第一级必须是对应角色的目录名, 非法路径拒绝

安全: 所有路径解析后校验在云盘根目录内 (防 ../ 路径穿越).

用法:
    store = DriveStore(base_dir="./data/drive")
    store.ensure_role_dir("郭晓东")
    store.upload("郭晓东", "郭晓东/设计稿.md", "内容")
    store.read("王建国", "郭晓东/设计稿.md")   # 只读 OK
    store.set_permission("郭晓东", "王建国", True)  # 王建国可写郭晓东目录
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ACL 文件名 (藏在云盘根, 工具层不可见/不可操作)
_ACL_FILE = ".permissions.json"


class DriveStore:
    """企业云盘: 角色目录树 + 权限控制 + 文件操作.

    参数:
        base_dir: 云盘根目录 (默认 ./data/drive).
    """

    def __init__(self, base_dir: str = "./data/drive"):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._acl_path = self._base / _ACL_FILE
        self._acl: dict[str, dict[str, list[str]]] = self._load_acl()

    # ── ACL (权限表) ──────────────────────────────────────

    def _load_acl(self) -> dict[str, dict[str, list[str]]]:
        """读取 ACL (文件不存在返回空表)."""
        if not self._acl_path.exists():
            return {}
        try:
            data = json.loads(self._acl_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("DriveStore 读取 ACL 失败: %s", exc)
            return {}

    def _save_acl(self) -> None:
        """原子写 ACL."""
        self._acl_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._acl_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._acl, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._acl_path)

    def can_write(self, actor_name: str, owner_name: str) -> bool:
        """判断 actor 是否对 owner 的目录有写权限.

        规则: 本人 = 读写; 他人默认只读, 仅当 owner 在 ACL 中授予了写权限.

        参数:
            actor_name: 访问者角色名.
            owner_name: 目录属主角色名.

        返回:
            True = 可写.
        """
        if actor_name == owner_name:
            return True
        return actor_name in self._acl.get(owner_name, {}).get("writable", [])

    def set_permission(self, actor_name: str, target_name: str,
                       writable: bool) -> bool:
        """owner 授予/撤销 target 对自己目录的写权限.

        参数:
            actor_name: 目录属主 (只有 owner 能改自己目录的 ACL).
            target_name: 被授权/撤销的角色名.
            writable: True = 授予写权限, False = 撤销.

        返回:
            True = 设置成功; False = actor 不是 owner 或 target 无目录.
        """
        if not self.role_dir_exists(actor_name) or not self.role_dir_exists(target_name):
            return False
        entry = self._acl.setdefault(actor_name, {"writable": []})
        writable_list = entry.setdefault("writable", [])
        if writable and target_name not in writable_list:
            writable_list.append(target_name)
        elif not writable and target_name in writable_list:
            writable_list.remove(target_name)
        self._save_acl()
        logger.info("DriveStore: %s 的目录授权 %s 写=%s",
                    actor_name, target_name, writable)
        return True

    # ── 角色目录 ──────────────────────────────────────────

    def role_dir(self, role_name: str) -> Path:
        """角色目录路径 (名字 = 根目录第一级文件夹)."""
        return self._base / role_name

    def role_dir_exists(self, role_name: str) -> bool:
        """角色目录是否已创建."""
        return self.role_dir(role_name).is_dir()

    def ensure_role_dir(self, role_name: str) -> Path:
        """创建角色目录 (幂等). 所有角色注册时调用."""
        d = self.role_dir(role_name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_role_dirs(self) -> list[str]:
        """列出云盘根目录下所有角色目录名 (排除 ACL 隐藏文件)."""
        return sorted(
            p.name for p in self._base.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    # ── 路径解析与校验 ────────────────────────────────────

    def _resolve(self, path: str) -> tuple[str, Path]:
        """解析云盘路径 → (owner_name, 绝对路径).

        规则:
          - 路径第一级必须是已存在的角色目录名
          - 解析后必须在云盘根目录内 (防 ../ 路径穿越)
          - 顶层 ACL 隐藏文件不可直接访问

        参数:
            path: 云盘相对路径, 如 "郭晓东/设计稿.md".

        返回:
            (owner_name, 绝对路径).

        异常:
            ValueError: 非法路径 (无属主/越界/隐藏文件).
        """
        p = Path(path)
        parts = p.parts
        if not parts or parts[0] in ("", ".", _ACL_FILE) or p.is_absolute():
            raise ValueError(f"非法路径: '{path}' (路径必须以角色目录名开头)")
        owner = parts[0]
        if not self.role_dir_exists(owner):
            raise ValueError(f"云盘中不存在角色目录 '{owner}' (目录 = 角色名字)")
        full = (self._base / p).resolve()
        base_resolved = self._base.resolve()
        if not str(full).startswith(str(base_resolved) + os.sep):
            raise ValueError(f"非法路径: '{path}' (越出云盘根目录)")
        return owner, full

    def _check_write(self, actor_name: str, owner_name: str) -> None:
        """写权限检查, 无权限抛 PermissionError."""
        if not self.can_write(actor_name, owner_name):
            raise PermissionError(
                f"无写权限: '{actor_name}' 只能读 '{owner_name}' 的目录 "
                f"(默认只读, 需 {owner_name} 授权)")

    # ── 文件操作 ──────────────────────────────────────────

    def upload(self, actor_name: str, path: str, content: str) -> str:
        """上传文件 (写入内容, 自动创建父目录).

        参数:
            actor_name: 操作者角色名.
            path:       目标路径 (须在 actor 有写权限的目录下).
            content:    文件内容.

        返回:
            保存的绝对路径.

        异常:
            ValueError / PermissionError.
        """
        owner, full = self._resolve(path)
        self._check_write(actor_name, owner)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        logger.info("DriveStore: %s 上传 %s (%d 字符)", actor_name, path, len(content))
        return str(full)

    def read(self, actor_name: str, path: str) -> str:
        """读取文件内容 (所有角色默认只读).

        参数:
            actor_name: 操作者角色名.
            path:       文件路径.

        返回:
            文件内容; 不存在返回错误提示字符串.

        异常:
            ValueError (非法路径).
        """
        _owner, full = self._resolve(path)
        if full.is_dir():
            raise ValueError(f"'{path}' 是目录, 请用 drive_list 查看")
        if not full.exists():
            return f"文件不存在: {path}"
        return full.read_text(encoding="utf-8")

    def list_dir(self, actor_name: str, path: str = "") -> list[dict[str, Any]]:
        """列出目录内容 (只读操作).

        参数:
            actor_name: 操作者角色名.
            path:       目录路径 (空 = 云盘根).

        返回:
            条目列表 [{"name", "type": file/dir, "path"}].
        """
        if not path:
            entries = []
            for name in self.list_role_dirs():
                entries.append({"name": name, "type": "dir", "path": name})
            return entries
        _owner, full = self._resolve(path)
        if full.is_file():
            return [{"name": full.name, "type": "file", "path": path}]
        if not full.exists():
            return []
        entries = []
        for p in sorted(full.iterdir()):
            if p.name.startswith("."):
                continue
            rel = f"{path.rstrip('/')}/{p.name}"
            entries.append({"name": p.name,
                            "type": "dir" if p.is_dir() else "file",
                            "path": rel})
        return entries

    def delete(self, actor_name: str, path: str) -> bool:
        """删除文件或目录 (需写权限).

        参数:
            actor_name: 操作者角色名.
            path:       目标路径.

        返回:
            True = 已删除; False = 不存在.
        """
        owner, full = self._resolve(path)
        self._check_write(actor_name, owner)
        if not full.exists():
            return False
        if full.is_dir():
            shutil.rmtree(full)
        else:
            full.unlink()
        logger.info("DriveStore: %s 删除 %s", actor_name, path)
        return True

    def rename(self, actor_name: str, path: str, new_name: str) -> str:
        """重命名文件/目录 (需对源所在目录有写权限).

        参数:
            actor_name: 操作者角色名.
            path:       源路径.
            new_name:   新文件名 (不含目录部分).

        返回:
            新路径.

        异常:
            ValueError / PermissionError.
        """
        owner, full = self._resolve(path)
        self._check_write(actor_name, owner)
        if not full.exists():
            raise ValueError(f"文件不存在: {path}")
        new_name = Path(new_name).name  # 只取文件名, 忽略目录部分
        if not new_name or new_name in (".", _ACL_FILE):
            raise ValueError(f"非法文件名: '{new_name}'")
        target = full.parent / new_name
        if target.exists():
            raise ValueError(f"目标已存在: {target.name}")
        full.rename(target)
        logger.info("DriveStore: %s 重命名 %s → %s", actor_name, path, new_name)
        return str(target.relative_to(self._base))

    def copy(self, actor_name: str, src: str, dst: str) -> str:
        """复制文件/目录 (读源 + 写目标, 两处权限都要校验).

        参数:
            actor_name: 操作者角色名.
            src:        源路径.
            dst:        目标路径.

        返回:
            目标绝对路径.
        """
        src_owner, src_full = self._resolve(src)
        dst_owner, dst_full = self._resolve(dst)
        if not src_full.exists():
            raise ValueError(f"源不存在: {src}")
        # 读源 (他人目录默认可读) + 写目标
        self._check_write(actor_name, dst_owner)
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        if src_full.is_dir():
            shutil.copytree(src_full, dst_full, dirs_exist_ok=True)
        else:
            shutil.copy2(src_full, dst_full)
        logger.info("DriveStore: %s 复制 %s → %s", actor_name, src, dst)
        return str(dst_full.relative_to(self._base))

    def move(self, actor_name: str, src: str, dst: str) -> str:
        """移动文件/目录 (源删 + 目标写, 源所在目录也需写权限).

        参数:
            actor_name: 操作者角色名.
            src:        源路径.
            dst:        目标路径.

        返回:
            目标绝对路径.
        """
        src_owner, src_full = self._resolve(src)
        dst_owner, dst_full = self._resolve(dst)
        if not src_full.exists():
            raise ValueError(f"源不存在: {src}")
        self._check_write(actor_name, src_owner)  # 移动 = 从源删除
        self._check_write(actor_name, dst_owner)  # + 写入目标
        if dst_full.exists():
            raise ValueError(f"目标已存在: {dst}")
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_full), str(dst_full))
        logger.info("DriveStore: %s 移动 %s → %s", actor_name, src, dst)
        return str(dst_full.relative_to(self._base))

    def search(self, actor_name: str, keyword: str) -> list[str]:
        """全盘查找 (匹配文件名, 忽略大小写).

        参数:
            actor_name: 操作者角色名 (所有角色默认可读全盘).
            keyword:    关键词.

        返回:
            匹配路径列表 (相对云盘根).
        """
        kw = keyword.lower()
        hits: list[str] = []
        for d in self.list_role_dirs():
            base = self.role_dir(d)
            for p in base.rglob("*"):
                if p.is_file() and kw in p.name.lower():
                    hits.append(str(p.relative_to(self._base)))
        return sorted(hits)

    def file_exists(self, path: str) -> bool:
        """路径是否为已存在的文件 (talk 附件校验用).

        参数:
            path: 云盘路径.

        返回:
            True = 存在且是文件.
        """
        try:
            _owner, full = self._resolve(path)
        except ValueError:
            return False
        return full.is_file()
