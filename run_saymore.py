# -*- coding: utf-8 -*-
"""PyInstaller 打包入口 —— 按显式子命令分派到目标模块。

打包后 sys.executable = Saymore.exe,bootloader 忽略 `-m`/`-c` 等 Python 参数
只跑本文件。子进程调用点(见 saymore/proc.py)用命名子命令告诉本入口该跑什么:

  Saymore.exe               → 主后端(saymore.main.main)
  Saymore.exe __ui-main ... → 主界面 GUI 子进程(saymore.ui.main_window._run_gui)

开发时(python -m saymore.main / -m saymore.ui.main_window)不经过本文件。
"""
import sys

from saymore.proc import UI_MAIN_CMD


def _main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == UI_MAIN_CMD:
        # 主界面 GUI 子进程:剥掉子命令后,argv 结构与开发时 `-m saymore.ui.main_window`
        # 完全一致(argv[1..6] = config_path, history_dir, reminders_log,
        # import_trigger, restart_trigger, tab),复用其 __main__ 分支。
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from saymore.ui.main_window import _run_gui
        _run_gui(sys.argv[1], sys.argv[2], sys.argv[3],
                 sys.argv[4], sys.argv[5], sys.argv[6])
        return

    from saymore.main import main
    main()


if __name__ == "__main__":
    _main()
