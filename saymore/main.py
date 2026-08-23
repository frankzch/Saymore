"""Saymore 主程序 —— 常驻后台，说出唤醒词后边说边转写并输入到当前焦点窗口，静默自动休眠。

引擎: Qwen3-ASR (本地, llama.cpp/Vulkan, 见 saymore/asr/llamacpp.py)
输出: 转写文本经剪贴板 + Ctrl+V 粘贴到光标处（对中文/Unicode 最可靠）
平台: Windows

入口: python -m saymore.main（一般由 start.ps1 拉起）
"""

# 开机自启诊断:在任何可能失败的 import(numpy/keyboard/pywebview 等)之前落一行
# 到 logs/boot.log,只用 stdlib。目的是分清三档故障:
#   ① 文件里根本没新增行 → Windows 没执行 Run 命令,或 python 解释器都没起来。
#   ② 只有 "loaded" 没有 "main() entered" → 后续 import 阶段崩了。
#   ③ "main() entered" 有,后续 voice_input-YYYY-MM-DD.log 里也有 "启动"
#       → 走到了正常流程,故障在业务代码里。
def _boot_marker(stage: str) -> None:
    import os as _os, sys as _sys, time as _t
    try:
        if getattr(_sys, "frozen", False):
            root = _os.path.dirname(_sys.executable)
        else:
            root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        d = _os.path.join(root, "logs")
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, "boot.log"), "a", encoding="utf-8") as f:
            ts = _t.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {stage} frozen={getattr(_sys,'frozen',False)} "
                    f"exe={_sys.executable} argv={_sys.argv}\n")
    except Exception:
        pass


_boot_marker("main.py module loaded")

import json
import os
import queue
import random
import re
import signal
import sys
import time
import threading
from pathlib import Path

import numpy as np
import keyboard

try:
    import opencc
except ImportError:
    opencc = None

import saymore.tts as tts
import saymore.ui.panel as panel
import saymore.ui.style as ui_style
from saymore.config import (DEFAULT_CONFIG, SENTENCE_END, JA_KO_RE,
                             load_config)
from saymore.paths import CONFIG_PATH, _resolve
from saymore import runtime_check
from saymore.hotwords.learn import HotWords
from saymore.hotwords.screen import ScreenContext, assemble_context, make_llm_extractor
from saymore.reminder.mode import ReminderMode
from saymore.win.focus import output_text, focus_window, click_permission_button
from saymore.ui.overlay import run_overlay
from saymore.audio.capture import Recorder, build_vad, build_keyword_spotter


# 提醒模式下每收一句的简短人声应答（轮换，听着自然），替代听写模式的猫叫
_REMINDER_CUES = ["嗯", "好的", "嗯哼", "好"]


