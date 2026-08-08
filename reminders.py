"""提醒的存储与到点检测——纯代码，不碰 LLM。

数据落在按到期日期分文件的目录里：<name>/2026-08-05.json 等，每个文件是当天到期的
提醒列表 [{id, text, due, created, fired, repeat?, repeat_until?, repeat_count?}]。
按日拆分是为了让 all()（LLM 对话用的"现有提醒"快照）只读今天及以后的文件——早已
fired 的历史提醒躺在过去日期的文件里，天然被排除，system prompt 不会随时间无限膨胀
（此前全塞一个文件，攒了两个月的每日课程提醒后把 llama-server 4096 上下文顶爆，报
HTTP 400）。pop_due 仍会扫全部文件，不受此限——停机错过的过期未响提醒照样能补上。
- due/created 都是本地朴素时间的 ISO 串（"2026-06-30T15:00:00"），用 datetime.fromisoformat 解析。
- fired 标记已播报，调度器据此不重复播。
- repeat 可选，循环提醒到点后不熄火而是把 due 推到下一次（见 _step），due 变了就可能
  挪去另一天的文件：
  {"every_days":N} / {"weekly":[0..6]}（0=周一）/ {"monthly":true} / {"yearly":true}。
  结束条件二选一：repeat_until="YYYY-MM-DD"（含当天）或 repeat_count=N（总触发次数）。
- LLM 对话产生的增删改查全走这里；调度线程只调 pop_due。

状态的唯一真相在这些 JSON 文件，不在聊天记录里——所以会话历史可随便丢。
"""
import calendar
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

_lock = threading.RLock()  # 调度线程 + 对话线程并发读写同一批文件，串行化

_REPEAT_KEYS = {"every_days", "weekly", "monthly", "yearly"}


def _validate_repeat(rep):
    if not isinstance(rep, dict) or len(set(rep) & _REPEAT_KEYS) != 1:
        raise ValueError(f"无效 repeat: {rep!r}")


def _add_months(dt, n):
    """加 n 个月，目标月没有该日就取当月最后一天（1/31 + 1月 → 2/28）。"""
    m = dt.month - 1 + n
    y, m = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def _step(dt, rep):
    """按 repeat 规则把时间往后挪一格，保留时刻。"""
    if "every_days" in rep:
        return dt + timedelta(days=rep["every_days"])
    if "weekly" in rep:
        days = sorted(rep["weekly"])
        for d in days:
            if d > dt.weekday():
                return dt + timedelta(days=d - dt.weekday())
        return dt + timedelta(days=7 - dt.weekday() + days[0])  # 跨到下周第一个
    if "monthly" in rep:
        return _add_months(dt, 1)
    if "yearly" in rep:
        return _add_months(dt, 12)
    raise ValueError(f"未知 repeat: {rep!r}")


def _next_due(cur, rep, now):
    """从 cur 按规则推进，直到晚于 now——错过多次只跳到下一个将来时刻，不补播。"""
    floor = max(cur, now)
    nxt = cur
    while nxt <= floor:
        nxt = _step(nxt, rep)
    return nxt


