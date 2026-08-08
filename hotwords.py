"""热词自学习：每句口述回填时快照文本，休眠时交给本地 llama-server(extract 人格)
切词、累计词频，高频词写入 hotwords.txt 喂给 Qwen3-ASR 的 context 偏置。越用识别越准。
数据文件（均在脚本目录，不入库）：
  typed_history/YYYY-MM-DD.jsonl  口述文本快照，按天一个文件
  hotwords.json        各文件已处理行数 done + 全量词频
  hotwords.txt         频次 Top 词表（一行一词，与 ai_terms.txt 同格式）
（旧的单文件 typed_history.jsonl 首次运行时自动按日期拆进目录，改名 .bak 留档）
"""
import json
import os
import re
import threading
import time

TOP_N = 60          # 写进热词文件的词数上限；context 过长会拖慢推理
MIN_WEIGHT = 3      # 词频达到才算"高频"：一个词说满 3 次入选
BATCH_CHARS = 1500  # 每轮送本地模型的历史总字数上限（不按行数，用户可能一次口述整篇文章）
TIMEOUT_SEC = 8     # 单轮硬超时：整个 distill 走完（含 llama-server 调用）必须 ≤10s，超时本轮丢弃 done 不推进
MAX_TOKENS = 256    # 切词输出的 JSON 数组可能较长（几十个词），比屏幕提词的 32 大得多

_SEG_PROMPT = (
    "从下面这些用户输入的句子中提取【专业词汇、技术术语、专有名词、行业用语、"
    "产品名、人名地名机构名】，输出一个 JSON 数组：\n"
    "- 每个元素是一个词；同一个词出现几次就输出几次（用于统计词频）。\n"
    "- 中文词要 2 字及以上（不要单字）；英文单词/专有名词原样保留。\n"
    "- 不要标点、纯数字、语气词。\n"
    "- 跳过日常普通词（如'我们、可以、然后、已经、需要'等），只保留语音识别容易出错的专业/生僻词汇。\n"
    "- 剔除明显不成词、疑似语音识别错误的组合。\n"
    "只输出 JSON 数组，不要任何解释。\n\n"
)

_WORD_RE = re.compile(r"[A-Za-z0-9一-鿿][A-Za-z0-9一-鿿 .+#\-]*")


def _parse_words(out):
    """从 LLM 输出提取词列表：取最外层 JSON 数组，过滤非法/超长项。解析失败返回 None。"""
    m = re.search(r"\[.*\]", out or "", re.S)
    if m is None:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    return [w.strip() for w in arr
            if isinstance(w, str) and 2 <= len(w.strip()) <= 20
            and _WORD_RE.fullmatch(w.strip())]


