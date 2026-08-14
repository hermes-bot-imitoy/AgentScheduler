"""NoteStore 单元测试 (本地目录 + 假电脑, 不依赖 podman).

覆盖 (对应代码审查报告 Low-9 / High-3 / Medium-6 / Low-12):
  - _sanitize_title 清洗 shell 元字符 (High-3 回归: 单引号/反引号/$ 不再原样保留)
  - get_latest_summary 按天数值排序 (commit 0facd69 回归: day_10 > day_9)
  - _write 电脑写入失败必须显式抛错 (Medium-6 回归: 不再谎报已保存)
  - delete_note 真实删除文件 (Low-12 回归: 不再只是置空)

运行: cd 项目根 && .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.note_store import NoteStore


class SanitizeTitleTest(unittest.TestCase):
    """标题清洗: shell 元字符必须被替换 (High-3 注入面)."""

    def test_shell_metachars_stripped(self) -> None:
        dirty = "it's my note $(whoami) `id`"
        clean = NoteStore._sanitize_title(dirty)
        # 单引号/反引号/$/分号/& 全部被替换 — 命令替换无法再执行 (注入面关闭).
        # whoami 作为字面文件名保留无害: 危险的是 $() 执行语义, 不是文本本身.
        for ch in "'`$;&":
            self.assertNotIn(ch, clean)
        self.assertNotIn("$(", clean)
        self.assertEqual(clean, "it_s_my_note_(whoami)_id_")

    def test_illegal_filename_chars_stripped(self) -> None:
        self.assertEqual(NoteStore._sanitize_title('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(NoteStore._sanitize_title("   "), "untitled")

    def test_normal_title_kept(self) -> None:
        self.assertEqual(NoteStore._sanitize_title("周报-2026-08"), "周报-2026-08")


class LatestSummaryTest(unittest.TestCase):
    """总结按天数值排序: day_10 必须排在 day_9 前面 (字典序会排错)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = NoteStore(base_dir=self._tmp.name, role_id="CEO")

    def test_numeric_ordering(self) -> None:
        self.store.save_summary("第九天总结", day=9)
        self.store.save_summary("第十天总结", day=10)
        # 字典序 _summary_day_9.md > _summary_day_10.md; 数值序必须取 day 10
        self.assertEqual(self.store.get_latest_summary(before_day=11), "第十天总结")
        self.assertEqual(self.store.get_latest_summary(before_day=10), "第九天总结")
        self.assertIsNone(self.store.get_latest_summary(before_day=9))

    def test_no_summaries(self) -> None:
        self.assertIsNone(self.store.get_latest_summary())


class WriteFailureTest(unittest.TestCase):
    """电脑写入失败必须抛错 (Medium-6): 不抛则 LLM 会收到"已保存"假象."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_write_error_raises(self) -> None:
        comp = _FakeComputer(error="错误: 电脑未开机.")
        store = NoteStore(base_dir=self._tmp.name, role_id="CEO", computer=comp)
        with self.assertRaises(IOError):
            store.write_note("test", "内容")

    def test_write_exit_error_raises(self) -> None:
        comp = _FakeComputer(error="[exit 1] podman exec 失败")
        store = NoteStore(base_dir=self._tmp.name, role_id="CEO", computer=comp)
        with self.assertRaises(IOError):
            store.save_summary("总结", day=1)


class DeleteNoteTest(unittest.TestCase):
    """delete_note 真实删除文件 (Low-12): 文件必须消失, 再删返回 False."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_local_delete_removes_file(self) -> None:
        store = NoteStore(base_dir=self._tmp.name, role_id="CEO")
        store.write_note("备忘录", "内容")
        path = Path(self._tmp.name) / "CEO" / "备忘录.md"
        self.assertTrue(path.exists())
        self.assertTrue(store.delete_note("备忘录"))
        self.assertFalse(path.exists())
        self.assertFalse(store.delete_note("备忘录"))  # 已删 → False

    def test_computer_delete_uses_delete_file(self) -> None:
        comp = _FakeComputer()
        store = NoteStore(base_dir=self._tmp.name, role_id="CEO", computer=comp)
        store.write_note("备忘录", "内容")
        self.assertTrue(store.delete_note("备忘录"))
        self.assertIn("备忘录.md", comp.deleted[0])  # 走的是 delete_file 接口
        self.assertEqual(len(comp.deleted), 1)


class _FakeComputer:
    """假电脑: workdir + read/write/delete_file, 可按需返回错误."""

    def __init__(self, error: str = "") -> None:
        self._error = error
        self.deleted: list[str] = []

    @property
    def workdir(self) -> str:
        return "/fake/workdir"

    def read_file(self, path: str) -> str:
        if self._error:
            return self._error
        return "内容"

    def write_file(self, path: str, content: str) -> str:
        if self._error:
            return self._error
        return path  # 成功返回路径 (Podman/Local 兼容)

    def delete_file(self, path: str) -> str:
        self.deleted.append(path)
        if self._error:
            return self._error
        return f"已删除: {path}"


if __name__ == "__main__":
    unittest.main()
