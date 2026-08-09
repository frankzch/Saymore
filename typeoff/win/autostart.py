# -*- coding: utf-8 -*-
"""Windows 开机自启：注册表 HKCU\\...\\Run 项。

选注册表而非任务计划：任务管理器「启动」和「设置→应用→启动」里能看到、能一键开关，
这是普通用户认得的"标准做法"。任务计划不进这两个列表。

免管理员：HKCU 分支只影响当前用户，不需要提权。
无控制台：命令行用 pythonw.exe，不会闪黑窗。
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from typeoff.paths import PROJECT_ROOT

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Typeoff"


def _open_run(write=False):
    """打开 HKCU\\...\\Run，返回 (winreg, key)；调用方负责 CloseKey。仅 Windows。"""
    import winreg
    access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)
    return winreg, key


def _pythonw_path():
    """pythonw.exe 优先（无控制台）；找不到就退回当前解释器。"""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe)


def _desired_command():
    """自启命令行：cd 到项目根后用 pythonw -m typeoff.main 起。
    包一层 cmd /c 是为了顺带 cd——PROJECT_ROOT 里有 config.json、logs/ 等相对路径要用。
    """
    pyw = _pythonw_path()
    root = str(PROJECT_ROOT)
    # cmd /c 内部 cd /d 支持切盘符；参数用 " 转义空格
    return f'cmd /c "cd /d "{root}" && "{pyw}" -m typeoff.main"'


def is_enabled():
    """Run 项存在即视为启用。"""
    if os.name != "nt":
        return False
    try:
        winreg, key = _open_run(write=False)
    except FileNotFoundError:
        return False
    try:
        winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    finally:
        winreg.CloseKey(key)


def enable():
    """写 Run 项。返回 (ok, msg)。已存在会覆盖为最新命令行。"""
    if os.name != "nt":
        return False, "仅 Windows 支持"
    try:
        winreg, key = _open_run(write=True)
        try:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _desired_command())
        finally:
            winreg.CloseKey(key)
        return True, "已开启开机自启（登录时自动后台启动）"
    except Exception as e:  # noqa: BLE001
        return False, f"注册自启失败：{e}"


def disable():
    """删 Run 项；已不存在也算成功。"""
    if os.name != "nt":
        return False, "仅 Windows 支持"
    try:
        winreg, key = _open_run(write=True)
        try:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        finally:
            winreg.CloseKey(key)
        return True, "已关闭开机自启"
    except Exception as e:  # noqa: BLE001
        return False, f"卸载自启失败：{e}"


def set_enabled(want):
    return enable() if want else disable()


if __name__ == "__main__":
    print("当前状态：", "已启用" if is_enabled() else "未启用")
    if is_enabled():
        print("命令行：", _desired_command())
