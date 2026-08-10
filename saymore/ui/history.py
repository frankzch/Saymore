# -*- coding: utf-8 -*-
"""历史记录面板（数据）：由 main_window 的「历史记录」tab 渲染，本模块只提供数据，不开窗。

两类记录：
- 语音输入：recent_entries()，从按天分文件的 typed_history/*.jsonl 倒序取最近 N 条。
- 提醒变更：reminder_changes()，读 reminders_log.jsonl（Reminders 增删改时追加，见 reminders.py）。
"""
import json
from pathlib import Path

MAX_ENTRIES = 50  # 每类展示的最近条数上限


def recent_entries(history_dir, n=MAX_ENTRIES):
    """从最新的日期文件往回收集最近 n 条语音记录，返回 [(时间, 文本)]，最新在前。"""
    history_dir = Path(history_dir)
    out = []
    if not history_dir.exists():
        return out
    for f in sorted(history_dir.glob("*.jsonl"), reverse=True):
        for ln in reversed(f.read_text(encoding="utf-8").splitlines()):
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
                out.append((e.get("t", ""), e.get("final", "")))
            except Exception:
                continue
            if len(out) >= n:
                return out
    return out


def delete_entry(history_dir, t, text):
    """从 typed_history/*.jsonl 里删掉时间=t 且 final=text 的那一行，命中即止返回 True。"""
    history_dir = Path(history_dir)
    if not history_dir.exists():
        return False
    for f in sorted(history_dir.glob("*.jsonl"), reverse=True):
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("t", "") == t and e.get("final", "") == text:
                del lines[i]
                f.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                return True
    return False


def delete_change(log_path, t, text):
    """从 reminders_log.jsonl 里删掉时间=t 且 text=text 的那一行，命中即止返回 True。"""
    p = Path(log_path)
    if not p.exists():
        return False
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("t", "") == t and e.get("text", "") == text:
            del lines[i]
            p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            return True
    return False


_ACTION_CN = {"add": "新增", "delete": "删除", "update": "修改"}


def reminder_changes(log_path, n=MAX_ENTRIES):
    """读提醒变更流水，返回 [(时间, 动作中文, 文本)]，最新在前。log 不存在返回空。"""
    p = Path(log_path)
    if not p.exists():
        return []
    out = []
    for ln in reversed(p.read_text(encoding="utf-8").splitlines()):
        if not ln.strip():
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        out.append((e.get("t", ""), _ACTION_CN.get(e.get("action"), e.get("action", "")),
                    e.get("text", "")))
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    # 无参 = 自检：两类读取跨文件倒序取 n 条
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "2026-07-01.jsonl").write_text(
            '{"t": "2026-07-01 09:00:00", "final": "一"}\n'
            '{"t": "2026-07-01 10:00:00", "final": "二"}\n', encoding="utf-8")
        (td / "2026-07-02.jsonl").write_text(
            '{"t": "2026-07-02 09:00:00", "final": "三"}\n坏行\n', encoding="utf-8")
        got = recent_entries(td, 3)
        assert [x[1] for x in got] == ["三", "二", "一"], got
        assert recent_entries(td, 1) == [("2026-07-02 09:00:00", "三")]
        assert recent_entries(td / "不存在的目录", 3) == []

        assert delete_entry(td, "2026-07-01 10:00:00", "二") is True
        assert [x[1] for x in recent_entries(td, 9)] == ["三", "一"], recent_entries(td, 9)
        assert delete_entry(td, "2026-07-01 10:00:00", "二") is False  # 已删，再删不命中

        log = td / "reminders_log.jsonl"
        log.write_text(
            '{"t":"2026-07-01 09:00:00","action":"add","id":1,"text":"开会","due":"..."}\n'
            '坏行\n'
            '{"t":"2026-07-01 10:00:00","action":"delete","id":1,"text":"开会","due":"..."}\n',
            encoding="utf-8")
        chg = reminder_changes(log)
        assert chg[0] == ("2026-07-01 10:00:00", "删除", "开会"), chg
        assert chg[1][1] == "新增" and len(chg) == 2, chg
        assert reminder_changes(td / "没有.jsonl") == []
        assert delete_change(log, "2026-07-01 09:00:00", "开会") is True
        assert len(reminder_changes(log)) == 1
        assert delete_change(log, "2026-07-01 09:00:00", "开会") is False
    print("history_view 自检通过")
