# -*- coding: utf-8 -*-
"""主界面窗口：一个 pywebview 原生无边框窗口，左侧竖排三 tab——设置 / 历史记录 / 导入。
悬浮圆环右键「主界面」拉起本窗口；设置、历史、导入原本各自的窗口/右键项全部收进这里。

设置面板数据/保存复用 settings_window，历史两类记录复用 history_view，导入借 pywebview 原生
选文件框选中路径后写一个触发文件、由语音进程接住跑导入（GUI 子进程不碰模型/TTS，见 voice_input）。

pywebview 事件循环占主线程，主程序主线程被语音循环占着，所以 show() 用子进程拉起本文件
（它自己的主线程跑窗口）。设置改动直接写 config.json（重启生效）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import typeoff.ui.settings as settings_window
import typeoff.ui.history as history_view
import typeoff.audio.mic_probe as mic_probe
def _build_html(cfg, cfg_dir, history_dir, reminders_log, tab):
    payload = {
        "settings": settings_window.settings_data(cfg, cfg_dir),
        "history": {
            "voice": [{"t": t, "text": s} for t, s in history_view.recent_entries(history_dir)],
            "reminder": [{"t": t, "action": a, "text": s}
                         for t, a, s in history_view.reminder_changes(reminders_log)],
        },
        "tab": tab if tab in ("settings", "hotwords", "history", "import") else "settings",
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return _HTML.replace("__DATA__", data)


class _Api:
    """暴露给页面的 Python 桥：window.pywebview.api.*。"""

    def __init__(self, config_path, import_trigger, restart_trigger, history_dir, reminders_log):
        self._cfg = config_path
        self._trigger = import_trigger
        self._restart_trigger = restart_trigger
        self._history_dir = history_dir
        self._reminders_log = reminders_log
        self._window = None  # _run_gui 建窗后回填，供选文件框用
        self._meter = None   # 麦克风电平表探针，页面按需 start/stop
        self.allow_close = False  # True=真要销毁窗口了；区分"用户点了关闭"和"win.destroy()自己触发的 closing 事件"

    def save(self, payload):
        try:
            settings_window.save(self._cfg, payload)
            return {"ok": True, "msg": "已保存"}
        except ValueError:
            return {"ok": False, "msg": "格式不合法，未保存"}
        except Exception as e:  # noqa: BLE001 兜底，别让保存把窗口搞崩
            return {"ok": False, "msg": f"保存失败：{e}"}

    def pick_import(self):
        """弹原生选文件框选中路径 → 写触发文件，交给语音进程跑导入（本进程不碰模型/TTS）。"""
        import webview
        try:
            types = ("支持的文件 (*.txt;*.docx;*.xlsx;*.pptx;*.png;*.jpg;*.jpeg;*.bmp;*.webp)",
                     "所有文件 (*.*)")
            res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                                  file_types=types)
            if not res:
                return {"ok": False, "msg": "未选择文件"}
            path = res[0] if isinstance(res, (list, tuple)) else res
            Path(self._trigger).write_text(str(path), encoding="utf-8")
            return {"ok": True, "msg": f"已提交导入：{Path(path).name}，正在建提醒…"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"导入失败：{e}"}

    def delete_history(self, kind, t, text):
        """删掉一条历史记录：kind=voice 删语音输入，reminder 删提醒变更流水。"""
        try:
            if kind == "voice":
                ok = history_view.delete_entry(self._history_dir, t, text)
            else:
                ok = history_view.delete_change(self._reminders_log, t, text)
            return {"ok": ok, "msg": "已删除" if ok else "未找到该记录"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"删除失败：{e}"}

    def restart(self):
        """设置改动需重启才生效：写触发文件，交语音进程接住——拉新进程接班、自己退出。"""
        try:
            Path(self._restart_trigger).write_text("1", encoding="utf-8")
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"重启失败：{e}"}

    def list_input_devices(self):
        """[{'name','is_default'}...]，页面初始化下拉时调一次。"""
        try:
            return {"ok": True, "devices": mic_probe.list_input_devices()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": str(e), "devices": []}

    def mic_meter_start(self, device_name):
        """启电平表探针（切设备就再调一次，内部会先 stop 旧的）。"""
        try:
            if self._meter is None:
                self._meter = mic_probe.LevelMeter(device_name or "")
            else:
                self._meter.device_name = device_name or ""
            self._meter.start()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": str(e)}

    def mic_meter_read(self):
        """页面轮询一次的最新 RMS（0..1）+ 错误信息。"""
        if self._meter is None:
            return {"rms": 0.0, "err": None}
        rms, err = self._meter.latest()
        return {"rms": rms, "err": err}

    def mic_meter_stop(self):
        if self._meter is not None:
            self._meter.stop()
        return {"ok": True}

    def minimize(self):
        if self._window is not None:
            self._window.minimize()

    def close(self):
        import webview
        # 关窗不等于退出程序（后台仍在托盘运行）——写触发文件，交语音进程从真正的托盘图标弹
        # 系统气泡通知；这里不弹窗口内提示，因为窗口马上就没了，用户根本看不到。
        # 只在主界面第一次关闭时提示一次：有 .tray_notice_seen 标记就说明用户已经知道了，
        # 以后每次关主界面都弹会很烦，不再重复。
        try:
            seen = Path(self._cfg).parent / ".tray_notice_seen"
            if not seen.exists():
                (Path(self._cfg).parent / ".tray_notice").write_text(
                    "已最小化到系统托盘，Typeoff 仍在后台运行", encoding="utf-8")
                seen.write_text("1", encoding="utf-8")
        except Exception:
            pass
        self.allow_close = True
        if self._meter is not None:
            self._meter.stop()
        for w in list(webview.windows):
            w.destroy()


def _run_gui(config_path, history_dir, reminders_log, import_trigger, restart_trigger, tab):
    """在本进程主线程开原生窗口并阻塞，直到关闭。由 show() 拉起的子进程执行。"""
    import webview

    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    cfg_dir = Path(config_path).parent
    api = _Api(config_path, import_trigger, restart_trigger, history_dir, reminders_log)
    win = webview.create_window(_WIN_TITLE, html=_build_html(cfg, cfg_dir, history_dir, reminders_log, tab),
                                js_api=api, width=900, height=660, min_size=(720, 500),
                                frameless=True, easy_drag=False, background_color="#FFFFFF")
    api._window = win

    def _on_closing():
        # 拦所有关闭途径（自绘 X、Alt+F4、任务栏关闭），转给页面 close_() 走统一的"托盘提示+延迟关"
        # 流程；api.close() 真正销毁时会把 allow_close 置 True，此时放行，不再拦第二次。
        if api.allow_close:
            return True
        win.evaluate_js("close_()")
        return False
    win.events.closing += _on_closing

    from typeoff.paths import PROJECT_ROOT
    ico = PROJECT_ROOT / "typeoff.ico"
    webview.start(icon=str(ico) if ico.exists() else None)


_WIN_TITLE = "Typeoff"


def _focus_existing():
    """已开着主界面就把它复原+前置，返回 True；没开返回 False（仅 Windows，纯 ctypes）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, _WIN_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE：最小化过就复原
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def show(config_path, history_dir, reminders_log, import_trigger, restart_trigger, tab="settings"):
    """拉起独立子进程显示主界面，立即返回（不阻塞调用线程）。后台线程可安全调用。
    已开着一个就前置复用它，不再叠开新窗口。"""
    if _focus_existing():
        return
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：别闪出控制台
    subprocess.Popen([sys.executable, os.path.abspath(__file__), str(config_path),
                      str(history_dir), str(reminders_log), str(import_trigger),
                      str(restart_trigger), tab], **kw)


