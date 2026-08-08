# -*- coding: utf-8 -*-
"""当前窗口上下文热词：抓前台窗口的可访问文字(UIA)，交本地大模型挑出专业术语/生僻词，
缓存成一小撮词，供转写时拼进 ASR 的 context 偏置。只读窗口、后台跑、变化才提词，
绝不主动进识别链路(但注意 llama-server 单槽串行，见 voice_input 接线处说明)。
UIA 抓不到的窗口(拿到空文本)本轮就没有屏幕热词——留给以后 OCR 兜底。

设计取舍(ponytail)：
- 抓取走 UIA(前台窗口无障碍树)，纯读、毫秒级；不做 OCR。
- 提词交本地大模型判断(不用 jieba 那种死板分词)：让模型挑出术语/生僻词/英文技术词，
  忽略常用词和界面噪声。模型由 voice_input 用本地 llama-server 注入(extractor 回调)，
  本模块不绑定具体模型——换模型/换成训练好的提词 LoRA 都不动这里。
- diff：屏幕原文没变就直接返回缓存，绝大多数轮次省掉一次 LLM 调用。
"""
import difflib
import json
import os
import re
import threading
import time

try:
    import uiautomation as auto
except Exception:  # 没装 uiautomation 时整个功能静默关闭，不拖垮主程序
    auto = None

# 【临时·提词微调 M 线】攒训练数据：把每段(diff 后)真实屏幕文本落盘，供云端标 gold 术语。
# M 线收工即删这一坨(常量 + _capture + refresh 里的调用)。生产不依赖它。
# 默认关:M1 已上线,生产不该持续落盘屏幕内容(隐私/磁盘)。日后要为 v2 采真实数据再手动置 True。
CAPTURE_RAW = False
CAPTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "finetune", "screen_capture.jsonl")

TOP_N = 30            # 屏幕热词上限：要短，别拉长 context 害短命令
SCAN_INTERVAL = 10.0  # 后台扫描间隔秒；抓屏+diff 是 CPU 活，变化才调 GPU 提词
GRAB_NODE_CAP = 3000  # UIA 遍历节点上限，防超大窗口卡住
MAX_INPUT_CHARS = 500   # 喂给提词模型的屏幕文字上限：超长截断只取末尾(最新/离输入框最近)，省 prefill 时间

# 提词判据=“挑不常见的词”(ASR 会听错的:生僻/专名/中英混杂),不是按类别罗列——罕见度才是
# ASR 听错的根因。训推一致是铁律:训练侧(finetune/extract_gen.py)与此处**读同一个文件**
# finetune/extract_prompt.txt,单一真相、不手抄副本防漂。文件缺失才回退内置字面串。
# (提词 LoRA(M 线)训好挂 role="extract" 后,这里再改为读 adapter 快照,物理上杜绝错配。)
_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "finetune", "extract_prompt.txt")
try:
    EXTRACT_SYSTEM = open(_PROMPT_FILE, encoding="utf-8").read().strip()
except Exception:
    EXTRACT_SYSTEM = (
        "从下面屏幕文字里挑出不常见、日常口语里少用的词——ASR 容易听错的那些：生僻词、专业术语、"
        "冷门专有名词（人名/地名/产品/机构/品牌/型号/代号）、夹带的英文词与缩写等。"
        "常用词、常见人名地名、口水词、通用界面词一律不要。每行一个词，原样保留，不要解释。"
    )


_NUM_PREFIX = re.compile(r"^\s*\d+\s*[.)、]\s*")  # 行首编号 "1. " / "2) " / "3、"
_SPLIT = re.compile(r"[\n,，、]+")                 # 模型可能一行逗号分隔，也可能一行一词

# 变动比例低于此值：视为噪声(时间戳跳字之类的小波动)，不值得重新提词，不管具体变的是什么。
CHANGE_RATIO_THRESHOLD = 0.2