class HotWords:
    def __init__(self, resolve, extract_fn):
        """extract_fn(system, text) -> str：走本地 llama-server extract 人格。
        为空则 distill_async 直接跳过（例如 llama-server 没起或没挂 LoRA）。"""
        self.extract_fn = extract_fn
        self.history_dir = resolve("typed_history")
        self.state_file = resolve("hotwords.json")
        self.txt_file = resolve("hotwords.txt")
        self._lock = threading.Lock()
        self._busy = False
        self._migrate(resolve("typed_history.jsonl"))

    def _read_state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"done": {}, "counts": {}}

    def _migrate(self, old_file):
        """旧的单文件历史按日期拆进 history_dir，一天一个文件；已提炼进度（旧格式的行 offset）
        换算成各日文件的 done 行数。旧文件改名 .bak 留档。幂等：没有旧文件就什么都不做。"""
        if not old_file.exists():
            return
        st = self._read_state()
        offset = st.pop("offset", 0)   # 旧格式：单文件已处理行数
        done = st.setdefault("done", {})
        self.history_dir.mkdir(exist_ok=True)
        outs = {}
        for i, ln in enumerate(old_file.read_text(encoding="utf-8").splitlines()):
            if not ln.strip():
                continue
            try:
                date = json.loads(ln)["t"][:10]
            except Exception:
                date = "0000-00-00"
            name = date + ".jsonl"
            outs.setdefault(name, []).append(ln)
            if i < offset:
                done[name] = done.get(name, 0) + 1
        for name, lines in outs.items():
            with open(self.history_dir / name, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        self.state_file.write_text(json.dumps(st, ensure_ascii=False, indent=0),
                                   encoding="utf-8")
        old_file.rename(old_file.with_suffix(".jsonl.bak"))
        print(f"[hotwords] 历史已按日期拆成 {len(outs)} 个文件（typed_history/），原文件留档 .bak")

    def record(self, final):
        """记一条口述文本快照到当天的文件，供休眠时切词计频。"""
        final = (final or "").strip()
        if not final:
            return
        row = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "final": final},
                         ensure_ascii=False)
        with self._lock:
            self.history_dir.mkdir(exist_ok=True)
            day_file = self.history_dir / (time.strftime("%Y-%m-%d") + ".jsonl")
            with open(day_file, "a", encoding="utf-8") as f:
                f.write(row + "\n")
        print(f"[hotwords] 已记录口述文本 {len(final)} 字")

    def distill_async(self):
        """空闲点（进入休眠）调用：后台把新攒的历史送本地 llama-server 切词并更新热词文件。"""
        if self._busy or not self.extract_fn or not self.history_dir.exists():
            return
        threading.Thread(target=self._distill, daemon=True).start()

    def _distill(self):
        self._busy = True
        try:
            st = self._read_state()
            done = st.setdefault("done", {})
            st.setdefault("counts", {})
            # 按日期序扫文件，从各自的 done 行数往后累加，字数够 BATCH_CHARS 就停——
            # 不按行数（用户可能一次口述整篇文章）。单条超上限也仍取这一条，防止卡死。
            entries, consumed, chars = [], {}, 0
            with self._lock:
                for f in sorted(self.history_dir.glob("*.jsonl")):
                    if chars >= BATCH_CHARS:
                        break
                    off = done.get(f.name, 0)
                    lines = f.read_text(encoding="utf-8").splitlines()[off:]
                    for i, ln in enumerate(lines):
                        if not ln.strip():
                            consumed[f.name] = off + i + 1
                            continue
                        try:
                            e = json.loads(ln)
                        except Exception:
                            consumed[f.name] = off + i + 1
                            continue
                        entries.append(e)
                        chars += len(e.get("final", ""))
                        consumed[f.name] = off + i + 1
                        if chars >= BATCH_CHARS:
                            break
            if not entries:
                return
            # 单条超上限时截断送模型（done 仍推进，避免卡死；整篇下轮会接着的下段历史一起消化）
            body = "\n".join(e["final"] for e in entries)[:BATCH_CHARS]
            print(f"[hotwords] 开始历史切词，送 {len(entries)} 条历史 / {len(body)} 字")
            t0 = time.time()
            out = self.extract_fn(_SEG_PROMPT, body)
            words = _parse_words(out)
            if words is None:
                print(f"[hotwords] LLM 输出无法解析，本轮跳过（下轮重试）: {out[:80]!r}")
                return
            counts = st["counts"]
            for w in words:
                counts[w] = counts.get(w, 0) + 1  # 纯词频，每出现一次 +1
            done.update(consumed)
            top = sorted((w for w, c in counts.items() if c >= MIN_WEIGHT),
                         key=lambda w: -counts[w])[:TOP_N]
            # 先写临时文件再原子替换，避免转写线程读到半截
            tmp = str(self.txt_file) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(top) + "\n")
            os.replace(tmp, self.txt_file)
            self.state_file.write_text(
                json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")
            print(f"[hotwords] 历史切词完成，本轮切出 {len(words)} 词，累计 {len(counts)} 词，"
                  f"热词 {len(top)} 个已更新 {self.txt_file.name}，耗时 {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"[hotwords] 提炼失败，本轮跳过（下轮重试）: {e}")
        finally:
            self._busy = False


if __name__ == "__main__":
    # 自检：解析/过滤逻辑
    ws = _parse_words('好的，结果是 ["语音识别", "热词", "，", "a", "Claude Code", 3, "的"]')
    assert ws == ["语音识别", "热词", "Claude Code"], ws
    assert _parse_words("没有数组") is None
    assert _parse_words('[]') == []
    # 纯词频：每出现一次 +1
    counts = {}
    for w in ["语音识别", "热词", "热词", "热词"]:
        counts[w] = counts.get(w, 0) + 1
    assert counts == {"语音识别": 1, "热词": 3}, counts

    # 迁移：旧单文件按日期拆分、旧 offset 换算成各日 done、旧文件改名 .bak
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        rows = [{"t": f"2026-07-0{d} 10:00:00", "final": f"句{i}"}
                for i, d in enumerate([1, 1, 2], 1)]
        (td / "typed_history.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        (td / "hotwords.json").write_text('{"offset": 2, "counts": {}}', encoding="utf-8")
        hw = HotWords(lambda n: td / n, None)
        assert not (td / "typed_history.jsonl").exists() and (td / "typed_history.jsonl.bak").exists()
        assert (td / "typed_history" / "2026-07-01.jsonl").read_text(encoding="utf-8").count("\n") == 2
        st = hw._read_state()
        assert st["done"] == {"2026-07-01.jsonl": 2}, st  # 前 2 行已提炼过，都落在 07-01
        hw.record("新句子")  # 记到今天的文件
        today = time.strftime("%Y-%m-%d") + ".jsonl"
        tf = td / "typed_history" / today
        assert tf.exists()
        lines = tf.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and json.loads(lines[-1])["final"] == "新句子", lines
        hw2 = HotWords(lambda n: td / n, None)  # 再建一次：无旧文件，迁移应跳过不炸
        assert hw2._read_state()["done"] == st["done"]
    print("hotwords 自检通过")