def _close_main_window():
    """重启前把主窗口子进程一并关掉——它是 subprocess.Popen 出来的独立进程，
    不会跟着本进程退。找 "Saymore" 顶层窗口 → 定位 PID → TerminateProcess。
    找不到就静默跳过（本来就没开）。仅 Windows。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        hwnd = u.FindWindowW(None, "Saymore")
        if not hwnd:
            return
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return
        PROCESS_TERMINATE = 0x0001
        h = k.OpenProcess(PROCESS_TERMINATE, False, pid.value)
        if h:
            k.TerminateProcess(h, 0)
            k.CloseHandle(h)
            print("[info] 已关闭主窗口子进程（重启前）")
    except Exception as e:  # noqa: BLE001 兜底：关不掉不该拖累重启
        print(f"[warn] 关闭主窗口失败：{e}")

# 首次唤醒的应答（轮换，别每次都一样）
_WAKE_CUES = ["我来了", "我在"]
# 听写模式下识别到命令词（发送/输入/回退等）的简短应答（轮换）
_CMD_ACK_CUES = ["好的", "收到"]
# 每识别出一句普通听写句的即时应答（随机选一个，短，别等整理好——整理太慢）
_HEAR_CUES = ["嗯", "好"]

# 缓存窗口文本成功发送后，连续空闲 10 秒才在后台切词并加入历史热词；期间重新开口会重置计时。
# 避开连续听写，同时让刚说过的术语在本次唤醒会话里也能帮助后续内容。
_HOTWORD_DISTILL_IDLE_SECONDS = 10.0


_BACKEND_LOCK = None  # 单例互斥量句柄,进程生命周期内必须挂着不释放


def main():
    _boot_marker("main() entered")
    # pythonw.exe（后台无控制台）下 stdout/stderr 为 None，任何 print 都会崩溃；
    # 重定向到按日切分的日志：logs/voice_input-YYYY-MM-DD.log，每行自动贴 HH:MM:SS.mmm
    # ——现有 print() 免改就带时间戳，便于事后定位哪一步慢。
    if sys.stdout is None or sys.stderr is None:
        from saymore.log_setup import install
        install(CONFIG_PATH.parent / "logs")
        print(f"===== 启动 =====")

    # 单实例守卫:两个后端同时跑会抢麦/托盘/config,乱套。命名互斥量按安装路径区分——
    # 开发版 (D:\Antigravity\...) 和打包版 (D:\Program Files (x86)\Saymore\) 允许并存,
    # 但同一路径下再启一个就直接退。
    from saymore.win.single_instance import acquire
    import hashlib
    global _BACKEND_LOCK
    # 不能用内置 hash():PYTHONHASHSEED 随机,两个进程会得出不同值 → 拿不到同一个锁。
    tag = hashlib.md5(str(CONFIG_PATH.parent).encode("utf-8")).hexdigest()[:12]
    _BACKEND_LOCK = acquire(f"Saymore.Backend.{tag}")
    if _BACKEND_LOCK is None:
        print(f"[info] 已有一个 Saymore 后端在跑({CONFIG_PATH.parent}),本进程退出")
        sys.exit(0)

    if os.name == "nt":  # 必须在本进程建任何窗口之前调用，否则托盘 toast 通知显示来源会是"Python"
        import saymore.ui.tray as tray
        tray.register_app_identity(CONFIG_PATH.parent / "saymore.ico")

    ready_marker = CONFIG_PATH.parent / ".ready"  # 存在=后台已在监听；悬浮窗/主界面靠它判断是否还在初始化
    ready_marker.unlink(missing_ok=True)

    # 头两次启动自动弹主窗:第 1 次(装完)引导用户;第 2 次是"下载完模型后自我重启"
    # 那一趟(见后面 restart_trigger),第 1 次的窗口会随着下载完自动关掉,得再弹一次
    # 让用户知道已就绪。之后就不再自动弹,由用户主动从托盘/圆环唤起。
    launch_marker = CONFIG_PATH.parent / ".launch_count"
    try:
        _launch_n = int(launch_marker.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        _launch_n = 0
    first_run = _launch_n < 2
    try:
        launch_marker.write_text(str(_launch_n + 1), encoding="utf-8")
    except OSError as e:
        print(f"[warn] 写 .launch_count 失败(不影响运行): {e}")
    cfg = load_config()

    # 运行环境检测：任一必需组件缺失（模型未下载/安装包被破坏）就不启动语音后端，
    # 只拉起悬浮窗+主界面引导用户去「运行环境」tab 补齐；补齐后重启即可（见后面 restart_trigger）。
    runtime = runtime_check.check(cfg)
    if not runtime["ready"]:
        print(f"[warn] 运行环境未就绪，缺失: "
              f"{[m['label'] for m in runtime['missing']]}")

    # 尽早建 state 并拉起悬浮圆环（独立线程）：不等模型/引擎/KWS 都初始化完才有画面——用户这时
    # 说唤醒词还没反应，圆环上会显示"正在初始化"，别让人以为程序卡死了。悬浮窗只靠 state 字典
    # 跟后面的初始化/识别逻辑通信，见 overlay.py。
    state = {"mode": "sleep", "status": "idle", "quit": False,
             "last_activity": 0.0, "sleep_since": time.time(),
             "sleep_after": cfg["sleep_after_seconds"],
             "nod_until": 0.0,  # 执行命令后让猫点头到此时刻
             "tok_in": 0, "tok_out": 0, "tok_time": 0.0,  # 最近一轮提醒 token 用量+时刻，悬浮窗飘字 10s 后淡出
             "levels": [],  # 最近若干帧麦克风音量(RMS)，供说话检测（猫左右张望）
             "backend_ready": False,  # 语音后台是否已开始监听（见下方 recorder.start()）；悬浮窗据此显示"正在初始化"
             "runtime": runtime,  # 运行环境自检结果：{ready, missing, present}；未就绪时 overlay/panel 显示红色警告并锁输入
             "history_file": _resolve("typed_history"),  # 主界面「历史记录·语音输入」（按天分文件的目录）
             "reminders_log": str(Path(_resolve(cfg["reminders_file"])).with_name("reminders_log.jsonl")),
             "import_trigger": str(CONFIG_PATH.parent / ".import_trigger"),  # GUI 导入把选中路径写这、由本进程接住
             "restart_trigger": str(CONFIG_PATH.parent / ".restart_trigger"),  # 设置窗口要求重启生效，写这个文件触发
             "overlay_pos": cfg.get("overlay_pos"),  # 小圆窗上次拖动位置；overlay 据此摆放并在拖动后写回
             "glass_cfg": {  # 玻璃面板外观参数传给 run_overlay（overlay 只依赖 state，不碰 cfg）
                 "tint": cfg.get("panel_tint", ui_style.PANEL_TINT),
                 "text_rgb": cfg.get("panel_text_rgb", ui_style.PANEL_TEXT_RGB),
                 "raw_text_rgb": cfg.get("panel_raw_text_rgb", ui_style.PANEL_RAW_TEXT_RGB),
                 "hint_text_rgb": cfg.get("panel_hint_text_rgb", ui_style.PANEL_HINT_TEXT_RGB),
                 "low_conf_rgb": cfg.get("panel_low_conf_rgb", ui_style.PANEL_LOW_CONF_RGB),
                 "width": panel.PANEL_W,
                 "font_size": cfg.get("panel_font_px", 13),
                 "max_h": cfg.get("panel_max_h", 480),
             }}
    # SIGINT(Ctrl+C) 只能在主线程注册；悬浮窗现在跑在独立线程里，不能像以前那样在 run_overlay
    # 内部注册（那会抛 ValueError: signal only works in main thread，且线程崩了没人看得见——
    # pythonw 无控制台，整个悬浮窗/托盘就悄无声息地没起来）。
    signal.signal(signal.SIGINT, lambda *_: state.__setitem__("quit", True))
    overlay_thread = None
    if cfg.get("overlay", True):
        keyboard.add_hotkey("ctrl+shift+q", lambda: state.__setitem__("quit", True))
        overlay_thread = threading.Thread(target=run_overlay, args=(state,), daemon=True)
        overlay_thread.start()

    if first_run:  # 装完第一次打开：自动拉起主界面，别让用户对着空桌面找不到入口
        print("[info] 首次启动，自动打开主界面。")
        import saymore.ui.main_window as main_window
        # 首启若运行环境未就绪，直接落到运行环境 tab；否则默认设置 tab
        _tab = "runtime" if not runtime["ready"] else "settings"
        main_window.show(CONFIG_PATH, state["history_file"], state["reminders_log"],
                         state["import_trigger"], state["restart_trigger"], tab=_tab)

    # 运行环境未就绪：静默后台下载 + KWS 允许唤醒（唤醒后面板显示下载进度让用户知情），
    # 但不起 llama-server / worker —— 没模型就没转写。用户说唤醒词后 15s 自动回休眠让圆环
    # 消失（下载中反正没啥可做）。下载全成功即写 restart_trigger 自我重启换新进程干净起后端。
    if not runtime["ready"]:
        from saymore import downloader
        network_missing = [m for m in runtime["missing"] if m.get("network")]
        # 内置项缺失（安装包被破坏）—— 无法自愈，拉主界面让用户重装
        if any(not m.get("network") for m in runtime["missing"]) or not network_missing:
            print(f"[warn] 有安装包内置组件缺失，需重装：{[m['label'] for m in runtime['missing'] if not m.get('network')]}")
            if not first_run:
                import saymore.ui.main_window as main_window
                main_window.show(CONFIG_PATH, state["history_file"], state["reminders_log"],
                                 state["import_trigger"], state["restart_trigger"], tab="runtime")
        # 逐项启后台下载（downloader.start 各自开线程，非阻塞）
        for item in network_missing:
            urls = item.get("urls") or []
            if not urls:
                print(f"[warn] {item['label']} 无下载源，跳过（请填 runtime_check.py 的 _HF_REPO/_MS_REPO）")
                continue
            downloader.start(item["key"], urls, Path(item["path"]))
            print(f"[info] 已启动后台下载：{item['label']}")

        # ── KWS + 麦克风：允许唤醒，唤醒后仅显示下载进度，不转写 ───────────────
        wake_words_dl = [w for w in cfg.get("wake_words", []) if w.strip()]
        spotter_dl = build_keyword_spotter(cfg, wake_words_dl) if wake_words_dl else None
        kws_stream_dl = spotter_dl.create_stream() if spotter_dl is not None else None
        kws_queue_dl = queue.Queue() if kws_stream_dl is not None else None

        def on_block_dl(block):
            """PortAudio 回调，只在待唤醒态入 KWS 队列；已唤醒态什么都不做（也不识别）。"""
            lv = state["levels"]
            lv.append(float(np.sqrt(np.mean(block ** 2))))
            if len(lv) > 40:
                del lv[:-40]
            if kws_queue_dl is not None and state["mode"] == "sleep":
                kws_queue_dl.put(block)

        def kws_worker_dl():
            while not state["quit"]:
                try:
                    block = kws_queue_dl.get(timeout=0.5)
                except queue.Empty:
                    continue
                if state["mode"] != "sleep":
                    continue
                kws_stream_dl.accept_waveform(cfg["sample_rate"], block)
                while spotter_dl.is_ready(kws_stream_dl):
                    spotter_dl.decode_stream(kws_stream_dl)
                hit = spotter_dl.get_result(kws_stream_dl)
                if hit:
                    spotter_dl.reset_stream(kws_stream_dl)
                    state["mode"] = "awake"
                    state["status"] = "awake"
                    state["last_activity"] = time.time()
                    print("✓ 唤醒（下载中，仅显示进度，不转写）")

        def _dl_idle_watcher():
            """下载中的休眠倒计时：距上次活动超过 sleep_after_seconds 就回休眠，
            与就绪态一致（默认 300s，走 cfg["sleep_after_seconds"]）。"""
            limit = cfg.get("sleep_after_seconds", 300)
            while not state["quit"]:
                time.sleep(1)
                if state["mode"] != "awake":
                    continue
                if time.time() - state.get("last_activity", 0) > limit:
                    state["mode"] = "sleep"
                    state["status"] = "idle"
        threading.Thread(target=_dl_idle_watcher, daemon=True).start()

        recorder_dl = None
        if kws_queue_dl is not None:
            recorder_dl = Recorder(
                cfg["sample_rate"],
                on_segment=lambda seg: None,   # 下载中不收集语音段（不识别）
                silence_rms=cfg["silence_rms"],
                silence_seconds=cfg["silence_seconds"],
                min_segment_seconds=cfg["min_segment_seconds"],
                max_segment_seconds=cfg.get("max_segment_seconds", 15.0),
                on_block=on_block_dl,
                vad=build_vad(cfg),
                device=cfg.get("input_device") or None,
            )
            threading.Thread(target=kws_worker_dl, daemon=True).start()
            recorder_dl.start()  # 进程退出走消息循环终止 → daemon 线程随退，无需手动 stop

        # ── 与主窗口子进程的文件 IPC ───────────────────────────
        # 主窗口是独立子进程，看不到本进程的 downloader._TASKS。用两个文件传状态：
        # 1) .download_status.json：本进程每 1s 覆写，主窗口 API 读取供进度条渲染
        # 2) .download_control.json：主窗口写触发（暂停/继续），本进程接住调 downloader
        status_file = CONFIG_PATH.parent / ".download_status.json"
        control_file = CONFIG_PATH.parent / ".download_control.json"
        keys_to_items = {m["key"]: m for m in network_missing}

        def _status_writer():
            while not state["quit"]:
                try:
                    payload = {"tasks": downloader.progress(), "ts": time.time()}
                    tmp = status_file.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(status_file)  # 原子替换，避免主窗口读到半截 JSON
                except Exception as e:  # noqa: BLE001 守护线程别被单次异常杀死
                    print(f"[warn] 写下载状态失败：{e}")
                time.sleep(1)
        threading.Thread(target=_status_writer, daemon=True).start()

        def _control_watcher():
            control_file.unlink(missing_ok=True)  # 清残留
            while not state["quit"]:
                try:
                    if control_file.exists():
                        data = json.loads(control_file.read_text(encoding="utf-8"))
                        control_file.unlink(missing_ok=True)
                        action = data.get("action")
                        key = data.get("key")
                        item = keys_to_items.get(key)
                        if action == "cancel":
                            downloader.cancel(key)
                            print(f"[info] 主窗口请求暂停：{key}")
                        elif action == "start" and item:
                            downloader.reset(key)  # 上次若 cancelled/error，清 STATE 再重启（.part 保留续传）
                            downloader.start(key, item.get("urls") or [], Path(item["path"]))
                            print(f"[info] 主窗口请求下载：{key}")
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] 下载控制处理出错：{e}")
                time.sleep(0.3)
        threading.Thread(target=_control_watcher, daemon=True).start()

        # 全部下载完成后：写 restart_trigger 让本进程退出+新进程接班（新进程走 ready 分支正常启）
        def _auto_restart_when_ready():
            keys = [m["key"] for m in network_missing]
            while not state["quit"]:
                time.sleep(2)
                progress = downloader.progress()
                # 有任一项没成功（还在跑/失败/取消）就继续等；用户可从主界面手动重试失败项
                if all(progress.get(k, {}).get("state") == "done" for k in keys):
                    print("[info] 所有模型下载完成，重启接班…")
                    Path(state["restart_trigger"]).write_text("1", encoding="utf-8")
                    return
        threading.Thread(target=_auto_restart_when_ready, daemon=True).start()

        # restart 触发监视：接住 _auto_restart_when_ready 或用户手动点"重启程序"的信号
        def _restart_watcher():
            trigger = Path(state["restart_trigger"])
            trigger.unlink(missing_ok=True)
            while not state["quit"]:
                try:
                    if trigger.exists():
                        trigger.unlink(missing_ok=True)
                        print("[info] 收到重启信号，拉起新进程接班…")
                        _close_main_window()  # 关掉可能还开着的主窗口子进程，避免遗留旧页面
                        from saymore.proc import spawn_backend
                        spawn_backend()
                        state["quit"] = True
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] 重启触发处理出错：{e}")
                time.sleep(0.5)
        threading.Thread(target=_restart_watcher, daemon=True).start()

        if overlay_thread is not None:
            overlay_thread.join()
        return

    # llama.cpp 后端：llama-server 常驻本地(Vulkan GPU)，HTTP 转写，见 asr_llamacpp.py
    from saymore.asr.llamacpp import LlamaASR
    # 整理 LoRA：合一 multi LoRA 挂给 llama-server——一个进程内 转写 + 四风格整理
    # （保守/深度/邮件/00后 同一 adapter，靠 system 切人格；finetune/ROUNDS.md multi 轮）。
    _loras = {}
    import saymore.polish.local as _lc  # find_gguf 只做 gguf 定位
    _want = []
    if cfg.get("local_polish"):
        if _lc.find_gguf("multi-lora.gguf"):
            _want.append(("multi", "multi-lora.gguf"))
        else:  # 回滚到老双人格（multi-lora.gguf 被移走时）
            _want += [("basic", "polish-lora.gguf"), ("deep", "deep-lora.gguf")]
    _want.append(("reminder", "reminder-lora.gguf"))  # 提醒人格：与整理开关无关，提醒走本地就得挂（ROUNDS.md K1）
    _want.append(("extract", "extract-lora.gguf"))  # 屏幕提词人格：独立 LoRA，挑"不常见词"（ROUNDS.md M1）；缺文件则 extract() 退回已挂人格
    for _role, _fn in _want:
        _g = _lc.find_gguf(_fn)
        if _g:
            _loras[_role] = _g
    _dev = cfg.get("device", "auto")
    if _dev == "auto":
        try:
            import ctypes
            ctypes.WinDLL("vulkan-1.dll")  # 有 GPU 驱动就有这个 DLL；<1ms 探测
            _dev_resolved = "cuda"
        except OSError:
            _dev_resolved = "cpu"
        print(f"[info] device=auto → 检测结果: {_dev_resolved}")
    else:
        _dev_resolved = _dev
    _ngl = 0 if _dev_resolved == "cpu" else 99  # cuda 用 GPU(-ngl 99)；cpu 强制纯 CPU(慢约 2 倍)
    llama_asr = LlamaASR(
        exe=_resolve(cfg["llama_server_exe"]),
        model=_resolve(cfg["gguf_model"]),
        mmproj=_resolve(cfg["gguf_mmproj"]),
        port=cfg.get("llama_port", 8901),
        ngl=_ngl,
        loras=_loras,
    )
    import atexit
    atexit.register(llama_asr.stop)  # 程序退出连带停 llama-server 释放显存
    def vram():
        return ""
    def load_model():
        if not llama_asr.alive():
            print(f"启动 llama-server ({Path(cfg['gguf_model']).name}, Vulkan) ...")
            _t = time.time()
            llama_asr.start()
            print(f"llama-server 就绪 ({time.time()-_t:.1f}s)。")
        return llama_asr
    def unload_model():
        if llama_asr.alive():  # 按"是否活着"判,复用来的 server(proc=None)休眠也要放显存
            llama_asr.stop()
            print("🗑 长时间休眠，已停 llama-server 释放显存。")

    warm_model = load_model

    converter = None
    if cfg["simplified"]:
        if opencc is not None:
            converter = opencc.OpenCC("t2s")
        else:
            print("[warn] 未安装 opencc，无法转简体（pip install opencc）")

    # 热词自学习：真正发送时记最终文本，连续空闲 10 秒后走本地 llama-server(extract 人格)；
    # 记录最近出现时间和累计次数，双榜名次融合写 hotwords.txt。单轮字数封顶 1500、硬超时 8s。
    hotwords = HotWords(_resolve,
        lambda system, text: llama_asr.extract(text, system, role="extract",
                                                timeout=8, max_tokens=256))
    # 屏幕上下文热词：后台抓前台窗口文字，交本地 llama-server 提术语，转写时拼进偏置。
    screen_extractor = make_llm_extractor(
        lambda system, text: llama_asr.extract(text, system, role="extract"),
        topn=cfg.get("screen_bias_max_terms", 10))
    screen_ctx = ScreenContext(screen_extractor)

    # 上下文偏置：命令词(send_words 等) + 屏幕热词 + 静态术语表(qwen_context_file)
    # + 自学习历史热词(hotwords.txt) 拼成 context 喂给 Qwen3-ASR。份额分配全部读配置：
    #   命令词：全进，不占份额（短命令刚需）
    #   屏幕：screen_bias_max_terms 硬封顶
    #   静态术语：static_bias_max_terms 硬封顶
    #   历史热词：无 cap 排最末——静态没填满、跨组去重省下的名额自动下放给历史，把总量吃满 bias_total
    # 总量封顶防稀释注意力+拖慢。屏幕热词实时变故每次现拼；两个文件按 mtime 缓存词表，改了自动重载。
    command_words = [
        w for k in ("send_words", "confirm_words", "undo_words",
                     "clear_words", "polish_words", "sleep_words", "quit_words",
                     "reminder_enter_words", "reminder_to_dictation_words")
        for w in cfg.get(k, [])]
    static_path = None
    cf = cfg.get("qwen_context_file", "")
    if cf:
        static_path = _resolve(cf)
    history_path = hotwords.txt_file
    file_cache = {"key": None, "static": [], "history": []}
    bias_total = cfg.get("bias_max_terms", 80)
    screen_cap = cfg.get("screen_bias_max_terms", 10)
    static_cap = cfg.get("static_bias_max_terms", 30)

    def _lines(p):
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def get_qwen_context():
        ps = [p for p in (static_path, history_path) if p and p.exists()]
        key = tuple((str(p), p.stat().st_mtime) for p in ps)
        if key != file_cache["key"]:
            file_cache["static"] = _lines(static_path) if static_path and static_path.exists() else []
            file_cache["history"] = _lines(history_path) if history_path and history_path.exists() else []
            file_cache["key"] = key
        groups = [
            (None, command_words),                                    # 命令词：优先全进，不占份额
            (screen_cap, screen_ctx.terms_str().split("\n")),         # 屏幕：硬封顶 screen_cap
            (static_cap, file_cache["static"]),                       # 静态术语：剩余的一半
            (None, file_cache["history"]),                            # 历史：无 cap，吃剩余到 bias_total
        ]
        return assemble_context(groups, bias_total)

    language = cfg.get("language") or None
    if isinstance(language, str) and language.lower() in ("zh", "zh-cn", "chinese"):
        language = "Chinese"  # Qwen 用英文语言名，把常见的 zh 别名归一化

    wake_words = [w for w in cfg.get("wake_words", []) if w.strip()]
    spotter = build_keyword_spotter(cfg, wake_words) if wake_words else None
    if spotter is None:
        print("[warn] KWS 未就绪，无法语音唤醒；请检查 kws_model_dir / 安装 sherpa-onnx。")
    print(f"待唤醒中…说出唤醒词之一即可开始：{wake_words}（听写时说「提醒/命令」进提醒模式）")
    print(f"（唤醒后正常说话即转写输入；静默 {cfg['sleep_after_seconds']}s 后完整休眠。Ctrl+Shift+Q 退出）")

    seg_queue = queue.Queue()
    # 屏幕热词后台扫描：唤醒态 + 距上次说话空闲 >7s 才提词——正说话时不扫，免得提词请求
    # 和转写/整理抢 llama-server 单槽把转写拖慢；转写只读它的缓存，绝不主动进识别链路。
    # 门设 7s(>单次提词耗时)：让提词只在明确停顿后启动，几乎不会跑进下一句转写。
    screen_ctx.start(lambda: state["mode"] != "sleep" and not state["quit"]
                     and time.time() - state["last_activity"] > 7)

    def transcribe(audio):
        """转写一句，返回简体文本；无有效语音返回 None。"""
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        # 峰值过低 = 持续背景噪音（说话峰值一般 0.2+），直接跳过不送识别：省 CPU、不顶高内存
        if peak < cfg.get("min_speech_peak", 0.12):
            print(f"（峰值 {peak:.3f} 过低，疑似背景噪音，跳过 {audio.size / cfg['sample_rate']:.1f}s）")
            return None
        print(f"○ 转写中 ({audio.size / cfg['sample_rate']:.1f}s, 峰值 {peak:.3f}){vram()}...")
        # Qwen3-ASR 对 VAD 裁得很紧的极短片段（~0.5s、语头无前导静音）常吐空串——补静音给它上下文
        sr = cfg["sample_rate"]
        # 上下文偏置只喂给"长段"（听写）：命令词天生短(<1s)，塞一大坨热词/术语反把短命令带偏还拖慢；
        # 长段才吃全套偏置越用越准。按 padding 前的真实语音时长切，短段裸跑。阈值 command_bias_max_seconds。
        # 屏幕热词已在 get_qwen_context 内按优先级并入，短段裸跑。
        bias_ctx = get_qwen_context() if audio.size / sr >= cfg.get("command_bias_max_seconds", 1.2) else ""
        if audio.size < sr:
            pad = np.zeros(int(0.25 * sr), dtype=np.float32)
            audio = np.concatenate([pad, audio, pad])
        _t0 = time.perf_counter()
        try:
            asr = load_model()  # 首次唤醒/卸载后重新唤醒时在此启动 llama-server
            text, conf = asr.transcribe(
                audio, cfg["sample_rate"],
                context=bias_ctx, language=language,
            )
        except Exception as e:
            print(f"[error] 转写失败: {e}")
            return None
        conf_s = f" 置信度 {conf:.3f}" if conf is not None else ""
        print(f"   原始输出={text!r} [推理 {time.perf_counter()-_t0:.2f}s{conf_s}]{vram()}")
        # 非语音时模型易幻觉出孤立标点（如"！"）；无任何文字/数字则丢弃
        if not re.search(r"[\w一-鿿]", text):
            print(f"（无有效语音，已忽略：{text!r}）")
            return None
        # 只说中英文：整句被误识别成日文假名/韩文，丢弃（绝不粘出日韩文）
        if cfg.get("asr_zh_en_only", True) and JA_KO_RE.search(text):
            print(f"（疑似误识别为日/韩文，已丢弃：{text!r}）")
            return None
        # 置信度太低=大概率听岔了：像人一样请对方重说，别把猜的字上屏。
        # 催办监听态不播（那本来就只认应答词，噪音低置信是常态，播了反而吵）
        if conf is not None and conf < cfg.get("asr_min_confidence", 0.6):
            print(f"（置信度 {conf:.3f} 低于阈值 {cfg.get('asr_min_confidence', 0.6)}，已丢弃：{text!r}）")
            if not reminder.nagging:
                threading.Thread(target=tts.play_cue, args=("没听清，麻烦再说一遍。",), daemon=True).start()
            return None
        if converter is not None:
            text = converter.convert(text)
        if cfg.get("save_audio", True):
            state["_seg_audio"] = (audio, sr, text, conf)  # 先攒着，只有真正发送才落盘（见 flush_pending_audio）；说"输入"不算数，进休眠/回退则丢弃，不留噪音
        return text

    send_words = set(cfg.get("send_words", []))
    auto_enter = cfg.get("auto_enter", True)
    confirm_words = set(cfg.get("confirm_words", []))
    undo_words = set(cfg.get("undo_words", []))
    clear_words = set(cfg.get("clear_words", []))
    polish_words = set(cfg.get("polish_words", []))
    sleep_words = set(cfg.get("sleep_words", []))
    quit_words = set(cfg.get("quit_words", []))
    reminder_enter_words = set(cfg.get("reminder_enter_words", []))
    # 提醒解析走本地 reminder 人格（挂在 llama_asr 上）；非 qwen_gguf 引擎无 llama_asr，传 None
    reminder = ReminderMode(cfg, _resolve, locals().get("llama_asr"))
    reminder.on_busy = lambda b: state.update(llm_busy=b)  # 解析期间悬浮窗显示"处理中"
    focus_title = cfg.get("focus_window_title", "")
    focus_input = cfg.get("focus_input_name", "")

    def enter_sleep(reason):
        """进入完整休眠：回到待唤醒并卸载模型释放内存/显存。
        面板里攒的缓存直接丢弃——用户没说"发送/输入"处理它，进休眠就当不要了，面板随之消失。"""
        was_reminder = state["mode"] in ("reminder", "nag")
        state["mode"] = "sleep"
        state["status"] = "idle"
        state["speaking"] = False  # 清残留，免得下次唤醒面板误显"正在说话"
        state["sleep_since"] = time.time()
        if state.get("panel") is not None:
            state["panel"].clear()  # 丢弃没处理的缓存文字，面板空了会自动隐藏
        state.pop("pending_audio", None)  # 音频留存与面板缓存同步丢弃：没真正发送就当噪音，不落盘
        n_list = state.get("hist_audio_n")
        if n_list:
            n_list[:] = [0] * len(n_list)  # pending_audio 已清空，旧计数作废，防止唤醒后回退误删新音频
        reminder.clear()  # 退出提醒模式即清空对话历史
        state["rbuf"] = []  # 丢弃没攒完的散句，免得泄进下次对话
        print(reason)
        if was_reminder:
            threading.Thread(target=tts.speak, args=("好的，退出提醒。",), daemon=True).start()
        unload_model()
        hotwords.distill_async()  # 空闲点：把新攒的发送历史交给 LLM 切词，更新热词文件

    def confirm_quit():
        """弹出 确认/取消 对话框，确认后置 quit 让主循环退出。阻塞当前 worker 线程，无妨。"""
        import saymore.ui.style as ui_style
        if ui_style.confirm("退出确认", "确认退出语音输入？", ok="退出", danger=True):
            state["quit"] = True
            print("[done] 用户确认退出")
        else:
            print("[info] 已取消退出")

    def flush_pending_audio():
        """把攒着的(音频,转写)对落盘：只在真正发送时调，给端到端微调攒同分布数据，见 audio_log.py。
        落盘后 hist_audio_n 记的计数全部作废（对应音频已不在 pending_audio 里），清零，
        防止之后回退到发送前的旧句时，误删发送后新攒的无关音频。"""
        pa = state.pop("pending_audio", None)
        n_list = state.get("hist_audio_n")
        if n_list:
            n_list[:] = [0] * len(n_list)
        if not pa:
            return
        import saymore.audio.log as audio_log
        for a, sr, t, c in pa:
            audio_log.save(a, sr, t, c)

    def ack_cue():
        """命令词识别后的简短语音应答（轮换，别每次都一样），让用户知道命令被听到了。"""
        i = state.get("cmd_cue_i", 0)
        state["cmd_cue_i"] = i + 1
        threading.Thread(target=tts.play_cue, args=(_CMD_ACK_CUES[i % len(_CMD_ACK_CUES)],), daemon=True).start()

    def run_global_command(cmd):
        """整句命令在听写/提醒两种模式下都生效：命中即执行并返回 True，否则 False。"""
        if cmd in send_words:
            state["nod_until"] = time.time() + 0.9  # 执行命令：猫点头致意
            # 前置探测：前台窗口若找不到输入框（焦点在任务栏/桌面等），提示用户重定位，
            # 不动面板缓存——避免把文字粘到"无关焦点"造成静默丢失。
            if not focus_window(focus_title, focus_input):
                warn_msg = "没找到输入框，请先将鼠标光标放置到输入框里，再说发送指令。"
                state["warn"] = (warn_msg, time.time() + 15)  # 面板红字提示 15 秒后自动消失
                if not reminder.nagging:
                    threading.Thread(target=tts.speak, args=(warn_msg,), daemon=True).start()
                print("[info] 前台窗口未找到输入框，未发送；面板缓存保留，请点入输入框后重说「发送」")
                return True
            state["warn"] = None  # 找到了输入框：清掉上次可能留下的红字警告
            sent = True
            if state.get("panel") is not None:
                sent = state["panel"].flush_all()  # 先把面板里攒的话整理回填，再回车
            if not sent:  # 整块置信度不足：扣下不发也不按回车，缓存原样留着，等下一轮/用户重说覆盖
                if not reminder.nagging:
                    threading.Thread(target=tts.play_cue, args=("这句没整理好，麻烦再说一遍。",), daemon=True).start()
                print("[info] 整理置信度不足，已扣下未发送")
                return True
            flush_pending_audio()
            focus_window(focus_title, focus_input)
            if auto_enter:
                keyboard.send("enter")
            # 真正发送了才算数：这一刻才写历史/热词。缓存窗口本身可编辑，回填进输入框的文本
            # （last_filled，含用户在面板里的手改）就是最终版，直接拿来写历史，不再回读输入框。
            # 只静默超时/噪音误触发都不会走到这里，不会污染历史。
            send_text = state.get("last_filled", "").strip()
            if send_text:
                hotwords.record(send_text)
            print(f"[done] 发送（{'回车' if auto_enter else '不回车'}）")
            ack_cue()
            return True
        if cmd in confirm_words:
            state["nod_until"] = time.time() + 0.9
            click_permission_button(focus_title)  # 优先"总是允许"，否则"允许一次"
            ack_cue()
            return True
        if cmd in polish_words:
            state["nod_until"] = time.time() + 0.9  # 执行命令：猫点头致意
            if state.get("panel") is not None:
                state["panel"].trigger_polish_now()
                print("[done] 语音命令：立即触发整理")
            ack_cue()
            return True
        if cmd in sleep_words:
            ack_cue()
            enter_sleep("💤 语音命令：进入完整休眠并卸载模型，等待唤醒。")
            return True
        if cmd in quit_words:
            confirm_quit()
            return True
        return False

    def _run_polish(text, mode):
        """按 mode（小范围整理/深度整理）跑一遍本地整理，返回 (整理文本, 置信度|None)。
        走 llama-server + 整理 LoRA（GPU 亚秒级，与转写同进程）。"""
        if not cfg.get("local_polish") or not llama_asr.lora:
            return text, None
        # 太短且无句读的整段（如只说了个"嗯"）不进 LLM：小 LoRA 对超短输入会
        # 幻觉出提示词模板，输出一堆"看着像整理结果"的东西。
        core = text.strip()
        if len(core) < 5 and not any(c in core for c in "，。！？、,.!?;；：:"):
            return text, None
        import saymore.polish.local as local_polish  # 取该 mode 的提示词（训推一致：合一 LoRA 靠 system 切人格）
        # 合一 multi 挂着就恒走它（四风格同一 adapter）；回滚到老双人格时才按 mode 分 deep/basic。
        role = "multi" if "multi" in llama_asr.loras else ("deep" if mode == "深度整理" else "basic")
        try:
            return llama_asr.polish(text, system=local_polish.system_for(mode), role=role)
        except Exception as e:
            print(f"[warn] llama-server 整理失败，原样回填: {e}")
            return text, None

    def polish_text(text):
        """小范围整理（滑动窗口用）：逐句删口水/修错/断句。"""
        return _run_polish(text, "小范围整理")

    def commit_text(text):
        """整理后的整段文字回填输入框，登记回退栈。由 TextBuffer 触发（发送/输入 命令都会走到这）。
        只回填，不写历史文件——历史/热词要等真正「发送」了才算数，见 run_global_command。"""
        # 整理结果原样回填,不自动补任何标点——邮件模式落款后面补个句号很丑,
        # 保守/00后 也没必要每段末尾都硬塞句号,让整理模型自己决定该有什么标点。
        output_text(text, cfg["paste"], focus_title, focus_input)
        hist = state.setdefault("history", [])
        hist.append(text)  # 入栈，供"回退"逐句删除
        del hist[:-20]  # ponytail: 只留最近 20 句，防无限增长
        # 这批未落盘音频有几段一并记下（与 hist 同步出入栈），供"输入"后不接着发送、
        # 直接整段回退时，精确丢弃这一批对应的音频，而不是不删/错删发送后新攒的音频
        n_list = state.setdefault("hist_audio_n", [])
        n_list.append(state.pop("_uncommit_audio_n", 0))
        del n_list[:-20]
        state["last_filled"] = text  # 缓存窗口回填进输入框的最终文本，发送时直接写历史/热词

    buffer = panel.TextBuffer(
        polish=polish_text, paste=commit_text,
        quiet_seconds=cfg.get("polish_quiet_seconds", 5.0),
        immediate=not cfg.get("panel", True),
        min_confidence=cfg.get("polish_min_confidence", 0.6),
        context_chars=cfg.get("polish_context_chars", 80),
        polish_mode=cfg.get("polish_mode", "小范围整理"),
        full_polish=lambda text, mode: _run_polish(text, mode),
    )
    state["panel"] = buffer  # 供悬浮窗渲染面板文字、enter_sleep 收尾

    def emit(text):
        """唤醒状态下：整句是命令词则执行动作；否则替换后攒进面板缓冲（不立刻粘贴）。"""
        cmd = text.strip().strip(SENTENCE_END + " ")
        if run_global_command(cmd):
            return
        if cmd in reminder_enter_words:  # 听写模式下的一句命令词，切进提醒对话模式（不再是独立唤醒词）
            state["mode"] = "reminder"
            state["last_activity"] = time.time()
            print("✓ 进入提醒模式…")
            reminder.greet()
            return
        if cmd in undo_words:
            state["nod_until"] = time.time() + 0.9  # 执行命令：猫点头致意
            if buffer.undo():  # 面板里还有没回填的句子：直接丢最后一句（没粘出去，无需退格）
                pa = state.get("pending_audio")
                if pa:
                    pa.pop()  # 音频缓存跟着弹一条，与面板缓冲对齐
                state["_uncommit_audio_n"] = max(0, state.get("_uncommit_audio_n", 0) - 1)
                print("[done] 回退：已删除面板里未回填的最后一句")
                ack_cue()
                return
            hist = state.setdefault("history", [])
            if hist:
                last = hist.pop()  # 面板空了才退已回填的：逐句出栈，可连续回退多次
                n_list = state.get("hist_audio_n")
                n = n_list.pop() if n_list else 0
                if n:  # 这一批（说过"输入"但没发送）对应的音频还在内存里，一并丢弃；已发送的 n 已被清零，不会误删
                    pa = state.get("pending_audio")
                    if pa:
                        del pa[-n:]
                focus_window(focus_title, focus_input)
                for _ in range(len(last)):
                    keyboard.send("backspace")  # ponytail: 1 字符=1 退格，富文本/输入法候选态可能不准
                print(f"[done] 回退，已删除上一句 {len(last)} 字（剩 {len(hist)} 句可回退）")
                ack_cue()
            else:
                print("[info] 无可回退的内容")
            return
        if cmd in clear_words:  # 整段重置：面板缓存和对应音频全丢，不留任何历史/热词/音频痕迹
            state["nod_until"] = time.time() + 0.9  # 执行命令：猫点头致意
            buffer.clear()
            state.pop("pending_audio", None)
            n_list = state.get("hist_audio_n")
            if n_list:
                n_list[:] = [0] * len(n_list)  # 对应计数作废，防止之后回退误删新音频
            state["_uncommit_audio_n"] = 0
            print("[done] 清空：面板缓存已重置")
            ack_cue()
            return
        buffer.add(text)  # 攒进面板缓冲，后台每 interval 秒全窗口整理；只有说"发送"才回填输入框
        # 即时回应一声"嗯/好"，让用户知道这句已听到——不等后台整理（那要好几秒）。
        # 但距上一声不够 hear_cue_min_gap 秒就跳过，免得说话密时反馈过密。
        now = time.time()
        if now - state.get("last_hear_cue", 0.0) >= cfg.get("hear_cue_min_gap", 3.0):
            state["last_hear_cue"] = now
            threading.Thread(target=tts.play_cue, args=(random.choice(_HEAR_CUES),), daemon=True).start()
        seg = state.pop("_seg_audio", None)  # 这句的音频跟着进待落盘缓存；命令词不走到这，不会被留存
        if seg:
            state.setdefault("pending_audio", []).append(seg)
            state["_uncommit_audio_n"] = state.get("_uncommit_audio_n", 0) + 1  # 供 commit_text 记这批有几段，回退整段时精确对齐

    def handle_reminder_turn(text):
        """提醒模式下收一句：退出词即时响应，否则攒进缓冲、等停顿够久由 idle_watcher 合并送 LLM。"""
        cmd = text.strip().strip(SENTENCE_END + " ")
        if reminder.is_to_dictation(cmd):  # 切回听写：模型不卸载、直接进听写态继续打字（区别于休眠）
            reminder.clear()
            state["mode"] = "awake"
            state["status"] = "awake"
            state["last_activity"] = time.time()
            print("✓ 语音命令：切回听写模式。")
            threading.Thread(target=tts.speak, args=("好的，切回听写。",), daemon=True).start()
            return
        if cmd in send_words:  # 提醒模式："发送"=立刻把已攒的话送 LLM，不等停顿超时
            flush_reminder_buffer()
            return
        if cmd in sleep_words:
            enter_sleep("💤 语音命令：进入完整休眠并卸载模型，等待唤醒。")
            return
        if cmd in quit_words:
            confirm_quit()
            return
        if state.get("llm_busy"):
            # 上一条还在调大模型：直接丢弃这句不入缓冲，只提醒用户稍候（4s 内只播一次，免得连说几句狂播）
            now = time.time()
            if now - state.get("busy_ack_time", 0) > 4:
                state["busy_ack_time"] = now
                threading.Thread(target=tts.speak, args=("正在处理上一条，请稍等。",), daemon=True).start()
            return
        state.setdefault("rbuf", []).append(text)
        # 提醒模式只用人声（不用猫叫）：收到一句先攒着，回个简短的"嗯/好的"应一声
        i = state.get("cue_i", 0)
        state["cue_i"] = i + 1
        cue = _REMINDER_CUES[i % len(_REMINDER_CUES)]
        threading.Thread(target=tts.play_cue, args=(cue,), daemon=True).start()

    def flush_reminder_buffer():
        """停顿够久，把攒的散句拼成一整句送 LLM；先清空缓冲再调，免得播报那几秒新来的段被旧 turn 带走。"""
        buf = state.get("rbuf")
        if not buf:
            return
        state["rbuf"] = []
        # 确认这一整轮说完、即将调大模型，才播一次即时反馈（一轮一次，不每句播）。
        # 这句故意说长一点，用播报时间盖住后面调模型的停顿。
        threading.Thread(target=tts.speak, args=("好的，收到了，正在处理中，请稍等一下。",), daemon=True).start()
        reminder.handle_turn("".join(buf))
        state["last_activity"] = time.time()  # 静默超时从「答完」起算，不被慢 LLM/长播报吃掉

    def handle_nag_turn(text):
        """催办监听态：只听"知道了"等应答词（或退出/休眠词）就停催办；其余全忽略，
        这样麦克风录进的催办播报声、环境噪音都不会被打字或送 LLM。"""
        cmd = text.strip().strip(SENTENCE_END + " ")
        if reminder.is_ack(cmd) or cmd in sleep_words:
            reminder.stop_nag()  # 回一声确认，并触发 on_nag_stop → 若是催办唤醒的则回休眠

    def worker():
        while True:
            seg = seg_queue.get()
            if seg is None:
                return
            if state["mode"] == "sleep" or state.get("panel_editing"):
                continue  # 待唤醒交给 KWS（CPU）；面板编辑中不接受语音输入，这段识别结果直接丢弃
            if state.get("warming"):
                continue  # 引擎还在冷启动：这句直接丢弃不转写，避免首句卡很久才有反应
            state["status"] = "transcribing"
            text = transcribe(seg)
            if text is not None:
                if reminder.nagging:
                    handle_nag_turn(text)  # 催办期间：只接"知道了"，其余（含被麦克风录进的播报声）一律忽略
                elif state["mode"] == "reminder":
                    handle_reminder_turn(text)
                else:
                    emit(text)
                state["last_activity"] = time.time()
            state["status"] = "awake" if state["mode"] in ("awake", "reminder", "nag") else "idle"

    def idle_watcher():
        """唤醒后静默超过 sleep_after_seconds（默认 5 分钟）即进入完整休眠：
        回到待唤醒并卸载模型释放内存/显存。下次唤醒重新加载（慢几秒）。"""
        while not state["quit"]:
            time.sleep(1)
            if state["mode"] == "awake" and not state.get("speaking") \
                    and seg_queue.empty() and state["status"] != "transcribing" \
                    and time.time() - state["last_activity"] > _HOTWORD_DISTILL_IDLE_SECONDS:
                hotwords.distill_async()
            # 提醒模式：从「话音落」(last_seg_time，切句时刻)算停顿，超阈值就合并送 LLM。
            # 要求没在说话(speaking)、队列空、且当前没段在识别(status≠transcribing)——
            # 即「已说的都识别完了」才送；识别还没跑完就继续等（不丢句）。
            if state["mode"] == "reminder" and state.get("rbuf") \
                    and not state.get("speaking") and seg_queue.empty() \
                    and state["status"] != "transcribing" \
                    and time.time() - state.get("last_seg_time", 0) > cfg.get("reminder_flush_seconds", 2.0):
                flush_reminder_buffer()
            # 听写模式的缓存不再因静默自动回填——只有说"发送"才回填（见 run_global_command）
            if state["mode"] not in ("awake", "reminder") or not seg_queue.empty():
                continue
            # 提醒对话用更短的静默超时，避免上下文越拖越长
            limit = cfg.get("reminder_idle_seconds", 60) if state["mode"] == "reminder" else cfg["sleep_after_seconds"]
            if time.time() - state["last_activity"] > limit:
                reason = ("💤 提醒对话静默超时，结束对话回到待唤醒。" if state["mode"] == "reminder"
                          else "💤 静默超时，进入完整休眠并卸载模型，等待唤醒。")
                enter_sleep(reason)

    def nag_wake():
        """催办开始：若在休眠就唤醒进「催办监听」态，好让 ASR 接住你的"知道了"；
        若你正在听写/对话则不动模式，只是后台开始循环催（你说"知道了"即停）。"""
        if state["mode"] == "sleep":
            state["mode"] = "nag"
            state["status"] = "awake"
            state["last_activity"] = time.time()
        print('🔔 到点提醒，开始催办（说"知道了"即停）…')

    def nag_sleep():
        """催办结束：仅当是催办把它从休眠唤醒的(mode==nag)才回休眠，
        别打断你本来正在进行的听写/提醒对话。"""
        if state["mode"] == "nag":
            enter_sleep("💤 催办结束，回到待唤醒。")

    reminder.on_nag_start = nag_wake
    reminder.on_nag_stop = nag_sleep

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=idle_watcher, daemon=True).start()
    reminder.start_watcher(lambda: state["quit"])  # 后台每分钟检查到点提醒，有就开催办

    kws_stream = spotter.create_stream() if spotter is not None else None
    kws_queue = queue.Queue() if kws_stream is not None else None

    def on_block(block):
        """每块音频（PortAudio 回调线程，必须轻量）：记录音量给波形；待唤醒时把块入队交给 KWS 线程。"""
        # 记录最近 40 帧 RMS（ponytail: GIL 下 list 读写够安全，仅用于可视化）
        lv = state["levels"]
        lv.append(float(np.sqrt(np.mean(block ** 2))))
        if len(lv) > 40:
            del lv[:-40]
        # KWS 解码搬到独立线程，回调里只入队：否则解码占着回调线程会 input overflow 丢唤醒音频
        if kws_queue is not None and state["mode"] == "sleep":
            kws_queue.put(block)

    def kws_worker():
        """待唤醒时持续从队列取音频喂 KWS，命中关键词则切到聆听态。独立线程，不阻塞录音回调。"""
        while not state["quit"]:
            try:
                block = kws_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if state["mode"] != "sleep":
                continue  # 已唤醒，丢弃入队后才切态的残留块
            kws_stream.accept_waveform(cfg["sample_rate"], block)
            while spotter.is_ready(kws_stream):
                spotter.decode_stream(kws_stream)
            hit = spotter.get_result(kws_stream)
            if hit:
                spotter.reset_stream(kws_stream)
                recorder.discard_current()  # 丢弃含唤醒词的当前缓冲，避免被当正文转写
                state["speaking"] = False  # 唤醒词那句话不算"正在说话"，别让面板一唤醒就显示提示
                state["mode"] = "awake"
                state["status"] = "awake"  # 立刻让悬浮框显示"聆听中"，否则唤醒后无反馈
                state["last_activity"] = time.time()
                print("✓ 已唤醒，开始聆听…")
                state["warming"] = True  # 引擎冷启动中：圆环显示"正在初始化"，期间说的话直接丢弃不转写
                state["warming_start"] = time.time()
                def _warm_then_ready():
                    try:
                        warm_model()
                    finally:
                        state["warming"] = False
                threading.Thread(target=_warm_then_ready, daemon=True).start()
                i = state.get("wake_cue_i", 0)
                state["wake_cue_i"] = i + 1
                threading.Thread(target=tts.play_cue, args=(_WAKE_CUES[i % len(_WAKE_CUES)],), daemon=True).start()

    def on_segment_cut(seg):
        """切句（话音落+静音确认）回调，PortAudio 线程：标记说完、记话音落时刻并入队。
        提醒模式的攒句停顿从 last_seg_time 算起，不含后续 ASR 识别耗时。"""
        state["speaking"] = False
        state["last_seg_time"] = time.time()
        screen_ctx.release_anchor()  # 说完这句，解锁锚点，空闲期重新跟着前台窗口预热
        if state.get("panel") is not None:
            state["panel"].notify_speech_end()
        seg_queue.put(seg)

    def on_speech_start():
        """一段语音起点：标记正在说话（避免话说一半被判停顿误 flush），awake 态顺带刷休眠倒计时。
        休眠中不置位——否则唤醒词那句话会让面板一唤醒就残留"正在说话"提示。"""
        if state["mode"] == "sleep":
            return
        state["speaking"] = True
        screen_ctx.set_anchor()  # 记下开口这一刻在哪个窗口，中途切走了后台扫描就跳过
        if state.get("panel") is not None:
            state["panel"].notify_speech_start()
        if state["mode"] == "awake":
            state["last_activity"] = time.time()

    recorder = Recorder(
        cfg["sample_rate"],
        on_segment=on_segment_cut,
        silence_rms=cfg["silence_rms"],
        silence_seconds=cfg["silence_seconds"],
        min_segment_seconds=cfg["min_segment_seconds"],
        max_segment_seconds=cfg.get("max_segment_seconds", 15.0),
        on_block=on_block,
        on_speech=on_speech_start,
        vad=build_vad(cfg),
        device=cfg.get("input_device") or None,
    )
    def do_import(path):
        """对给定文件跑导入：唤醒进提醒模式 + 抽字建提醒（在后台线程里跑，别卡监视线程）。
        文件由主界面「导入」tab 选好后经触发文件传来（GUI 子进程不碰模型/TTS）。"""
        if state["mode"] != "reminder":
            reminder.clear()  # 从休眠/听写切入，作为一次全新提醒对话
            state["mode"] = "reminder"
        state["status"] = "awake"
        state["last_activity"] = time.time()
        print(f"✓ 导入文件，进入提醒模式…{path}")
        reminder.import_file(path)

    def import_trigger_watcher():
        """监视主界面写来的导入触发文件：出现即读路径、删文件、后台跑导入。"""
        trigger = Path(state["import_trigger"])
        trigger.unlink(missing_ok=True)  # 清掉可能残留的旧触发，免得一启动就误导入
        while not state["quit"]:
            try:
                if trigger.exists():
                    path = trigger.read_text(encoding="utf-8").strip()
                    trigger.unlink(missing_ok=True)
                    if path:
                        threading.Thread(target=lambda p=path: do_import(p), daemon=True).start()
            except Exception as e:  # noqa: BLE001 监视线程别被单次异常杀死
                print(f"[warn] 导入触发处理出错：{e}")
            time.sleep(0.5)
    threading.Thread(target=import_trigger_watcher, daemon=True).start()

    def restart_trigger_watcher():
        """监视设置窗口写来的重启触发文件：出现即拉起新进程接班、本进程退出。"""
        trigger = Path(state["restart_trigger"])
        trigger.unlink(missing_ok=True)  # 清掉可能残留的旧触发，免得一启动就误重启
        while not state["quit"]:
            try:
                if trigger.exists():
                    trigger.unlink(missing_ok=True)
                    print("[info] 设置窗口请求重启，拉起新进程接班…")
                    _close_main_window()  # 关掉可能还开着的主窗口子进程，避免遗留旧页面
                    from saymore.proc import spawn_backend
                    spawn_backend()
                    state["quit"] = True
            except Exception as e:  # noqa: BLE001 监视线程别被单次异常杀死
                print(f"[warn] 重启触发处理出错：{e}")
            time.sleep(0.5)
    threading.Thread(target=restart_trigger_watcher, daemon=True).start()

    if kws_queue is not None:
        threading.Thread(target=kws_worker, daemon=True).start()
    recorder.start()  # 常驻监听，直到退出
    ready_marker.write_text("1", encoding="utf-8")  # 真正开始监听了：悬浮窗/主界面的"初始化中"提示据此消失
    state["backend_ready"] = True

    if overlay_thread is not None:
        overlay_thread.join()  # 悬浮窗已在前面拉起独立线程跑，这里只是等它退出（用户点"退出程序"）
    else:
        try:
            keyboard.wait()
        except KeyboardInterrupt:
            print("\n退出。")


if __name__ == "__main__":
    main()
