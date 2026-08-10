# 打包与发布流程

Typeoff 用 PyInstaller 把 Python 运行时 + 依赖打成 onedir 应用,再用 Inno Setup 封成 Windows
安装包。用户下载安装即可用,首次启动去主界面「运行环境」tab 下载 Qwen3-ASR 权重(约 1.5 GB)。

## 一次性准备

1. **装 Inno Setup 6+**: <https://jrsoftware.org/isinfo.php>。装完把 `ISCC.exe` 加进 PATH,
   或改 `build.ps1` 里的 `$ISCC` 常量为绝对路径。
2. **准备干净 venv**(强烈推荐,不然会把 7 GB 训练依赖一起打进去):

   ```powershell
   python -m venv .venv-build
   .\.venv-build\Scripts\Activate.ps1
   pip install -r requirements-runtime.txt
   ```

3. **上传 gguf 到 HuggingFace / ModelScope**,并把仓库 ID 填进 [typeoff/runtime_check.py](typeoff/runtime_check.py) 顶部:

   ```python
   _HF_REPO = "yourname/Typeoff-Qwen3-ASR"
   _MS_REPO = "yourname/Typeoff-Qwen3-ASR"
   ```

   文件名必须是 `Qwen3-ASR-1.7B-IQ4_NL.gguf` 和 `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf`
   (改文件名的话同步改 `_REQUIRED` 里的 `filename` 字段)。

## 打包

```powershell
.\build.ps1
```

产物:

- `dist\Typeoff\Typeoff.exe` + 一堆依赖(直接双击可跑,用于本机验证)
- `installer\output\Typeoff-Setup-1.0.0.exe` (最终安装包,发给用户)

## 打包前会发生什么

`typeoff.spec` 会把这些**打进 exe 目录**(占大头):

- `llama-cpp/`(92 MB,带 Vulkan)
- `polish_lora/multi-lora.gguf`(34 MB)+ 4 个 prompt.txt
- `kws-model/`(39 MB)
- `silero_vad.onnx`(2.3 MB)
- `rapidocr_onnxruntime` 自带 OCR 模型(~200 MB,rapidocr 数据文件)
- Python 运行时 + 依赖(~200 MB)

预计**安装包 ~400-600 MB**(Inno Setup lzma2/max 压缩后可能 ~200 MB)。

**不打进去**(留给运行时下载):

- Qwen3-ASR-1.7B-IQ4_NL.gguf(1.15 GB)
- mmproj-Qwen3-ASR-1.7B-Q8_0.gguf(340 MB)

## 常见坑

首次打包**几乎不会一次成功**。遇到问题按下面顺序排查:

### 1. `ModuleNotFoundError: sherpa_onnx.xxx` / `webview.xxx` / `edge_tts.xxx`

PyInstaller 静态分析漏掉动态导入。打开 `typeoff.spec` 找 `hidden = []` 那段,加进去:

```python
hidden += ['你.缺.的.模块名']
```

### 2. 运行时报 `FileNotFoundError: 猫图 / KWS 模型 / llama-server.exe`

打包时资源没搬全。检查 `dist\Typeoff\` 里对应目录是否存在。不在就在 `typeoff.spec` 的
`datas = []` 段加一行。

### 3. `DLL load failed while importing xxx`

某个 native 依赖的 DLL 没跟着走。多见于 sherpa-onnx / onnxruntime。运行 `dist\Typeoff\Typeoff.exe`
后看命令行错误(临时把 `console=False` 改成 `True` 重打)。

### 4. WebView2 未安装

Windows 11 自带,Windows 10 可能没有。用户报错请指引安装:
<https://developer.microsoft.com/en-us/microsoft-edge/webview2/>。
Inno Setup 里加 WebView2 Bootstrapper 也行(下一版再做)。

### 5. rapidocr 首次运行下载 OCR 模型

其实 `rapidocr_onnxruntime` 是自带模型的,`collect_data_files` 应该会带上。若发现启动才拉,
检查 `dist\Typeoff\rapidocr_onnxruntime\` 目录是否含 `.onnx` 文件。

### 6. 提交 exe 到杀软后被误杀

PyInstaller 打的未签名 exe 常被 Windows Defender / 360 报毒。要么代码签名(要买证书),
要么在下载页写清"请添加白名单"。这是 Python 打包的通病。

## 快速验证清单

打包完先本机验证一遍再发用户:

1. `dist\Typeoff\Typeoff.exe` 直接双击 → 系统托盘出图标 + 悬浮窗出圆环
2. 由于是打包环境,`kws-model / polish_lora / llama-cpp / silero_vad.onnx` 都在 → 需下载
   的两个 gguf 缺失 → 悬浮窗应变红,主界面自动开在「运行环境」tab
3. 手动把两个 gguf 放到 `dist\Typeoff\models\Qwen3-ASR-1.7B-GGUF\` 下 → 点「重新检测」→
   全绿 → 点「重启程序」→ 圆环变蓝,可正常语音输入
4. 关窗 → 托盘图标还在 → 右键"退出程序"能干净退
5. 装安装包(`installer\output\Typeoff-Setup-*.exe`),重复步骤 1-4

## 版本号

`installer\setup.iss` 里 `AppVersion` 手改。以后要自动化就外提到环境变量。
