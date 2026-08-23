# -*- coding: utf-8 -*-
"""右下角玻璃文字面板：识别句全部攒进缓冲、显示在半透明磨砂窗口里，不立刻粘贴，
只有用户显式说"发送"才整体回填输入框——其余任何情况（攒多少字、静默多久、进休眠）
都不自动回填，全留在缓存里等指令。用户停止说话后等 quiet_seconds 秒自动按所选模式
整理一次（小范围走滑动窗口，深度/邮件/00后 对全文跑一次并冻结）。

- TextBuffer：停说后自动整理 + 仅发送/flush_all 时回填。可单测。
- GlassWindow：Win32 无边框分层窗口（WS_EX_LAYERED 整窗统一半透）+ 圆角，里面嵌一个
  RichEdit 控件（真·系统文本控件，不是画出来的字）。默认只读，文字可鼠标拖选、右键
  菜单复制；点一下正文即可编辑（背景变色提示"编辑中"），Enter 保存/Esc 放弃/点外部
  失焦按保存处理，编辑期间暂停 update() 覆写 + 暂停自动整理（外部还需自行暂停语音识别，见调用方）。
  配色/透明度走配置，便于不改代码微调。
"""
import ctypes
import threading
import time
from ctypes import wintypes

PANEL_W = 300          # 面板文字内容宽度(px) 默认值
_PAD = 16
_EDIT_HINT_TEXT = "编辑中 · Esc 放弃 · Enter 保存"

# RichEdit 消息/样式常量（Msftedit.dll，RICHEDIT50W 类）
_WM_SETTEXT, _WM_SETFONT, _WM_COPY, _WM_CONTEXTMENU = 0x000C, 0x0030, 0x0301, 0x007B
_WM_GETTEXT, _WM_GETTEXTLENGTH = 0x000D, 0x000E
_WM_KEYDOWN, _WM_CHAR, _WM_LBUTTONDOWN, _WM_KILLFOCUS = 0x0100, 0x0102, 0x0201, 0x0008
_WM_LBUTTONDBLCLK = 0x0203
_VK_RETURN, _VK_ESCAPE = 0x0D, 0x1B
_EM_SETSEL, _EM_LINESCROLL, _EM_GETLINECOUNT = 0x00B1, 0x00B6, 0x00BA
_EM_GETFIRSTVISIBLELINE = 0x00CE
_EM_SETREADONLY = 0x00CF
_WM_MOUSEWHEEL = 0x020A
_WHEEL_LINES = 3          # 滚轮一格滚几行
_EM_SETCHARFORMAT, _EM_SETBKGNDCOLOR, _EM_SETPARAFORMAT = 0x0444, 0x0443, 0x0447
_EM_REPLACESEL = 0x00C2
_SCF_SELECTION, _SCF_ALL, _CFM_COLOR = 0x0001, 0x0004, 0x40000000
_PFM_LINESPACING, _LINESPACING_EXACT = 0x00000100, 4  # dyLineSpacing 是精确 twips 高度
_ES_MULTILINE, _ES_READONLY = 0x0004, 0x0800
_GWLP_WNDPROC = -4
_TPM_RETURNCMD = 0x0100
_LOGPIXELSY = 90
_THUMB_W, _THUMB_INSET, _THUMB_MIN = 4, 6, 24  # 自绘滚动条：宽/距右边缘/最短


# ---- DWM/Win32 结构体 ----
class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", _RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class _CHARFORMATW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwMask", wintypes.DWORD), ("dwEffects", wintypes.DWORD),
                ("yHeight", ctypes.c_long), ("yOffset", ctypes.c_long), ("crTextColor", wintypes.DWORD),
                ("bCharSet", ctypes.c_byte), ("bPitchAndFamily", ctypes.c_byte),
                ("szFaceName", ctypes.c_wchar * 32)]


class _PARAFORMAT2(ctypes.Structure):
    """只用得到行距字段，其余（缩进/对齐/编号…）留空取默认——按 MSDN 布局占位对齐即可。"""
    _fields_ = [("cbSize", wintypes.UINT), ("dwMask", wintypes.DWORD),
                ("wNumbering", ctypes.c_ushort), ("wReserved", ctypes.c_ushort),
                ("dxStartIndent", ctypes.c_long), ("dxRightIndent", ctypes.c_long),
                ("dxOffset", ctypes.c_long), ("wAlignment", ctypes.c_ushort), ("cTabCount", ctypes.c_short),
                ("rgxTabs", ctypes.c_long * 32),
                ("dySpaceBefore", ctypes.c_long), ("dySpaceAfter", ctypes.c_long),
                ("dyLineSpacing", ctypes.c_long), ("sStyle", ctypes.c_short),
                ("bLineSpacingRule", ctypes.c_byte), ("bOutlineLevel", ctypes.c_byte),
                ("wShadingWeight", ctypes.c_ushort), ("wShadingStyle", ctypes.c_ushort),
                ("wNumberingStart", ctypes.c_ushort), ("wNumberingStyle", ctypes.c_ushort),
                ("wNumberingTab", ctypes.c_ushort), ("wBorderSpace", ctypes.c_ushort),
                ("wBorderWidth", ctypes.c_ushort), ("wBorders", ctypes.c_ushort)]