def parse_terms(out, topn=TOP_N):
    """把模型输出(逗号或换行分隔，可能带编号/符号)解析成词表：去噪、去重、限量。
    不按空格切——保留 'Bonsai 8B'、'cleanup LoRA' 这类多词术语。"""
    terms, seen = [], set()
    for piece in _SPLIT.split(out or ""):
        w = _NUM_PREFIX.sub("", piece)
        if "：" in w:            # 去掉 "专业术语：Qwen3-ASR" 这类标签前缀，取冒号后
            w = w.rsplit("：", 1)[1]
        if ":" in w:
            w = w.rsplit(":", 1)[1]
        w = w.strip().strip("-·•*、,.。：: 　").strip()
        if len(w) < 2 or w in seen:   # 单字/单字母噪声、重复
            continue
        seen.add(w)
        terms.append(w)
    return terms[:topn]


def make_llm_extractor(chat_fn, topn=TOP_N):
    """把"发一轮对话"的 chat_fn(system, user)->str 包成 extractor(text)->list[str]。
    chat_fn 由 voice_input 注入(本地 llama-server base 模型)。调用失败返回空、不炸主程序。"""
    def extract(text):
        text = (text or "").strip()
        if not text:
            return []
        if len(text) > MAX_INPUT_CHARS:
            text = text[-MAX_INPUT_CHARS:]  # 取末尾：离输入框最近/最新的内容，比开头更贴近当前语境
        print(f"[screen_ctx] 开始屏幕切词，输入 {len(text)} 字")
        t0 = time.time()
        try:
            out = chat_fn(EXTRACT_SYSTEM, text)
        except Exception as e:
            print(f"[screen_ctx] 提词 LLM 调用失败: {e}")
            return []
        terms = parse_terms(out, topn)
        print(f"[screen_ctx] 屏幕切词完成，提到 {len(terms)} 词，耗时 {time.time() - t0:.1f}s")
        return terms
    return extract


def assemble_context(groups, max_total):
    """按优先级把多来源词表拼成偏置 context(一行一词)，总量不超过 max_total。
    groups: [(cap, words), ...] 按优先级从高到低；cap 是本组词数上限(None 不限)。
    先来的组先占位，跨组去重，总数到 max_total 就停——高优先来源挤掉低优先的。"""
    out, seen = [], set()
    for cap, words in groups:
        taken = 0
        for w in words:
            if len(out) >= max_total or (cap is not None and taken >= cap):
                break
            w = w.strip()
            if not w or w in seen:
                continue
            seen.add(w)
            out.append(w)
            taken += 1
    return "\n".join(out)


def _diff_preview(old, new, maxlines=3):
    """诊断用：原文变了时，摘要新增/减少了哪些行，定位是什么内容在跳（不落盘，只打日志）。"""
    old_lines = set(old.splitlines()) if old else set()
    new_lines = set(new.splitlines()) if new else set()
    added = [l for l in (new.splitlines() if new else []) if l not in old_lines]
    removed = [l for l in (old.splitlines() if old else []) if l not in new_lines]
    fmt = lambda lines: "; ".join(l[:40] for l in lines[:maxlines]) if lines else "(无)"
    return f"+{len(added)}行(如: {fmt(added)}) -{len(removed)}行(如: {fmt(removed)})"


def _change_ratio(old, new):
    """两段文本的变动比例(0~1)：1 - 相似度。不识别变的是什么内容，纯粹按比例判断值不值得重新提词。"""
    if not old:
        return 1.0
    return 1.0 - difflib.SequenceMatcher(None, old, new).quick_ratio()


def _capture(raw, cur_terms):
    """【临时·M 线】追加一条 {ts, text, cur_terms} 到 screen_capture.jsonl；出错静默不炸主程序。"""
    try:
        os.makedirs(os.path.dirname(CAPTURE_PATH), exist_ok=True)
        with open(CAPTURE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "text": raw, "cur_terms": cur_terms},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


