"""屏幕右下角常驻悬浮窗：状态圆环 + 猫 + token 飘字 + 玻璃文字面板。

Pillow 超采样抗锯齿渲染 → Win32 分层窗口逐像素 alpha 贴图；主线程跑消息循环。
从 voice_input.py 拆出——只靠传入的 state 字典与主程序通信。
"""
import ctypes
import json
import threading
import time
from ctypes import wintypes

import numpy as np

import typeoff.ui.panel as panel
import typeoff.ui.tray as tray
import typeoff.ui.style as ui_style
from typeoff.paths import CONFIG_PATH

_ICON_PATH = CONFIG_PATH.parent / "typeoff.ico"


# 悬浮窗几何/配色（供 _render_overlay 与 run_overlay 共用）
_OV_D = 58           # 控件逻辑尺寸(px)
_OV_SS = 4           # 超采样倍率：先放大 4× 画再缩小，得到抗锯齿平滑边缘
_OV_FONT = None      # 数字字体（首次渲染惰性加载）
_CAT_FRAMES = None   # 猫姿势帧缓存（首次渲染惰性加载并预处理）
_OV_TH = 22          # 悬浮窗顶部 token 飘字条高度(px)；猫位置不变，窗口整体上移这么多


def _ov_font():
    """token 飘字用的小号粗体（惰性加载）：微软雅黑粗体，与 ui_style 主题字体一致
    （中文也有字形，Arial 没有）。都取不到则退回默认字体。"""
    global _OV_FONT
    if _OV_FONT is None:
        from PIL import ImageFont
        for face in ("msyhbd.ttc", "msyh.ttc", "arialbd.ttf"):
            try:
                _OV_FONT = ImageFont.truetype(face, 13)
                break
            except Exception:
                continue
        else:
            _OV_FONT = ImageFont.load_default()
    return _OV_FONT


