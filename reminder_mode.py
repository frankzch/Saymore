"""提醒模式总控——把整个提醒功能从 voice_input 主流程隔离出来。

voice_input 只需：实例化 ReminderMode，在「命中第二唤醒词」「提醒模式下转写出一句」
「退出/休眠」几个钩子处调用对应方法。对话历史、存储、后台调度都收在这里。
"""
import threading
import time
from collections import deque
from pathlib import Path

import tts
import reminder_chat
from reminders import Reminders

# 催办默认值（config 没给时的兜底；正式默认在 voice_input.DEFAULT_CONFIG，方便用户改）
_DEFAULT_NAG_TEMPLATES = [
    "提醒，{text}",
    "该{text}了。",
    "别忘了，{text}。",
    "{text}，时间到了哦。",
    "还在催你，{text}，回我一声就停。",
]
_DEFAULT_ACK_WORDS = ["知道了", "听到了", "我知道", "收到", "好的", "好啦", "行了", "行行行", "好好好",
                      "嗯", "知道", "别响了", "别念了", "停一下", "停", "够了", "我来了", "我起了",
                      "起来了", "闭嘴", "别催了", "不用了"]

# 文件导入分块：4096 上下文 - system prompt(~1000 token 量级，随现有提醒数浮动) - 1024 输出，
# 留够余量。滑动窗口重叠切块，不依赖文档排版（不管按行/按段落/按表格单元格怎么摆日程）：
# 只要单条记录长度不超过 _IMPORT_OVERLAP_CHARS，就必然完整出现在至少一个窗口里，配合
# reminder_chat._execute 按 (text, due) 去重，重叠区被识别两次也不会建重复提醒。
_IMPORT_CHUNK_CHARS = 1400
_IMPORT_OVERLAP_CHARS = 350


