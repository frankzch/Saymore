# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 Saymore 打包成 onedir 应用。
   pyinstaller saymore.spec   → dist/Saymore/  (含 Saymore.exe + 所有依赖)

设计:
- Windowed（-w）+ pythonw：无控制台闪现；日志走 saymore/log_setup.py 落文件。
- onedir 而非 onefile：启动快、模型/lora 就地就绪，也便于 Inno Setup 打进安装包目录里。
- assets 走 datas=[]：图标、猫帧 PNG、KWS/LoRA/VAD/llama.cpp 全部整目录搬过去。
- hidden imports：sherpa_onnx / rapidocr_onnxruntime / edge_tts / webview 等含子模块动态导入。
"""
import os, sys, glob
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Anaconda 的 stdlib .pyd（_ctypes / _ssl / _hashlib / _bz2 / _lzma / _tkinter …）依赖的
# 一堆 DLL 躺在 <env>\Library\bin\ 下（非标准 DLLs 目录,PyInstaller 默认不扫）。不显式收
# 就会在目标机报 "ImportError: DLL load failed"（_ctypes 最先炸,后面 ssl/hashlib 更致命）。
# 这里扫 <base_prefix>\DLLs 里所有 .pyd 的 IAT,凡是它们 import、且能在 Library\bin 里找到
# 的 DLL 全部搬进根目录。递归解析,避免链式漏收。
_extra_bins = []
_conda_bin = os.path.join(sys.base_prefix, 'Library', 'bin')
if os.path.isdir(_conda_bin):
    try:
        import pefile
        _bin_map = {f.lower(): os.path.join(_conda_bin, f)
                    for f in os.listdir(_conda_bin) if f.lower().endswith('.dll')}
        _seen = set()
        def _scan(fp):
            try:
                pe = pefile.PE(fp, fast_load=True)
                pe.parse_data_directories(directories=[
                    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
                if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
                    for entry in pe.DIRECTORY_ENTRY_IMPORT:
                        dep = entry.dll.decode(errors='ignore').lower()
                        if dep in _bin_map and dep not in _seen:
                            _seen.add(dep)
                            src = _bin_map[dep]
                            _extra_bins.append((src, '.'))
                            _scan(src)  # 递归收 DLL 的依赖
                pe.close()
            except Exception:
                pass
        _dlls_dir = os.path.join(sys.base_prefix, 'DLLs')
        if os.path.isdir(_dlls_dir):
            for _pyd in glob.glob(os.path.join(_dlls_dir, '*.pyd')):
                _scan(_pyd)
        print(f'[saymore.spec] 从 Anaconda Library\\bin 收 {len(_extra_bins)} 个 DLL: '
              f'{sorted(n for n,_ in ((os.path.basename(s),d) for s,d in _extra_bins))}')
    except ImportError:
        print('[saymore.spec] 警告: pefile 未装,无法自动扫 Anaconda DLL 依赖。'
              'pip install pefile 后重打,否则 _ctypes/_ssl 等 stdlib 会加载失败。')

hidden = []
hidden += collect_submodules('sherpa_onnx')
hidden += collect_submodules('rapidocr_onnxruntime')
hidden += collect_submodules('webview')
hidden += collect_submodules('edge_tts')
hidden += collect_submodules('opencc')
# sherpa_onnx.cli 用 click 做子命令(text2token 生成唤醒词 tokens 文件)。
# 不显式收会打不进去,用户改唤醒词时静默失败(旧 keywords.txt 沿用、新词永远不生效)。
hidden += collect_submodules('click')
# comtypes 动态生成 gen 模块（SAPI），常规静态分析看不到
hidden += ['comtypes', 'comtypes.client', 'comtypes.gen']

datas = []
# 第三方带数据的包（自动收）
datas += collect_data_files('sherpa_onnx')
datas += collect_data_files('rapidocr_onnxruntime')
datas += collect_data_files('opencc')
datas += collect_data_files('webview')

# 项目自带资源。**只**留 Python 代码内引用的资源(猫帧 PNG、图标),这些必须
# 和 saymore/ 一起进 _internal 才能通过包相对路径找到。
# 用户可见的大件(kws-model / polish_lora / llama-cpp / silero_vad.onnx /
# config.json / terms.txt)由 Inno Setup 直接从仓库拷到 {app}\,不走 PyInstaller。
# 见 saymore/paths.py: 打包后 PROJECT_ROOT = Saymore.exe 所在目录。
datas += [
    ('saymore/ui/assets', 'saymore/ui/assets'),  # 猫姿势 PNG
    # saymore.ico 不进 _internal：Inno 会把它拷到 {app}\ 根,运行时按
    # PROJECT_ROOT/saymore.ico 加载(托盘/圆环/主窗)。EXE 本身的图标已通过下面
    # EXE(icon='saymore.ico') 静态嵌入,不依赖此 datas 项。
]

a = Analysis(
    ['run_saymore.py'],
    pathex=['.'],
    binaries=_extra_bins,
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
    name='Saymore',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压 sherpa-onnx/onnxruntime 的原生 DLL 常出问题，关掉
    console=False,       # pythonw 模式：无控制台窗口
    windowed=True,
    icon='saymore.ico',
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Saymore',
)
