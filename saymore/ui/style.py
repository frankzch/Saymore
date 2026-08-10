# -*- coding: utf-8 -*-
"""统一的 Apple 风 UI 主题：配色/字体常量 + DWM 圆角 + 自绘弹出菜单。
历史小窗（history_view）和小圆环右键菜单共用，保证观感一致。
tkinter + DWM（均为系统自带，零新依赖）；调用方在后台线程里跑。"""
import ctypes

# macOS 系配色（浅色模式）
BG = "#F5F5F7"        # 窗口底：苹果官网同款浅灰
CARD = "#FFFFFF"      # 卡片底
BORDER = "#E5E5EA"    # 分隔线/描边
TEXT = "#1D1D1F"      # 正文：近黑
MUTED = "#86868B"     # 次要信息：中灰
ACCENT = "#007AFF"    # 强调蓝（链接/主按钮）
HOVER = "#EAEAEC"     # 菜单项悬停底色
DANGER = "#FF3B30"    # 危险动作红（退出）

FONT_FACE = "Microsoft YaHei UI"
FONT = (FONT_FACE, 10)
FONT_SMALL = (FONT_FACE, 9)

# 玻璃缓存面板（panel.GlassWindow）同主题的默认值：底色同 BG 的磨砂（AARRGGBB），
# 已整理=绿色（整理完成），未整理=黑色（原始转写），状态提示=蓝灰（底部状态栏）
PANEL_TINT = "E6F5F5F7"
PANEL_TEXT_RGB = [34, 139, 34]
PANEL_RAW_TEXT_RGB = [0, 0, 0]
PANEL_HINT_TEXT_RGB = [120, 132, 158]  # 蓝灰：跟绿色正文换色相，避免看着像正文延续
PANEL_LOW_CONF_RGB = [255, 59, 48]  # 整理置信度偏低：跟 DANGER 同色的红


def round_corners(tk_root):
    """给 tkinter 顶层窗口套 Win11 DWM 圆角（DWMWA_WINDOW_CORNER_PREFERENCE=ROUND）。
    Win10 无此属性，失败静默忽略（方角而已）。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(tk_root.winfo_id()) or tk_root.winfo_id()
        pref = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


def flat_button(parent, text, command, fg=ACCENT, bg=None, font=FONT):
    """无边框文字按钮（macOS 链接按钮风）：平时纯文字，悬停变底色。"""
    import tkinter as tk
    b = tk.Label(parent, text=text, fg=fg, bg=bg or parent["bg"], font=font,
                 padx=10, pady=4, cursor="hand2")
    b.bind("<Button-1>", lambda e: command())
    b.bind("<Enter>", lambda e: b.configure(bg=HOVER))
    b.bind("<Leave>", lambda e: b.configure(bg=bg or parent["bg"]))
    return b


def popup_menu(items):
    """光标处弹出 Apple 风菜单。items=[(文字, 回调) 或 None(分隔线)]，点项执行回调后关闭；
    鼠标移开 0.5s、失焦或 Esc 关闭。自带 Tk 实例，须在后台线程调用。"""
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)   # 无系统边框，自己画
    root.attributes("-topmost", True)
    root.configure(bg=BORDER)     # 最外 1px 当描边
    body = tk.Frame(root, bg=CARD, padx=4, pady=4)
    body.pack(padx=1, pady=1)

    def close(*_):
        try:
            root.destroy()
        except Exception:
            pass

    pending = [None]  # 鼠标离开整个菜单后延时关闭；回到菜单则取消

    def schedule_close(_e):
        pending[0] = root.after(500, close)

    def cancel_close(_e):
        if pending[0]:
            root.after_cancel(pending[0])
            pending[0] = None

    for it in items:
        if it is None:
            tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=6, pady=3)
            continue
        label, cb = it[0], it[1]
        fg = DANGER if "danger" in it[2:] else TEXT  # ("退出", cb, "danger") → 红字
        row = tk.Label(body, text=label, anchor="w", bg=CARD, fg=fg,
                       font=FONT, padx=12, pady=6, width=18, cursor="hand2")
        row.pack(fill="x")
        row.bind("<Enter>", lambda e, w=row: w.configure(bg=HOVER))
        row.bind("<Leave>", lambda e, w=row: w.configure(bg=CARD))
        row.bind("<Button-1>", lambda e, f=cb: (close(), f()))
    # 定位到光标右下，屏幕边缘往回收
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x, y = root.winfo_pointerx(), root.winfo_pointery()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{min(x, sw - w - 4)}+{min(y, sh - h - 4)}")
    round_corners(root)
    root.bind("<Escape>", close)
    root.bind("<FocusOut>", close)
    root.bind("<Leave>", schedule_close)
    root.bind("<Enter>", cancel_close)
    root.focus_force()
    root.mainloop()


def confirm(title, text, ok="确认", cancel="取消", danger=False):
    """Apple 风确认框：白卡片、右下角 取消/确认 文字按钮。返回 True/False。
    自带 Tk 实例并阻塞到用户选择，与 MessageBox 一样在调用线程里用。"""
    import tkinter as tk

    result = [False]
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.configure(bg=BG)
    root.resizable(False, False)
    tk.Label(root, text=text, bg=BG, fg=TEXT, font=FONT,
             wraplength=260, justify="left").pack(padx=24, pady=(20, 12))
    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", padx=16, pady=(0, 12))

    def done(v):
        result[0] = v
        root.destroy()

    flat_button(bar, ok, lambda: done(True),
                fg=DANGER if danger else ACCENT, bg=BG).pack(side="right")
    flat_button(bar, cancel, lambda: done(False), fg=MUTED, bg=BG).pack(side="right")
    root.bind("<Escape>", lambda e: done(False))
    round_corners(root)
    # 居中到屏幕
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.focus_force()
    root.mainloop()
    return result[0]


if __name__ == "__main__":
    # 自检：常量齐全 + flat_button/round_corners 不炸（无头也能建 Tk）
    import tkinter as tk
    r = tk.Tk()
    r.withdraw()
    f = tk.Frame(r, bg=BG)
    b = flat_button(f, "测试", lambda: None)
    assert b["text"] == "测试" and b["fg"] == ACCENT
    round_corners(r)
    r.destroy()
    print("ui_style 自检通过")