def _draw_tokens(canvas, w, strip_h, tin, tout, fade):
    """在画布顶部窄带里画 `in/out` token 数字（绿=输入 橙=输出），按 fade(0~1) 整体淡出。"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    font = _ov_font()
    a = int(255 * fade)
    parts = [(str(tin), (90, 220, 120, a)), ("/", (170, 170, 170, a)), (str(tout), (255, 170, 60, a))]
    widths = [d.textlength(t, font=font) for t, _ in parts]
    x = (w - sum(widths)) / 2
    y = (strip_h - 13) / 2
    for (t, col), tw in zip(parts, widths):
        d.text((x, y), t, font=font, fill=col)
        x += tw


def _get_cat_frames():
    """从 cat_src/ 加载猫的姿势 PNG，按 alpha 边界裁切、缩放居中到超采样画布，缓存返回。
    返回 {状态名: 超采样 RGBA 图} 的 dict；缺图则返回 None（退回纯进度环）。"""
    global _CAT_FRAMES
    if _CAT_FRAMES is not None:
        return _CAT_FRAMES or None
    from PIL import Image
    SD = _OV_D * _OV_SS
    d = CONFIG_PATH.parent / "cat_src"

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


import math

# 圆环配色：休眠=冷静蓝呼吸；激活态=固定绿，说话时由浅到深连续呼吸，一段白光沿环扫过。
# 白流光两端渐隐（无硬起点）。
_SLEEP_RING_RGB = (90, 170, 255)     # 休眠：冷静的蓝
_ACTIVE_RING_RGB = (52, 199, 89)     # 激活态：固定绿（不说话时保持不变，即呼吸的最深色）
_SWEEP_RGB = (150, 205, 255)         # 流光默认色（激活态由白光覆盖）
_RING_RGB = _SLEEP_RING_RGB          # 兼容旧调用的默认色


def _speaking_ring_rgb(phase, base=_ACTIVE_RING_RGB):
    """说话时颜色明暗呼吸：以 base（非说话时的固定色）为最深点，只往浅了变，不再更深。
    phase 由外部按速度累加，经正弦来回摆动。"""
    u = 0.5 + 0.5 * math.sin(2 * math.pi * phase)      # 0..1 往返
    light = 0.45 * u                                    # 0(=base 最深)..0.45(最浅) 混入白色比例
    return tuple(c + (255 - c) * light for c in base)


def _lerp_rgb(cur, tgt, k=0.18):
    """把当前色朝目标色逐帧靠拢，避免休眠↔激活切换与巡回换色时突变（渐变）。"""
    return tuple(c + (t - c) * k for c, t in zip(cur, tgt))


def _ring(img, SD, cx, cy, rc, w, rgb, glow=0.55):
    """扁平发光环：实心色环 + 柔和外扩光晕（霓虹发光感），无 3D 管状明暗/高光。
    glow 为光晕强度，外部按时间脉动传入 → 闪闪发光。"""
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:SD, 0:SD]
    d = np.abs(np.hypot(xx - cx, yy - cy) - rc)    # 到环中心线的距离
    edge = max(1.0, SD * 0.002)                    # 边缘羽化（抗锯齿）：调小=更硬
    core = np.clip((w / 2.0 - d) / edge + 0.5, 0, 1)   # 实心环带，边缘平滑过渡
    halo = np.exp(-(d / (w * 0.8)) ** 2) * glow        # 高斯光晕：向内外柔和发光
    base = np.array(rgb, np.float64)
    light = np.minimum(255.0, base + 100.0)            # 光晕偏亮 → 发光感
    whalo = np.clip(halo - core, 0, 1)                 # 光晕中被实心环盖住的部分不重复算
    denom = np.clip(core + whalo, 1e-6, None)
    rgbmap = (core[..., None] * base + whalo[..., None] * light) / denom[..., None]
    a = np.clip(np.maximum(core, halo), 0, 1)
    out = np.dstack([np.clip(rgbmap, 0, 255), a * 255]).astype("uint8")
    img.alpha_composite(Image.fromarray(out, "RGBA"))


def _sweep(img, SD, cx, cy, rc, w, ang, rgb=_SWEEP_RGB, tail=0.8):
    """光束扫过：一段亮弧沿环带叠加，以 ang(弧度)为中心向两侧对称渐隐——
    头尾都模糊、没有硬起点。作为半透明层叠在暗底环上：中心盖出亮色，两端渐透露出底环。"""
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:SD, 0:SD]
    d = np.abs(np.hypot(xx - cx, yy - cy) - rc)        # 到环中心线的距离
    edge = max(1.0, SD * 0.002)
    core = np.clip((w / 2.0 - d) / edge + 0.5, 0, 1)   # 环带形状（限定亮弧只在环上）
    dist = np.abs((ang - np.arctan2(yy - cy, xx - cx) + math.pi) % (2 * math.pi) - math.pi)  # 到中心的最短角距
    lum = np.exp(-(dist / tail) ** 2)                  # 中心对称钟形，两端渐隐
    col = np.broadcast_to(np.array(rgb, np.float64), (SD, SD, 3))
    alpha = np.clip(lum * core, 0, 1)                  # 靠 alpha 渐隐，两端柔和融进底环
    out = np.dstack([col, alpha * 255]).astype("uint8")
    img.alpha_composite(Image.fromarray(out, "RGBA"))


def _overlay_image(cat=None, ring_rgb=_RING_RGB, glow=0.55, sweep=None, sweep_rgb=_SWEEP_RGB,
                   sweep_tail=0.8):
    """用 Pillow 超采样渲染一帧悬浮窗，返回逻辑尺寸的 RGBA 图。
    背景透明；猫为主体铺在圆框内，外圈一整圈发光装饰环；sweep(弧度)非空则叠一段绕环流光。"""
    from PIL import Image

    D, S = _OV_D, _OV_SS
    SD = D * S
    f = lambda frac_: int(SD * frac_)  # 按控件尺寸取比例像素，改 _OV_D 即整体缩放
    cx = cy = SD // 2
    img = Image.new("RGBA", (SD, SD), (0, 0, 0, 0))
    # 整圆先铺一层近乎不可见的底(alpha=1)：逐像素 alpha 分层窗靠 alpha 命中鼠标，否则圆环内部及
    # 四周透明处右键/拖动都会穿透到背后窗口——只有猫的实心像素能点中（原「只有下半能右键」之因）。
    # 必须做最底层用 alpha_composite 让猫/环叠上，否则 ImageDraw 直画会把上层像素覆盖掉。
    from PIL import ImageDraw
    hit_r = SD / 2 - f(0.02)               # 盖到环外沿，整个可见圆都可点
    hit = Image.new("RGBA", (SD, SD), (0, 0, 0, 0))
    ImageDraw.Draw(hit).ellipse([cx - hit_r, cy - hit_r, cx + hit_r, cy + hit_r], fill=(0, 0, 0, 1))
    img.alpha_composite(hit)
    if cat is not None:
        img.alpha_composite(cat)

    ring_w = max(3, f(0.055))
    rpad = f(0.04)
    rc = SD / 2 - rpad - ring_w / 2       # 环中心线半径
    _ring(img, SD, cx, cy, rc, ring_w, ring_rgb, glow)
    if sweep is not None:
        _sweep(img, SD, cx, cy, rc, ring_w, sweep, sweep_rgb, tail=sweep_tail)
    bw = max(2.0, f(0.016))               # 内外边框：与环同底色、够宽 → 缩放后平滑不锯齿，框住亮环边缘
    _ring(img, SD, cx, cy, rc + ring_w / 2, bw, ring_rgb, glow=0.0)
    _ring(img, SD, cx, cy, rc - ring_w / 2, bw, ring_rgb, glow=0.0)

    return img.resize((D, D), Image.LANCZOS)  # 缩回逻辑尺寸 → 抗锯齿


def run_overlay(state):
    """屏幕右下角常驻的状态控件：聆听=绿(进度环+倒计时)，休眠=灰，内含随麦克风音量
    跳动的声音波形。Pillow 抗锯齿渲染 + Win32 分层窗口(逐像素 alpha)，边缘平滑无锯齿。
    右键弹菜单退出，退出后才整体消失。跑在调用方起的独立线程里(见 voice_input.py)，
    Ctrl+C(SIGINT) 只能在主线程注册，不在这里处理——由 voice_input.py 的 main() 注册。"""
    from ctypes import wintypes

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    D = _OV_D

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

    sw = u.GetSystemMetrics(0)
    sh = u.GetSystemMetrics(1)
    TH = _OV_TH
    W, H = D, D + TH            # 窗口比控件高 TH，多出的顶部窄带画 token 飘字
    # 默认摆放：屏幕右下角。窗口左上角＝猫位再上移 TH（顶部飘字带），使猫落在原位。
    def_x, def_y = sw - D - 40, (sh - D - 90) - TH
    pos_x, pos_y = def_x, def_y
    saved = state.get("overlay_pos")          # 上次拖动记住的窗口左上角 [x,y]
    if isinstance(saved, (list, tuple)) and len(saved) == 2:
        # 夹到屏幕内，防分辨率/多屏变化后窗口跑到屏幕外点不到
        pos_x = max(0, min(int(saved[0]), sw - W))
        pos_y = max(0, min(int(saved[1]), sh - H))

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

    _ring_cur = list(_SLEEP_RING_RGB)  # 底环色平滑过渡的当前值，逐帧趋近目标
    _hue = [0.0]                        # 说话时颜色明暗呼吸的相位累加器（按速度推进，不因变速跳变）
    _sweep_ang = [0.0]                  # 流光中心角度累加器（变速不跳变；休眠不推进也不画）
    _tprev = [time.time()]
    _last_img = [None]                  # 最近一帧成品图：拖动时用它即时重贴，跟手不等 80ms 定时器

    def render():
        """渲染当前状态并通过 UpdateLayeredWindow 贴到屏幕（逐像素 alpha）。"""
        now = time.time()
        # 提醒模式下「正在识别 / 已识别待攒句」也算忙碌：从话音落就显示，填满识别那几秒的空等
        reminder_processing = state["mode"] == "reminder" and (
            state.get("rbuf") or state["status"] == "transcribing")
        busy = state.get("llm_busy") or reminder_processing
        warming = state.get("warming")
        frames = _get_cat_frames()
        cat = None
        if frames:
            if warming:                                           # 冷启动中：完全静止，代表停止状态
                cat = frames["stand"]
            else:
                import typeoff.tts as _tts_mod
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
        from PIL import Image
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        # 环三档：①休眠=冷静蓝呼吸、无流光 ②激活静默=暗底环+流光缓慢扫 ③说话/识别=流光快速扫。
        # 底环色经 _lerp_rgb 渐变（休眠↔激活不突变）；流光头部角度按当前速度累加（变速不跳变）。
        dt = min(0.2, now - _tprev[0]); _tprev[0] = now
        speaking = state["mode"] != "sleep" and bool(
            state.get("speaking") or state["status"] == "transcribing"
            or state.get("llm_busy") or state.get("rbuf"))
        sweep_rgb = _SWEEP_RGB
        sweep_tail = 0.7
        if warming:
            target = _ACTIVE_RING_RGB                            # 冷启动：纯色环，无流光，无呼吸
            sweep = None
        elif state["mode"] == "sleep":
            v = 0.80 + 0.20 * (0.5 + 0.5 * math.sin(now * 1.6))  # 呼吸：明暗轻微起伏
            target = tuple(c * v for c in _SLEEP_RING_RGB)
            sweep = None                                         # 休眠：不出现流光
        else:
            sweep_rgb = (255, 255, 255)                          # 流光=白光扫过
            _sweep_ang[0] += dt * (10.4 if speaking else 1.1)    # 说话≈0.6s一圈(很快)，静默≈6s(缓慢)
            sweep = _sweep_ang[0]
            sweep_tail = 0.32 if speaking else 0.7               # 说话时收窄成短彗尾，避免快扫拖成整圈白
            if speaking:
                _hue[0] += dt * (1 / 1.4)                         # 说话时颜色明暗呼吸的速度
                target = _speaking_ring_rgb(_hue[0])
            else:
                target = _ACTIVE_RING_RGB                        # 非说话：固定色，不随时间变化
        _ring_cur[:] = _lerp_rgb(_ring_cur, target, 0.35)
        ring_rgb = tuple(int(round(c)) for c in _ring_cur)
        img.alpha_composite(_overlay_image(cat, ring_rgb, glow=0.0, sweep=sweep,
                                           sweep_rgb=sweep_rgb, sweep_tail=sweep_tail), (0, TH))
        if not busy:
            age = now - state.get("tok_time", 0)
            if state.get("tok_time", 0) and age < 10:       # 最近一轮 token：显示 10s，末 3s 淡出
                fade = 1.0 if age < 7 else (10 - age) / 3.0
                _draw_tokens(img, W, TH, state["tok_in"], state["tok_out"], fade)

        _last_img[0] = img
        blit(hwnd, img, pos_x, pos_y)

        # 玻璃面板：右边缘紧贴猫左侧(留 2px 缝)、底边对齐猫底边，文字多时只往上撑；空文字则隐藏。
        if state["mode"] == "sleep" and glass.editing:
            # 编辑到一半就静默超时/说了休眠词进休眠：缓存已被 enter_sleep 清空，但编辑态若不
            # 一并退出，update() 会因 self.editing 跳过刷新，面板就卡在编辑界面不随之隐藏。
            glass.cancel_edit()
        pbuf = state.get("panel")
        clean, raw, low_conf = pbuf.text_parts if pbuf is not None else ("", "", False)
        # 状态提示靠 hint 颜色（蓝灰）跟绿色正文分开；整理状态不区分模式，一律"整理中"
        hint = ""
        dots = "…" * (1 + int(now * 2) % 3)
        if state["mode"] == "awake":
            if state.get("warming"):
                elapsed = now - state.get("warming_start", now)
                remain = max(0, 10 - int(elapsed))
                hint = f"正在启动推理引擎并加载模型…{remain}s"
            elif state.get("speaking"):
                hint = f"正在说话{dots}"
            elif state["status"] == "transcribing":
                hint = f"识别中{dots}"
            elif pbuf is not None and pbuf.cleaning_mode:
                hint = f"整理中{dots}"
            elif pbuf is not None and pbuf.countdown is not None:
                secs = int(pbuf.countdown) + 1
                hint = f"{secs}秒后整理"
        # 警告行（红色，"没找到输入框"等），到期自动消失
        warn_entry = state.get("warn")
        warn_text = ""
        if warn_entry:
            text, expire = warn_entry
            if now < expire:
                warn_text = text
            else:
                state["warn"] = None  # 到期一次性清掉，别每帧再判
        # 面板锚点由当前窗口位置实时推出（拖动后跟随）：猫左=窗口左 pos_x，猫底=pos_y+TH+D
        glass.update(clean, raw, hint, pos_x - 2, pos_y + TH + D, low_conf=low_conf,
                     warn=warn_text, warm=bool(state.get("warming")))

    def open_main():
        """拉起主界面（圆环右键菜单、托盘左键/菜单共用）。"""
        import typeoff.ui.main_window as main_window
        try:
            main_window.show(CONFIG_PATH, state["history_file"],
                             state["reminders_log"], state["import_trigger"], state["restart_trigger"])
        except Exception as e:
            print(f"[warn] 打不开主界面：{e}")

    def show_menu():
        """小圆环右键菜单：只两项——主界面 / 退出程序（设置/历史/导入全收进主界面 tab）。
        Apple 风自绘弹窗（ui_style），后台线程跑 tkinter 不阻塞消息循环。"""
        import typeoff.ui.style as ui_style
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
        """把当前窗口左上角写回 config.json 的 overlay_pos，下次启动沿用。读-改-写整份，
        只在拖动松手这一刻发生（低频），不会与设置窗抢写。"""
        state["overlay_pos"] = [x, y]
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            cfg["overlay_pos"] = [x, y]
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[warn] 记住小圆窗位置失败：{e}")

    def wndproc(hwnd, msg, wp, lp):
        nonlocal pos_x, pos_y
        if msg == tray.MSG:      # 托盘图标回调：左键/双击拉起主界面，右键弹菜单
            if _tray[0] is not None:
                _tray[0].handle(lp)
            return 0
        if msg == 0x0205:        # WM_RBUTTONUP
            show_menu()
            return 0
        if msg == 0x0201:        # WM_LBUTTONDOWN：开始拖动
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            _drag.update(on=True, gx=pt.x, gy=pt.y, px=pos_x, py=pos_y)
            u.SetCapture(hwnd)   # 捕获鼠标，拖出窗口也能收到移动/抬起
            return 0
        if msg == 0x0200:        # WM_MOUSEMOVE：拖动中按光标增量挪窗
            if _drag["on"]:
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                pos_x = _drag["px"] + (pt.x - _drag["gx"])
                pos_y = _drag["py"] + (pt.y - _drag["gy"])
                if _last_img[0] is not None:
                    blit(hwnd, _last_img[0], pos_x, pos_y)  # 即时重贴，跟手
            return 0
        if msg == 0x0202:        # WM_LBUTTONUP：结束拖动，落盘记住位置
            if _drag["on"]:
                _drag["on"] = False
                u.ReleaseCapture()
                if (pos_x, pos_y) != (_drag["px"], _drag["py"]):  # 真挪动过才写盘（纯点击不写）
                    save_pos(pos_x, pos_y)
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
    # 玻璃文字面板：原生 DWM 亚克力毛玻璃窗口（真·模糊背后桌面），点击穿透、置顶。
    gc = state.get("glass_cfg", {})

    def on_panel_edit_start():
        state["panel_editing"] = True  # 暂停语音识别（worker）+ 圆环置顶重申，见对应位置
        pbuf = state.get("panel")
        if pbuf is not None:
            pbuf.pause()  # 编辑中暂停自动整理，别拿整理结果盖用户正在改的字

    def on_panel_edit_end(text):
        state["panel_editing"] = False
        pbuf = state.get("panel")
        if text is not None:  # None=用户按 Esc 放弃，保留原缓存
            if pbuf is not None:
                pbuf.replace_all(text)
        if pbuf is not None:
            pbuf.resume()  # 退出编辑态，恢复自动整理

    glass = panel.GlassWindow(
        hinst,
        tint=gc.get("tint", ui_style.PANEL_TINT),
        text_rgb=tuple(gc.get("text_rgb", ui_style.PANEL_TEXT_RGB)),
        raw_text_rgb=tuple(gc.get("raw_text_rgb", ui_style.PANEL_RAW_TEXT_RGB)),
        hint_text_rgb=tuple(gc.get("hint_text_rgb", ui_style.PANEL_HINT_TEXT_RGB)),
        low_conf_rgb=tuple(gc.get("low_conf_rgb", ui_style.PANEL_LOW_CONF_RGB)),
        width=gc.get("width", panel.PANEL_W),
        font_size=gc.get("font_size", 18),
        max_h=gc.get("max_h", 480),
        on_edit_start=on_panel_edit_start,
        on_edit_end=on_panel_edit_end,
    )
    if state["mode"] == "sleep":       # 启动即处于休眠：直接不显示，避免先露一帧再隐藏的闪烁
        _visible[0] = False
    else:
        render()
        u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    u.SetTimer(hwnd, 1, 80, None)

    # 系统托盘常驻图标：左键/双击拉起主界面，右键弹与圆环同款菜单（主界面 / 退出）。还没初始化完时
    # 用 tooltip 带一句"正在初始化"——纯被动，鼠标划过去才看得到，不会像悬浮窗那样无端弹出来。
    _TRAY_TIP = "Typeoff"
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
                        _tray[0].balloon("Typeoff", msg_text)
            except Exception as e:  # noqa: BLE001 监视线程别被单次异常杀死
                print(f"[warn] 托盘气泡触发处理出错：{e}")
            time.sleep(0.3)
    threading.Thread(target=tray_notice_watcher, daemon=True).start()

    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    print("\n退出。")

