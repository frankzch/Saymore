# Typeoff

Windows 中文语音输入常驻程序。**说出唤醒词后直接说中文，自动转写并输入到当前光标处** —— 任何窗口都能用（Claude Code 终端、VS Code、浏览器等）。软件品牌 **Typeoff**，logo 是一只端坐的猫。

它和 Claude Code 完全解耦：本质是一个"中文语音 → 键盘"的输入法级后台程序，而不是 Claude Code 的命令/插件。因为 Claude Code 内置的 `/voice` 暂不支持中文，所以用本地 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) 自己做一个。

## 工作原理

```
说唤醒词 → 常驻录麦克风 → 静音停顿切句 → Qwen3-ASR 本地转写 → 经剪贴板 Ctrl+V 粘贴到光标处
```

用剪贴板粘贴而不是逐字模拟键盘，是因为这对中文/Unicode 最可靠。

## 安装

需要 Python 3.9+（Windows）。建议用项目独立的虚拟环境 `.venv`，避免污染系统或 conda 环境：

```powershell
# 1. 创建虚拟环境（只需一次）
python -m venv .venv

# 2. 激活（每次新开终端都要执行）
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖（激活后只需一次）
pip install -r requirements.txt
```

激活成功后命令提示符前会出现 `(.venv)`，此时 `python` 就指向本项目环境。

> ⚠️ 若 PowerShell 报「无法加载脚本，因为在此系统上禁止运行脚本」，先执行一次
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 再激活。
>
> ⚠️ 不要在没激活 `.venv` 的情况下直接 `python -m typeoff.main`——`python` 可能指向其它
> conda/系统环境（里面没装依赖），就会报 `ModuleNotFoundError: No module named 'sounddevice'`。

