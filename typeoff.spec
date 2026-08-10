# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 Typeoff 打包成 onedir 应用。
   pyinstaller typeoff.spec   → dist/Typeoff/  (含 Typeoff.exe + 所有依赖)

设计:
- Windowed（-w）+ pythonw：无控制台闪现；日志走 typeoff/log_setup.py 落文件。
- onedir 而非 onefile：启动快、模型/lora 就地就绪，也便于 Inno Setup 打进安装包目录里。
- assets 走 datas=[]：图标、猫帧 PNG、KWS/LoRA/VAD/llama.cpp 全部整目录搬过去。
- hidden imports：sherpa_onnx / rapidocr_onnxruntime / edge_tts / webview 等含子模块动态导入。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = []
hidden += collect_submodules('sherpa_onnx')
hidden += collect_submodules('rapidocr_onnxruntime')
hidden += collect_submodules('webview')
hidden += collect_submodules('edge_tts')
hidden += collect_submodules('opencc')
# comtypes 动态生成 gen 模块（SAPI），常规静态分析看不到
hidden += ['comtypes', 'comtypes.client', 'comtypes.gen']

datas = []
# 第三方带数据的包（自动收）
datas += collect_data_files('sherpa_onnx')
datas += collect_data_files('rapidocr_onnxruntime')
datas += collect_data_files('opencc')
datas += collect_data_files('webview')

# 项目自带资源（相对路径 → 打包后 dist/Typeoff/ 下同名目录）
datas += [
    ('typeoff/ui/assets', 'typeoff/ui/assets'),  # 猫姿势 PNG
    ('kws-model',         'kws-model'),          # 命令词唤醒模型
    ('polish_lora',       'polish_lora'),        # 整理 LoRA + prompt
    ('llama-cpp',         'llama-cpp'),          # 推理引擎（含 Vulkan）
    ('silero_vad.onnx',   '.'),                  # 端点检测
    ('typeoff.ico',       '.'),
    ('config.json',       '.'),                  # 默认配置模板；用户改动写就地
    ('terms.txt',         '.'),                  # 静态术语表种子
]

a = Analysis(
    ['run_typeoff.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排掉训练/开发依赖，避免误装到 .venv-build 里被抓走
        'torch', 'torchvision', 'torchaudio', 'transformers', 'datasets',
        'peft', 'accelerate', 'bitsandbytes', 'safetensors',
        'matplotlib', 'jupyter', 'IPython', 'notebook', 'tqdm.notebook',
        'sklearn', 'scipy', 'pandas',
        'tests', 'unittest',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Typeoff',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压 sherpa-onnx/onnxruntime 的原生 DLL 常出问题，关掉
    console=False,       # pythonw 模式：无控制台窗口
    windowed=True,
    icon='typeoff.ico',
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Typeoff',
)
