"""笔记存储 (NoteStore) — 基于文件的笔记与日记存储.

每个 Role 绑定一个 NoteStore 实例, 内容按角色隔离:
    data/notes/<role_id>/<标题>.md          # 普通笔记
    data/notes/<role_id>/_summary_<日期>.md # 每日总结 (下一天注入提示词)

支持:
  - write_note: 写笔记 (标题 + 内容)
  - edit_note:  编辑已有笔记
  - list_notes: 列出所有笔记标题
  - read_note:  读取笔记内容
  - save_summary / get_latest_summary: 每日总结 (作息系统用)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NoteStore:
    """文件型笔记存储. 每个角色实例独立目录.

    参数:
        base_dir: 存储根目录 (默认 ./data/notes) — 无 computer 时的本地回退路径
        role_id:  角色标识, 用于隔离目录 (可为空, 由 AgentRole 传入)
        computer: 个人电脑实例 (可选). 提供后笔记/总结读写到电脑工作目录
                  <workdir>/notes/ 下 (默认 Podman 电脑), 否则落到本地 base_dir.
    """

    def __init__(self, base_dir: str = "./data/notes", role_id: str = "",
                 computer: Any = None):
        self._base = Path(base_dir)
        self.role_id = role_id
        self._computer = computer
        self._local_dir = self._base / (role_id or "shared")
        self._local_dir.mkdir(parents=True, exist_ok=True)

    # ── 路径工具 ──────────────────────────────────────────

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """清洗标题为合法文件名. 非法字符替换为下划线."""
        cleaned = re.sub(r'[\\/:*?"<>|#%\s]+', "_", title.strip())
        return cleaned or "untitled"

    @property
    def _dir(self) -> Path:
        """当前使用的目录 (电脑 workdir/notes 或本地)."""
        if self._computer is not None:
            return Path(self._computer.workdir) / "notes"
        return self._local_dir

    def _note_path(self, title: str) -> str:
        """笔记路径 (字符串, 供 computer 文件接口使用)."""
        return str(self._dir / f"{self._sanitize_title(title)}.md")

    def _summary_path(self, day: int) -> str:
        return str(self._dir / f"_summary_day_{day}.md")

    # ── 底层读写 (走电脑或本地) ──────────────────────────

    def _write(self, path: str, content: str) -> None:
        if self._computer is not None:
            self._computer.write_file(path, content)
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def _read(self, path: str) -> Optional[str]:
        if self._computer is not None:
            r = self._computer.read_file(path)
            if r.startswith("文件不存在") or r.startswith("错误:"):
                return None
            return r
        p = Path(path)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def _list_md_files(self) -> list[Path]:
        """列出 notes 目录下所有 .md 文件 (仅文件名, 按名排序)."""
        if self._computer is not None:
            listing = self._computer.list_dir(str(self._dir))
            names = []
            for line in listing.splitlines():
                # ls 输出取最后一列文件名
                name = line.split()[-1] if line.split() else ""
                if name.endswith(".md"):
                    names.append(Path(name))
            return sorted(names)
        if not self._dir.exists():
            return []
        return sorted(p for p in self._dir.glob("*.md"))

    # ── 笔记操作 ──────────────────────────────────────────

    def write_note(self, title: str, content: str) -> str:
        """写笔记. 已存在则覆盖.

        参数:
            title:   笔记标题
            content: 笔记内容

        返回:
            保存路径.
        """
        path = self._note_path(title)
        self._write(path, content)
        logger.info("[%s] 笔记已写入: %s", self.role_id, Path(path).name)
        return path

    def edit_note(self, title: str, content: str) -> str:
        """编辑已有笔记 (覆盖内容). 不存在则创建.

        参数:
            title:   笔记标题
            content: 新内容

        返回:
            保存路径.
        """
        path = self._note_path(title)
        self._write(path, content)
        logger.info("[%s] 笔记已编辑: %s", self.role_id, Path(path).name)
        return path

    def list_notes(self) -> list[str]:
        """列出所有笔记标题 (不含每日总结). 按文件名排序.

        返回:
            标题字符串列表.
        """
        titles = []
        for p in self._list_md_files():
            if p.name.startswith("_summary_"):
                continue  # 跳过总结文件
            titles.append(p.stem)
        return titles

    def read_note(self, title: str) -> Optional[str]:
        """读取笔记内容.

        参数:
            title: 笔记标题

        返回:
            内容字符串, 不存在返回 None.
        """
        path = self._note_path(title)
        return self._read(path)

    def delete_note(self, title: str) -> bool:
        """删除笔记. 返回是否删除成功."""
        path = self._note_path(title)
        if self._read(path) is not None:
            self._write(path, "")  # 无删除接口, 置空文件
            logger.info("[%s] 笔记已删除: %s", self.role_id, Path(path).name)
            return True
        return False

    # ── 每日总结 (作息系统, 按天序号存储) ─────────────────

    def save_summary(self, content: str, day: Optional[int] = None) -> str:
        """保存某一天的总结.

        参数:
            content: 总结内容
            day:     第几天 (默认 1)

        返回:
            保存路径.
        """
        d = day or 1
        path = self._summary_path(d)
        self._write(path, content)
        logger.info("[%s] 第 %d 天总结已保存: %s", self.role_id, d, Path(path).name)
        return path

    def get_summary(self, day: Optional[int] = None) -> Optional[str]:
        """读取指定天的总结.

        参数:
            day: 第几天 (默认 1)

        返回:
            总结内容, 不存在返回 None.
        """
        d = day or 1
        return self._read(self._summary_path(d))

    def get_latest_summary(self, before_day: Optional[int] = None) -> Optional[str]:
        """读取最近一次总结 (用于下一天冷启动).

        参数:
            before_day: 截止天数 (只找严格早于该天的总结, 默认不限)

        返回:
            最近总结内容, 没有则返回 None.
        """
        candidates = sorted(self._list_md_files(), reverse=True)
        for p in candidates:
            # 文件名格式: _summary_day_<N>.md
            if not p.name.startswith("_summary_day_"):
                continue
            try:
                d = int(p.name[len("_summary_day_"):-len(".md")])
            except ValueError:
                continue
            if before_day is None or d < before_day:
                content = self._read(str(self._dir / p.name))
                if content is not None:
                    return content
        return None