转写引擎见下方 [转写引擎与 GPU 提速](#转写引擎与-gpu-提速)。默认/唯一推荐的 `qwen_gguf`（llama.cpp + Vulkan）需手动下载 GGUF 权重。

> 国内从 HuggingFace 下载慢/连不上时，可在运行前设置镜像：
> `$env:HF_ENDPOINT = "https://hf-mirror.com"`（PowerShell）。

> Windows 上 `keyboard` 库注册全局快捷键（仅退出用的 `Ctrl+Shift+Q`）通常需要**以管理员身份**运行终端，否则可能监听不到按键。

### 语音唤醒模型（sherpa-onnx KWS）

待唤醒检测由 **sherpa-onnx 关键词检测**（CPU/ONNX，待机时不占 GPU）负责，Qwen3-ASR 只在唤醒后才推理。需下载一个 KWS 模型解压到 `kws-model/`：

```powershell
# 中英混合（推荐，能识别带英文的唤醒词），约 3MB
curl -L -o kws.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
tar xjf kws.tar.bz2
ren sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20 kws-model
```

> 纯中文模型用 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`，并把 config 里 `kws_tokens_type` 改为 `ppinyin`。

### 静音切句模型（Silero VAD v5，推荐）

切句默认走 **Silero VAD v5**（本地 ONNX 小神经网络，~2MB，CPU 无感），比原来的 RMS 能量阈值更抗噪、更远场、能识别短停顿——键盘/空调/风扇不会误判成说话，离麦一米也不切碎。放到项目根目录即可：

```powershell
curl -L -o silero_vad.onnx https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
```

用的是 sherpa-onnx 的 `VoiceActivityDetector` 高级接口，**内置平滑**：段结束需要 `silence_seconds` 秒连续判非人声，抖一两下扛得住。灵敏度用 `vad_threshold` 调（默认 0.5）：说话总被漏识别调低到 0.4；老把噪声当人声调高到 0.6。

找不到 `silero_vad.onnx` 时自动回退到 RMS 方案（`silence_rms`），老配置不受影响。

**自定义唤醒词：零训练。** 直接改 `config.json` 的 `wake_words`（如 `["你好军哥", "Hi Claude"]`），重启即生效——程序会自动用 `sherpa-onnx-cli text2token` 把中文转成拼音 token 写进 `kws-model/keywords.txt`。建议 4 字以上、不常用的词，误触发更少。灵敏度用 `kws_threshold` 调（越小越易触发）。

## 使用

一键后台启动（推荐，无黑窗、日志按日切分到 `logs/voice_input-YYYY-MM-DD.log`，每行带毫秒时间戳）：

```powershell
powershell -ExecutionPolicy Bypass -File start.ps1   # 起
powershell -ExecutionPolicy Bypass -File stop.ps1    # 停
```

已装了开机自启（见下节）时也可以：`Start-ScheduledTask -TaskName Typeoff` / `Stop-ScheduledTask -TaskName Typeoff`。

前台调试（能看到实时输出）：

```powershell
.\.venv\Scripts\Activate.ps1   # 先激活虚拟环境
python -m typeoff.main
```

> ⚠️ 前台跑时**不要**用 `| Select-Object` 之类的管道，会把 stdout 变成 GBK 编码，程序里的 emoji `print` 会抛 `UnicodeEncodeError` 让后台线程崩掉（表现为"说话没反应"）。要看输出就直接跑，或用 `start.ps1` 走日志文件。

1. 启动后看到「待唤醒中」即可（Qwen 模型改为懒加载，首次唤醒时才加载，启动几乎不占内存/显存）。
2. 把光标点进任意输入框（如 Claude Code）。
3. **说出唤醒词**（默认 `你好小爱`），然后正常说中文；停顿超过 `silence_seconds` 秒即切句转写。
4. 文字自动粘贴到光标处，检查无误后回车（或整句说"发送"）。

屏幕右下角会**常驻**一个状态小圆环（`overlay: true` 时，背景透明、抗锯齿）：

- **聆听中** = 绿环，环内一排**声音波形**随麦克风音量实时跳动（没声音时近乎平线）；
- **休眠** = 整体变灰、波形静止（仍可见，不消失）；
- 距完整休眠**剩最后 1 分钟**才显示**倒计时数字**，进度环按这一分钟递减，≤10 秒转红提醒；
- **右键**小圆环弹出菜单可退出程序，退出后才整体消失。

> 实现：Pillow 超采样抗锯齿渲染 + Win32 分层窗口（逐像素 alpha），边缘平滑无锯齿。尺寸由 `typeoff/ui/overlay.py` 顶部 `_OV_D` 控制（整体等比缩放）。

### 聚焦当前窗口的输入框

默认（`focus_window_title`/`focus_input_name` 均留空）程序不切换窗口——你在哪个前台窗口，就用 **UI Automation** 在该窗口里自动找到输入框聚焦后粘贴，方便在 DeepSeek 等网页里用语音。首次定位约 0.4s，之后缓存控件复用近乎瞬时。

> 想对某个应用精确定位：把 `focus_window_title` 填成目标进程名或标题关键字、`focus_input_name` 填该应用输入框在 UIA 里的控件名（如 Claude 桌面端为 `Prompt`）；仅当前台命中该目标时才用这个名字定位，否则仍走通用查找。

### 语音"发送"

整句只说 `send_words` 里的词之一（默认 `发送`/`回车`/`提交`）时，程序不打字，而是切回目标窗口（并聚焦输入框）按一次回车提交。

退出：右键小圆环选「退出程序」、或按 `Ctrl+Shift+Q`；关闭悬浮窗（`overlay: false`）时按 `Ctrl+C`。

## 配置

编辑 `config.json`：

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `wake_words` | 唤醒词列表，说中其一进入聆听态。改完重启即生效（自动重新生成关键词文件） | `["你好小爱"]` |
| `kws_model_dir` | sherpa-onnx KWS 模型目录 | `kws-model` |
| `kws_tokens_type` | 关键词转 token 方式：中英模型 `phone+ppinyin`，纯中文模型 `ppinyin` | `phone+ppinyin` |
| `kws_threshold` | 唤醒灵敏度，越小越易触发也越易误触 | `0.25` |
| `sleep_after_seconds` | 唤醒后静默超过此秒数进入**完整休眠**（回到待唤醒并卸载模型释放内存/显存，下次唤醒重载慢几秒）；最后 60s 悬浮窗显示倒计时 | `300` |
| `language` | 识别语言（Qwen 用英文语言名；留空=自动检测） | `Chinese` |
| `device` | `llama-server` 走 GPU(Vulkan) 还是纯 CPU：`auto`/`cuda` 都用 GPU，`cpu` 强制纯 CPU | `auto` |
| `min_seconds` | 短于此秒数视为误触丢弃 | `0.3` |
| `paste` | `true` 自动粘贴；`false` 仅复制到剪贴板 | `true` |
| `simplified` | `true` 用 OpenCC 把繁体统一转成简体 | `true` |
| `append_period` | `true` 句末无结束标点时自动补一个句号 | `true` |
| `overlay` | `true` 屏幕右下角常驻状态小圆环（绿=聆听+波形，灰=休眠，最后 1 分钟显示倒计时，右键：主界面 / 退出程序） | `true` |
| `focus_window_title` | 留空=通用：在当前前台窗口自动找输入框。填【进程名或标题】关键字则仅对该目标用下面的名字精确定位 | `""` |
| `focus_input_name` | 仅当前台命中 `focus_window_title` 时，用 UIA 把焦点定位到此名字的输入控件（如 Claude 为 `Prompt`）；否则走通用查找 | `""` |
| `send_words` | 整句只说这些词之一时，不打字而是切回窗口（聚焦输入框）按回车提交 | `["发送","回车","提交"]` |

> **为什么有时输出繁体？** `simplified: true` 会在转写后用 OpenCC（`t2s`）统一转成简体，避免简繁混杂。

**术语纠正：** 专有名词/常用词识别不准时有两层手段：① `qwen_context_file`（默认 `terms.txt`，一行一词）作为 context 喂给 Qwen3-ASR 做上下文偏置——把你的专业术语放这里，任何领域都行；② **热词自学习**——语音说「发送」时程序先用 UIA 读取输入框里的最终文本（含你手动改过的部分）记入 `typed_history.jsonl`，进入休眠时交给 LLM 切词、累计词频（你改过的词加权 3 倍），高频词自动写入 `hotwords.txt` 并与术语表一起偏置，越用越准（手动按回车发送的不采集）。

### 转写引擎与 GPU 提速

Qwen3-ASR 跑在 [llama.cpp](https://github.com/ggml-org/llama.cpp)（常驻 `llama-server`）上，GGUF 量化权重，**无需 PyTorch**。快、轻、冷启动短，还能把文本整理 LoRA 挂在同一个 server 上复用显存。GPU 加速走 **Vulkan**（老 N 卡/A 卡/核显都能加速，免 CUDA 版本地狱）。

配好这几个字段即可（默认值见 `typeoff/config.py` 里的 `DEFAULT_CONFIG`）：

```jsonc
"llama_server_exe": "llama-cpp/llama-server.exe",                              // Vulkan 版 llama-server
"gguf_model": "models/Qwen3-ASR-1.7B-GGUF/Qwen3-ASR-1.7B-Q4_K_M.gguf",         // 解码器（4bit 量化）
"gguf_mmproj": "models/Qwen3-ASR-1.7B-GGUF/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf",   // 音频编码器
```

程序按需拉起/杀掉 `llama-server`（休眠时杀进程释放显存，下次唤醒重启几秒）。显存全由 server 进程自管。

> **显存（如 GTX 1650 4GB）：** 1.7B 的 **Q4_K_M** 量化 + `mmproj` Q8，配 `-c 4096` 单槽 KV，实测能稳跑在 4GB 卡上。纯 CPU 也能跑（`"device": "cpu"`），速度约慢 2 倍。

## 本地文本整理模型

停顿几秒后程序会自动调用**本地整理模型**（Qwen3-0.6B + 自训 LoRA，跑在同一个 llama.cpp 后端里，复用显存），把口语文本改成通顺书面语。四种人格（保守/深度/邮件/00后）用同一份 adapter，靠 system 提示词切换，在设置面板里选默认整理模式。

成品 LoRA 权重和四份人格提示词随仓库分发在 [`polish_lora/`](polish_lora/) 目录下，克隆即用，不需要自己训练。

## 说明

- 仅在 Windows 上测试。
- 模型缓存目录已在 `.gitignore` 中忽略，不会提交到仓库。
