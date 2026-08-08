"""窗口焦点定位与文本输出（Win32 + UI Automation）。

把语音识别结果粘到当前前台窗口的输入框：不切换窗口，用 UIA 精确定位
（Claude 的 Prompt 框）或自动挑面积最大的输入框；另含点掉 CC 权限弹框。
从 voice_input.py 拆出——纯 Windows API，不依赖主程序状态。
"""
import ctypes
import time
from ctypes import wintypes

import keyboard
import pyperclip


_FOCUS_READY = False


def _setup_focus_api():
    """给用到的 user32/kernel32 函数设好 argtypes，避免 64 位句柄被默认 c_int 截断。"""
    global _FOCUS_READY
    if _FOCUS_READY:
        return
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    P, DWORD, HWND, INT, BOOL = (ctypes.c_void_p, wintypes.DWORD, wintypes.HWND,
                                 ctypes.c_int, wintypes.BOOL)
    u.GetForegroundWindow.restype = HWND
    u.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, INT]
    u.IsWindowVisible.argtypes = [HWND]
    u.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    u.GetWindowThreadProcessId.restype = DWORD
    u.SetForegroundWindow.argtypes = [HWND]
    u.BringWindowToTop.argtypes = [HWND]
    u.ShowWindow.argtypes = [HWND, INT]
    u.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
    u.IsIconic.argtypes = [HWND]
    k.GetCurrentThreadId.restype = DWORD
    k.OpenProcess.restype = P
    k.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    k.QueryFullProcessImageNameW.argtypes = [P, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)]
    k.CloseHandle.argtypes = [P]
    _FOCUS_READY = True


def _proc_name(hwnd):
    """返回拥有该窗口的进程名（如 'claude.exe'），失败返回 ''。"""
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        k.CloseHandle(h)
    return ""


_UIA_COM_INIT = False
_UIA_CACHE = {"ctrl": None, "key": None}


def _focus_input_uia(hwnd, ctrl_name):
    """用 UI Automation 把焦点定位到 hwnd 窗口里名为 ctrl_name 的可聚焦控件
    （如 Claude 桌面端的输入框在 UIA 里名为 'Prompt'）。直接用窗口句柄定位，
    不依赖窗口标题文本（标题可能变成会话名而不含 'Claude'）。
    首次查找约 0.4s，之后缓存控件对象复用近乎瞬时；缓存失效则自动重查。返回是否成功。"""
    global _UIA_COM_INIT
    try:
        import comtypes
        import uiautomation as auto
    except Exception as e:
        print(f"[warn] 未安装 uiautomation，无法精确聚焦输入框：{e}")
        return False
    if not _UIA_COM_INIT:
        try:
            comtypes.CoInitializeEx()  # worker 线程初始化 COM
        except Exception:
            pass
        _UIA_COM_INIT = True

    key = (int(hwnd), ctrl_name)
    if _UIA_CACHE["key"] == key and _UIA_CACHE["ctrl"] is not None:
        try:
            if _UIA_CACHE["ctrl"].SetFocus():  # SetFocus 返回 bool，False 表示没真正聚焦
                return True
        except Exception:
            pass
        _UIA_CACHE["ctrl"] = None  # 缓存失效或聚焦失败，重查

    win = auto.ControlFromHandle(hwnd)
    if win is None:
        print(f"[warn] UIA 无法从句柄 {int(hwnd)} 获取窗口控件")
        return False
    ctrl = win.Control(searchDepth=40,
                       Compare=lambda c, d: (c.Name == ctrl_name and c.IsKeyboardFocusable))
    if not ctrl.Exists(0, 0):
        print(f"[warn] UIA 未找到名为 {ctrl_name!r} 的可聚焦控件")
        return False
    try:
        ok = bool(ctrl.SetFocus())
    except Exception as e:
        print(f"[warn] UIA SetFocus 失败：{e}")
        return False
    _UIA_CACHE["ctrl"], _UIA_CACHE["key"] = ctrl, key
    return ok


def click_permission_button(win_title):
    """在目标窗口里找权限确认弹框的按钮并点击：优先"总是允许"(名字含 always)，
    否则"允许一次"(含 allow)。用 UIA Invoke 点击（无需把窗口切到前台），
    返回点中的按钮名；没有待确认弹框则返回 None。"""
    global _UIA_COM_INIT
    try:
        import comtypes
        import uiautomation as auto
    except Exception as e:
        print(f"[warn] 未安装 uiautomation，无法点击弹框：{e}")
        return None
    if not _UIA_COM_INIT:
        try:
            comtypes.CoInitializeEx()
        except Exception:
            pass
        _UIA_COM_INIT = True

    win = None
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.Name and win_title.lower() in w.Name.lower():
                win = w
                break
        except Exception:
            pass
    if win is None:
        print(f"[warn] UIA 未找到标题含 {win_title!r} 的窗口")
        return None

    def find(kw):
        # ponytail: 取树序第一个名字含 kw 的按钮；权限弹框按钮通常唯一，够用
        ctrl = win.Control(searchDepth=40,
                           Compare=lambda c, d: (c.ControlTypeName == "ButtonControl"
                                                 and bool(c.Name) and kw in c.Name.lower()))
        return ctrl if ctrl.Exists(0, 0) else None

    # 用精确词组，避开标题按钮（如 'Allow Claude to use PowerShell?' 也含 allow）
    btn = find("always allow") or find("allow once")
    if btn is None:
        print("[info] 未发现 allow/always 按钮（当前可能没有待确认弹框）")
        return None
    name = btn.Name
    try:
        btn.GetInvokePattern().Invoke()
    except Exception:
        try:
            btn.Click()  # 退路：Invoke 不支持时用模拟点击
        except Exception as e:
            print(f"[warn] 点击弹框按钮失败：{e}")
            return None
    print(f"[done] 弹框确认：已点击 {name!r}")
    return name


