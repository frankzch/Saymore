"""Saymore 自我进程管理：如何"再启一个 Saymore"。

打包后 sys.executable 是 Saymore.exe，PyInstaller bootloader 忽略 `-m`/`-c`
等 Python 参数、只跑固定入口——历史上代码里 `Popen([sys.executable, "-m", ...])`
在打包后会把每个"启动一个 GUI 子进程"变成"再启一个完整语音后端",叠加成
进程病毒。

此模块集中管理:
- `spawn_backend()`: 拉起一个新的主后端(供 restart 分支使用)
- `spawn_ui_main(args...)`: 拉起主界面 GUI 子进程
- `IS_FROZEN`: 是否 PyInstaller 打包运行

打包环境用**命名子命令**(`__ui-main`)显式告诉 exe 该跑什么;开发环境走
`python -m saymore.xxx`。两条路径都不依赖"exe 假装是 python"。
"""
import os
import subprocess
import sys
from pathlib import Path

from saymore.paths import CONFIG_PATH


IS_FROZEN = bool(getattr(sys, "frozen", False))

# 命名子命令(打包后由 run_saymore.py 分派)。用双下划线前缀,避免与用户可能
# 通过命令行传入的普通参数撞车。
UI_MAIN_CMD = "__ui-main"


def _base_cwd() -> str:
    """所有子进程共用的工作目录:配置文件所在目录(=安装目录/开发根目录)。"""
    return str(CONFIG_PATH.parent)


def _popen_kwargs() -> dict:
    kw = {"cwd": _base_cwd()}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kw


def spawn_backend() -> None:
    """启动一个新的主后端进程(供 restart 分支使用)。

    打包: `Saymore.exe`(无参即主后端)
    开发: `python -m saymore.main`
    """
    if IS_FROZEN:
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-m", "saymore.main"]
    subprocess.Popen(cmd, **_popen_kwargs())


def spawn_ui_main(config_path, history_dir, reminders_log,
                  import_trigger, restart_trigger, tab: str = "settings") -> None:
    """拉起主界面 GUI 子进程。

    打包: `Saymore.exe __ui-main <args...>`,run_saymore.py 分派到
          `saymore.ui.main_window._run_gui()`
    开发: `python -m saymore.ui.main_window <args...>`
    """
    args = [str(config_path), str(history_dir), str(reminders_log),
            str(import_trigger), str(restart_trigger), tab]
    if IS_FROZEN:
        cmd = [sys.executable, UI_MAIN_CMD, *args]
    else:
        cmd = [sys.executable, "-m", "saymore.ui.main_window", *args]
    subprocess.Popen(cmd, **_popen_kwargs())