class Reminders:
    def __init__(self, path, log_path=None):
        # 目录名去掉 .json 后缀，如 "reminders.json" -> "reminders/"；每天一个文件
        base = Path(path)
        self.dir = base.with_suffix("")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._migrate_flat_file(base)
        # 变更流水：add/delete/update 各追加一行，喂主界面「历史记录」tab；None=不记（自检用）
        self.log_path = Path(log_path) if log_path else None

    def _migrate_flat_file(self, base):
        """旧版本是单个 reminders.json 平铺数组；发现了就一次性拆进按日文件，旧文件改名保留备份。"""
        if not base.exists() or not base.is_file():
            return
        try:
            items = json.loads(base.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for r in items:
            fp = self._file(r["due"][:10])
            bucket = self._load_file(fp)
            bucket.append(r)
            self._save_file(fp, bucket)
        base.rename(base.with_suffix(base.suffix + ".migrated.bak"))

    def _file(self, due_date):
        """due_date: 'YYYY-MM-DD' -> 该日的存储文件路径。"""
        return self.dir / f"{due_date}.json"

    @staticmethod
    def _load_file(fp):
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []  # ponytail: 单日文件损坏当空处理，下次写覆盖；丢一天的提醒好过崩溃

    @staticmethod
    def _save_file(fp, items):
        if items:
            fp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fp.exists():
            fp.unlink()  # 当天空了就删文件，目录不留空壳

    def _all_files(self):
        return sorted(self.dir.glob("*.json"))

    def _append_log(self, action, item):
        """把一次增删改追加进 reminders_log.jsonl（一行一条，坏了也不影响主流程）。"""
        if not self.log_path or not item:
            return
        try:
            rec = {"t": datetime.now().isoformat(timespec="seconds"), "action": action,
                   "id": item.get("id"), "text": item.get("text", ""), "due": item.get("due", "")}
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志失败不该拖垮提醒本身

    def all(self):
        """今天及以后到期的提醒（LLM 对话用的"现有提醒"快照）。过去日期的文件天然是已
        播报的历史，不读——停机错过、仍未播报的过期提醒对话看不到，但 pop_due 照样会补播。"""
        today = datetime.now().strftime("%Y-%m-%d")
        with _lock:
            items = []
            for fp in self._all_files():
                if fp.stem >= today:
                    items.extend(self._load_file(fp))
            return items

    def _find(self, rid):
        """跨全部文件（含过去日期）按 id 查找，返回 (文件路径, 该文件条目列表, 条目) 或 None。"""
        for fp in self._all_files():
            items = self._load_file(fp)
            for r in items:
                if r["id"] == rid:
                    return fp, items, r
        return None

    def add(self, text, due, repeat=None, repeat_until=None, repeat_count=None, phrases=None):
        """新增一条，返回分配的 id。due 为 ISO 串或 datetime；repeat 等可选见模块文档。
        phrases：到点循环催办用的 3 句语境化话术（可选），无则催办时套通用模板。"""
        if isinstance(due, datetime):
            due = due.isoformat(timespec="seconds")
        datetime.fromisoformat(due)  # 提前校验格式，坏值早暴露
        if repeat is not None:
            _validate_repeat(repeat)
        if repeat_until is not None:
            datetime.fromisoformat(repeat_until)
        with _lock:
            rid = max((r["id"] for fp in self._all_files() for r in self._load_file(fp)), default=0) + 1
            item = {
                "id": rid, "text": text, "due": due,
                "created": datetime.now().isoformat(timespec="seconds"), "fired": False,
            }
            for k, v in (("repeat", repeat), ("repeat_until", repeat_until),
                         ("repeat_count", repeat_count), ("phrases", phrases)):
                if v is not None:
                    item[k] = v  # 一次性提醒不留这些键，保持文件干净
            fp = self._file(due[:10])
            items = self._load_file(fp)
            items.append(item)
            self._save_file(fp, items)
            self._append_log("add", item)
            return rid

    def delete(self, rid):
        with _lock:
            found = self._find(rid)
            if not found:
                return False
            fp, items, gone = found
            self._save_file(fp, [r for r in items if r["id"] != rid])
            self._append_log("delete", gone)
            return True

    def update(self, rid, **fields):
        """改 text/due/repeat 等字段。改了 due 自动清 fired，让它能再次触发；due 变了会
        把这条挪去对应日期的文件。"""
        if isinstance(fields.get("due"), datetime):
            fields["due"] = fields["due"].isoformat(timespec="seconds")
        if "due" in fields:
            datetime.fromisoformat(fields["due"])
            fields.setdefault("fired", False)
        if fields.get("repeat") is not None:
            _validate_repeat(fields["repeat"])
        if fields.get("repeat_until") is not None:
            datetime.fromisoformat(fields["repeat_until"])
        with _lock:
            found = self._find(rid)
            if not found:
                return False
            fp, items, r = found
            r.update(fields)
            self._save_file(fp, [x for x in items if x["id"] != rid])
            self._move_if_needed(fp, r)
            self._append_log("update", r)
            return True

    def _move_if_needed(self, fp, r):
        """r 已从 fp 摘除并落盘；把它放回它 due 日期对应的文件（可能还是 fp，可能是别的）。"""
        nfp = self._file(r["due"][:10])
        items = self._load_file(nfp)
        items.append(r)
        self._save_file(nfp, items)

    def pop_due(self, now=None):
        """返回所有到点且未播报的提醒。一次性的标记 fired；循环的把 due 推到下一次（可能挪文件）。
        扫全部文件（含过去日期），停机错过的过期未播报提醒也能补上。"""
        now = now or datetime.now()
        with _lock:
            due = []
            for fp in self._all_files():
                items = self._load_file(fp)
                keep, moved = [], []
                changed = False
                for r in items:
                    if not r["fired"] and datetime.fromisoformat(r["due"]) <= now:
                        due.append(r)
                        changed = True
                        if r.get("repeat") and self._advance(r, now):
                            (keep if r["due"][:10] == fp.stem else moved).append(r)
                        else:
                            r["fired"] = True
                            keep.append(r)
                    else:
                        keep.append(r)
                if changed:
                    self._save_file(fp, keep)
                for r in moved:  # 循环提醒推到了别的日期，挪过去
                    nfp = self._file(r["due"][:10])
                    nitems = self._load_file(nfp)
                    nitems.append(r)
                    self._save_file(nfp, nitems)
            return due

    @staticmethod
    def _advance(r, now):
        """循环提醒就地把 due 推到下一次；已达结束条件返回 False（交给调用方熄火）。"""
        nxt = _next_due(datetime.fromisoformat(r["due"]), r["repeat"], now)
        until = r.get("repeat_until")
        if until and nxt.date() > datetime.fromisoformat(until).date():
            return False
        if "repeat_count" in r:
            r["repeat_count"] -= 1
            if r["repeat_count"] <= 0:
                return False
        r["due"] = nxt.isoformat(timespec="seconds")
        return True


def _selfcheck():
    import tempfile, os, shutil
    from datetime import timedelta
    p = Path(tempfile.gettempdir()) / "reminders_selftest.json"
    d = p.with_suffix("")
    bak = p.with_suffix(p.suffix + ".migrated.bak")
    if p.exists():
        os.remove(p)
    if bak.exists():
        os.remove(bak)
    if d.exists():
        shutil.rmtree(d)
    r = Reminders(p)
    assert r.all() == []
    past = datetime.now() - timedelta(minutes=1)
    future = datetime.now() + timedelta(hours=1)
    rid1 = r.add("过去的会议", past)
    rid2 = r.add("将来的会议", future)
    assert rid1 == 1 and rid2 == 2
    assert len(r.all()) == 2
    # 只有到点的被弹出，且只弹一次
    due = r.pop_due()
    assert [x["id"] for x in due] == [rid1], due
    assert r.pop_due() == [], "已播报的不该再弹"
    # 改时间会重置 fired，使其能再次触发
    r.update(rid2, due=datetime.now() - timedelta(seconds=1))
    assert [x["id"] for x in r.pop_due()] == [rid2]
    # 删除
    assert r.delete(rid1) is True
    assert r.delete(999) is False
    assert len(r.all()) == 1

    # —— 按日期分文件：today() 只看到今天及以后，过去日期不出现在 all() 里 ——
    old = r.add("老早以前的事", datetime(2020, 1, 1, 9, 0))
    assert old not in [x["id"] for x in r.all()], "过去日期的提醒不该出现在 all() 里"
    assert (r.dir / "2020-01-01.json").exists(), "但落盘文件应该按日期建了"
    r.delete(old)

    # —— 循环提醒 ——
    def get(rid):
        found = r._find(rid)
        assert found, f"{rid} 没找到"
        return found[2]

    base = datetime(2026, 6, 1, 9, 0)  # 2026-06-01 周一

    # 每 3 天：到点后不熄火，且 due 推到将来的下一格，并挪到对应日期的文件
    e = r.add("吃药", base, repeat={"every_days": 3})
    assert [x["id"] for x in r.pop_due(now=base + timedelta(days=1))] == [e]
    assert get(e)["fired"] is False
    assert datetime.fromisoformat(get(e)["due"]) == base + timedelta(days=3)
    assert (r.dir / "2026-06-04.json").exists(), "该挪到新日期的文件了"
    assert r.pop_due(now=base + timedelta(days=1)) == [], "已推到将来，不该再弹"

    # 停机错过多次：只弹一次，并跳到下一个将来时刻（不补播）
    m = r.add("浇花", base, repeat={"every_days": 3})
    popped = r.pop_due(now=base + timedelta(days=10))  # June11
    assert [x["id"] for x in popped].count(m) == 1, "错过多次只弹一次"
    assert datetime.fromisoformat(get(m)["due"]) == base + timedelta(days=12)  # June13>June11

    # 每周一：响完推到下周一，时刻不变
    wd = base.weekday()
    w = r.add("晨会", base, repeat={"weekly": [wd]})
    r.pop_due(now=base + timedelta(hours=1))
    nxt = datetime.fromisoformat(get(w)["due"])
    assert nxt == base + timedelta(days=7) and nxt.weekday() == wd

    # repeat_until：含当天，过后熄火
    u = r.add("打卡", base, repeat={"every_days": 1}, repeat_until="2026-06-02")
    r.pop_due(now=datetime(2026, 6, 1, 9, 30))
    assert get(u)["fired"] is False, "6/2 还要响一次"
    r.pop_due(now=datetime(2026, 6, 2, 9, 30))
    assert get(u)["fired"] is True, "6/2 是最后一次"
    assert r.pop_due(now=datetime(2026, 6, 3, 9, 30)) == []

    # repeat_count：恰好触发 N 次
    c = r.add("三连", base, repeat={"every_days": 1}, repeat_count=2)
    r.pop_due(now=datetime(2026, 6, 1, 9, 30))
    assert get(c)["repeat_count"] == 1 and get(c)["fired"] is False
    r.pop_due(now=datetime(2026, 6, 2, 9, 30))
    assert get(c)["fired"] is True, "第 2 次后停"

    # 每月同日 + 月末夹取：1/31 + 1月 → 2/28
    assert _add_months(datetime(2026, 1, 31, 8, 0), 1) == datetime(2026, 2, 28, 8, 0)

    # —— 旧版单文件迁移：写一个平铺数组，重新打开该目录应自动拆分且不丢数据 ——
    shutil.rmtree(d)
    future_due = (datetime.now() + timedelta(days=365)).isoformat(timespec="seconds")
    flat = [{"id": 1, "text": "旧提醒", "due": future_due,
             "created": "2026-06-01T00:00:00", "fired": False}]
    p.write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
    r2 = Reminders(p)
    assert not p.exists(), "旧文件应改名备份，不再以原名存在"
    assert (p.with_suffix(p.suffix + ".migrated.bak")).exists()
    assert len(r2.all()) == 1 and r2.all()[0]["text"] == "旧提醒"
    assert (d / f"{future_due[:10]}.json").exists(), "应按 due 日期拆到对应文件"

    shutil.rmtree(d)
    os.remove(p.with_suffix(p.suffix + ".migrated.bak"))
    print("reminders 自检通过。")


if __name__ == "__main__":
    _selfcheck()