def _chunk_overlap(text, size=_IMPORT_CHUNK_CHARS, overlap=_IMPORT_OVERLAP_CHARS):
    """滑动窗口切块，相邻块重叠 overlap 字符。"""
    if len(text) <= size:
        return [text]
    step = size - overlap
    chunks = []
    start = 0
    n = len(text)
    while True:
        end = min(start + size, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start += step
    return chunks


class ReminderMode:
    def __init__(self, cfg, resolve, asr):
        rp = resolve(cfg["reminders_file"])
        # 变更流水落在提醒文件同目录的 reminders_log.jsonl，供主界面「历史记录」tab 展示
        self.store = Reminders(rp, log_path=str(Path(rp).with_name("reminders_log.jsonl")))
        self.asr = asr  # LlamaASR：提醒解析走本地 llama-server 的 reminder 人格（完全切本地，不联网）
        self.to_dictation_words = set(cfg.get("reminder_to_dictation_words", []))
        # 对话历史；状态真相在 reminders.json，故只留最近几轮接指代，退出即清空
        self.history = deque(maxlen=8)
        self.on_busy = None  # 可选回调(bool)：解析调用开始/结束时通知（悬浮窗显示"处理中"用）
        # —— 催办（到点后循环播报直到用户应答）——
        self.nag_cfg = {
            "templates": cfg.get("reminder_nag_templates", _DEFAULT_NAG_TEMPLATES),
            "ack_words": cfg.get("reminder_ack_words", _DEFAULT_ACK_WORDS),
            "interval": cfg.get("reminder_nag_interval", 8),
            "max_repeats": cfg.get("reminder_nag_max_repeats", 20),
        }
        self._nag = None              # 当前催办会话 {"texts":[...], "stop":Event}；None=没在催
        self._nag_lock = threading.Lock()
        self.on_nag_start = None      # 回调()：催办开始，让主程序唤醒进监听态接住"知道了"
        self.on_nag_stop = None       # 回调()：催办结束，让主程序回休眠（仅当是它唤醒的）

    def greet(self):
        """进入提醒模式：清历史并打招呼。"""
        self.clear()
        tts.speak("提醒模式，请说。")

    def clear(self):
        """结束/重置一次对话：清空历史（状态真相在 reminders.json，历史只用来接指代）。"""
        self.history.clear()

    def is_to_dictation(self, cmd):
        return cmd in self.to_dictation_words

    def handle_turn(self, text):
        """提醒模式下处理一句话：交给本地 reminder 人格出 JSON、执行动作并 SAPI 播报回复。
        （"好的，收到。"已在 voice_input 确认整轮说完、即将调模型时(flush)播，这里不再重复。）"""
        say, _actions = self._run_turn(text)
        tts.speak(say)

    def _run_turn(self, text):
        """交给本地 reminder 人格解析一轮文本、执行动作，返回 (要朗读的话, 已执行 actions)，
        不朗读——由调用方决定要不要念（文件分块导入时只念最后的汇总，不必每块念一次）。"""
        if self.asr is None:
            return "提醒模式需要本地语音引擎，请切换到 qwen_gguf 引擎。", []
        if self.on_busy:
            self.on_busy(True)
        try:
            say, actions = reminder_chat.handle(self.store, self.history, text, None,
                                                 chat_fn=lambda messages, _cfg: self.asr.reminder_chat(messages))
        except Exception as e:
            print(f"[warn] 提醒对话出错: {e}")
            say, actions = "抱歉，处理出错了，请再说一遍。", []
        finally:
            if self.on_busy:
                self.on_busy(False)
        return say, actions

    def import_file(self, path):
        """从文件/图片抽出文字，滑动窗口重叠分块喂给 LLM 创建提醒（一次性塞全文会撑爆 4096
        上下文）。重叠切块不管文档怎么排版都安全，见 _chunk_overlap；重叠区可能被识别两次，
        靠 reminder_chat._execute 按 (text, due) 去重。每块都调一次模型，但只在处理完后念
        一次汇总，不然长文件导入会连续念好几句「好的」。"""
        import doc_import  # 延迟导入：office/OCR 库没装也不影响其他功能
        try:
            text = doc_import.extract(path)
        except Exception as e:
            print(f"[warn] 文件抽取失败: {e}")
            tts.speak("文件读取失败。")
            return
        if not text.strip():
            tts.speak("没从文件里读到内容。")
            return
        chunks = _chunk_overlap(text)
        print(f"[info] 已从 {path} 抽出 {len(text)} 字，分 {len(chunks)} 段交给提醒助手…")
        added = 0
        for i, chunk in enumerate(chunks, 1):
            prompt = (f"以下是导入的文件内容（第{i}/{len(chunks)}段，与相邻段有重叠，"
                      f"重复出现的内容别重复建），请据此帮我创建提醒（有多个时间点就建多条）：\n{chunk}")
            _say, actions = self._run_turn(prompt)
            added += sum(1 for a in actions if a.get("op") == "add")
        tts.speak(f"文件导入完成，创建了{added}条提醒。" if added else "文件读完了，但没找到需要提醒的内容。")

    # —— 催办：到点不是只播一遍，而是循环变着花样念，直到用户说"知道了" ——

    @property
    def nagging(self):
        return self._nag is not None

    def is_ack(self, text):
        """这句是不是"知道了/听到了"之类的应答词（用于停止催办）。"""
        t = (text or "").strip()
        return any(w in t for w in self.nag_cfg["ack_words"])

    def _phrases_of(self, r):
        """这条提醒到点要轮换念的话术：优先用 LLM 生成的语境化 phrases，没有就套通用模板。"""
        p = r.get("phrases")
        return list(p) if p else [t.format(text=r["text"]) for t in self.nag_cfg["templates"]]

    def _fire(self, due):
        """有提醒到点：开催。已在催办则把新条目并进去，共用同一个催办循环。"""
        items = [self._phrases_of(r) for r in due]
        with self._nag_lock:
            if self._nag is not None:
                self._nag["items"].extend(items)
                return
            session = {"items": items, "stop": threading.Event()}
            self._nag = session
        if self.on_nag_start:
            self.on_nag_start()
        threading.Thread(target=self._nag_loop, args=(session,), daemon=True).start()

    def _nag_loop(self, session):
        """循环播报：每隔 interval 秒念一轮，每条提醒按各自话术轮换，直到被停或达上限。"""
        stop = session["stop"]
        i = 0
        while not stop.is_set() and i < self.nag_cfg["max_repeats"]:
            lines = [p[i % len(p)] for p in session["items"]]  # 每条取它自己的第 i 句话术
            tts.speak("、".join(dict.fromkeys(lines)))  # 去重保序，多条一起念
            i += 1
            stop.wait(self.nag_cfg["interval"])
        self.stop_nag(silent=True)  # 达上限（用户多半不在）也要清理回休眠

    def stop_nag(self, silent=False):
        """停止催办：用户应答(silent=False，回一声确认) 或 达上限(silent=True)。幂等。"""
        with self._nag_lock:
            session = self._nag
            if session is None:
                return
            self._nag = None
        session["stop"].set()
        if not silent:
            threading.Thread(target=tts.speak, args=("好的，知道了。",), daemon=True).start()
        if self.on_nag_stop:
            self.on_nag_stop()

    def start_watcher(self, should_stop):
        """启动后台守护线程：每分钟检查到点提醒，有就开催办；休眠态也跑（纯 CPU）。
        should_stop() 返回真时退出。"""
        def loop():
            while not should_stop():
                try:
                    due = self.store.pop_due()
                    if due:
                        self._fire(due)
                except Exception as e:
                    print(f"[warn] 提醒检查出错: {e}")  # 别让单次异常杀死整个线程
                time.sleep(60)
            self.stop_nag(silent=True)
        threading.Thread(target=loop, daemon=True).start()


def _selfcheck():
    import tempfile, time as _t
    spoken = []
    tts.speak = lambda s: spoken.append(s)  # 拦截真实 SAPI 播报
    cfg = {"reminders_file": "x.json", "reminder_to_dictation_words": ["听写模式"],
           "reminder_nag_interval": 0.02, "reminder_nag_max_repeats": 3}
    rm = ReminderMode(cfg, lambda p: tempfile.gettempdir() + "/rm_self.json", asr=None)
    ev = []
    rm.on_nag_start = lambda: ev.append("start")
    rm.on_nag_stop = lambda: ev.append("stop")
    assert rm.is_ack("我知道了") and not rm.is_ack("吃药")
    # 开催 → 并入新条目 → 手动停
    rm._fire([{"text": "吃药"}])  # 无 phrases → 套通用模板
    assert rm.nagging and ev == ["start"]
    rm._fire([{"text": "喝水"}])
    assert len(rm._nag["items"]) == 2, "并入失败"
    rm.stop_nag()
    assert not rm.nagging and ev[-1] == "stop"
    _t.sleep(0.1)
    # 带语境话术：到点循环时按 phrases 轮换原样念；达上限自停（无应答场景）
    ev.clear(); spoken.clear()
    rm._fire([{"text": "睡觉", "phrases": ["该起床了", "起来啦", "醒醒"]}])
    _t.sleep(0.3)
    assert not rm.nagging and "stop" in ev, "达上限未自停"
    assert spoken and all(s in {"该起床了", "起来啦", "醒醒"} for s in spoken), f"未用 phrases: {spoken}"

    # —— 滑动窗口重叠切块：不依赖排版，只保证相邻块有重叠、拼起来能盖住全文 ——
    text = "".join(f"第{i}条日程内容" for i in range(200))  # 不带换行，模拟任意排版（不按行分）
    chunks = _chunk_overlap(text, size=100, overlap=30)
    assert len(chunks) > 1, "该切成多块"
    covered = chunks[0]
    for i in range(1, len(chunks)):
        overlap_region = covered[-min(30, len(chunks[i])):]
        assert chunks[i].startswith(overlap_region), "相邻块该重叠 overlap 字符"
        covered += chunks[i][30:]
    assert covered == text, "重叠区去掉后拼起来该等于原文，没丢内容"
    short = "太短不用切"
    assert _chunk_overlap(short, size=100, overlap=30) == [short], "不超预算就别切"

    # —— 文件导入：长文件分块喂模型，只在末尾念一次汇总，不逐块念；重叠区重复识别不重复建 ——
    import doc_import
    from datetime import datetime, timedelta
    calls = []

    def fake_reminder_chat(messages):
        i = len(calls)
        calls.append(messages[-1]["content"])
        due = (datetime.now() + timedelta(days=1, minutes=i)).isoformat(timespec="seconds")
        return f'{{"say":"已添加","actions":[{{"op":"add","text":"课{i}","due":"{due}"}}]}}'

    rm.asr = type("FakeASR", (), {"reminder_chat": staticmethod(fake_reminder_chat)})()
    doc_import.extract = lambda path: "".join(f"第{i}节课安排" for i in range(300))
    spoken.clear()
    rm.import_file("fake.txt")
    assert len(calls) > 1, "长文件该分成多块分别调模型"
    assert len(spoken) == 1, f"导入完成只该念一次汇总，不该逐块念：{spoken}"
    assert "创建了" in spoken[0] and str(len(calls)) in spoken[0]
    print("reminder_mode 自检通过。")


if __name__ == "__main__":
    _selfcheck()
