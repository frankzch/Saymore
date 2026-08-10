# -*- coding: utf-8 -*-
"""路径基座：项目根目录 + 配置文件位置 + 相对路径解析。

开发态：PROJECT_ROOT = saymore/../ (仓库根)。
打包态(PyInstaller onedir)：PROJECT_ROOT = Saymore.exe 所在目录,而不是 _internal/。
这样 config.json / logs / 下载的 models / kws-model / polish_lora 等用户可见资源
就摆在 {app}\ 根,别再埋 _internal 里；_internal 只留 Python 运行时。
相对路径统一按项目根解析，让 config.json 里可以继续用 `models/xxx` 这种相对写法。"""
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


def _resolve(path):
    """相对路径按项目根解析；绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p