_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Typeoff</title>
<style>
:root{
  --gray:#f5f5f7; --white:#fff; --text:#1d1d1f; --muted:#86868b; --border:#e5e5ea;
  --accent:#0a84ff; --accent-soft:#e7f0ff; --hover:#eaeaec; --field:#fff; --card:#fff;
  --tile:#f5f5f7; --tile-line:#ececef;
}
@media (prefers-color-scheme:dark){
  :root{--gray:#1c1c1e; --white:#262628; --text:#f5f5f7; --muted:#98989d; --border:#3a3a3c;
    --accent:#0a84ff; --accent-soft:#0a84ff2e; --hover:#3a3a3c; --field:#1c1c1e; --card:#1c1c1e;
    --tile:#2c2c2e; --tile-line:#3a3a3c;}
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{color:var(--text);background:var(--white);
  font-family:-apple-system,"Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;font-size:14px}
.app{display:flex;height:100vh;overflow:hidden}
/* 左：整块浅灰 */
.side{flex:0 0 190px;background:var(--gray);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:0 10px}
.brand{padding:18px 12px 12px;user-select:none}
.brand .en{font-weight:600;font-size:15px;letter-spacing:.02em}
nav a{display:flex;align-items:center;gap:10px;padding:9px 12px;margin-bottom:2px;border-radius:8px;
  color:var(--text);cursor:pointer;font-weight:500;user-select:none}
nav a svg{width:17px;height:17px;flex:0 0 auto;stroke:currentColor}
nav a:hover{background:var(--hover)}
nav a.active{background:var(--accent-soft);color:var(--accent)}
.foot{margin-top:auto;padding:12px;color:var(--muted);font-size:12px;user-select:none}
/* 右：整块纯白 */
.content{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--white)}
.tbar{display:flex;align-items:center;gap:6px;padding:8px 12px 8px 22px;border-bottom:1px solid var(--border)}
.tbar .drag{flex:1;align-self:stretch}
.toast{font-size:13px;color:var(--muted);opacity:0;transition:opacity .2s;margin-right:6px}
.toast.show{opacity:1}
.toast.err{color:#ff453a}
/* 窗口按钮：正方形，图标用 SVG 线条(非文字字形)保证不变形 */
.winbtn{width:30px;height:30px;flex:0 0 auto;padding:0;border:0;background:transparent;
  color:var(--muted);cursor:pointer;border-radius:7px;
  display:inline-flex;align-items:center;justify-content:center}
.winbtn svg{width:15px;height:15px;stroke:currentColor;stroke-width:2;fill:none;
  stroke-linecap:round;stroke-linejoin:round}
.winbtn:hover{background:var(--hover);color:var(--text)}
.winbtn.close:hover{background:#ff453a;color:#fff}
.body{flex:1;overflow-y:auto;padding:6px 26px 30px}
.panel{display:none}
.panel.active{display:block}
/* 设置：三分类各一色底的圆角分区；区内每条命令一张卡片，控件聚合其中（Apple 分组列表风）*/
#p-settings{max-width:660px}
#p-hotwords{max-width:820px}
/* 热词分区里 textarea 更高一点，方便浏览与编辑 */
#p-hotwords textarea.wordlist{min-height:260px;max-height:520px}
.zone{padding:0 2px;margin:16px 0}
.sec{margin:8px 4px 12px}
.sec h2{display:flex;align-items:center;gap:8px;margin:0;font-size:18px;font-weight:700;letter-spacing:-.015em}
.sec h2::before{content:"";width:9px;height:9px;border-radius:50%;flex:0 0 auto;background:var(--muted)}
.z0 .sec h2::before{background:#0a84ff}
.z1 .sec h2::before{background:#30d158}
.z2 .sec h2::before{background:#ff9f0a}
.sec p{margin:4px 0 0 17px;color:var(--muted);font-size:12.5px;line-height:1.4}
.cmd-card{background:var(--tile);border:1px solid var(--tile-line);border-radius:14px;
  padding:15px 17px;margin-bottom:9px}
.cmd-card:last-child{margin-bottom:2px}
/* 命令卡片按所属分类着色（分类本身不再有色底）*/
.z0 .cmd-card{background:rgba(10,132,255,.07);border-color:rgba(10,132,255,.16)}   /* 程序：蓝 */
.z1 .cmd-card{background:rgba(48,209,88,.09);border-color:rgba(48,209,88,.18)}     /* 语音输入：绿 */
.z2 .cmd-card{background:rgba(255,159,10,.10);border-color:rgba(255,159,10,.20)}   /* 提醒：橙 */
.hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.label{font-weight:600;font-size:14.5px}
.desc{color:var(--muted);font-size:12.5px;line-height:1.5}
input[type=text],input[type=number],select{font:inherit;color:var(--text);background:var(--field);
  border:1px solid var(--border);border-radius:8px;padding:7px 10px;outline:none;width:210px;margin-top:9px}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
/* 热词多行编辑框：一行一词，滚动条内嵌，别撑大整页 */
textarea.wordlist{font:inherit;color:var(--text);background:var(--field);
  border:1px solid var(--border);border-radius:8px;padding:9px 12px;outline:none;
  width:100%;margin-top:10px;min-height:180px;max-height:340px;resize:vertical;
  line-height:1.55;font-family:ui-monospace,"Cascadia Mono","Consolas",monospace;font-size:13px}
.wl-meta{display:flex;justify-content:space-between;align-items:center;margin-top:6px;
  color:var(--muted);font-size:12px}
/* 下拉框 + 当前选项释义同一行 */
.ctl-row{display:flex;align-items:center;gap:10px;margin-top:9px;flex-wrap:wrap}
.ctl-row select{margin-top:0}
.opt-desc{color:var(--muted);font-size:12.5px}
/* 麦克风：下拉 + 电平条一排。各占一半，条紧贴下拉，出错才显示红字 */
.mic-wrap{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.mic-row{display:flex;align-items:center;gap:12px}
select.mic-select{flex:1 1 0;min-width:0;margin-top:0;width:auto}
.mic-meter{flex:1 1 0;min-width:80px;position:relative;height:8px;border-radius:4px;
  background:var(--border);overflow:hidden}
.mic-meter .bar{position:absolute;left:0;top:0;bottom:0;width:0%;
  background:linear-gradient(90deg,#30d158 0%,#30d158 70%,#ffd60a 85%,#ff453a 100%);
  border-radius:4px;transition:width .06s linear}
.mic-meter .peak{position:absolute;top:0;bottom:0;width:2px;background:var(--text);opacity:.45;
  left:0;transition:left .12s ease-out}
.mic-err{color:#ff453a;font-size:12px}
/* 命令词：每个词一个小方块，末尾一个带「+」的空方块，点它就地变输入框，回车加词 */
.chip-box{display:flex;flex-wrap:wrap;gap:12px 14px;margin-top:11px}
.chip{position:relative;display:inline-flex;align-items:center;background:var(--accent-soft);
  border:1px solid var(--border);border-radius:9px;padding:8px 15px;font-size:13.5px;font-weight:500}
.chip button{position:absolute;top:-7px;right:-7px;width:18px;height:18px;padding:0;border:0;
  border-radius:50%;background:var(--muted);color:#fff;font-size:12px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.chip button:hover{background:#ff453a}
.chip-plus{background:transparent;border:1px dashed var(--border);color:var(--muted);
  cursor:pointer;padding:8px 14px;font-size:16px;line-height:1;user-select:none}
.chip-plus:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.chip-edit{display:inline-flex;align-items:center;background:var(--field);border:1px solid var(--accent);
  border-radius:9px;padding:4px 8px}
.chip-edit input{border:0;outline:0;background:transparent;color:var(--text);font:inherit;
  font-weight:500;font-size:13.5px;width:120px;margin:0;padding:2px 4px}
.sw{position:relative;display:inline-block;width:44px;height:26px;margin-top:6px}
.sw input{opacity:0;width:0;height:0}
.sw span{position:absolute;inset:0;background:var(--border);border-radius:26px;transition:.2s;cursor:pointer}
.sw span:before{content:"";position:absolute;width:22px;height:22px;left:2px;top:2px;
  background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.sw input:checked+span{background:var(--accent)}
.sw input:checked+span:before{transform:translateX(18px)}
/* 命令词卡片底部的独立开关行（如发送指令词的「自动回车」） */
.toggle-row{display:flex;align-items:center;gap:8px;margin-top:10px;padding-top:10px;
  border-top:1px solid var(--tile-line)}
.toggle-row .sw{margin-top:0}
.toggle-row .label{font-size:13px}
.toggle-row .desc{font-size:12px}
/* 历史：分段切换 + 卡片 */
.seg{display:inline-flex;background:var(--gray);border:1px solid var(--border);border-radius:9px;
  padding:2px;margin:10px 0 4px}
.seg button{border:0;background:transparent;color:var(--text);font:inherit;font-weight:500;
  padding:6px 16px;border-radius:7px;cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:12px 14px;margin-bottom:10px;transition:background .12s,border-color .12s}
.card:hover{background:var(--accent-soft);border-color:var(--accent)}
.chead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
.cright{display:flex;align-items:center;gap:8px}
.cacts{display:none;gap:4px}
.card:hover .cacts{display:flex}
.cacts button{width:26px;height:26px;padding:0;border:1px solid var(--border);border-radius:7px;
  background:var(--card);color:var(--muted);cursor:pointer;display:flex;align-items:center;
  justify-content:center}
.cacts button svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.cacts button:hover{color:var(--accent);border-color:var(--accent)}
.cacts button.del:hover{color:#fff;background:#ff453a;border-color:#ff453a}
.time{color:var(--muted);font-size:12px}
.tag{font-size:12px;font-weight:600;color:var(--accent)}
.ctext{line-height:1.55;white-space:pre-wrap;word-break:break-word;user-select:text}
.empty{color:var(--muted);text-align:center;padding:56px 0}
/* 导入 */
.imp{max-width:520px;padding-top:16px}
.imp p{color:var(--muted);line-height:1.6}
button.primary{border:0;background:var(--accent);color:#fff;font-size:14px;font-weight:500;
  padding:9px 20px;border-radius:9px;cursor:pointer;margin-top:8px}
button.primary:active{opacity:.85}
.impmsg{margin-top:14px;font-size:13.5px;color:var(--muted)}
.impmsg.err{color:#ff453a}
/* 重启提示条：左下角常驻，直到点了重启或关窗自动重启 */
.restart-bar{position:fixed;left:16px;bottom:16px;display:none;align-items:center;gap:10px;
  background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 14px;
  box-shadow:0 4px 16px rgba(0,0,0,.15);font-size:13px;z-index:100}
.restart-bar.show{display:flex}
.restart-bar button{border:0;background:var(--accent);color:#fff;font:inherit;font-weight:500;
  padding:6px 14px;border-radius:8px;cursor:pointer;white-space:nowrap}
.restart-bar button:active{opacity:.85}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand pywebview-drag-region"><div class="en">Typeoff</div></div>
    <nav id="nav"></nav>
    <div class="foot">设置改动自动保存<br>多数设置重启后生效</div>
  </aside>
  <section class="content">
    <div class="tbar">
      <div class="drag pywebview-drag-region"></div>
      <span class="toast" id="toast"></span>
      <button class="winbtn" onclick="minimize_()" title="最小化">
        <svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
      <button class="winbtn close" onclick="close_()" title="关闭 (Esc)">
        <svg viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button>
    </div>
    <div class="body">
      <div class="panel" id="p-settings"></div>
      <div class="panel" id="p-hotwords"></div>
      <div class="panel" id="p-history"></div>
      <div class="panel" id="p-import"></div>
    </div>
  </section>
  <div class="restart-bar" id="restartBar">
    <span>设置已修改，需重启生效</span>
    <button onclick="restartNow()">立即重启</button>
  </div>
</div>
<script>
const DATA = __DATA__;
const _S = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">';
const TABS = [
  ["settings", "设置", _S + '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'],
  ["hotwords", "热词", _S + '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>'],
  ["history", "历史记录", _S + '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'],
  ["import", "导入", _S + '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>'],
];
const nav = document.getElementById('nav');
TABS.forEach(([id, name, icon]) => {
  const a = document.createElement('a');
  a.dataset.tab = id;
  a.innerHTML = icon + '<span>' + name + '</span>';
  a.onclick = () => activate(id);
  nav.appendChild(a);
});
function activate(id) {
  document.querySelectorAll('nav a').forEach(x => x.classList.toggle('active', x.dataset.tab === id));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  document.getElementById('p-' + id).classList.add('active');
}

/* pywebview 桥接首帧可能未就绪，需要立即触发 api 的地方走这个门 */
function whenApiReady(fn) {
  if (window.pywebview && window.pywebview.api) { fn(); return; }
  window.addEventListener('pywebviewready', fn, {once: true});
}

/* —— 设置 —— */
const ps = document.getElementById('p-settings');
/* 命令词：每词一个小方块（右上角叉删除）+ 末尾「+」方块（点它就地展开输入框，回车加词） */
const chipState = {};  // key -> {label, words}，关窗前查一遍别让哪类指令词被删光
const originalValues = {};  // key -> 打开设置窗口时的原值，用于判断改回原值后是否还需要重启
const noRestartKeys = new Set();  // wordlist 等实时生效的键，跳过重启提示（必须先声明，buildWordlist 用得到）
function buildChips(row, f) {
  const single = !!(f.options && f.options.single);  // 唤醒词等：只保留一个方块，加新词即替换
  const words = (f.value || []).slice();
  chipState[f.key] = {label: f.label, words};
  originalValues[f.key] = words.join('\n');
  const box = document.createElement('div'); box.className = 'chip-box';
  row.appendChild(box);
  const persist = () => window.pywebview.api.save({[f.key]: words.join('\n')}).then(r => { toast(r); if (r.ok) checkDirty(f.key, words.join('\n')); })
    .catch(() => toast({ok: false, msg: '保存失败：桥接未就绪'}));
  function draw() {
    box.innerHTML = '';
    words.forEach((w, idx) => {
      const chip = document.createElement('div'); chip.className = 'chip'; chip.textContent = w;
      const del = document.createElement('button'); del.textContent = '✕'; del.title = '删除';
      del.onclick = () => { words.splice(idx, 1); draw(); persist(); };
      chip.appendChild(del); box.appendChild(chip);
    });
    const plus = document.createElement('div');
    plus.className = 'chip chip-plus'; plus.textContent = '+';
    plus.title = single ? '输入唤醒词（替换现有）' : '添加命令词';
    plus.onclick = () => openEditor(plus);
    box.appendChild(plus);
  }
  if (f.options && f.options.toggle) {  // 挂在这组命令词卡片底部的独立一行开关（如发送指令词的「自动回车」）
    const tg = f.options.toggle;
    const tgRow = document.createElement('div'); tgRow.className = 'toggle-row';
    const sw = document.createElement('label'); sw.className = 'sw';
    sw.innerHTML = '<input type="checkbox"><span></span>';
    sw.querySelector('input').checked = !!tg.value;
    originalValues[tg.key] = !!tg.value;
    sw.querySelector('input').addEventListener('change', e => {
      window.pywebview.api.save({[tg.key]: e.target.checked}).then(r => { toast(r); if (r.ok) checkDirty(tg.key, e.target.checked); })
        .catch(() => toast({ok: false, msg: '保存失败：桥接未就绪'}));
    });
    const lbl = document.createElement('span'); lbl.className = 'label'; lbl.textContent = tg.label;
    const desc = document.createElement('span'); desc.className = 'desc'; desc.textContent = tg.desc || '';
    tgRow.appendChild(sw); tgRow.appendChild(lbl); tgRow.appendChild(desc);
    row.appendChild(tgRow);
  }
  function openEditor(plus) {
    const wrap = document.createElement('div'); wrap.className = 'chip chip-edit';
    const inp = document.createElement('input'); inp.type = 'text';
    inp.placeholder = single ? '输入唤醒词…' : '输入命令词…';
    wrap.appendChild(inp);
    plus.replaceWith(wrap); inp.focus();
    let done = false;
    const commit = () => {
      if (done) return; done = true;
      const v = inp.value.trim();
      if (v && !words.includes(v)) {
        if (single) words.length = 0;
        words.push(v); draw(); persist();
      } else { draw(); }
    };
    const cancel = () => { if (done) return; done = true; draw(); };
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
    inp.addEventListener('blur', commit);  // 点别处也算确认，别丢用户已经打的字
  }
  draw();
}
/* 热词：多行 textarea，一行一词。失焦或停手 800ms 自动存，右下显示词数 */
function buildWordlist(row, f) {
  const words = (f.value || []).slice();
  originalValues[f.key] = words.join('\n');
  noRestartKeys.add(f.key);
  const ta = document.createElement('textarea');
  ta.className = 'wordlist'; ta.spellcheck = false;
  ta.placeholder = '每行一个词，例如：\nprompt engineering\nclaude code\n量子计算';
  ta.value = words.join('\n');
  const meta = document.createElement('div'); meta.className = 'wl-meta';
  const count = document.createElement('span');
  const hint = document.createElement('span'); hint.textContent = '实时保存';
  const upd = () => {
    const n = ta.value.split('\n').filter(x => x.trim()).length;
    count.textContent = n + ' 个词';
  };
  upd();
  meta.appendChild(count); meta.appendChild(hint);
  row.appendChild(ta); row.appendChild(meta);
  const persist = () => window.pywebview.api.save({[f.key]: ta.value}).then(r => {
    toast(r); if (r.ok) checkDirty(f.key, ta.value.split('\n').filter(x => x.trim()).join('\n'));
  }).catch(() => toast({ok: false, msg: '保存失败：桥接未就绪'}));
  let timer = null;
  ta.addEventListener('input', () => {
    upd();
    clearTimeout(timer); timer = setTimeout(persist, 800);
  });
  ta.addEventListener('blur', () => { clearTimeout(timer); persist(); });
}

/* 麦克风：绿电平条 + 设备下拉。选完实时保存（重启生效），电平表跟着切设备重启 */
const micState = {pollTimer: null, peakLeft: 0, peakDecay: null};
function buildMic(row, f) {
  const cur = String(f.value || '');
  originalValues[f.key] = cur;
  const wrap = document.createElement('div'); wrap.className = 'mic-wrap';
  const inline = document.createElement('div'); inline.className = 'mic-row';
  // 下拉
  const sel = document.createElement('select'); sel.className = 'mic-select';
  sel.dataset.key = f.key; sel.dataset.type = 'mic';
  // 电平条
  const meter = document.createElement('div'); meter.className = 'mic-meter';
  const bar = document.createElement('div'); bar.className = 'bar';
  const peak = document.createElement('div'); peak.className = 'peak';
  meter.appendChild(bar); meter.appendChild(peak);
  inline.appendChild(sel); inline.appendChild(meter);
  const errBox = document.createElement('div'); errBox.className = 'mic-err';
  wrap.appendChild(inline); wrap.appendChild(errBox);
  row.appendChild(wrap);

  // 占位一项，防止桥接未就绪时下拉是空的
  const placeholder = document.createElement('option');
  placeholder.value = cur; placeholder.textContent = cur || '系统默认';
  sel.appendChild(placeholder);

  const applyDevice = () => {
    if (!(window.pywebview && window.pywebview.api)) return;
    const v = sel.value;
    window.pywebview.api.mic_meter_start(v).then(r => {
      errBox.textContent = r && r.ok ? '' : ('探针失败：' + (r && r.msg || ''));
    }).catch(() => {});
  };

  // pywebview 桥接可能页面首帧还没就绪：等 pywebviewready 再列设备+起探针，别炸掉后面的 activate()/close_ 定义
  whenApiReady(() => {
    window.pywebview.api.list_input_devices().then(r => {
      sel.innerHTML = '';
      const opt0 = document.createElement('option');
      opt0.value = ''; opt0.textContent = '系统默认';
      sel.appendChild(opt0);
      let matched = (cur === '');
      ((r && r.devices) || []).forEach(d => {
        const o = document.createElement('option');
        o.value = d.name;
        o.textContent = d.name + (d.is_default ? '  ·  当前系统默认' : '');
        if (d.name === cur) { o.selected = true; matched = true; }
        sel.appendChild(o);
      });
      if (!matched && cur) {
        const miss = document.createElement('option');
        miss.value = cur; miss.textContent = cur + '  ·  当前未连接';
        miss.selected = true; sel.appendChild(miss);
      }
      applyDevice();
      startPolling();
    }).catch(e => { errBox.textContent = '列设备失败：' + e; });
  });

  sel.addEventListener('change', () => {
    const v = sel.value;
    window.pywebview.api.save({[f.key]: v}).then(r => { toast(r); if (r.ok) checkDirty(f.key, v); });
    applyDevice();
  });

  function startPolling() {
    if (micState.pollTimer) return;
    micState.pollTimer = setInterval(() => {
      window.pywebview.api.mic_meter_read().then(r => {
        if (!r) return;
        // 把 0..0.3 的 RMS 映射到 0..100%（0.3 已经是很响的音量，别让常规讲话只推到 10% 看不出跳动）
        const w = Math.min(1, (r.rms || 0) / 0.3) * 100;
        bar.style.width = w.toFixed(1) + '%';
        // 峰值线：追新峰，然后按 ~1.5s/满宽 的速度回落
        if (w > micState.peakLeft) micState.peakLeft = w;
        else micState.peakLeft = Math.max(w, micState.peakLeft - 4);
        peak.style.left = micState.peakLeft.toFixed(1) + '%';
        if (r.err) errBox.textContent = '探针错误：' + r.err;
      });
    }, 80);
  }
}

function renderCategories(host, cats) {
 cats.forEach((cat, ci) => {
  const zone = document.createElement('div'); zone.className = 'zone z' + (ci % 3);
  const sec = document.createElement('div'); sec.className = 'sec';
  sec.innerHTML = '<h2></h2>' + (cat.caption ? '<p></p>' : '');
  sec.querySelector('h2').textContent = cat.title;
  if (cat.caption) sec.querySelector('p').textContent = cat.caption;
  zone.appendChild(sec);
  cat.fields.forEach(f => {
   try {
    const row = document.createElement('div'); row.className = 'cmd-card';
    row.innerHTML = '<div class="hd"><span class="label"></span>'
      + (f.desc ? '<span class="desc"></span>' : '') + '</div>';
    row.querySelector('.label').textContent = f.label;
    if (f.desc) row.querySelector('.desc').textContent = f.desc;
    if (f.type === 'list') { buildChips(row, f); zone.appendChild(row); return; }
    if (f.type === 'wordlist') { buildWordlist(row, f); zone.appendChild(row); return; }
    if (f.type === 'mic') { buildMic(row, f); zone.appendChild(row); return; }
    let ctl, mount;
    if (f.type === 'bool') {
      ctl = document.createElement('label'); ctl.className = 'sw';
      ctl.innerHTML = '<input type="checkbox"><span></span>';
      ctl.querySelector('input').checked = !!f.value;
      mount = ctl;
    } else if (f.options) {  // select：options 归一为 [{v,d}]，右侧同行显示当前选项释义
      ctl = document.createElement('select');
      f.options.forEach(o => {
        const op = document.createElement('option');
        op.value = o.v; op.textContent = o.label || o.v; op.dataset.d = o.d || '';
        if (o.v === f.value) op.selected = true;
        ctl.appendChild(op);
      });
      const optDesc = document.createElement('span'); optDesc.className = 'opt-desc';
      const upd = () => { const sel = ctl.options[ctl.selectedIndex];
        optDesc.textContent = sel ? sel.dataset.d : ''; };
      ctl.addEventListener('change', upd); upd();
      mount = document.createElement('div'); mount.className = 'ctl-row';
      mount.appendChild(ctl); mount.appendChild(optDesc);
    } else {
      ctl = document.createElement('input');
      ctl.type = (f.type === 'int' || f.type === 'float') ? 'number' : 'text';
      if (f.type === 'float') ctl.step = 'any'; ctl.value = f.value;
      mount = ctl;
    }
    ctl.dataset.key = f.key; ctl.dataset.type = f.type;
    const field = f.type === 'bool' ? ctl.querySelector('input') : ctl;
    originalValues[f.key] = f.type === 'bool' ? !!f.value : String(f.value);
    field.addEventListener('change', () => autosave(ctl));
    row.appendChild(mount); zone.appendChild(row);
   } catch (e) { console.error('render field failed', f && f.key, e); }
  });
  host.appendChild(zone);
 });
}
renderCategories(ps, DATA.settings.settings);
renderCategories(document.getElementById('p-hotwords'), DATA.settings.hotwords);
function autosave(ctl) {
  const val = ctl.dataset.type === 'bool' ? ctl.querySelector('input').checked : ctl.value;
  window.pywebview.api.save({[ctl.dataset.key]: val}).then(r => { toast(r); if (r.ok) checkDirty(ctl.dataset.key, val); })
    .catch(() => toast({ok: false, msg: '保存失败：桥接未就绪'}));
}
function toast(r) {
  const t = document.getElementById('toast');
  t.textContent = r.msg; t.className = 'toast show' + (r.ok ? '' : ' err');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), 2000);
}

/* —— 需重启提示：只有当前值偏离打开窗口时的原值才算“待重启”，改回去了就不提示 ——
   热词（wordlist）改动实时生效，不进这里 —— */
let dirty = false;
const changedKeys = new Set();
function checkDirty(key, val) {
  if (noRestartKeys.has(key)) return;
  if (JSON.stringify(val) === JSON.stringify(originalValues[key])) changedKeys.delete(key);
  else changedKeys.add(key);
  dirty = changedKeys.size > 0;
  document.getElementById('restartBar').classList.toggle('show', dirty);
}
function restartNow() {
  const bad = emptyChipField();
  if (bad) { blockClose(bad); return; }
  window.pywebview.api.restart().then(() => { dirty = false; close_(); });
}
function emptyChipField() {
  for (const key in chipState) { if (chipState[key].words.length === 0) return chipState[key]; }
  return null;
}
function blockClose(field) {
  activate('settings');
  toast({ok: false, msg: `「${field.label}」至少保留一个词，请先添加再关闭`});
}

/* —— 历史记录 —— */
const ph = document.getElementById('p-history');
const seg = document.createElement('div'); seg.className = 'seg';
seg.innerHTML = '<button data-k="voice" class="on">语音输入</button><button data-k="reminder">提醒变更</button>';
ph.appendChild(seg);
const list = document.createElement('div'); ph.appendChild(list);
function renderHistory(kind) {
  seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.k === kind));
  list.innerHTML = '';
  const rows = kind === 'voice' ? DATA.history.voice : DATA.history.reminder;
  if (!rows.length) { list.innerHTML = '<div class="empty">还没有记录</div>'; return; }
  rows.forEach(e => {
    const card = document.createElement('div'); card.className = 'card';
    const head = document.createElement('div'); head.className = 'chead';
    const time = document.createElement('span'); time.className = 'time'; time.textContent = e.t;
    head.appendChild(time);
    const right = document.createElement('div'); right.className = 'cright';
    if (kind === 'reminder') {
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = e.action;
      right.appendChild(tag);
    }
    const acts = document.createElement('div'); acts.className = 'cacts';
    const copyBtn = document.createElement('button'); copyBtn.title = '复制';
    copyBtn.innerHTML = _ICO_COPY;
    copyBtn.onclick = () => copyText(e.text);
    const delBtn = document.createElement('button'); delBtn.className = 'del'; delBtn.title = '删除';
    delBtn.innerHTML = _ICO_TRASH;
    delBtn.onclick = () => deleteEntry(kind, e, card);
    acts.appendChild(copyBtn); acts.appendChild(delBtn);
    right.appendChild(acts); head.appendChild(right);
    const text = document.createElement('div'); text.className = 'ctext'; text.textContent = e.text;
    card.appendChild(head); card.appendChild(text); list.appendChild(card);
  });
}
const _ICO_COPY = '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/>'
  + '<path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
const _ICO_TRASH = '<svg viewBox="0 0 24 24"><path d="M3 6h18"/>'
  + '<path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>'
  + '<path d="M6 6v13a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V6"/></svg>';
function copyText(s) {
  const done = () => toast({ok: true, msg: '已复制'});
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(s).then(done).catch(() => fallbackCopy(s, done));
  } else { fallbackCopy(s, done); }
}
function fallbackCopy(s, done) {
  const ta = document.createElement('textarea'); ta.value = s;
  ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); }
  catch (_) { toast({ok: false, msg: '复制失败'}); }
  document.body.removeChild(ta);
}
function deleteEntry(kind, e, card) {
  window.pywebview.api.delete_history(kind, e.t, e.text).then(r => {
    toast(r);
    if (r.ok) card.remove();
  }).catch(() => toast({ok: false, msg: '删除失败：桥接未就绪'}));
}
seg.querySelectorAll('button').forEach(b => b.onclick = () => renderHistory(b.dataset.k));
renderHistory('voice');

/* —— 导入 —— */
const pi = document.getElementById('p-import');
pi.innerHTML =
  '<div class="imp"><div class="label">导入文件或图片到提醒</div>' +
  '<p>选一个文件或截图，自动抽出里面的文字并交给提醒助手建提醒。' +
  '支持 txt / docx / xlsx / pptx，以及 png / jpg / bmp / webp 截图（OCR）。</p>' +
  '<button class="primary" id="pickBtn">选择文件 / 图片…</button>' +
  '<div class="impmsg" id="impMsg"></div></div>';
document.getElementById('pickBtn').onclick = () => {
  const msg = document.getElementById('impMsg');
  msg.textContent = '正在打开选择框…'; msg.className = 'impmsg';
  window.pywebview.api.pick_import().then(r => {
    msg.textContent = r.msg; msg.className = 'impmsg' + (r.ok ? '' : ' err');
  }).catch(() => { msg.textContent = '导入失败：桥接未就绪'; msg.className = 'impmsg err'; });
};

activate(DATA.tab);

function close_() {
  const bad = emptyChipField();
  if (bad) { blockClose(bad); return; }
  // 关窗不等于退出程序（后台仍在托盘运行）——提示走系统托盘气泡（见 api.close），这里不弹
  // 窗口内提示：窗口马上就没了，用户根本看不到。
  if (dirty) {
    dirty = false;  // 别在 api.restart 的 promise 落地前被再点一次触发第二回
    window.pywebview.api.restart().finally(() => window.pywebview.api.close());
  } else {
    window.pywebview.api.close();
  }
}
function minimize_() { window.pywebview.api.minimize(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') close_(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    if len(sys.argv) > 6:  # 由 show() 拉起：config, history_dir, reminders_log, import_trigger, restart_trigger, tab
        _run_gui(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
        sys.exit(0)

    # 无参 = 自检：HTML 内嵌与数据拼装（不起 GUI）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cf = Path(td) / "config.json"
        cf.write_text('{"wake_words":["你好"],"send_words":["发送"]}', encoding="utf-8")
        html = _build_html(json.loads(cf.read_text(encoding="utf-8")), td, td,
                           Path(td) / "reminders_log.jsonl", "history")
        assert "__DATA__" not in html and "你好" in html and '"tab": "history"' in html
    print("main_window 自检通过")
