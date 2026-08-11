"""窗口焦点定位与文本输出（Win32 + UI Automation）。

把语音识别结果粘到当前最上层窗口的输入框：不切换窗口，用 UIA 精确定位
（Claude 的 Prompt 框）或自动挑面积最大的输入框；另含点掉 CC 权限弹框。
从 voice_input.py 拆出——纯 Windows API，不依赖主程序状态。

目标窗口选择：不用 GetForegroundWindow（用户点一下任务栏前台就变 shell，
输入就落空），改成沿 Z 序自顶向下找第一个「真实」窗口——跳过 Saymore
自己的窗口（overlay/panel/主界面，按 PID 判定，覆盖所有子窗口）、系统
shell、隐藏/最小化/cloaked 的空壳。
"""
import ctypes
import os
import time
from ctypes import wintypes

import keyboard
import pyperclip


_FOCUS_READY = False

# 明显不接受文本粘贴的控件类型:焦点落在这些上就不信任,走遍历兜底。
# 挡"用户随手点在工具栏按钮/菜单上"的常见误判。
_NON_INPUT_TYPES = {
    "ButtonControl", "MenuItemControl", "MenuControl", "TabItemControl",
    "ListItemControl", "TreeItemControl", "ToolBarControl", "StatusBarControl",
    "CheckBoxControl", "RadioButtonControl", "HyperlinkControl", "ImageControl",
    "SeparatorControl", "ScrollBarControl", "SliderControl",
}

# 系统 shell / 桌面 类名，不当作用户窗口
_SHELL_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
    "Windows.UI.Core.CoreWindow",  # 开始菜单/搜索等 UWP 壳（通常也 cloaked）
    "MultitaskingViewFrame", "XamlExplorerHostIslandWindow",
    "ForegroundStaging", "ApplicationManager_DesktopShellWindow",
}


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
    u.GetTopWindow.argtypes = [HWND]
    u.GetTopWindow.restype = HWND
    u.GetWindow.argtypes = [HWND, wintypes.UINT]
    u.GetWindow.restype = HWND
    u.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, INT]
    u.GetWindowLongW.argtypes = [HWND, INT]
    u.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetAncestor.argtypes = [HWND, wintypes.UINT]
    u.GetAncestor.restype = HWND
    # DWM: 判 UWP 隐藏壳（cloaked），避免把「开始菜单/搜索」这类假顶层选进来
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute.argtypes = [HWND, DWORD, P, DWORD]
    except Exception:
        pass
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


def _ensure_uia():
    """按需 import + COM 初始化。返回 (auto, ok);ok=False 表示 uiautomation 不可用。"""
    global _UIA_COM_INIT
    try:
        import comtypes
        import uiautomation as auto
    except Exception as e:
        print(f"[warn] 未安装 uiautomation:{e}")
        return None, False
    if not _UIA_COM_INIT:
        try:
            comtypes.CoInitializeEx()
        except Exception:
            pass
        _UIA_COM_INIT = True
    return auto, True


def _is_editable_ctrl(c, auto):
    """判「可编辑控件」:经典 Edit/Document/ComboBox 直接算;Group/Custom/Pane 型
    要 pattern 证实(Electron/网页输入框类型对不上,只有能力对得上)。"""
    try:
        if not c.IsKeyboardFocusable:
            return False
        t = c.ControlTypeName
        if t in ("EditControl", "DocumentControl", "ComboBoxControl"):
            return True
        if t not in ("GroupControl", "CustomControl", "PaneControl"):
            return False
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
    except Exception:
        return False


