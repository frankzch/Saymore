"""屏幕右下角悬浮小圆：按可用空间展开标题栏＋缓存文字，位置与轮廓平滑变形。

Pillow 超采样抗锯齿渲染 → Win32 分层窗口逐像素 alpha 贴图；主线程跑消息循环。
从 voice_input.py 拆出——只靠传入的 state 字典与主程序通信。
"""
import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

import saymore.ui.panel as panel
from saymore.ui.capsule import (CapsuleText, Morph, default_position, draw_capsule,
                                expansion_direction, expansion_bounds, expansion_offset)
from saymore.ui.capsule_toolbar import HEADER_HEIGHT, MIN_WIDTH, button_rects, draw_toolbar, hit_button
import saymore.ui.tray as tray
import saymore.ui.style as ui_style
from saymore.paths import CONFIG_PATH

_ICON_PATH = CONFIG_PATH.parent / "saymore.ico"


# 悬浮窗几何/配色（供 _render_overlay 与 run_overlay 共用）
_OV_D = 58           # 控件逻辑尺寸(px)
_OV_SS = 4           # 超采样倍率：先放大 4× 画再缩小，得到抗锯齿平滑边缘
_CAT_FRAMES = None   # 猫姿势帧缓存（首次渲染惰性加载并预处理）
_OV_TH = 22          # 悬浮窗顶部 token 飘字条高度(px)；猫位置不变，窗口整体上移这么多
_OV_RIGHT_GAP = 40   # 默认距主屏工作区右边缘
_OV_BOTTOM_GAP = 90  # 默认距主屏工作区下边缘


def _overlay_position(work_area, offset=None, window_size=None):
    """按距工作区右下角的偏移计算窗口左上角，并保证窗口仍在工作区内。"""
    left, top, right, bottom = work_area
    right_gap, bottom_gap = offset or (_OV_RIGHT_GAP, _OV_BOTTOM_GAP)
    width, height = window_size or (_OV_D, _OV_D + _OV_TH)
    x = right - width - int(right_gap)
    y = bottom - height - int(bottom_gap)
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


def _dpi_scale(dpi):
    """把 Windows 显示 DPI 换算成逻辑像素倍率，不根据某个具体分辨率猜尺寸。"""
    return max(0.75, min(3.0, dpi / 96))


def _panel_font_size(configured_px, dpi, system_px):
    """默认直接采用系统字体实际像素；显式配置时才把逻辑像素按 DPI 换算。"""
    return system_px if configured_px is None else max(1, round(configured_px * dpi / 96))


def _system_message_font_px(user32):
    """读取 Windows NONCLIENTMETRICS 中已经按当前 DPI 换算好的默认消息字体高度。"""
    class LOGFONTW(ctypes.Structure):
        _fields_ = [("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
                    ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
                    ("lfWeight", wintypes.LONG), ("lfItalic", ctypes.c_ubyte),
                    ("lfUnderline", ctypes.c_ubyte), ("lfStrikeOut", ctypes.c_ubyte),
                    ("lfCharSet", ctypes.c_ubyte), ("lfOutPrecision", ctypes.c_ubyte),
                    ("lfClipPrecision", ctypes.c_ubyte), ("lfQuality", ctypes.c_ubyte),
                    ("lfPitchAndFamily", ctypes.c_ubyte), ("lfFaceName", ctypes.c_wchar * 32)]

    class NONCLIENTMETRICSW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("iBorderWidth", ctypes.c_int),
                    ("iScrollWidth", ctypes.c_int), ("iScrollHeight", ctypes.c_int),
                    ("iCaptionWidth", ctypes.c_int), ("iCaptionHeight", ctypes.c_int),
                    ("lfCaptionFont", LOGFONTW), ("iSmCaptionWidth", ctypes.c_int),
                    ("iSmCaptionHeight", ctypes.c_int), ("lfSmCaptionFont", LOGFONTW),
                    ("iMenuWidth", ctypes.c_int), ("iMenuHeight", ctypes.c_int),
                    ("lfMenuFont", LOGFONTW), ("lfStatusFont", LOGFONTW),
                    ("lfMessageFont", LOGFONTW), ("iPaddedBorderWidth", ctypes.c_int)]

    metrics = NONCLIENTMETRICSW()
    metrics.cbSize = ctypes.sizeof(metrics)
    if user32.SystemParametersInfoW(0x0029, metrics.cbSize, ctypes.byref(metrics), 0):
        return max(1, abs(metrics.lfMessageFont.lfHeight))
    return 16


