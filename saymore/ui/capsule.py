"""悬浮胶囊：连续轮廓动画和内嵌原生文字区，不处理语音或缓存业务。"""
import ctypes
from ctypes import wintypes

from PIL import Image, ImageDraw

from saymore.ui import panel


def default_position(work_area, diameter, margin=24):
    left, top, right, bottom = work_area
    return max(left, right - diameter - margin), max(top, bottom - diameter - margin)


def expansion_direction(work_area, anchor, diameter, wanted, previous=None):
    """优先保留能容纳卡片的方向；空间不足时才转向更宽裕的一侧。"""
    left, top, right, bottom = work_area
    x, y = anchor
    spaces = ((x + diameter - left, right - x), (y + diameter - top, bottom - y))
    chosen = []
    for axis, (negative, positive) in enumerate(spaces):
        old = previous[axis] if previous else (-1 if negative > positive else 1)
        current, other = (negative, positive) if old < 0 else (positive, negative)
        if current < wanted[axis] and other > current + diameter / 2:
            old = -old
        chosen.append(old)
    return tuple(chosen)


def expansion_bounds(work_area, anchor, diameter, direction):
    left, top, right, bottom = work_area
    x, y = anchor
    return (x + diameter - left if direction[0] < 0 else right - x,
            y + diameter - top if direction[1] < 0 else bottom - y)


def expansion_offset(work_area, anchor, diameter, size, direction):
    """收起时偏移为零；展开后以原小圆为锚点，并守住工作区边界。"""
    left, top, right, bottom = work_area
    x, y = anchor
    w, h = size
    dx = diameter - w if direction[0] < 0 else 0
    dy = diameter - h if direction[1] < 0 else 0
    return (max(left, min(x + dx, right - w)) - x,
            max(top, min(y + dy, bottom - h)) - y)


class Morph:
    """按单调时钟插值；中途换目标从当前轮廓继续，不跳回起点。"""
    def __init__(self, initial):
        self.current = self.start = self.target = (
            tuple(initial) if isinstance(initial, (tuple, list)) else (initial, initial, initial / 2))
        self.since = 0.0
        self.duration = 0.26

    def sample(self, now):
        t = min(1.0, max(0.0, (now - self.since) / self.duration))
        ease = 1 - (1 - t) ** 3
        self.current = tuple(a + (b - a) * ease for a, b in zip(self.start, self.target))
        return self.current

    def move(self, target, now):
        self.sample(now)
        if tuple(target) != self.target:
            self.start, self.target, self.since = self.current, tuple(target), now
        return self.current

    @property
    def moving(self):
        return self.current != self.target


def _rounded_layer(mode, w, h, radius, fill=None, outline=None, outline_width=1):
    """只对四个角超采样，避免每帧放大整块长文本窗口。"""
    radius = max(1, min(round(radius), w // 2, h // 2))
    layer = Image.new(mode, (w, h))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w - 1, h - 1), radius,
                                           fill=fill, outline=outline, width=outline_width)
    scale = 3
    circle = Image.new(mode, (radius * 2 * scale, radius * 2 * scale))
    ImageDraw.Draw(circle).ellipse((1, 1, circle.width - 2, circle.height - 2),
                                  fill=fill, outline=outline, width=max(scale, outline_width * scale))
    circle = circle.resize((radius * 2, radius * 2), Image.Resampling.LANCZOS)
    for x, cx in ((0, 0), (w - radius, radius)):
        for y, cy in ((0, 0), (h - radius, radius)):
            layer.paste(circle.crop((cx, cy, cx + radius, cy + radius)), (x, y))
    return layer


