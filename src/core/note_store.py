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
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class NoteStore:
    """文件型笔记存储. 每个角色实例独立目录.

    参数:
        base_dir: 存储根目录 (默认 ./data/notes)
        role_id:  角色标识, 用于隔离目录 (可为空, 由 AgentRole 传入)
    """

    def __init__(self, base_dir: str = "./data/notes", role_id: str = ""):
        self._base = Path(base_dir)
        self.role_id = role_id
        self._dir = self._base / (role_id or "shared")
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── 路径工具 ──────────────────────────────────────────

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """清洗标题为合法文件名. 非法字符替换为下划线."""
        cleaned = re.sub(r'[\\/:*?"<>|#%\s]+', "_", title.strip())
        return cleaned or "untitled"

    def _note_path(self, title: str) -> Path:
        return self._dir / f"{self._sanitize_title(title)}.md"

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
        path.write_text(content, encoding="utf-8")
        logger.info("[%s] 笔记已写入: %s", self.role_id, path.name)
        return str(path)

    def edit_note(self, title: str, content: str) -> str:
        """编辑已有笔记 (覆盖内容). 不存在则创建.

        参数:
            title:   笔记标题
            content: 新内容

        返回:
            保存路径.
        """
        path = self._note_path(title)
        path.write_text(content, encoding="utf-8")
        logger.info("[%s] 笔记已编辑: %s", self.role_id, path.name)
        return str(path)

    def list_notes(self) -> list[str]:
        """列出所有笔记标题 (不含每日总结). 按文件名排序.

        返回:
            标题字符串列表.
        """
        titles = []
        for p in sorted(self._dir.glob("*.md")):
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
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def delete_note(self, title: str) -> bool:
        """删除笔记. 返回是否删除成功."""
        path = self._note_path(title)
        if path.exists():
            path.unlink()
            logger.info("[%s] 笔记已删除: %s", self.role_id, path.name)
            return True
        return False

    # ── 每日总结 (作息系统) ───────────────────────────────

    def save_summary(self, content: str, summary_date: Optional[str] = None) -> str:
        """保存某一天的总结.

        参数:
            content:      总结内容
            summary_date: 日期 (ISO 格式, 默认今天)

        返回:
            保存路径.
        """
        d = summary_date or date.today().isoformat()
        path = self._dir / f"_summary_{d}.md"
        path.write_text(content, encoding="utf-8")
        logger.info("[%s] 当日总结已保存: %s", self.role_id, path.name)
        return str(path)

    def get_summary(self, summary_date: Optional[str] = None) -> Optional[str]:
        """读取指定日期的总结.

        参数:
            summary_date: 日期 (ISO 格式, 默认今天)

        返回:
            总结内容, 不存在返回 None.
        """
        d = summary_date or date.today().isoformat()
        path = self._dir / f"_summary_{d}.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def get_latest_summary(self, before_date: Optional[str] = None) -> Optional[str]:
        """读取最近一次总结 (用于下一天冷启动).

        参数:
            before_date: 截止日期 (只找严格早于该日期的总结, 默认不限)

        返回:
            最近总结内容, 没有则返回 None.
        """
        candidates = sorted(self._dir.glob("_summary_*.md"), reverse=True)
        for p in candidates:
            # 文件名格式: _summary_YYYY-MM-DD.md
            d = p.name[len("_summary_"):-len(".md")]
            if before_date is None or d < before_date:
                return p.read_text(encoding="utf-8")
        return None