def _get_cat_frames():
    """从 saymore/ui/assets/ 加载猫的姿势 PNG，按 alpha 边界裁切、缩放居中到超采样画布，缓存返回。
    返回 {状态名: 超采样 RGBA 图} 的 dict；缺图则返回 None（退回纯进度环）。"""
    global _CAT_FRAMES
    if _CAT_FRAMES is not None:
        return _CAT_FRAMES or None
    from PIL import Image
    SD = _OV_D * _OV_SS
    d = Path(__file__).parent / "assets"

    def load(name, vstretch=1.0):
        im = Image.open(d / name).convert("RGBA")
        box = im.getbbox()  # 裁掉透明边
        if box:
            im = im.crop(box)
        if vstretch != 1.0:  # 纵向微拉伸做"呼吸"第二帧
            im = im.resize((im.width, int(im.height * vstretch)), Image.LANCZOS)
        return im

    def place(im, scale, baseline):
        """按统一 scale 缩放，并让猫脚底对齐到 baseline 高度（底对齐，保证摇头/点头时身体不缩放、不跳动）。"""
        w2, h2 = max(1, int(im.width * scale)), max(1, int(im.height * scale))
        im = im.resize((w2, h2), Image.LANCZOS)
        canvas = Image.new("RGBA", (SD, SD), (0, 0, 0, 0))
        canvas.alpha_composite(im, ((SD - w2) // 2, int(SD * baseline) - h2))
        return canvas

    def base_width(im):
        """坐姿猫底部 30% 区域的不透明宽度（=坐着的身/腿宽），作为身体大小基准，
        避免转头/竖耳让 bbox 高度乱跳导致身体被缩放得忽大忽小。"""
        import numpy as np
        a = np.asarray(im)[:, :, 3]
        base = a[int(a.shape[0] * 0.7):, :]
        cols = np.where(base.max(0) > 20)[0]
        return (cols.max() - cols.min() + 1) if len(cols) else im.width

    try:
        # 坐姿帧按"底部身宽"归一化 + 同一脚底基线 → 身体大小一致、不跳动
        sit = {"stand": load("stand_t.png"), "lookL": load("look_left_t.png"),
               "headdown": load("head_down_t.png"),
               "blink": load("blink_t.png"), "ear_tilt": load("ear_back_v2_t.png"),
               "mouth": load("mouth_open_t.png")}
        tgt = SD * 0.36  # 目标底部身宽
        f = {k: place(v, tgt / base_width(v), 0.86) for k, v in sit.items()}
        f["lookR"] = f["lookL"].transpose(Image.FLIP_LEFT_RIGHT)  # 看右=看左镜像
        sl = load("sleep_t.png")
        sleep_scale = (SD * 0.66) / max(sl.width, sl.height)  # 趴睡单独适配（不与坐姿同框）
        f["sleep0"] = place(sl, sleep_scale, 0.80)
        f["sleep1"] = place(load("sleep_t.png", vstretch=1.06), sleep_scale, 0.80)
        _CAT_FRAMES = f
    except Exception as e:
        print(f"[warn] 猫帧加载失败，退回纯进度环: {e}")
        _CAT_FRAMES = {}
    return _CAT_FRAMES or None




_BORDER_RGB = (52, 168, 83)  # 小圆与缓存窗口统一 1px 绿描边，在浅薄荷底和白底应用上都框得住


def _downloading_hint(runtime):
    """未就绪时的面板文案：把所有网络下载项进度聚合成一句"正在下载模型文件 (X%)"。
    没提示具体哪个文件——用户不关心 gguf 文件名，只关心什么时候能用。"""
    try:
        from saymore import downloader
    except Exception:  # noqa: BLE001 兜底：downloader 未导入时静默
        return "运行环境未就绪。"
    missing = [m for m in (runtime.get("missing") or []) if m.get("network")]
    if not missing:
        # 缺的都是安装包内置项：无法自愈，提示重装
        return "运行环境组件缺失，请重装 Saymore。"
    tasks = downloader.progress()
    # 按 size_mb 加权聚合进度：大文件更能体现真实完成度
    total_mb = sum(m.get("size_mb", 1) for m in missing) or 1
    done_mb = 0.0
    any_running = False
    any_error = False
    any_paused = False
    for m in missing:
        t = tasks.get(m["key"], {})
        state = t.get("state", "idle")
        size = m.get("size_mb", 1)
        if state == "done":
            done_mb += size
        elif state == "running":
            any_running = True
            if t.get("total"):
                done_mb += size * (t.get("done", 0) / max(1, t.get("total", 1)))
        elif state == "error":
            any_error = True
        elif state == "cancelled":
            any_paused = True
    pct = done_mb / total_mb * 100
    if any_running:
        return f"正在下载模型文件 ({pct:.1f}%)，请稍候…"
    if any_error:
        return f"模型下载失败（{pct:.0f}% 已完成）。右键小圆环 → 主界面 → 运行环境 重试。"
    if any_paused:
        return f"模型下载已暂停（{pct:.0f}% 已完成）。右键小圆环 → 主界面 → 运行环境 继续。"
    # 全部 idle：还没启动（配置无源等边界情况）
    return "运行环境未就绪：正在准备下载…"




def run_overlay(state):
    """单一视觉轮廓承载小猫与缓存文字；原生文字区在动画结束后恢复交互。
    不激活窗口，右键打开菜单，拖动小猫保存位置；状态仅通过 state 交换。
    """
    from ctypes import wintypes

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    try:
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        dpi = u.GetDpiForSystem()
    except (AttributeError, OSError):
        dpi = 96
    ui_scale = _dpi_scale(dpi)
    scaled = lambda px: max(1, round(px * ui_scale))
    # 旧配置常留有 72px；新版统一缩小，显式更小的配置仍保留。
    D = scaled(min(state.get("overlay_size", 48), 48))
    gc = state.get("glass_cfg", {})
    max_panel_h = scaled(gc.get("max_h", 240))
    expanded_w = max(scaled(MIN_WIDTH), scaled(gc.get("width", panel.PANEL_W)) + scaled(24))
    header_h = scaled(HEADER_HEIGHT)
    idle_cat = scaled(44)  # 猫帧四周有透明边，44 在 48 的小圆里约占八成高，再大耳朵会碰到描边

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    P = ctypes.c_void_p   # 通用句柄/指针类型，避免 64 位被默认 c_int 截断
    HWND, DWORD, UINT, LPCWSTR = wintypes.HWND, wintypes.DWORD, wintypes.UINT, wintypes.LPCWSTR
    INT = ctypes.c_int
    k = ctypes.windll.kernel32
    for fn, res, args in [
        (u.GetDC, P, [HWND]),
        (u.ReleaseDC, INT, [HWND, P]),
        (g.CreateCompatibleDC, P, [P]),
        (g.CreateDIBSection, P, [P, P, UINT, ctypes.POINTER(P), P, DWORD]),
        (g.SelectObject, P, [P, P]),
        (g.DeleteObject, INT, [P]),
        (g.DeleteDC, INT, [P]),
        (u.DefWindowProcW, ctypes.c_ssize_t, [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (u.UpdateLayeredWindow, INT, [HWND, P, P, P, P, P, DWORD, P, DWORD]),
        (u.CreateWindowExW, HWND,
         [DWORD, LPCWSTR, LPCWSTR, DWORD, INT, INT, INT, INT, HWND, P, P, P]),
        (u.ShowWindow, INT, [HWND, INT]),
        (u.SetWindowPos, INT, [HWND, HWND, INT, INT, INT, INT, UINT]),
        (u.SetTimer, P, [HWND, P, UINT, P]),
        (u.KillTimer, INT, [HWND, P]),
        (u.DestroyWindow, INT, [HWND]),
        (u.GetMessageW, INT, [P, HWND, UINT, UINT]),
        (u.TranslateMessage, INT, [P]),
        (u.DispatchMessageW, ctypes.c_ssize_t, [P]),
        (u.RegisterClassW, wintypes.ATOM, [P]),
        (u.LoadCursorW, P, [P, P]),
        (u.CreatePopupMenu, P, None),
        (u.AppendMenuW, INT, [P, UINT, P, LPCWSTR]),
        (u.TrackPopupMenu, INT, [P, UINT, INT, INT, INT, HWND, P]),
        (u.DestroyMenu, INT, [P]),
        (u.GetCursorPos, INT, [P]),
        (u.SystemParametersInfoW, INT, [UINT, UINT, P, UINT]),
        (u.SetCapture, HWND, [HWND]),
        (u.ReleaseCapture, INT, None),
        (u.SetForegroundWindow, INT, [HWND]),
        (u.PostQuitMessage, None, [INT]),
        (u.GetSystemMetrics, INT, [INT]),
        (k.GetModuleHandleW, P, [LPCWSTR]),
    ]:
        fn.restype = res
        if args is not None:
            fn.argtypes = args

    system_font_px = _system_message_font_px(u)
    W = H = D  # 保存位置以小猫为锚点，不随胶囊尺寸变化

    def get_work_area():
        """主屏工作区（排除任务栏）；API 失败时退回整块主屏。"""
        rect = wintypes.RECT()
        if u.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
        return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)

    work_area = get_work_area()
    # 新布局独立保存锚点，旧版右下角位置不把新版胶囊带回屏幕边缘。
    def initial_position():
        offset = state.get("capsule_offset")
        if isinstance(offset, (list, tuple)) and len(offset) == 2:
            return _overlay_position(work_area, tuple(round(v * ui_scale) for v in offset), (W, H))
        return default_position(work_area, D, scaled(24))

    pos_x, pos_y = initial_position()

    def blit(target, img, x, y):
        """把 RGBA 图预乘 alpha 后经 UpdateLayeredWindow 逐像素贴到 target 窗口的屏幕 (x,y)。"""
        w, h = img.size
        arr = np.asarray(img).astype(np.uint16)
        a = arr[:, :, 3:4]
        arr[:, :, 0:3] = arr[:, :, 0:3] * a // 255
        bgra = arr.astype(np.uint8)[:, :, [2, 1, 0, 3]].tobytes()
        screen = u.GetDC(None)
        memdc = g.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # 负 = 顶向下，与图像行序一致
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = g.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra, len(bgra))
        old = g.SelectObject(memdc, hbmp)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        size = SIZE(w, h)
        ptdst = wintypes.POINT(x, y)
        ptsrc = wintypes.POINT(0, 0)
        u.UpdateLayeredWindow(target, screen, ctypes.byref(ptdst), ctypes.byref(size),
                              memdc, ctypes.byref(ptsrc), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
        g.SelectObject(memdc, old)
        g.DeleteObject(hbmp)
        g.DeleteDC(memdc)
        u.ReleaseDC(None, screen)

    idle_shape = (D, D, D / 2, 0, 0, (D - idle_cat) / 2, (D - idle_cat) / 2, idle_cat)
    morph = Morph(idle_shape)
    _last_img = [None]
    _direction = [None]
    _buttons = [{}]
    _pressed = [None]
    _copied_until = [0.0]  # 点复制后标题栏显示"已复制"到此时刻
    _folded = [False]      # 点猫收起：保留缓存只缩回小圆，再点猫展开
    _cat_rect = [(0, 0, 0, 0)]  # 当前帧猫在窗口内的位置，供点击命中
    _speaking = [False]
    _last_warn = [""]
    _timer_ms = [80]

    def render():
        """渲染当前状态并通过 UpdateLayeredWindow 贴到屏幕（逐像素 alpha）。"""
        now = time.time()
        # 提醒模式下「正在识别 / 已识别待攒句」也算忙碌：从话音落就显示，填满识别那几秒的空等
        reminder_processing = state["mode"] == "reminder" and (
            state.get("rbuf") or state["status"] == "transcribing")
        busy = state.get("llm_busy") or reminder_processing
        warming = state.get("warming")
        # 运行环境未就绪（后台静默下载中）：视觉等价于"启动初始化中"——圆环纯色不动，猫静止。
        # 只是面板里的 hint/warn 由下方分支覆盖成下载进度，让用户唤醒后看得懂。
        runtime = state.get("runtime") or {}
        not_ready = runtime.get("ready") is False
        if not_ready:
            warming = True
        frames = _get_cat_frames()
        cat = None
        if frames:
            if warming:                                           # 冷启动中：完全静止，代表停止状态
                cat = frames["stand"]
            else:
                import saymore.tts as _tts_mod
                if _tts_mod.playing:                              # TTS 播报中：张嘴↔闭嘴
                    cat = frames["mouth" if int(now * 4) % 2 else "stand"]
                elif busy:                                        # 调大模型/识别中：快速低头↔抬头
                    cat = frames["headdown" if int(now * 5) % 2 else "stand"]
                elif state["status"] in ("awake", "transcribing"):
                    lv = state["levels"][-6:]
                    if now < state.get("nod_until", 0):           # 执行命令：点头
                        cat = frames["headdown" if int(now * 4) % 2 else "stand"]
                    elif lv and max(lv) > 0.02:                   # 用户说话：眨眼+动耳朵
                        cycle = now % 2.0                         # 2秒一轮（眨眼）
                        ear_cycle = now % 0.5                     # 0.5秒一轮（耳朵，比上一版再快一倍）
                        if cycle < 0.12:                          # 0~0.12s 眨眼（快闪）
                            cat = frames["blink"]
                        elif ear_cycle < 0.25:                     # 前一半耳朵后收，后一半立起，来回明显
                            cat = frames["ear_tilt"]
                        else:
                            cat = frames["stand"]
                    else:                                         # 聆听待命：偶尔眨眼
                        cat = frames["blink"] if now % 4.0 < 0.12 else frames["stand"]
                else:                                             # 休眠：趴下睡觉
                    cat = frames["sleep1" if int(now * 1.1) % 2 else "sleep0"]
        if state["mode"] == "sleep" and glass.editing:
            # 编辑到一半就静默超时/说了休眠词进休眠：缓存已被 enter_sleep 清空，但编辑态若不
            # 一并退出，update() 会因 self.editing 跳过刷新，面板就卡在编辑界面不随之隐藏。
            glass.cancel_edit()
        pbuf = state.get("panel")
        clean, raw, low_conf = pbuf.text_parts if pbuf is not None else ("", "", False)
        # 运行环境未就绪：面板只显示红色告警，正文/hint 全清空；用户去主界面「运行环境」tab 补齐
        if not_ready:
            clean, raw, low_conf = "", "", False
        # 状态提示靠 hint 颜色（蓝灰）跟绿色正文分开；整理状态不区分模式，一律"整理中"
        hint = ""
        dots = "…"  # 固定提示宽度，避免无正文时胶囊跟随省略号反复伸缩
        if state["mode"] == "awake":
            if state.get("warming"):
                elapsed = now - state.get("warming_start", now)
                remain = max(0, 10 - int(elapsed))
                hint = f"正在启动推理引擎并加载模型…剩余 {remain} 秒"
            elif state.get("speaking"):
                hint = f"正在说话{dots}"
            elif state["status"] == "transcribing":
                hint = f"正在识别{dots}"
            elif pbuf is not None and pbuf.cleaning_mode:
                hint = f"正在整理{dots}"
        # 警告行（红色，"没找到输入框"等），到期自动消失
        warn_entry = state.get("warn")
        warn_text = ""
        if warn_entry:
            text, expire = warn_entry
            if now < expire:
                warn_text = text
            else:
                state["warn"] = None  # 到期一次性清掉，别每帧再判
        # 未就绪时：面板显示后台下载进度（不列文件名，用户不关心具体是哪个 gguf）
        if not_ready:
            hint = ""
            warn_text = _downloading_hint(runtime)
        if state["mode"] == "sleep":
            _folded[0] = False  # 收起只管本次唤醒；下次唤醒的启动提示等要照常弹出
            glass.place(0, 0, False)
            return
        if (state.get("speaking") and not _speaking[0]) or (warn_text and warn_text != _last_warn[0]):
            _folded[0] = False  # 收起后又开口说话/来了新警告：自动展开，别让新内容藏着
        _speaking[0], _last_warn[0] = bool(state.get("speaking")), warn_text
        anchor = (pos_x, pos_y)
        _direction[0] = expansion_direction(work_area, anchor, D, (expanded_w, max_panel_h), _direction[0])
        room_w, room_h = expansion_bounds(work_area, anchor, D, _direction[0])
        available_w = max(D, min(expanded_w, room_w))
        available_h = max(D, min(max_panel_h, room_h))
        glass.prepare(clean, raw, hint, warn_text, low_conf,
                      max(scaled(24), available_h - header_h - scaled(16)), available_w - scaled(24))
        expanded = glass.present and not _folded[0]
        text_xy = (scaled(12), header_h + scaled(4))
        if expanded:
            width = min(available_w, max(scaled(MIN_WIDTH), glass.w + scaled(24)))
            height = min(available_h, max(D, header_h + glass.h + scaled(16)))
            dx, dy = expansion_offset(work_area, anchor, D, (width, height), _direction[0])
            target = (width, height, scaled(16), dx, dy, scaled(8), scaled(3), scaled(30))
        else:
            target = idle_shape
        shape = morph.move(target, time.monotonic())
        size, dx, dy, cat_x, cat_y, cat_size = shape[:3], *shape[3:]
        progress = max(0, min(1, (size[0] - D) / max(1, (target[0] if expanded else morph.start[0]) - D)))
        rgb = tuple((glass.bg_color >> shift) & 255 for shift in (0, 8, 16))
        img = draw_capsule(size, D, cat, rgb, _BORDER_RGB,
                           glass.snapshot if morph.moving else None, text_xy, progress,
                           (cat_x, cat_y), cat_size, scaled(1))
        # 切换方向的动画途中也不能越过工作区（尤其是拖到另一个角落时）。
        wx = max(work_area[0], min(pos_x + round(dx), work_area[2] - img.width))
        wy = max(work_area[1], min(pos_y + round(dy), work_area[3] - img.height))
        _buttons[0] = {}
        _cat_rect[0] = (cat_x, cat_y, cat_x + cat_size, cat_y + cat_size)
        if progress > 0.65:
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            hover = hit_button(button_rects(img.width, ui_scale), pt.x - wx, pt.y - wy)
            rects = draw_toolbar(img, ui_scale, hover, bool(clean or raw), glass.editing,
                                 bool(state.get("panel_sending")), (progress - 0.65) / 0.35,
                                 notice="已复制" if now < _copied_until[0] else "")
            if expanded and not morph.moving:
                _buttons[0] = rects
        _last_img[0] = img
        blit(hwnd, img, wx, wy)
        glass.place(wx + text_xy[0], wy + text_xy[1], expanded and not morph.moving)
        # Windows 定时器常为 15.6ms 粒度；请求 16ms 容易进位到约 31ms。
        interval = 15 if morph.moving else 80
        if interval != _timer_ms[0]:
            u.SetTimer(hwnd, 1, interval, None)
            _timer_ms[0] = interval

    def open_main():
        """拉起主界面（圆环右键菜单、托盘左键/菜单共用）。
        运行环境未就绪（下载中）时默认落到"运行环境"tab，让用户看得到进度；否则回默认设置 tab。"""
        import saymore.ui.main_window as main_window
        _rt = state.get("runtime") or {}
        _tab = "runtime" if _rt.get("ready") is False else "settings"
        try:
            main_window.show(CONFIG_PATH, state["history_file"],
                             state["reminders_log"], state["import_trigger"],
                             state["restart_trigger"], tab=_tab)
        except Exception as e:
            print(f"[warn] 打不开主界面：{e}")

    def show_menu():
        """小圆环右键菜单：只两项——主界面 / 退出程序（设置/历史/导入全收进主界面 tab）。
        Apple 风自绘弹窗（ui_style），后台线程跑 tkinter 不阻塞消息循环。"""
        import saymore.ui.style as ui_style
        items = [("主界面", open_main),
                 None,
                 ("退出程序", lambda: state.__setitem__("quit", True), "danger")]

        def run_menu():
            # 菜单开着时先关掉圆环的每帧置顶重申，否则 80ms 后圆环会把菜单盖回去
            state["menu_open"] = True
            try:
                ui_style.popup_menu(items)
            finally:
                state["menu_open"] = False

        threading.Thread(target=run_menu, daemon=True).start()

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    # 拖动状态：按下记起点(屏幕光标+当时窗口左上角)，移动时按增量挪窗，抬起落盘记住位置。
    _drag = {"on": False, "gx": 0, "gy": 0, "px": 0, "py": 0}
    _tray = [None]  # TrayIcon 实例（建窗后回填）；退出时移除，避免残留死图标
    _visible = [True]  # 休眠时隐藏悬浮窗只留托盘图标，避免无事可做时还悬着一个圆环

    def save_pos(x, y):
        """保存窗口距工作区右下角的距离，换显示器/分辨率后仍保持相对位置。"""
        _, _, right, bottom = work_area
        offset = [round((right - (x + W)) / ui_scale),
                  round((bottom - (y + H)) / ui_scale)]
        state["capsule_offset"] = offset
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            cfg["capsule_offset"] = offset
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[warn] 记住小圆窗位置失败：{e}")

    def perform_action(action):
        pbuf = state.get("panel")
        if glass.editing:
            if action == "edit":  # 编辑中其余键已变灰不响应；再点编辑键＝保存并退出
                glass._end_edit(save=True)
        elif pbuf is not None and not state.get("panel_sending"):
            if action == "copy":
                import pyperclip
                text = "".join(pbuf.text_parts[:2])
                if text:
                    try:
                        pyperclip.copy(text)
                        _copied_until[0] = time.time() + 1.5
                    except Exception as exc:
                        print(f"[warn] 复制缓存失败：{exc}")
                        state["warn"] = ("复制失败，请稍后重试。", time.time() + 5)
            elif action == "edit" and (pbuf.text_parts[0] or pbuf.text_parts[1]):
                glass._start_edit()
            elif action == "polish" and not pbuf.cleaning_mode:
                # 与语音"文本整理"同一入口；要调本地模型好几秒，放后台线程别卡住界面，
                # 期间面板底部照常显示"正在整理…"
                threading.Thread(target=pbuf.trigger_polish_now, daemon=True).start()
            elif action == "send" and state.get("send_panel"):
                if pbuf.text_parts[0] or pbuf.text_parts[1]:
                    state["send_panel"]()
        state["last_activity"] = time.time()
        render()

    def wndproc(hwnd, msg, wp, lp):
        nonlocal pos_x, pos_y, work_area
        if msg == tray.MSG:      # 托盘图标回调：左键/双击拉起主界面，右键弹菜单
            if _tray[0] is not None:
                _tray[0].handle(lp)
            return 0
        if msg == 0x0205:        # WM_RBUTTONUP
            show_menu()
            return 0
        if msg == 0x0021:        # MA_NOACTIVATE：工具按钮不能抢走原输入框焦点。
            return 3
        if msg == 0x0201:        # WM_LBUTTONDOWN：按钮按下或拖动标题栏
            x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
            _pressed[0] = hit_button(_buttons[0], x, y)
            if _pressed[0]:
                u.SetCapture(hwnd)
                return 0
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            _drag.update(on=True, gx=pt.x, gy=pt.y, px=pos_x, py=pos_y)
            u.SetCapture(hwnd)   # 捕获鼠标，拖出窗口也能收到移动/抬起
            return 0
        if msg == 0x0200:        # WM_MOUSEMOVE：拖动中按光标增量挪窗
            if _drag["on"]:
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                pos_x = max(work_area[0], min(work_area[2] - D, _drag["px"] + (pt.x - _drag["gx"])))
                pos_y = max(work_area[1], min(work_area[3] - D, _drag["py"] + (pt.y - _drag["gy"])))
                if _last_img[0] is not None:
                    render()  # 背景与原生文字一起移动
            return 0
        if msg == 0x0202:        # WM_LBUTTONUP：同一图标内松开才触发；拖动不误点。
            if _pressed[0]:
                action, _pressed[0] = _pressed[0], None
                u.ReleaseCapture()
                x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
                if hit_button(_buttons[0], x, y) == action:
                    perform_action(action)
                return 0
            if _drag["on"]:
                _drag["on"] = False
                u.ReleaseCapture()
                if (pos_x, pos_y) != (_drag["px"], _drag["py"]):  # 真挪动过才写盘（纯点击不写）
                    save_pos(pos_x, pos_y)
                else:  # 纯点击猫：有缓存时收起/展开切换
                    x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
                    l, t, r, b = _cat_rect[0]
                    if glass.present and l <= x < r and t <= y < b:
                        if not _folded[0] and glass.editing:
                            glass._end_edit(save=True)
                        _folded[0] = not _folded[0]
                        render()
            return 0
        if msg in (0x007E, 0x001A):  # WM_DISPLAYCHANGE / WM_SETTINGCHANGE（分辨率、任务栏变化）
            work_area = get_work_area()
            pos_x, pos_y = initial_position()
            if _last_img[0] is not None:
                render()
            return 0
        if msg == 0x0113:        # WM_TIMER
            if state["quit"]:
                u.KillTimer(hwnd, 1)
                if _tray[0] is not None:
                    _tray[0].remove()
                glass.destroy()
                u.DestroyWindow(hwnd)
            elif state["mode"] == "sleep":
                if _visible[0]:            # 休眠：隐藏悬浮窗，只留托盘图标
                    render()  # 补渲染最后一帧：休眠后不再调用 render()，面板(glass)得靠这一帧才会跟着隐藏
                    u.ShowWindow(hwnd, 0)  # SW_HIDE
                    _visible[0] = False
            else:
                if not _visible[0]:        # 唤醒：恢复悬浮窗显示
                    u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                    _visible[0] = True
                # 重新置顶：WS_EX_TOPMOST 会被别的置顶窗/全屏抢掉，每帧重申一次
                # HWND_TOPMOST=-1, SWP_NOSIZE|NOMOVE|NOACTIVATE=0x13
                # 右键菜单开着/面板编辑中时跳过——否则圆环每 80ms 抢一次置顶，把菜单/面板盖回去
                if not state.get("menu_open") and not state.get("panel_editing"):
                    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)
                render()
            return 0
        if msg == 0x0002:        # WM_DESTROY
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    proc = WNDPROC(wndproc)  # 保持引用，防止被 GC

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
    wc = WNDCLASS()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "VoiceInputOverlay"
    wc.hCursor = u.LoadCursorW(None, 32512)  # IDC_ARROW
    u.RegisterClassW(ctypes.byref(wc))

    # WS_EX_LAYERED|WS_EX_TOPMOST|WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE, WS_POPUP
    hwnd = u.CreateWindowExW(0x80000 | 0x8 | 0x80 | 0x8000000, "VoiceInputOverlay", "",
                            0x80000000, pos_x, pos_y, W, H, None, None, hinst, None)
    # 原生文字区复用缓存编辑语义，外轮廓统一由胶囊绘制。

    def on_panel_edit_start():
        state["panel_editing"] = True  # 暂停语音识别（worker）+ 圆环置顶重申，见对应位置
        pbuf = state.get("panel")
        if pbuf is not None:
            pbuf.pause()  # 编辑中暂停主动整理，别拿整理结果盖用户正在改的字

    def on_panel_edit_end(text):
        state["panel_editing"] = False
        pbuf = state.get("panel")
        if text is not None:  # None=用户按 Esc 放弃，保留原缓存
            if pbuf is not None:
                pbuf.replace_all(text)
        if pbuf is not None:
            pbuf.resume()  # 退出编辑态，恢复主动整理

    glass = CapsuleText(
        hinst,
        tint=gc.get("tint", ui_style.PANEL_TINT),
        text_rgb=tuple(gc.get("text_rgb", ui_style.PANEL_TEXT_RGB)),
        raw_text_rgb=tuple(gc.get("raw_text_rgb", ui_style.PANEL_RAW_TEXT_RGB)),
        hint_text_rgb=tuple(gc.get("hint_text_rgb", ui_style.PANEL_HINT_TEXT_RGB)),
        low_conf_rgb=tuple(gc.get("low_conf_rgb", ui_style.PANEL_LOW_CONF_RGB)),
        width=scaled(gc.get("width", panel.PANEL_W)),
        font_size=_panel_font_size(gc.get("font_size"), dpi, system_font_px),
        pad=scaled(4),
        max_h=scaled(gc.get("max_h", 480)),
        on_edit_start=on_panel_edit_start,
        on_edit_end=on_panel_edit_end,
    )
    glass.u.SetWindowLongPtrW(glass.hwnd, -8, hwnd)  # 原生文字区归属外轮廓，置顶时不会互相覆盖
    if state["mode"] == "sleep":       # 启动即处于休眠：直接不显示，避免先露一帧再隐藏的闪烁
        _visible[0] = False
    else:
        render()
        u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    u.SetTimer(hwnd, 1, _timer_ms[0], None)

    # 系统托盘常驻图标：左键/双击拉起主界面，右键弹与圆环同款菜单（主界面 / 退出）。还没初始化完时
    # 用 tooltip 带一句"正在初始化"——纯被动，鼠标划过去才看得到，不会像悬浮窗那样无端弹出来。
    _TRAY_TIP = "Saymore"
    try:
        tip = _TRAY_TIP if state.get("backend_ready", True) else f"{_TRAY_TIP}（正在初始化，请稍候…）"
        _tray[0] = tray.TrayIcon(hwnd, _ICON_PATH, tip,
                                 on_activate=open_main, on_menu=show_menu)
    except Exception as e:
        print(f"[warn] 托盘图标创建失败：{e}")

    def tray_notice_watcher():
        """监视主界面(main_window.py)关窗时写来的提示文件，从真正的托盘图标弹系统气泡通知
        （不是窗口内提示——主界面已经关了，用户根本看不到）；顺带等后台初始化完，把 tooltip
        改回正常文字。"""
        trigger = CONFIG_PATH.parent / ".tray_notice"
        trigger.unlink(missing_ok=True)
        tip_reset = state.get("backend_ready", True)  # tooltip 是否已经是正常文字（无需再改）
        while not state["quit"]:
            if not tip_reset and state.get("backend_ready", True) and _tray[0] is not None:
                _tray[0].set_tip(_TRAY_TIP)
                tip_reset = True
            try:
                if trigger.exists():
                    msg_text = trigger.read_text(encoding="utf-8").strip()
                    trigger.unlink(missing_ok=True)
                    if msg_text and _tray[0] is not None:
                        _tray[0].balloon("Saymore", msg_text)
            except Exception as e:  # noqa: BLE001 监视线程别被单次异常杀死
                print(f"[warn] 托盘气泡触发处理出错：{e}")
            time.sleep(0.3)
    threading.Thread(target=tray_notice_watcher, daemon=True).start()

    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    print("\n退出。")