def _focus_any_input_uia(hwnd):
    """通用输入框定位（用于 Claude 以外的任意应用：DeepSeek 等网页、桌面软件）。
    判据：页面/文档「根容器」铺满整窗（面积≈窗口面积），它是容器不是输入框；真正的
    输入框是窗口里那些「比整窗小」的可编辑控件，其中面积最大的通常就是主输入框
    （聊天框 > 地址栏，自动躲开浏览器地址栏）。
    - 焦点已在一个「非整窗」的输入框上（用户自己点过）→ 不动；
    - 否则在「非整窗」可编辑控件里挑面积最大的 → 聚焦；
    - 一个都没有（如整窗文本编辑器，没有更小的独立输入框）→ 不动，贴在当前焦点处。
    返回是否成功聚焦到某控件。"""
    global _UIA_COM_INIT
    try:
        import comtypes
        import uiautomation as auto
    except Exception as e:
        print(f"[warn] 未安装 uiautomation，无法定位输入框：{e}")
        return False
    if not _UIA_COM_INIT:
        try:
            comtypes.CoInitializeEx()
        except Exception:
            pass
        _UIA_COM_INIT = True

    # 前台窗口面积：用来把「铺满整窗的页面根/文档容器」从输入框候选里剔除
    wr = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(wr))
    win_area = max(1, (wr.right - wr.left) * (wr.bottom - wr.top))

    def _editable_by_pattern(c):
        """按「能力」而非类型名判断可输入：支持 TextEditPattern（可编辑文本）或可写
        ValuePattern。Electron/网页应用的输入框常是 Group/Custom 型（如 Claude 桌面端
        的 'Prompt' 组），类型名对不上，只有能力对得上——这是跨应用通用的关键。"""
        try:
            if c.GetPattern(auto.PatternId.TextEditPattern):
                return True
        except Exception:
            pass
        try:
            vp = c.GetPattern(auto.PatternId.ValuePattern)
            if vp and not vp.CurrentIsReadOnly:
                return True
        except Exception:
            pass
        return False

    def field_area(c):
        """是「非整窗」可输入可聚焦控件则返回其面积，否则返回 -1（非候选）。
        判据不认类型名，认能力：经典 Edit/Document 直接算；其余容器型（Group/Custom/
        Pane）须经 pattern 证实可编辑。按钮/列表等一律快速排除，省下 COM 探测开销。"""
        try:
            if not c.IsKeyboardFocusable:
                return -1
            t = c.ControlTypeName
            if t in ("EditControl", "DocumentControl", "ComboBoxControl"):
                pass  # 经典输入型，直接算候选
            elif t in ("GroupControl", "CustomControl", "PaneControl"):
                if not _editable_by_pattern(c):
                    return -1
            else:
                return -1  # 按钮/列表/图片等非输入型，不必探 pattern
            r = c.BoundingRectangle
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            # ponytail: 0.7 阈值——占窗 70%+ 视为页面根/整窗编辑器容器，不是要跳转的输入框
            return -1 if area >= 0.7 * win_area else area
        except Exception:
            return -1

    try:
        foc = auto.GetFocusedControl()
        if foc is not None and field_area(foc) >= 0:
            return True  # 焦点已在一个真正的（非整窗）输入框上，不动
    except Exception:
        pass

    win = auto.ControlFromHandle(hwnd)
    if win is None:
        return False
    best, best_area = None, -1

    def walk(c, depth):
        nonlocal best, best_area
        if depth > 40:
            return
        for ch in c.GetChildren():
            area = field_area(ch)
            if area > best_area:
                best, best_area = ch, area
            walk(ch, depth + 1)

    walk(win, 0)  # ponytail: 全树遍历，panel 模式下仅说「发送/输入」时触发一次，可接受
    # ceiling: 整窗编辑器里若另有小输入框（如搜索框），可能被误选；主用途是网页聊天框，暂不处理
    if best is None:
        print("[info] 未在当前窗口找到独立输入框，直接粘贴到当前焦点处")
        return False
    try:
        best.SetFocus()
    except Exception as e:
        print(f"[warn] 聚焦输入框失败：{e}")
        return False
    return True


def focus_window(target, input_name=""):
    """把焦点定位到当前前台窗口的输入框（不再切换/激活窗口——你在哪个窗口就用哪个，
    便于在 DeepSeek 等网页里用语音）。前台是配置的目标窗口(如 Claude)且给了 input_name 时，
    用 UIA 精确定位该控件(Prompt)；否则自动找输入框聚焦。"""
    _setup_focus_api()
    u = ctypes.windll.user32
    fg = u.GetForegroundWindow()
    if not fg:
        return
    is_target = False
    if target:
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(fg, buf, 512)
        tl = target.lower()
        is_target = tl in buf.value.lower() or tl in _proc_name(fg).lower()
    if is_target and input_name:
        if not _focus_input_uia(fg, input_name):
            _focus_any_input_uia(fg)  # Prompt 没找到时退回通用查找
    else:
        _focus_any_input_uia(fg)


def output_text(text, paste, focus_title=None, focus_input=""):
    text = text.strip()
    if not text:
        print("[info] 未识别到文本")
        return
    if not paste:
        pyperclip.copy(text)
        print(f"[done] 已复制到剪贴板: {text}")
        return
    focus_window(focus_title, focus_input)
    old = ""
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")
    print(f"[done] {text}")
    time.sleep(0.2)
    try:
        pyperclip.copy(old)  # 还原原剪贴板内容
    except Exception:
        pass