def draw_capsule(size, diameter, cat, background, border, text=None, text_xy=(0, 0), opacity=1,
                 cat_xy=(0, 0), cat_size=None, border_width=1):
    """背景、猫、动画中的文字同帧合成；文字只裁切/淡出，不拉伸。"""
    w, h, radius = size
    w, h = max(diameter, round(w)), max(diameter, round(h))
    mask = _rounded_layer("L", w, h, radius, fill=255)
    img = Image.new("RGBA", (w, h), (*background, 255))
    if text is not None:
        layer = text.copy()
        if opacity < 1:
            layer.putalpha(layer.getchannel("A").point(lambda a: round(a * opacity)))
        img.alpha_composite(layer, text_xy)
    if cat is not None:
        cat_size = max(1, round(cat_size or diameter))
        img.alpha_composite(cat.resize((cat_size, cat_size), Image.Resampling.LANCZOS),
                            tuple(round(v) for v in cat_xy))
    img.alpha_composite(_rounded_layer("RGBA", w, h, radius, outline=(*border, 255),
                                       outline_width=border_width))
    img.putalpha(mask)
    return img


class CapsuleText(panel.GlassWindow):
    """复用 RichEdit 的选择/复制/编辑/滚动，文字区只在轮廓内显示。"""
    _CLASS = "SaymoreCapsuleText"

    def __init__(self, *args, **kwargs):
        self.limit_width = kwargs["width"]
        self.snapshot = None
        self.present = False
        super().__init__(*args, **kwargs)
        self.g.SetViewportOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.g.SetViewportOrgEx.restype = wintypes.BOOL
        self.g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.g.CreateCompatibleDC.restype = wintypes.HDC
        self.g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        self.g.CreateDIBSection.restype = ctypes.c_void_p
        self.g.DeleteDC.argtypes = [wintypes.HDC]
        # 有意为之：统一实色底，避免两个半透明图层叠加产生可见接缝。
        self.u.SetLayeredWindowAttributes(self.hwnd, 0, 255, 2)

    def _round_corners(self):
        pref = ctypes.c_int(1)  # 文字区由外轮廓包围，不另画窗口圆角。
        self.dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), 4)

    def _start_edit(self):
        self.editing = True
        self._hint_h = 0
        self._prev_fg = self.u.GetForegroundWindow()
        self.u.SendMessageW(self.edit, panel._EM_SETREADONLY, 0, 0)
        self.u.SendMessageW(self.edit, panel._EM_SETBKGNDCOLOR, 0, self.edit_bg_color)
        self.u.SetForegroundWindow(self.hwnd)
        self.u.SetFocus(self.edit)
        self.u.InvalidateRect(self.hwnd, None, True)
        if self.on_edit_start:
            self.on_edit_start()

    def _paint_hint(self, hdc):
        pass  # 有意为之：编辑不额外撑高；Enter/Esc/失焦语义沿用原控件。

    def prepare(self, clean, raw, hint, warn, low_conf, max_h, max_w):
        if self.editing:
            return
        clean, raw, hint, warn = (s.strip() for s in (clean, raw, hint, warn))
        key = (clean, raw, hint, warn, low_conf, max_h, max_w)
        if key == self._cur:
            return
        self.u.ShowWindow(self.hwnd, 0)
        self.present = bool(clean or raw or hint or warn)
        self._cur = key
        if not self.present:
            return
        text = clean + raw
        self.u.SendMessageW(self.edit, panel._WM_SETTEXT, 0, ctypes.c_wchar_p(text))
        cf = panel._CHARFORMATW()
        cf.cbSize, cf.dwMask = ctypes.sizeof(cf), panel._CFM_COLOR
        pos = 0
        for part, color in ((clean, self.low_conf_color if low_conf else self.text_color),
                            (raw, self.raw_color)):
            end = pos + len(part.encode("utf-16-le")) // 2
            self.u.SendMessageW(self.edit, panel._EM_SETSEL, pos, end)
            cf.crTextColor = color
            self.u.SendMessageW(self.edit, panel._EM_SETCHARFORMAT, panel._SCF_SELECTION, ctypes.byref(cf))
            pos = end
        hdc = self.u.GetDC(None)
        old = self.g.SelectObject(hdc, self.font)
        extent = wintypes.SIZE()
        measured = 0
        for line in "\n".join((text, hint, warn)).splitlines():
            self.g.GetTextExtentPoint32W(hdc, line, len(line.encode("utf-16-le")) // 2, ctypes.byref(extent))
            measured = max(measured, extent.cx)
        self.g.SelectObject(hdc, old)
        self.u.ReleaseDC(None, hdc)
        self.content_w = max(1, min(self.limit_width, max_w - self.pad * 2, measured + 12))
        self.u.MoveWindow(self.edit, self.pad, self.pad, self.content_w, max_h, False)
        line_h = self._line_h()
        line_count = self.u.SendMessageW(self.edit, panel._EM_GETLINECOUNT, 0, 0)
        self._status_text, self._status_color = hint, self.hint_color
        self._status_h = line_h if hint else 0
        self._warn_text = warn
        self._warn_h = min(line_h * 2, max_h - self.pad * 2) if warn else 0
        body_h = panel._text_area_height(line_count, line_h) if text else 0
        self.w = self.content_w + self.pad * 2
        self.h = min(max_h, body_h + self._status_h + self._warn_h + self.pad * 2)
        edit_h = max(0, self.h - self.pad * 2 - self._status_h - self._warn_h)
        self.u.MoveWindow(self.hwnd, 0, 0, self.w, self.h, False)
        self.u.MoveWindow(self.edit, self.pad, self.pad, self.content_w, edit_h, False)
        self.u.ShowWindow(self.edit, 4 if text else 0)
        self.u.SendMessageW(self.edit, panel._EM_SETSEL, pos, pos)
        self._scroll_to(max(0, line_count - panel._visible_line_count(edit_h, line_h)))
        self.snapshot = self.capture()

    def capture(self):
        """打印原生文字区到位图，动画与静止态使用完全相同的字形/换行。"""
        class INFO(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("w", wintypes.LONG), ("h", wintypes.LONG),
                        ("planes", wintypes.WORD), ("bits", wintypes.WORD),
                        ("compression", wintypes.DWORD), ("image_size", wintypes.DWORD),
                        ("x", wintypes.LONG), ("y", wintypes.LONG),
                        ("used", wintypes.DWORD), ("important", wintypes.DWORD)]
        info = INFO(40, self.w, -self.h, 1, 32)
        screen = self.u.GetDC(None)
        dc = self.g.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bitmap = self.g.CreateDIBSection(screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        old = self.g.SelectObject(dc, bitmap)
        try:
            rect = panel._RECT(0, 0, self.w, self.h)
            self.u.FillRect(dc, ctypes.byref(rect), self.bg_brush)
            if self._status_text:
                self._paint_status(dc)
            if self._warn_text:
                self._paint_warn(dc)
            # RichEdit 画到位于正文原点的 DC，保持系统排版。
            if self._cur[0] or self._cur[1]:
                self.g.SetViewportOrgEx(dc, self.pad, self.pad, None)
                self.u.SendMessageW(self.edit, 0x0318, dc, 0x14)  # WM_PRINTCLIENT
                self.g.SetViewportOrgEx(dc, 0, 0, None)
            self._paint_thumb(dc)
            data = ctypes.string_at(bits, self.w * self.h * 4)
            return Image.frombytes("RGB", (self.w, self.h), data, "raw", "BGRX").convert("RGBA")
        finally:
            self.g.SelectObject(dc, old)
            self.g.DeleteObject(bitmap)
            self.g.DeleteDC(dc)
            self.u.ReleaseDC(None, screen)

    def place(self, x, y, visible):
        if visible:
            self.u.SetWindowPos(self.hwnd, -1, x, y, self.w, self.h, 0x10 | 0x40)
        else:
            self.u.ShowWindow(self.hwnd, 0)