def _focus_is_own_input():
    """当前全局键盘焦点是否值得信任(直接原地贴,不去找 Z 序顶层窗口)。
    关键场景:上面压着悬浮窗(可能是 Saymore 自己的面板,也可能是别的应用),
    用户光标却在后面某扇窗口的输入框里——只要焦点在,就该用它,别去找 Z 序顶层
    (会拿到那扇悬浮窗,然后在里面找不到输入框,报"没找到")。

    判定策略(和 _focus_any_input_uia 一致):
    - 焦点属于 Saymore 自己进程 → 不信任(避免语音面板短暂拿焦点时把文字贴回自己)
    - 焦点控件是黑名单类型(Button/MenuItem 等明显非输入)→ 不信任,让上层遍历兜底
    - 其余(含 WPS 正文这种 UIA 认不出的自定义控件)→ 信任

    不再用 _is_editable_ctrl 严检:WPS/Word 正文这类自绘控件三样(类型/pattern/
    IsKeyboardFocusable)全对不上,却真的能接文本;严检就漏掉。"""
    auto, ok = _ensure_uia()
    if not ok:
        return False
    try:
        foc = auto.GetFocusedControl()
        if foc is None:
            return False
        try:
            if foc.ControlTypeName in _NON_INPUT_TYPES:
                return False
        except Exception:
            pass
        hwnd = foc.NativeWindowHandle
        if hwnd:
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                wintypes.HWND(hwnd), ctypes.byref(pid))
            if pid.value == os.getpid():
                return False
        return True
    except Exception:
        return False


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

    def _classify(c):
        """返回 (is_small, area):is_small=True 是「非整窗」小输入框(聊天框/搜索框),
        False 是「整窗级」可编辑文档容器(WPS/Word/Excel/PPT 主编辑区、整窗文本编辑器);
        None 表示非候选。判据不认类型名,认能力:经典 Edit/Document 直接算;其余容器型
        (Group/Custom/Pane)须经 pattern 证实可编辑。按钮/列表等一律快速排除。"""
        try:
            if not c.IsKeyboardFocusable:
                return None
            t = c.ControlTypeName
            if t in ("EditControl", "DocumentControl", "ComboBoxControl"):
                pass  # 经典输入型,直接算候选
            elif t in ("GroupControl", "CustomControl", "PaneControl"):
                if not _editable_by_pattern(c):
                    return None
            else:
                return None  # 按钮/列表/图片等非输入型,不必探 pattern
            r = c.BoundingRectangle
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            # 0.7 阈值:占窗 70%+ 视为整窗级容器(网页页面根 or WPS/Word 主编辑区);
            # 优先小输入框(聊天框优先于地址栏),都没有再退到整窗文档容器
            return (True, area) if area < 0.7 * win_area else (False, area)
        except Exception:
            return None

    # 关键短路:焦点若已经在目标窗口内,原则上一律不动——用户已经点过一次光标,
    # 信任那个位置。WPS/Word 正文常是 UIA 认不出的自定义控件,靠类型/pattern
    # 反猜会把文字甩到工具栏字体框那种奇怪地方。
    # 例外:焦点明摆着是按钮/菜单/列表项等"绝不接受粘贴"的类型,则不信任,走遍历兜底
    # (挡住"用户随手点在工具栏按钮上"的常见误判)。黑名单见模块级 _NON_INPUT_TYPES。
    try:
        foc = auto.GetFocusedControl()
        if foc is not None:
            try:
                is_non_input = foc.ControlTypeName in _NON_INPUT_TYPES
            except Exception:
                is_non_input = False
            if not is_non_input:
                fh = foc.NativeWindowHandle
                if fh:
                    root = ctypes.windll.user32.GetAncestor(wintypes.HWND(int(fh)), 2)  # GA_ROOT
                    if int(root) == int(hwnd):
                        return True
                # 拿不到句柄的兜底:控件被 _classify 认可就算数
                elif _classify(foc) is not None:
                    return True
    except Exception:
        pass

    win = auto.ControlFromHandle(hwnd)
    if win is None:
        return False
    best_small, best_small_area = None, -1
    best_doc, best_doc_area = None, -1

    def walk(c, depth):
        nonlocal best_small, best_small_area, best_doc, best_doc_area
        if depth > 40:
            return
        for ch in c.GetChildren():
            cls = _classify(ch)
            if cls is not None:
                is_small, area = cls
                if is_small:
                    if area > best_small_area:
                        best_small, best_small_area = ch, area
                else:
                    if area > best_doc_area:
                        best_doc, best_doc_area = ch, area
            walk(ch, depth + 1)

    walk(win, 0)  # ponytail: 全树遍历,panel 模式下仅说「发送/输入」时触发一次,可接受
    # 优先聊天框式的小输入框;都没有再退到整窗文档容器(WPS/Word/Excel/PPT 主编辑区)
    best = best_small if best_small is not None else best_doc
    # ceiling: 整窗编辑器里若另有小输入框(如搜索框),可能被误选;主用途是网页聊天框,暂不处理
    if best is None:
        print("[info] 未在当前窗口找到独立输入框,直接粘贴到当前焦点处")
        return False
    try:
        best.SetFocus()
    except Exception as e:
        print(f"[warn] 聚焦输入框失败：{e}")
        return False
    return True


