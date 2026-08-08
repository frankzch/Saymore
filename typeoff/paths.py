# -*- coding: utf-8 -*-
"""路径基座：项目根目录 + 配置文件位置 + 相对路径解析。

包内所有 __file__ 都在 typeoff/**/*.py，PROJECT_ROOT = typeoff/../。
相对路径统一按项目根解析，让 config.json 里可以继续用 `models/xxx` 这种相对写法。"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


def _resolve(path):
    """相对路径按项目根解析；绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p
