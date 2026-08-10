# -*- coding: utf-8 -*-
"""PyInstaller 打包用的入口脚本。开发时直接跑 `python -m typeoff.main` 即可；
本脚本存在只是给 PyInstaller 一个明确的入口文件（PyInstaller 不喜欢 `-m` 模块调用）。"""
from typeoff.main import main

if __name__ == "__main__":
    main()