def _is_cloaked(hwnd):
    """UWP 后台/搜索面板等虽在 Z 序高位，但被 DWM 标为 cloaked（视觉上不可见）。"""
    try:
        val = wintypes.DWORD(0)
        # DWMWA_CLOAKED = 14
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and val.value != 0
    except Exception:
        return False


def _find_topmost_user_window():
    """沿 Z 序自顶向下找第一个「真实」窗口：排除 Saymore 自己（按 PID，覆盖 overlay/
    panel/主界面所有子窗口）、系统 shell、隐藏/最小化/cloaked/零面积的空壳。
    找不到返回 0。"""
    u = ctypes.windll.user32
    own_pid = os.getpid()
    # GetTopWindow(NULL) 返回桌面 Z 序最顶端的顶层窗口；GW_HWNDNEXT=2 沿 Z 序往下
    hwnd = u.GetTopWindow(None)
    cls_buf = ctypes.create_unicode_buffer(128)
    while hwnd:
        try:
            if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
                pass
            else:
                # 跳过自己进程的窗口
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == own_pid:
                    pass
                else:
                    u.GetClassNameW(hwnd, cls_buf, 128)
                    if cls_buf.value in _SHELL_CLASSES:
                        pass
                    elif _is_cloaked(hwnd):
                        pass
                    else:
                        r = wintypes.RECT()
                        u.GetWindowRect(hwnd, ctypes.byref(r))
                        if (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                            return hwnd
        except Exception:
            pass
        hwnd = u.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return 0


def focus_window(target, input_name=""):
    """把焦点定位到当前最上层用户窗口的输入框（不切换/激活窗口——你在哪个窗口就用
    哪个，便于在 DeepSeek 等网页里用语音）。目标是配置的窗口(如 Claude)且给了
    input_name 时，用 UIA 精确定位该控件(Prompt)；否则自动找输入框聚焦。

    用 Z 序而非 GetForegroundWindow：用户点一下任务栏前台就变 shell，
    找不到输入框；Z 序自顶向下，跳过 Saymore 自己的窗口（overlay/面板/主界面）
    与系统 shell/隐藏壳，拿到用户真正在看的那扇窗。

    返回是否真的聚焦到了一个输入框：调用方（如"发送"命令）据此决定是否粘贴/回车，
    没找到时应提示用户先点进输入框，避免把文字扔到无关焦点（任务栏/桌面）。"""
    _setup_focus_api()
    # 短路:用户已经把光标点进某个输入框(哪怕它不是 Z 序顶层——比如上方压着悬浮窗),
    # 就直接用那个,不去找顶层窗口,避免把文字贴错地方。
    if _focus_is_own_input():
        return True
    u = ctypes.windll.user32
    fg = _find_topmost_user_window()
    if not fg:
        return False
    is_target = False
    if target:
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(fg, buf, 512)
        tl = target.lower()
        is_target = tl in buf.value.lower() or tl in _proc_name(fg).lower()
    if is_target and input_name:
        if _focus_input_uia(fg, input_name):
            return True
        return _focus_any_input_uia(fg)  # Prompt 没找到时退回通用查找
    return _focus_any_input_uia(fg)


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