class ScreenContext:
    def __init__(self, extractor, topn=TOP_N, interval=SCAN_INTERVAL):
        # extractor(text)->list[str]；为 None 时整个功能停用(如非 qwen_gguf 引擎无本地 LLM)
        self.extractor = extractor
        self.topn = topn
        self.interval = interval
        self._terms = []
        self._last_raw = None
        self._anchor = None    # 当前认定的目标窗口；空闲时跟随前台窗口走，开口说话时锁定
        self._locked = False   # True=录音中，锚点冻结，忽略中途切屏
        self._cur_window_name = ""  # 【诊断】最近一次抓到的窗口标题
        self._lock = threading.Lock()

    @staticmethod
    def _window_id(win):
        return getattr(win, "NativeWindowHandle", 0) or getattr(win, "ProcessId", 0)

    def set_anchor(self):
        """开口说话(语音起点)时调用：把锚点锁定在当前窗口，之后中途 alt-tab 到别的窗口，
        后台扫描直接跳过——不然话说一半切一下屏就白打一炮提词。"""
        if auto is None:
            return
        try:
            win = auto.GetForegroundControl()
        except Exception:
            return
        if win is not None and getattr(win, "ProcessId", 0) != os.getpid():
            self._anchor = self._window_id(win)
        self._locked = True

    def release_anchor(self):
        """一句话说完(切句)时调用：解锁——空闲期锚点重新跟着前台窗口走，趁你还没开口先把新
        切到的窗口提前提好词，省得开口那一刻现抓现提。"""
        self._locked = False

    def _grab_foreground(self):
        """遍历前台窗口无障碍树，收集非空控件名当屏幕文字。只读，不点击。
        前台是本程序自己的窗口（编辑缓存面板/弹菜单抢了前台、悬浮猫），或录音中锁定期切到了
        别的窗口，都返回 None 表示"跳过本轮"——别把面板文字/切走后的别的窗口当屏幕上下文去白调
        一次提词 LLM，也别冲掉上一个真实窗口的缓存词（区别于 ""：""是真前台但没抓到文字）。
        预热只在完全没提过词(冷启动)时做：锚点跟着前台窗口走，趁你还没开口先把当前窗口提上。
        一旦提到过词(说明已经"绑定"了某个窗口)，锚点就不再跟随空闲切屏，只有你真开口说新一句
        (set_anchor)才会换——避免瞟一眼别的窗口(比如看一眼日志)就白打一炮提词。"""
        if auto is None:
            return ""
        try:
            win = auto.GetForegroundControl()
        except Exception:
            return ""
        if win is None:
            return ""
        if getattr(win, "ProcessId", 0) == os.getpid():
            return None
        try:  # 【诊断】记下窗口标题，配合日志能直接看出是哪个窗口触发/被跳过，不用猜内容片段
            self._cur_window_name = (win.Name or "")[:30]
        except Exception:
            self._cur_window_name = ""
        win_id = self._window_id(win)
        if self._locked or self._terms:
            # 录音中，或者已经提到过词(说明已经"绑定"了某个窗口)：锚点不再跟随，
            # 只有开口说新一句(set_anchor)才能换——切到别的窗口瞟一眼不触发。
            if self._anchor is not None and win_id != self._anchor:
                return None
        else:
            self._anchor = win_id  # 还没提过任何词(真正冷启动)：锚点跟前台窗口走，为第一次开口预热
        texts, seen = [], set()
        stack, n = [win], 0
        while stack and n < GRAB_NODE_CAP:
            node = stack.pop()
            n += 1
            try:
                nm = (node.Name or "").strip()
            except Exception:
                nm = ""
            if nm and len(nm) >= 2 and nm not in seen:
                seen.add(nm)
                texts.append(nm)
            try:
                stack.extend(node.GetChildren())
            except Exception:
                pass
        return "\n".join(texts)

    def refresh(self):
        """抓前台窗口 → 原文变了才调模型重新提词。返回是否更新。"""
        raw = self._grab_foreground()
        if raw is None:            # 前台是自家窗口（编辑面板等）→ 跳过，保留上个真实窗口的缓存
            return False
        if raw == self._last_raw:
            return False
        if self._last_raw is not None:
            ratio = _change_ratio(self._last_raw, raw)
            if ratio < CHANGE_RATIO_THRESHOLD:  # 变动比例太小(时间戳跳字之类)，不值得重新提词
                print(f"[screen_ctx] [{self._cur_window_name}] 原文微调({ratio:.1%})，忽略 "
                      f"{_diff_preview(self._last_raw, raw)}")
                self._last_raw = raw  # 仍更新基线，避免和这次的差异重复计入下次比较
                return False
            print(f"[screen_ctx] [{self._cur_window_name}] 原文变化({ratio:.1%})，重新提词 "
                  f"{_diff_preview(self._last_raw, raw)}")
        self._last_raw = raw
        terms = self.extractor(raw) if raw else []
        if CAPTURE_RAW and raw:   # 【临时·M 线】落盘真实屏幕文本 + 现役(待修)输出，供标注/前后对比
            _capture(raw, terms)
        with self._lock:
            self._terms = terms
        return True

    def terms_str(self):
        """给转写用的屏幕热词字符串(一行一词)；后台还没提到就是空串。"""
        with self._lock:
            return "\n".join(self._terms)

    def start(self, should_scan):
        """起后台线程：should_scan() 为真(唤醒态且未退出)时每 interval 秒扫一次。
        extractor 为 None 则不启动(功能停用)。转写只读 terms_str() 的缓存。"""
        if self.extractor is None:
            return
        def loop():
            try:
                import comtypes  # 新线程里 UIA 走 COM，先初始化本线程的 COM 单元
                comtypes.CoInitialize()
            except Exception:
                pass
            while True:
                try:
                    if should_scan():
                        self.refresh()
                except Exception as e:
                    print(f"[screen_ctx] 扫描出错: {e}")
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    # 自检：解析与合并逻辑(不连真模型，用假 chat_fn)
    fake_out = "专业术语：GGUF, LoRA\n1. llama.cpp\n- 量化，模型\nBonsai 8B\na\nGGUF\n"  # 标签前缀+逗号+换行+编号+符号+多词+单字+重复
    ex = make_llm_extractor(lambda system, text: fake_out, topn=30)
    terms = ex("随便一段含 GGUF LoRA 的屏幕文字")
    assert terms == ["GGUF", "LoRA", "llama.cpp", "量化", "模型", "Bonsai 8B"], terms  # 多词术语保留，单字a/重复去掉
    assert make_llm_extractor(lambda s, t: (_ for _ in ()).throw(RuntimeError("x")))("非空") == []  # 调用失败不炸
    assert ex("") == [] and ex("   ") == []
    print("提词解析 OK:", terms)

    # 优先级组装：命令词/静态全进 → 屏幕(有 cap，优先) → 历史填剩余；跨组去重、总量截断
    groups = [
        (None, ["发送", "撤销"]),          # 命令词
        (None, ["Qwen3-ASR", "GGUF"]),     # 静态术语
        (2, ["GGUF", "量化", "推理"]),     # 屏幕：cap=2，且 GGUF 已在静态里去重
        (None, ["语音识别", "热词", "偏置"]),  # 历史：填剩余
    ]
    asm = assemble_context(groups, max_total=7)
    # 命令2+静态2+屏幕(去重GGUF后取量化/推理=2)=6，历史只剩1格→语音识别
    assert asm.split("\n") == ["发送", "撤销", "Qwen3-ASR", "GGUF", "量化", "推理", "语音识别"], asm
    assert assemble_context([(None, ["a", "b", "c"])], 2) == "a\nb"  # 总量硬截断
    assert assemble_context([], 5) == ""
    print("优先级组装 OK:", asm.replace("\n", " "))

    # 实抓一次前台窗口(手动跑时看抓到多少字；提词需连真模型，这里只验证抓取)
    sc = ScreenContext(extractor=lambda t: parse_terms(""))  # 空提词器，只测抓取
    raw = sc._grab_foreground()
    print(f"前台窗口抓到 {len(raw)} 字，样本: {raw[:80]!r}")
    print("screen_context 自检通过")