class GlassWindow:
    """一个常驻置顶的无边框分层窗口，整窗统一半透（磨砂感），里面嵌一个 RichEdit 控件
    承载文字——真正的系统文本控件，支持鼠标拖选和右键复制、超高时滚轮滚动+自绘滚动条。
    默认只读；双击正文进入编辑态（背景变色），Enter 保存/Esc 放弃/失焦按保存处理，
    结果经 on_edit_end(text_or_None) 回调交给外部落地。
    update() 按当前文字量决定窗口高度（撑到 max_h 为止）、移动到目标位置；文字为空则隐藏；
    编辑期间跳过，避免识别结果覆盖用户正在改的文字。"""

    _CLASS = "VoiceInputGlass"

    def __init__(self, hinst, tint="E6ECEFF5", text_rgb=(34, 139, 34),
                 raw_text_rgb=(0, 0, 0), hint_text_rgb=(178, 184, 194),
                 low_conf_rgb=(255, 59, 48),
                 width=PANEL_W, font_size=18, pad=_PAD, max_h=480,
                 on_edit_start=None, on_edit_end=None, menu_items_provider=None):
        self.u = ctypes.windll.user32
        self.g = ctypes.windll.gdi32
        self.dwm = ctypes.windll.dwmapi
        self._msftedit = ctypes.WinDLL("Msftedit.dll")  # 加载后系统才认得 RICHEDIT50W 这个窗口类
        self.content_w = int(width)
        self.pad = int(pad)
        self.max_h = int(max_h)   # 面板最大高度：文字撑到这个高度就不再往上涨，改滚动条滚动
        self.w = self.h = 0
        self._cur = None  # 上次 (clean, raw, hint, low_conf, warm)，不变则跳过重排/重绘
        self._drag = None  # 拖滑块中：(按下时的鼠标 y, 按下时的首可见行)
        self.editing = False  # 编辑态：True 时 update() 整块跳过，别拿识别结果覆盖正在改的字
        self._hint_h = 0      # 编辑态顶部提示条高度，进入编辑时撑开、退出后随下次 update() 收回
        self._status_text = ""  # 底部状态栏文字（正在说话/整理中/倒计时）
        self._status_color = 0  # 底部状态栏文字色 COLORREF
        self._status_h = 0      # 底部状态栏高度（有状态文字时=一行高，无则=0）
        self._warn_text = ""    # 警告行文字（如"没找到输入框"），红色，画在状态栏下面独占一行
        self._warn_h = 0        # 警告行高度（有 warn 文字时=一行高，无则=0）
        self._floor_h = 0       # 防抖：update 期间窗口高度只增不减，正文变短时才重置
        self._prev_body_len = 0 # 上次正文字数，用于检测 undo/clear 导致的正文缩短
        self._anchor = (0, 0)  # 最近一次 update() 的 (right_x, bottom_y)，供进入编辑态时原地长高用
        self._prev_fg = None   # 进入编辑态前的前台窗口，退出时还回去（语音发送要靠它找对输入框）
        self.on_edit_start = on_edit_start  # 进入编辑态回调：外部借此暂停语音识别
        self.on_edit_end = on_edit_end      # 退出编辑态回调(text_or_None)：None=放弃，否则是保存的新文字
        self.menu_items_provider = menu_items_provider  # 右键菜单动态项：()->[(文字,回调)…]，非编辑态才调，外部按当前状态给「深度整理/还原」
        # 文字色 COLORREF = 0x00BBGGRR。已整理=绿色（整理完成），未整理=黑色（原始转写），
        # 状态提示=蓝灰（底部状态栏）。低置信度单独换红色。
        r, gg, b = text_rgb
        self.text_color = (b << 16) | (gg << 8) | r
        r, gg, b = raw_text_rgb
        self.raw_color = (b << 16) | (gg << 8) | r
        r, gg, b = hint_text_rgb
        self.hint_color = (b << 16) | (gg << 8) | r
        r, gg, b = low_conf_rgb
        self.low_conf_color = (b << 16) | (gg << 8) | r
        # 面板底色 + 整窗不透明度：tint='AARRGGBB'。A 越小越透（统一 alpha 会连带文字变淡，别调太小）
        s = int(tint, 16)
        self.alpha = (s >> 24) & 255
        br, bgc, bb = (s >> 16) & 255, (s >> 8) & 255, s & 255
        self.bg_color = (bb << 16) | (bgc << 8) | br
        # 编辑态底色：往暖黄混一点，跟平时的冷灰底一眼区分开，提示"现在能打字"
        blend = lambda a, target: round(a + (target - a) * 0.35)
        er, eg, eb = blend(br, 255), blend(bgc, 240), blend(bb, 200)
        self.edit_bg_color = (eb << 16) | (eg << 8) | er
        self._setup_api()
        self.bg_brush = self.g.CreateSolidBrush(self.bg_color)
        self.edit_bg_brush = self.g.CreateSolidBrush(self.edit_bg_color)
        self.thumb_brush = self.g.CreateSolidBrush(self.raw_color)  # 滚动条用中档灰，跟未整理文字同色
        self.font = self._make_font(font_size)
        self._register(hinst)
        self.hwnd = self._create(hinst)
        self.edit = self._create_edit(hinst)
        self.u.SetLayeredWindowAttributes(self.hwnd, 0, self.alpha, 0x2)  # LWA_ALPHA
        self._round_corners()
        self.u.ShowWindow(self.hwnd, 0)  # 初始隐藏

    def _setup_api(self):
        u, g = self.u, self.g
        P, HWND, HDC, BOOL, INT, UINT, DWORD = (ctypes.c_void_p, wintypes.HWND, wintypes.HDC,
                                                wintypes.BOOL, ctypes.c_int, wintypes.UINT,
                                                wintypes.DWORD)
        LPCWSTR = wintypes.LPCWSTR
        for fn, res, args in [
            (u.CreateWindowExW, HWND, [DWORD, LPCWSTR, LPCWSTR, DWORD, INT, INT, INT, INT,
                                       HWND, P, P, P]),
            (u.DefWindowProcW, ctypes.c_ssize_t, [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (u.SendMessageW, ctypes.c_ssize_t, [HWND, UINT, P, P]),  # wParam/lParam 走 void*：既能传整数也能传指针
            (u.BeginPaint, HDC, [HWND, P]),
            (u.EndPaint, BOOL, [HWND, P]),
            (u.InvalidateRect, BOOL, [HWND, P, BOOL]),
            (u.MoveWindow, BOOL, [HWND, INT, INT, INT, INT, BOOL]),
            (u.SetWindowPos, BOOL, [HWND, HWND, INT, INT, INT, INT, UINT]),
            (u.ShowWindow, BOOL, [HWND, INT]),
            (u.SetCapture, HWND, [HWND]),
            (u.ReleaseCapture, BOOL, None),
            (u.SetFocus, HWND, [HWND]),
            (u.GetDC, HDC, [HWND]),
            (u.ReleaseDC, INT, [HWND, HDC]),
            (u.DestroyWindow, BOOL, [HWND]),
            (u.RegisterClassW, wintypes.ATOM, [P]),
            (u.LoadCursorW, P, [P, P]),
            (u.FillRect, INT, [HDC, P, P]),
            (u.DrawTextW, INT, [HDC, LPCWSTR, INT, P, UINT]),
            (u.SetLayeredWindowAttributes, BOOL, [HWND, DWORD, ctypes.c_ubyte, DWORD]),
            (u.GetWindowLongPtrW, P, [HWND, INT]),
            (u.SetWindowLongPtrW, P, [HWND, INT, P]),
            (u.CallWindowProcW, ctypes.c_ssize_t, [P, HWND, UINT, wintypes.WPARAM, wintypes.LPARAM]),
            (u.CreatePopupMenu, P, None),
            (u.AppendMenuW, BOOL, [P, UINT, P, LPCWSTR]),
            (u.TrackPopupMenu, ctypes.c_int, [P, UINT, INT, INT, INT, HWND, P]),
            (u.DestroyMenu, BOOL, [P]),
            (u.SetForegroundWindow, BOOL, [HWND]),
            (u.GetForegroundWindow, HWND, None),
            (g.CreateSolidBrush, P, [DWORD]),
            (g.CreateFontW, P, [INT] * 13 + [LPCWSTR]),
            (g.SelectObject, P, [HDC, P]),
            (g.GetTextExtentPoint32W, BOOL, [HDC, LPCWSTR, INT, P]),
            (g.GetDeviceCaps, INT, [HDC, INT]),
            (g.DeleteObject, BOOL, [P]),
            (g.SetTextColor, DWORD, [HDC, DWORD]),
            (g.SetBkMode, INT, [HDC, INT]),
            (self.dwm.DwmSetWindowAttribute, ctypes.c_long, [HWND, DWORD, P, DWORD]),
        ]:
            fn.restype = res
            fn.argtypes = args

    def _make_font(self, px):
        """微软雅黑 UI（与 ui_style 主题一致）；默认字号由调用方取 Windows 系统 UI 字体高度，
        显式 panel_font_px 才覆盖。400=Regular，CLEARTYPE 平滑。"""
        return self.g.CreateFontW(-int(px), 0, 0, 0, 400, 0, 0, 0,
                                  1, 0, 0, 5, 0, "Microsoft YaHei UI")

    def _register(self, hinst):
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
        self._proc = WNDPROC(self._wndproc)  # 保引用防 GC

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._CLASS
        wc.hCursor = self.u.LoadCursorW(None, 32512)
        wc.hbrBackground = None  # 不擦背景 → 亚克力毛玻璃透出来
        self.u.RegisterClassW(ctypes.byref(wc))

    def _create(self, hinst):
        # WS_EX_TOOLWINDOW|TOPMOST|NOACTIVATE|LAYERED(整窗半透)，WS_POPUP
        # 不再加 WS_EX_TRANSPARENT：要让鼠标能选中/右键复制文字，就不能再点击穿透。
        # NOACTIVATE 仍保留——点它不会抢走你正在口述的输入框的键盘焦点。
        exstyle = 0x80 | 0x8 | 0x8000000 | 0x80000
        return self.u.CreateWindowExW(exstyle, self._CLASS, "", 0x80000000,
                                      0, 0, 10, 10, None, None, hinst, None)

    def _create_edit(self, hinst):
        """RichEdit 子控件，承载实际文字：原生可拖选、超高滚轮滚动，默认只读（双击正文切可编辑，见 _start_edit）。
        宽度固定=content_w（换行只看宽度、跟高度无关），高度按 max_h 建，之后只在 update() 里改高。"""
        # 不带 WS_VSCROLL：系统条在分层窗口里被 Win11 画成一根位置和量程都对不上的灰短棍，
        # 且面板 NOACTIVATE 也拖不动它；ShowScrollBar 压不住（RichEdit 一溢出就自己调回来），
        # 索性不给这个样式，滚动条改 _paint_thumb() 自绘。EM_LINESCROLL 和滚轮不依赖它。
        style = 0x40000000 | 0x10000000 | _ES_MULTILINE | _ES_READONLY  # WS_CHILD|WS_VISIBLE
        hwnd = self.u.CreateWindowExW(0, "RICHEDIT50W", "", style,
                                      self.pad, self.pad, self.content_w, self.max_h,
                                      self.hwnd, None, hinst, None)
        self.u.SendMessageW(hwnd, _WM_SETFONT, self.font, 1)
        self.u.SendMessageW(hwnd, _EM_SETBKGNDCOLOR, 0, self.bg_color)
        # RichEdit 自己排的行距比 GDI 量出的 line_h 松（同款雅黑实测约松 30%），行距用 GDI 值
        # 反而更挤更矮——不锁死会导致：面板看着比以前"变大"、撑高/溢出高度算错、自动滚到底
        # 差一截、滚动条量程对不上。锁成与 line_h 一致的精确值，三个毛病一次修。
        hdc = self.u.GetDC(None)
        dpi = self.g.GetDeviceCaps(hdc, _LOGPIXELSY) or 96
        self.u.ReleaseDC(None, hdc)
        pf = _PARAFORMAT2()
        pf.cbSize = ctypes.sizeof(pf)
        pf.dwMask = _PFM_LINESPACING
        pf.bLineSpacingRule = _LINESPACING_EXACT
        pf.dyLineSpacing = round(self._line_h() * 1440 / dpi)  # twips
        self.u.SendMessageW(hwnd, _EM_SETPARAFORMAT, _SCF_ALL, ctypes.byref(pf))
        # RichEdit 没有系统默认的右键菜单（不像普通 Edit 控件），手动接管窗口过程加一个「复制」，
        # 其余消息原样转给 RichEdit 自己的过程——拖选/滚轮等原生行为不受影响。
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
        self._edit_proc = WNDPROC(self._edit_wndproc)  # 保引用防 GC
        self._edit_old_proc = self.u.GetWindowLongPtrW(hwnd, _GWLP_WNDPROC)
        self.u.SetWindowLongPtrW(hwnd, _GWLP_WNDPROC, self._edit_proc)
        return hwnd

    def _edit_wndproc(self, hwnd, msg, wp, lp):
        if msg == _WM_LBUTTONDBLCLK and not self.editing:
            self._start_edit()  # 双击才进编辑（单击太容易误触）；落到下面 CallWindowProcW，RichEdit 顺带选中双击的词
            # 单击(read-only)不进编辑，仍可拖选文字右键复制
        if msg == _WM_KEYDOWN and self.editing and wp in (_VK_RETURN, _VK_ESCAPE):
            self._end_edit(save=(wp == _VK_RETURN))
            return 0
        if msg == _WM_CHAR and self.editing and wp in (_VK_RETURN, _VK_ESCAPE):
            return 0  # 吞掉对应的字符消息，别让 RichEdit 插入换行/响铃
        if msg == _WM_KILLFOCUS and self.editing:
            self._end_edit(save=True)  # 点到面板外失焦：按保存处理，别让顺手一点丢了刚改的字
        if msg == _WM_CONTEXTMENU:
            x, y = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
            menu = self.u.CreatePopupMenu()
            # 动态项（深度整理/还原）排最上，分隔线，再「复制」；编辑态只留复制，别在改字时误触发整段重写
            extras = []
            if self.menu_items_provider and not self.editing:
                try:
                    extras = self.menu_items_provider() or []
                except Exception:
                    extras = []
            cmd_map = {}
            for i, (label, cb) in enumerate(extras, start=2):
                cmd_map[i] = cb
                self.u.AppendMenuW(menu, 0, i, label)
            if extras:
                self.u.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            self.u.AppendMenuW(menu, 0, 1, "复制")
            prev_fg = self.u.GetForegroundWindow()  # 记住口述目标（如 Claude 输入框），菜单用完还回去
            self.u.SetForegroundWindow(self.hwnd)  # 面板窗口 NOACTIVATE 从不聚焦，菜单要能收到点击得先把前台切过来
            cmd = self.u.TrackPopupMenu(menu, _TPM_RETURNCMD, x, y, 0, hwnd, None)
            self.u.DestroyMenu(menu)
            if cmd == 1:
                self.u.SendMessageW(hwnd, _WM_COPY, 0, 0)
            elif cmd in cmd_map:
                cmd_map[cmd]()
            # 恢复右键前的前台：否则前台停在面板窗口，之后说「发送」会把文字粘到面板而凭空消失
            if prev_fg and prev_fg != self.hwnd:
                self.u.SetForegroundWindow(prev_fg)
            return 0
        if msg == _WM_MOUSEWHEEL:
            # 没有 WS_VSCROLL，RichEdit 会把滚轮直接吞掉（实测 firstvisible 纹丝不动），自己滚
            delta = ctypes.c_short((wp >> 16) & 0xFFFF).value
            self._scroll_by(-delta * _WHEEL_LINES // 120)
            return 0
        return self.u.CallWindowProcW(self._edit_old_proc, hwnd, msg, wp, lp)

    def _start_edit(self):
        """双击正文进入编辑态：切可写、背景变色、抢前台接键盘（NOACTIVATE 窗口默认不抢），
        并在原地往上多长出一条提示条（不挤占正文区）。"""
        self.editing = True
        self._status_text = ""  # 编辑态不显示状态栏
        self._status_h = 0
        self.u.SendMessageW(self.edit, _EM_SETREADONLY, 0, 0)
        self.u.SendMessageW(self.edit, _EM_SETBKGNDCOLOR, 0, self.edit_bg_color)
        self._prev_fg = self.u.GetForegroundWindow()  # 记下原来在哪个窗口口述，退出编辑要还回去
        self.u.SetForegroundWindow(self.hwnd)
        self.u.SetFocus(self.edit)
        self._hint_h = self._line_h() + 10
        right_x, bottom_y = self._anchor
        new_h = self.h + self._hint_h
        self.u.MoveWindow(self.hwnd, right_x - self.w, bottom_y - new_h, self.w, new_h, True)
        self.u.MoveWindow(self.edit, self.pad, self.pad + self._hint_h,
                          self.content_w, new_h - self.pad * 2 - self._hint_h, True)
        self.h = new_h
        self.u.InvalidateRect(self.hwnd, None, True)
        if self.on_edit_start:
            self.on_edit_start()

    def cancel_edit(self):
        """外部强制退出编辑态、不保存：休眠等场景用（用户可能编辑到一半就静默超时/说了休眠词，
        这时缓存已被外部清空，编辑态若不一并退出，update() 会因 self.editing 跳过刷新，
        面板就卡在编辑界面不隐藏）。"""
        if self.editing:
            self._end_edit(save=False)

    def _end_edit(self, save):
        """退出编辑态：Enter 保存/Esc 放弃/失焦按保存处理。改回只读、背景变回原色、释放键盘焦点
        （否则只读框里光标还接着闪）、把前台还给编辑前的窗口（否则"发送/输入"语音指令找前台窗口
        找到的会是面板自己，回填不进真正的输入框），结果交给 on_edit_end 回调（None=放弃，否则是
        保存的新文字）落地；提示条撑高的那部分随下次 update() 自然收回（此处不用管）。"""
        if not self.editing:
            return
        self.editing = False
        text = self._get_text() if save else None
        self.u.SendMessageW(self.edit, _EM_SETREADONLY, 1, 0)
        self.u.SendMessageW(self.edit, _EM_SETBKGNDCOLOR, 0, self.bg_color)
        self.u.SetFocus(None)
        if self._prev_fg:
            self.u.SetForegroundWindow(self._prev_fg)
        self.u.InvalidateRect(self.hwnd, None, True)
        self._cur = None  # 编辑期间被跳过的刷新，这里强制下次 update() 补上
        if self.on_edit_end:
            self.on_edit_end(text)

    def _get_text(self):
        n = self.u.SendMessageW(self.edit, _WM_GETTEXTLENGTH, 0, 0)
        buf = ctypes.create_unicode_buffer(n + 1)
        self.u.SendMessageW(self.edit, _WM_GETTEXT, n + 1, ctypes.byref(buf))
        return buf.value.replace("\r\n", "\n")

    def _round_corners(self):
        # Win11: DWMWA_WINDOW_CORNER_PREFERENCE(33) = DWMWCP_ROUND(2)
        pref = ctypes.c_int(2)
        self.dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == 0x000F:      # WM_PAINT：只填边距那圈底色，文字区已经是 RichEdit 子控件自己画
            self._paint(hwnd)
            return 0
        if msg == 0x0014:      # WM_ERASEBKGND：不擦，保留亚克力背景（否则黑闪）
            return 1
        if msg == _WM_MOUSEWHEEL:   # 指在滑动条/留白上滚也要能滚（那里是父窗，不走子控件）
            self._scroll_by(-ctypes.c_short((wp >> 16) & 0xFFFF).value * _WHEEL_LINES // 120)
            return 0
        # 拖滑块：右侧那条留白整条都算命中区（滑块才 4px 宽，按像素抠太难点中）。
        # 按下时只记锚点、不跳位——纯相对拖动。
        if msg == 0x0201 and (lp & 0xFFFF) >= self.w - self.pad:   # WM_LBUTTONDOWN
            self._drag = (ctypes.c_short((lp >> 16) & 0xFFFF).value, self._first_line())
            self.u.SetCapture(hwnd)
            return 0
        if msg == 0x0200 and self._drag:                            # WM_MOUSEMOVE
            m = self._thumb_metrics()
            if m:
                track, total, visible, th = m
                y0, first0 = self._drag
                dy = ctypes.c_short((lp >> 16) & 0xFFFF).value - y0
                self._scroll_to(first0 + dy * (total - visible) // max(1, track - th))
            return 0
        if msg == 0x0202 and self._drag:                            # WM_LBUTTONUP
            self._drag = None
            self.u.ReleaseCapture()
            return 0
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def _first_line(self):
        return self.u.SendMessageW(self.edit, _EM_GETFIRSTVISIBLELINE, 0, 0)

    def _scroll_by(self, lines):
        self._scroll_to(self._first_line() + lines)

    def _scroll_to(self, first):
        """滚到"首可见行=first"，越界夹回；EM_LINESCROLL 只认相对行数，所以算差值。"""
        m = self._thumb_metrics()
        if not m:
            return
        track, total, visible, th = m
        cur = self._first_line()
        first = min(max(0, first), total - visible)
        if first != cur:
            self.u.SendMessageW(self.edit, _EM_LINESCROLL, 0, first - cur)
            self.u.InvalidateRect(self.hwnd, None, False)  # 滑块跟着挪

    def _paint(self, hwnd):
        ps = _PAINTSTRUCT()
        hdc = self.u.BeginPaint(hwnd, ctypes.byref(ps))
        if hdc:
            full = _RECT(0, 0, self.w, self.h)
            self.u.FillRect(hdc, ctypes.byref(full), self.edit_bg_brush if self.editing else self.bg_brush)
            if self.editing:
                self._paint_hint(hdc)
            if self._status_text and not self.editing:
                self._paint_status(hdc)
            if self._warn_text and not self.editing:
                self._paint_warn(hdc)
            self._paint_thumb(hdc)
        self.u.EndPaint(hwnd, ctypes.byref(ps))

    def _paint_hint(self, hdc):
        """编辑态顶部提示条："编辑中 · Esc 放弃 · Enter 保存"，居中显示在正文上方多长出的那段。"""
        self.g.SelectObject(hdc, self.font)
        self.g.SetTextColor(hdc, self.hint_color)
        self.g.SetBkMode(hdc, 1)  # TRANSPARENT
        r = _RECT(self.pad, 0, self.w - self.pad, self._hint_h)
        self.u.DrawTextW(hdc, _EDIT_HINT_TEXT, -1, ctypes.byref(r), 0x25)  # DT_CENTER|DT_VCENTER|DT_SINGLELINE

    def _paint_status(self, hdc):
        """底部状态栏：正在说话/整理中/倒计时等状态文字。有 warn 时状态栏在 warn 上方，否则贴底。"""
        self.g.SelectObject(hdc, self.font)
        self.g.SetTextColor(hdc, self._status_color)
        self.g.SetBkMode(hdc, 1)  # TRANSPARENT
        bottom = self.h - self.pad - self._warn_h
        r = _RECT(self.pad, bottom - self._status_h, self.w - self.pad, bottom)
        self.u.DrawTextW(hdc, self._status_text, -1, ctypes.byref(r), 0x24)  # DT_VCENTER|DT_SINGLELINE（靠左）

    def _paint_warn(self, hdc):
        """警告行：贴面板最底部，红色（复用 low_conf_color，同"红=需注意"语义），
        自动换行——长句（如"没找到输入框…请…再说发送指令"）单行放不下会截断，两行足够容纳。"""
        self.g.SelectObject(hdc, self.font)
        self.g.SetTextColor(hdc, self.low_conf_color)
        self.g.SetBkMode(hdc, 1)  # TRANSPARENT
        r = _RECT(self.pad, self.h - self.pad - self._warn_h, self.w - self.pad, self.h - self.pad)
        self.u.DrawTextW(hdc, self._warn_text, -1, ctypes.byref(r), 0x10)  # DT_WORDBREAK（左上起排，自动折行）

    def _thumb_metrics(self):
        """(滑道高, 总行数, 可见行数, 滑块高)，文字装得下就返回 None。量程只按行数算。
        编辑态顶部多出提示条、底部状态栏，滑道都要让开。"""
        top = self.pad + (self._hint_h if self.editing else 0)
        bottom = self.pad + self._status_h + self._warn_h
        track = self.h - bottom - top
        total = self.u.SendMessageW(self.edit, _EM_GETLINECOUNT, 0, 0)
        visible = max(1, track // self._line_h())
        if total <= visible:
            return None
        return track, total, visible, max(_THUMB_MIN, track * visible // total)

    def _paint_thumb(self, hdc):
        """自绘竖滚动条：画在文字区右侧那圈留白里，装得下就不画。"""
        m = self._thumb_metrics()
        if not m:
            return
        track, total, visible, th = m
        first = min(self._first_line(), total - visible)
        top = self.pad + (self._hint_h if self.editing else 0)
        ty = top + (track - th) * first // (total - visible)
        x2 = self.w - _THUMB_INSET
        r = _RECT(x2 - _THUMB_W, ty, x2, ty + th)
        self.u.FillRect(hdc, ctypes.byref(r), self.thumb_brush)

    def _line_h(self):
        hdc = self.u.GetDC(None)
        self.g.SelectObject(hdc, self.font)
        size = wintypes.SIZE()
        self.g.GetTextExtentPoint32W(hdc, "国", 1, ctypes.byref(size))
        self.u.ReleaseDC(None, hdc)
        return size.cy

    def update(self, clean, raw, hint, right_x, bottom_y, low_conf=False, warn="", **_kw):
        """正文（已整理/未整理）塞进 RichEdit，状态提示（正在说话/整理中/倒计时）固定在面板
        最底部一行作为状态栏、由父窗口 _paint() 绘制，不占正文区。
        把窗口右边缘对到 right_x、底边对到 bottom_y，文字多时只往上撑高；撑到 max_h 就不再涨，
        改显示尾部（最新内容）+ 滚动条（滚轮滚动）；全空则隐藏。
        low_conf=True：整理置信度偏低，clean 段改红色提示用户复核。
        warn：非空则在状态栏下面单开一行红字警告（如"没找到输入框"），提示紧急且不该被状态覆盖。"""
        self._anchor = (right_x, bottom_y)  # 供 _start_edit() 原地长高时用，编辑中也要保持更新
        if self.editing:
            return  # 编辑中：别拿识别结果覆盖用户正在改的字，退出编辑态后自然恢复刷新
        clean, raw, hint, warn = ((clean or "").strip(), (raw or "").strip(),
                                    (hint or "").strip(), (warn or "").strip())
        if not clean and not raw and not hint and not warn:
            if self._cur is not None:
                self.u.ShowWindow(self.hwnd, 0)
                self._cur = None
                self._floor_h = 0
                self._prev_body_len = 0
            return
        key = (clean, raw, hint, low_conf, warn)
        if key == self._cur:
            return  # 内容没变：别每帧重申置顶——会和圆环窗抢 z 序，重叠处一直抖

        line_h = self._line_h()

        # 底部状态栏：有 hint 时占一行高，无则不占；warn 在其下方（红色，两行足够容长句）
        self._status_text = hint
        self._status_color = self.hint_color
        self._status_h = line_h if hint else 0
        prev_warn_h = self._warn_h
        self._warn_text = warn
        self._warn_h = line_h * 2 if warn else 0  # 固定两行：默认警告文字（"没找到输入框…"）单行放不下
        if self._warn_h < prev_warn_h:  # 警告消失：让防抖 floor 一起回落，否则窗口停留在带警告的高度不缩
            self._floor_h = 0

        # RichEdit 只放正文（已整理+未整理），不含状态提示
        has_body = bool(clean or raw)
        clean_color = self.low_conf_color if low_conf else self.text_color
        segs = [(s.replace("\n", "\r\n"), c) for s, c in
                ((clean, clean_color), (raw, self.raw_color)) if s]
        full = "".join(s for s, _ in segs)
        self.u.SendMessageW(self.edit, _WM_SETTEXT, 0, ctypes.c_wchar_p(full))
        pos = 0
        cf = _CHARFORMATW()
        cf.cbSize = ctypes.sizeof(cf)
        cf.dwMask = _CFM_COLOR
        for s, color in segs:
            end = pos + len(s)
            self.u.SendMessageW(self.edit, _EM_SETSEL, pos, end)
            cf.crTextColor = color
            self.u.SendMessageW(self.edit, _EM_SETCHARFORMAT, _SCF_SELECTION, ctypes.byref(cf))
            pos = end
        body_len = len(full)
        if body_len < self._prev_body_len:
            self._floor_h = 0
        self._prev_body_len = body_len
        w = self.content_w + self.pad * 2
        if has_body:
            line_cnt = self.u.SendMessageW(self.edit, _EM_GETLINECOUNT, 0, 0)
            text_h = line_cnt * line_h
            full_h = text_h + self._status_h + self._warn_h + self.pad * 2
            overflow = full_h > self.max_h
            h = self.max_h if overflow else max(full_h, line_h + self._status_h + self._warn_h + self.pad * 2)
            edit_h = h - self.pad * 2 - self._status_h - self._warn_h
            self.u.ShowWindow(self.edit, 4)  # SW_SHOWNOACTIVATE
        else:
            # 没有正文、只有状态栏：状态栏当第一行，RichEdit 隐藏
            h = self._status_h + self._warn_h + self.pad * 2
            edit_h = 0
            self.u.ShowWindow(self.edit, 0)  # SW_HIDE
        # 防抖：窗口高度只增不减，避免状态栏切换时上下跳
        h = max(h, self._floor_h)
        self._floor_h = h
        edit_h = h - self.pad * 2 - self._status_h - self._warn_h
        self.w, self.h = w, h
        self.u.MoveWindow(self.hwnd, right_x - w, bottom_y - h, w, h, True)
        self.u.MoveWindow(self.edit, self.pad, self.pad, self.content_w, max(edit_h, 0), True)

        if has_body:
            self.u.SendMessageW(self.edit, _EM_SETSEL, pos, pos)   # 光标收到尾部（不留可见选区）
            visible_lines = max(1, edit_h // line_h)
            scroll_delta = max(0, line_cnt - visible_lines)
            if scroll_delta:
                self.u.SendMessageW(self.edit, _EM_LINESCROLL, 0, scroll_delta)
        self.u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        self.u.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x13)  # HWND_TOPMOST|NOSIZE|NOMOVE|NOACTIVATE
        self.u.InvalidateRect(self.hwnd, None, True)
        self._cur = key

    def destroy(self):
        try:
            self.u.DestroyWindow(self.hwnd)
            self.g.DeleteObject(self.font)
            self.g.DeleteObject(self.bg_brush)
            self.g.DeleteObject(self.edit_bg_brush)
            self.g.DeleteObject(self.thumb_brush)
        except Exception:
            pass


_ENDERS = "。！？!?…；;\n"  # 挤出/回退时认的"完整一句"边界


class TextBuffer:
    """未回填识别文字的缓冲，窗口分三段：frozen（已定稿、不再重整理的头部）+ clean_text
    （最近 context_chars 字的活跃窗口，已整理但仍随新句重整理）+ raw_segs（还没并入整理
    结果的新句）。用户停止说话后等 quiet_seconds 秒再触发整理（小范围走滑动窗口，
    深度/邮件/00后 对全文跑一次并冻结）。只有发送/flush_all 才整体回填输入框——
    攒多少字、静默多久都不自动回填。
    整理(polish: str->str)与回填(paste: str->None)由外部注入，本类不碰模型/剪贴板。"""

    def __init__(self, polish, paste, quiet_seconds=5.0, immediate=False,
                 min_confidence=0.6, context_chars=80,
                 polish_mode="小范围整理", full_polish=None):
        self.polish = polish            # str -> (整理文本, 置信度|None)，小范围滑动窗口用
        self.paste = paste
        self.quiet_seconds = quiet_seconds
        self.immediate = immediate
        self.min_confidence = min_confidence
        self.context_chars = context_chars
        self.polish_mode = polish_mode
        self.full_polish = full_polish  # (text, mode) -> (整理文本, 置信度|None)，深度/邮件/00后用
        self.frozen = ""
        self.clean_text = ""
        self.clean_conf = None
        self.raw_segs = []
        self._committing = []
        self._stop = False
        self._speaking = False
        self._speech_ended_at = None    # 话音落时刻；None=没在倒计时
        self.cleaning_mode = None       # 后台正在跑的整理模式；None=没在整理。供面板显示"XX中…"
        self._paused = False            # 面板编辑中：True 时跳过自动整理触发，避免和用户手改的字打架
        self._edit_needs_polish = False  # 进入编辑态时是否还有未整理内容（倒计时没走完），决定保存后要不要接着整理
        self._seg_lock = threading.Lock()
        self._commit_lock = threading.Lock()
        self._clean_lock = threading.Lock()
        if not immediate:
            threading.Thread(target=self._quiet_timer_worker, daemon=True).start()

    @property
    def countdown(self):
        """剩余几秒触发整理；None=没在倒计时（正在说话/已整理完/无新内容/面板已空）。
        面板文字为空（发送/回退/清空后）时直接 None，别让"X秒后整理"孤零零挂着。"""
        t = self._speech_ended_at
        if t is None or self._speaking:
            return None
        if not (self.raw_segs or self.clean_text or self.frozen):
            return None
        remain = self.quiet_seconds - (time.time() - t)
        return remain if remain > 0 else None

    def notify_speech_start(self):
        self._speaking = True
        self._speech_ended_at = None

    def notify_speech_end(self):
        self._speaking = False
        self._speech_ended_at = time.time()

    def pause(self):
        """面板进入手动编辑态：暂停自动整理触发，别拿整理结果盖用户正在改的字。
        记下此刻是否还有没整理完的内容——倒计时没走完就双击编辑的话，保存后不能当成
        "用户已确认无需整理"，得接着走整理，否则这段文字永远不会被整理。"""
        self._paused = True
        with self._seg_lock:
            self._edit_needs_polish = bool(self.raw_segs)

    def resume(self):
        """退出编辑态：恢复自动整理。倒计时期间时间没停，多半已到点，下一 tick 就会立刻触发一次。"""
        self._paused = False

    def _quiet_timer_worker(self):
        """停说后等 quiet_seconds 秒再触发整理。"""
        while not self._stop:
            time.sleep(0.5)
            if self._paused:
                continue
            t = self._speech_ended_at
            if t is None or self._speaking:
                continue
            if time.time() - t < self.quiet_seconds:
                continue
            has_work = bool(self.raw_segs)
            if not has_work and self.polish_mode != "小范围整理":
                with self._seg_lock:
                    has_work = bool(self.clean_text or self.raw_segs)
            if has_work:
                if self.polish_mode == "小范围整理":
                    self._clean_pending()
                else:
                    self._full_polish_pending()
            # 只在时间戳没被新 speech-end 更新过时才清零：整理跑 LLM 那几秒里,
            # 用户又说完一句时 notify_speech_end 会把 _speech_ended_at 更新成新的 T1;
            # 若无条件清零,这个 T1 被抹掉,末尾几句永远不会再触发整理。
            if self._speech_ended_at == t:
                self._speech_ended_at = None

    def trigger_polish_now(self):
        """不等 quiet_seconds 倒计时，立即触发一次整理（语音命令"文本整理"用）。"""
        if self.immediate or self._paused:
            return
        has_work = bool(self.raw_segs)
        if not has_work and self.polish_mode != "小范围整理":
            with self._seg_lock:
                has_work = bool(self.clean_text or self.raw_segs)
        if not has_work:
            return
        t = self._speech_ended_at
        if self.polish_mode == "小范围整理":
            self._clean_pending()
        else:
            self._full_polish_pending()
        if self._speech_ended_at == t:
            self._speech_ended_at = None

    def _full_polish_pending(self):
        """对全文跑深度/邮件/00后整理，结果落成 frozen。整理期间（LLM 调用耗时数秒）
        若用户又说了新句子（追加到 raw_segs 尾部）→ 本轮 LLM 结果对老快照那段仍作数，
        落成 frozen 就行；尾部新句留在 raw_segs 里等下一轮再整理，别整轮丢弃（否则老句
        白整理一次、新句也永远不会被处理）。但若 frozen/clean 变了或 raws 头部被动过
        （undo/clear/replace），说明窗口已失效，本轮结果作废。"""
        if not self.full_polish:
            self._clean_pending()
            return
        with self._clean_lock:
            with self._seg_lock:
                snap_frozen, snap_clean, snap_raws = self.frozen, self.clean_text, list(self.raw_segs)
                all_text = (snap_frozen + snap_clean + "".join(snap_raws)).strip()
            if not all_text:
                return
            self.cleaning_mode = self.polish_mode
            try:
                out, _conf = self.full_polish(all_text, self.polish_mode)
            except Exception as e:
                print(f"[warn] {self.polish_mode}失败：{e}")
                return
            finally:
                self.cleaning_mode = None
            if out and out.strip():
                with self._seg_lock:
                    if (self.frozen == snap_frozen
                            and self.clean_text == snap_clean
                            and self.raw_segs[:len(snap_raws)] == snap_raws):
                        self.frozen, self.clean_text, self.clean_conf = out.strip(), "", None
                        del self.raw_segs[:len(snap_raws)]

    def stop(self):
        self._stop = True

    def add(self, text):
        text = text.strip()
        if not text:
            return
        if self.immediate:
            self._commit("", [text])
            return
        with self._seg_lock:
            self.raw_segs.append(text)  # 只攒不回填、不挤出；整理交给后台定时线程

    def flush_all(self):
        """整体回填。返回是否真的发出去了：整块置信度不足时扣下不发、缓存原样留着（等下一轮
        整理或用户重说覆盖），不清空、不悄悄丢内容——调用方据此决定要不要按回车。
        没内容可发（面板本来就空）也算 True，不算"被拦下"，交给调用方按老规矩处理。
        按当前 polish_mode 决定收尾整理：小范围走 _clean_pending;深度/邮件/00后 走
        _full_polish_pending——否则倒计时没到就说"发送"会被小范围整理抢先,输出跟设置
        的风格对不上（例如邮件模式下变成轻度整理）。
        只在有未整理新句(raw_segs)时才收尾;整理完用户又手改过的文本(落在 clean_text/frozen)
        不再重跑整理——用户既然亲手改了,就是最终版,别拿模型再翻一遍把用户改的字盖回去。"""
        with self._seg_lock:
            has_raw = bool(self.raw_segs)
        if has_raw:
            if self.polish_mode == "小范围整理" or not self.full_polish:
                self._clean_pending()
            else:
                self._full_polish_pending()
        with self._seg_lock:
            clean, conf, raws = self.frozen + self.clean_text, self.clean_conf, self.raw_segs
            if not clean and not raws:
                return True
            if conf is not None and conf < self.min_confidence:
                return False  # 置信度不足：不清空缓存，留给下一轮/用户重说覆盖
            self.frozen, self.clean_text, self.clean_conf, self.raw_segs = "", "", None, []
        self._commit(clean, raws)
        return True

    def undo(self):
        """删面板里的最后一句：优先删还没整理的新句；没有则从整理结果尾部掐掉一句。
        返回是否删了。被删内容可能正被后台整理，本轮结果会因窗口对不上而丢弃，无妨。"""
        with self._seg_lock:
            if self.raw_segs:
                self.raw_segs.pop()
                return True
            t = self.clean_text.rstrip()
            if t:
                i = max(t.rfind(c, 0, len(t) - 1) for c in _ENDERS)  # 跳过末尾句号找上一个句界
                self.clean_text = t[:i + 1] if i >= 0 else ""
                return True
            f = self.frozen.rstrip()  # 活跃窗已删空，再从冻结头部尾巴掐一句
            if f:
                i = max(f.rfind(c, 0, len(f) - 1) for c in _ENDERS)
                self.frozen = f[:i + 1] if i >= 0 else ""
                return True
        return False

    def clear(self):
        """丢弃面板里攒的全部文字（整理结果+未整理新句）。进休眠时用：用户没处理就直接扔。"""
        with self._seg_lock:
            self.frozen, self.clean_text, self.clean_conf, self.raw_segs = "", "", None, []
            self._speech_ended_at = None
            self._speaking = False

    def replace_all(self, text):
        """整体替换缓存内容：面板手动编辑保存后落地。若进编辑前已经整理过（倒计时已走完），
        整段当作已整理文本落地、不用模型再复核；若进编辑前还在倒计时（内容从没整理过），
        编辑只是顺手改个字，不代表用户要跳过整理，整段照样当新句扔回去接着走整理流程。"""
        with self._seg_lock:
            if self._edit_needs_polish:
                self.frozen, self.clean_text, self.clean_conf, self.raw_segs = "", "", None, [text]
            else:
                self.frozen, self.clean_text, self.clean_conf, self.raw_segs = "", text, None, []

    def integrate(self, text):
        """深度整理结果落地：整段作为已定稿的 frozen 块，后续新句只在其后追加、绝不回炉
        重整理——否则一来新句，_clean_pending 会把深度成文的「一、二、三」按小范围拍平。
        跟 replace_all 的区别就在这：整段进 frozen（不再重跑），不进 clean_text（会重跑）。"""
        with self._seg_lock:
            self.frozen, self.clean_text, self.clean_conf, self.raw_segs = text, "", None, []

    @property
    def text(self):
        with self._seg_lock:
            return "".join(self._committing) + self.frozen + self.clean_text + "".join(self.raw_segs)  # 回填中的最旧，排在最前

    @property
    def text_parts(self):
        """面板分色用：(已整理, 未整理, 整理置信度是否偏低)。回填中的那段已过模型，算已整理。
        low_conf 只反映活跃窗(clean_text)那轮——frozen 是当初达标才冻结的，不再复核。"""
        with self._seg_lock:
            low_conf = self.clean_conf is not None and self.clean_conf < self.min_confidence
            return "".join(self._committing) + self.frozen + self.clean_text, "".join(self.raw_segs), low_conf

    def _clean_call(self, text):
        """整理一段文字，返回 (文本, 置信度)。调用方须已持 _clean_lock；失败兜底回原文，别丢话。"""
        try:
            out, conf = self.polish(text)
            return (out or text), conf
        except Exception:
            return text, None

    def _freeze_head(self):
        """把活跃窗里滚出上限的头部并入 frozen——只在句界(_ENDERS)切，保证冻结段是完整整句；
        clean_text 保留最后 <=context_chars 字继续参与重整理。低置信度那轮不冻（留在活跃窗
        继续标红复核，转高再冻）；尾部 context_chars 字内无句界也不冻（等下句补上标点再说）。
        调用方须持 _seg_lock。"""
        cap = self.context_chars
        ct = self.clean_text
        if cap <= 0 or len(ct) <= cap:
            return
        if self.clean_conf is not None and self.clean_conf < self.min_confidence:
            return
        start = len(ct) - cap  # 想保留最后 cap 字：从这里往后找第一个句界当冻结边界
        cut = next((i for i in range(start, len(ct)) if ct[i] in _ENDERS), -1)
        if cut < 0:
            return
        self.frozen += ct[:cut + 1]
        self.clean_text = ct[cut + 1:]

    def _clean_pending(self):
        """把还没整理的新句并入整理结果：只把「活跃窗口 clean_text + 新句」一起送模型（不含
        已冻结的 frozen 头部——那截字数封顶的关键）。幂等，快照在拿到模型锁后才取（排队线程醒来
        拿最新窗口，前一轮已捎带则直接返回）；整理期间窗口被挤出/回退/清空则本轮结果作废。"""
        with self._clean_lock:
            with self._seg_lock:
                self._freeze_head()  # 先把滚出上限的整句冻起来，snap 只带活跃窗，封住输入长度
                snap_clean, snap_raws = self.clean_text, list(self.raw_segs)
            if not snap_raws:
                return
            self.cleaning_mode = "小范围整理"
            try:
                out, conf = self._clean_call(snap_clean + "".join(snap_raws))
            finally:
                self.cleaning_mode = None
            with self._seg_lock:
                if self.clean_text == snap_clean and self.raw_segs[:len(snap_raws)] == snap_raws:
                    self.clean_text, self.clean_conf = out, conf
                    del self.raw_segs[:len(snap_raws)]

    def _commit(self, clean, raws):
        """回填：已整理部分直接贴；raw 部分（整理没跟上/immediate 模式）现整理再贴。"""
        with self._commit_lock:
            with self._seg_lock:
                self._committing = [clean] + list(raws)
            try:
                out = clean
                if raws:
                    with self._clean_lock:
                        seg, _conf = self._clean_call("".join(raws))
                        out += seg
                if out.strip():
                    self.paste(out)
            finally:
                with self._seg_lock:
                    self._committing = []


if __name__ == "__main__":
    def _trigger(buf):
        """模拟一次"话音落+等安静"触发整理。"""
        buf.notify_speech_end()
        buf._speech_ended_at = time.time() - buf.quiet_seconds - 0.1  # 跳过等待

    def settle(buf):
        _trigger(buf)
        for _ in range(400):
            with buf._seg_lock:
                if not buf.raw_segs:
                    return
            time.sleep(0.005)
        raise AssertionError("后台整理没收敛")

    got, calls = [], []
    def cln(t):
        calls.append(t)
        return t.upper(), 0.9
    b = TextBuffer(polish=cln, paste=got.append, quiet_seconds=0.01)
    b.add("aa。"); settle(b)
    assert got == [] and b.text == "AA。", (got, b.text)
    b.add("bb。"); settle(b)
    assert b.text == "AA。BB。", b.text
    assert b.text_parts == ("AA。BB。", "", False), b.text_parts
    b.add("cccccc。" * 5); settle(b)
    assert got == [], got
    b.flush_all()
    assert got == ["AA。BB。" + "CCCCCC。" * 5] and b.text == "", (got, b.text)
    assert b.undo() is False
    b2 = TextBuffer(polish=lambda t: (t, None), paste=lambda t: None, quiet_seconds=999)
    b2.clean_text = "AA。"
    b2.add("bb。")
    assert b2.text_parts == ("AA。", "bb。", False), b2.text_parts
    b3 = TextBuffer(polish=lambda t: (t.upper(), 0.9), paste=lambda t: None, quiet_seconds=0.01)
    b3.add("x。"); assert b3.undo() is True and b3.text == ""
    b3.add("y。z。y。z。"); settle(b3)
    assert b3.undo() is True and b3.text == "Y。Z。Y。", b3.text
    got2 = []
    im = TextBuffer(lambda t: (t, None), got2.append, immediate=True)
    im.add("a"); im.add("b")
    assert got2 == ["a", "b"], got2
    conf4, got4 = {"v": 0.3}, []
    b4 = TextBuffer(polish=lambda t: (t.upper(), conf4["v"]), paste=got4.append, quiet_seconds=0.01)
    b4.add("m。"); settle(b4)
    assert b4.text_parts == ("M。", "", True), b4.text_parts
    assert b4.flush_all() is False and got4 == [] and b4.text == "M。"
    conf4["v"] = 0.9
    b4.add("n。"); settle(b4)
    assert b4.text_parts == ("M。N。", "", False), b4.text_parts
    assert b4.flush_all() is True and got4 == ["M。N。"] and b4.text == ""
    b5 = TextBuffer(polish=lambda t: (t.upper(), 0.3), paste=lambda t: None, quiet_seconds=999)
    b5.clean_text, b5.raw_segs = "旧文本。", ["新句。"]
    b5.replace_all("手动改过的文本。")
    assert b5.text_parts == ("手动改过的文本。", "", False), b5.text_parts
    # 深度整理模式测试
    full_calls = []
    def fake_full(text, mode):
        full_calls.append((text, mode))
        return f"【{mode}】{text.upper()}", 0.9
    b6 = TextBuffer(polish=cln, paste=lambda t: None, quiet_seconds=0.01,
                    polish_mode="深度整理", full_polish=fake_full)
    b6.add("hello。"); _trigger(b6); time.sleep(0.1)
    # 深度整理应把全文 integrate 进 frozen
    for _ in range(200):
        if b6.frozen:
            break
        time.sleep(0.005)
    assert "深度整理" in b6.frozen, b6.frozen
    assert not b6.raw_segs and not b6.clean_text
    # 深度整理跑 LLM 那几秒里新到的句子：老快照结果应落成 frozen，新句留在 raw_segs 等下轮
    slow_gate = threading.Event()
    def slow_full(text, mode):
        slow_gate.wait(2.0)
        return f"【{mode}】{text}", 0.9
    b7 = TextBuffer(polish=cln, paste=lambda t: None, quiet_seconds=0.01,
                    polish_mode="深度整理", full_polish=slow_full)
    b7.add("老句1。")
    poll = threading.Thread(target=lambda: b7._full_polish_pending(), daemon=True)
    poll.start()
    time.sleep(0.05)  # 保证 LLM 调用已开始阻塞
    b7.add("新句2。"); b7.add("新句3。")  # 整理期间新增
    slow_gate.set()
    poll.join(2.0)
    assert "老句1" in b7.frozen and "新句2" not in b7.frozen, b7.frozen
    assert b7.raw_segs == ["新句2。", "新句3。"], b7.raw_segs
    # 倒计时保住：整理返回时若 _speech_ended_at 已被新的 notify 更新，不能抹掉
    b8 = TextBuffer(polish=cln, paste=lambda t: None, quiet_seconds=0.01,
                    polish_mode="深度整理", full_polish=fake_full)
    b8.add("x。"); b8.notify_speech_end()
    b8._speech_ended_at = time.time() - 1  # 已过 quiet
    t_before = b8._speech_ended_at
    # 手动模拟 worker 逻辑，中途更新 _speech_ended_at
    def racing_full(text, mode):
        b8._speech_ended_at = time.time()  # 模拟新句 stop
        return f"【{mode}】{text}", 0.9
    b8.full_polish = racing_full
    b8._full_polish_pending()
    # 模拟 worker 的清零判断
    if b8._speech_ended_at == t_before:
        b8._speech_ended_at = None
    assert b8._speech_ended_at is not None, "新 speech-end 时间戳不能被抹"
    print("panel self-check OK")
