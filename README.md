# Typeoff · 本地中文语音输入

> Windows 上一个**任何窗口都能用**的中文语音输入常驻程序。说唤醒词 → 说话 → 文字直接落在光标处。全程本地(Qwen3-ASR + 本地整理模型),不联网、不上传、断网可用。


---

## 它是什么

一句话:**"中文语音 → 键盘"的输入法级后台程序**。跟 Claude Code / VS Code / 浏览器 / 微信 / DeepSeek 网页... 谁都不绑定——你光标在哪儿,文字就落在哪儿。

做这个的直接动机是 Claude Code 内置的 `/voice` 暂不支持中文,凑合不了。顺手就把它做成了一个通用的中文语音输入替代品。

### 核心特点(一览)

- **全本地、零上传**:ASR、文本整理、热词学习都在本机跑，所有内容全在本地，0上传。
- **常驻 + 唤醒词**:后台驻留,说 `小美`(可修改)才开始录,不用切窗口、不用按键。超过5分钟无语音输入则主动休眠，卸载模型，节省系统资源。
- **任意窗口自动填字**:自动寻找当前窗口里的输入框，自动聚焦粘贴。
- **多种文本整理方式**:已微调文本整理LoRA,支持多种整理方式(轻度/深度/邮件/00后),可以去掉口水词、重复词、自动纠错、转换为书面用语/邮件格式、转换说话风格（00后）。
- **CPU可流畅运行、GPU只需4G显卡**:CPU运行语音识别大概1-2秒，GPU只需0.5秒。整段文字的文本整理CPU稍慢，大概5秒左右，GPU在2秒左右。
- **自定义专业术语**:可以自定义几十个专业术语，会自动加入大模型上下文，让程序识别更精准。
- **从上下文提词、从历史记录提词**:从当前对话窗口的上下文里提取术语，并从用户本地历史记录提取高频专业词汇，放入模型的上下文，让模型越用越懂你。
- **语音命令**:通过一系列自定义的语音命令来控制程序，无须触碰键盘，比如直接说"发送"回车、"回退"删词、"清空"清屏、"休眠"下线。

---

## 跟市面上的东西比,它到底不一样在哪

| | Typeoff | 讯飞/搜狗输入法(语音) | Windows 自带语音识别 | Whisper / SenseVoice 一类的开源桌面前端 | superwhisper / Wispr Flow(商业) |
|---|---|---|---|---|---|
| **中文识别质量** | ✅ Qwen3-ASR,当前中文 SOTA 之一 | ✅ 很好 | ⚠️ 一般 | ⚠️ Whisper 中文一般;SenseVoice OK | ✅ 好(多为云端) |
| **本地 / 隐私** | ✅ **100% 本地** | ❌ 云端上传 | ✅ 本地 | ✅ 本地 | ❌ 多为云端(Mac 有本地版) |
| **任意窗口都能填字** | ✅ UIA 自动定位输入框 | ✅(输入法层) | ⚠️ 仅系统控件 | ❌ 多为剪贴板/自家窗口 | ✅ |
| **唤醒词免手操作** | ✅ 本地 KWS,零训练自定义 | ❌ 要按键 | ❌ 要按键 | ❌ 多为快捷键 | ⚠️ 多为快捷键 |
| **口语 → 书面语润色** | ✅ 本地 LoRA,四种人格 | ⚠️ 简单标点 | ❌ | ❌ | ✅(靠云端 LLM) |
| **GPU 门槛** | 🟢 Vulkan,4GB 显存 / 也能纯 CPU | — | — | 🔴 多要 CUDA | — |
| **平台** | Windows | Win/Mac/移动 | Windows | 跨平台 | Mac 为主 |
| **可扩展** | ✅ 开源 Python,配置文件全暴露 | ❌ | ❌ | ✅ | ❌ |

**跟输入法比:** 输入法要么上传云端(隐私),要么中文识别弱;而且你切窗口经常要重新点它一下、按快捷键。Typeoff 是一个后台守护进程,唤醒词一喊就工作,配一个本地 LoRA 顺手把口语改成能直接发出去的书面语。

**跟 Whisper 系开源方案比:** Whisper 中文效果撑不起严肃使用,SenseVoice 尚可但没有润色;而且大多是"打开一个窗口按住某个键说话",不是常驻输入法。Typeoff 用 Qwen3-ASR + 4bit 量化 + Vulkan,冷启动几秒、显存要求低、常驻工作,识别 + 润色 + 落字打包解决。

**跟 superwhisper / Wispr Flow 比:** 那些东西思路最接近,但一是 Mac 优先、二是普遍走云 LLM 做润色。Typeoff 是 **Windows + 完全本地** 的替代品。

---

## 详细特性

### 语音识别本身
- **引擎**:[Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) 1.7B,GGUF 4bit 量化,跑在 [llama.cpp](https://github.com/ggml-org/llama.cpp) 上,支持 Vulkan / 纯 CPU。
- **静音切句**:默认 Silero VAD v5(2MB ONNX 小网),抗噪、抗远场、扛短停顿——键盘声/空调/风扇不误触,离麦一米也不切碎。灵敏度可调,找不到模型时自动回退 RMS 能量阈值。
- **术语纠正两层**:
  1. `terms.txt` 一行一词,作为 context 喂 Qwen3-ASR 偏置。
  2. **热词自学习**:说"发送"时用 UIA 读输入框最终文本(含你手改过的),入 `typed_history.jsonl`,休眠时 LLM 切词累计词频(手改词加权 3 倍),高频自动写 `hotwords.txt` 一起偏置,越用越准。
  3. **屏幕上下文热词**:抓前台窗口的 UIA 文本,LLM 挑术语当临时热词——聊什么就更认识什么。

### 唤醒 & 状态机
- **本地 KWS**:sherpa-onnx 关键词检测(~3MB ONNX,待机 CPU 无感,ASR 只在唤醒后启动)。
- **自定义唤醒词零训练**:改 `config.json` 的 `wake_words`(如 `["你好军哥", "Hi Claude"]`)重启即生效。程序自动跑 `sherpa-onnx-cli text2token` 把中文转拼音 token。建议 4 字以上、不常用词,防误触。
- **完整休眠**:静默超过 `sleep_after_seconds`(默认 300s)自动下线并**杀 llama-server 释放显存**,再唤醒时几秒重启。

### 落字方式
- 用**剪贴板 + Ctrl+V** 粘贴,而不是逐字模拟键盘——中文 / Unicode 最可靠。
- **UIA 自动找输入框**:默认不切窗口,在前台窗口里找到 Edit / Document 控件聚焦后粘贴(首次约 0.4s,之后控件缓存复用近乎瞬时)。也可以对某个应用精确指定输入框控件名(如 Claude 桌面端 `Prompt`)。

### 语音命令
- 整句只说 `发送` / `回车` / `提交` → 切回目标窗口按一次回车提交(不打字)。
- 其它内置命令词:回退删词、清空清屏、休眠下线、切换整理模式、开关提醒等(设置面板里都能看/改)。

### 本地文本整理(口语 → 书面语)
- 与 ASR 共用 Qwen3-ASR-1.7B 基座 + **自训合一 LoRA**,四种人格(保守 / 深度 / 邮件 / 00后)共用同一份 adapter,靠 system 提示词切换。
- 挂在同一个 llama.cpp 后端上,和 ASR 复用显存,不额外多起进程。
- 停止说话 `polish_quiet_seconds` 秒后自动触发:保守模式走滑动窗口只润当前句,深度/邮件/00后 对全文跑一次并冻结。
- LoRA 权重随仓库分发在 `polish_lora/`,克隆即用,**不需要自己训练**。

### 提醒
一句话对话:说"提醒我下午三点开会",本地 LLM 解析成结构化提醒存 `reminders/`(按日期分文件,不会把老提醒塞进 LLM 上下文撑爆),到点弹通知 + TTS 朗读。历史流水在主界面「历史」tab 可查。

### UI
- **右下角常驻圆环**:绿=聆听(环内实时音量波形)、灰=休眠、剩最后 1 分钟显示倒计时(≤10s 转红),右键弹菜单:主界面 / 退出。Pillow 超采样 + Win32 分层窗口(逐像素 alpha),边缘无锯齿。
- **玻璃文字面板**:识别句先攒到面板显示,说"发送"才整体回填,给你机会看/口头改。
- **系统托盘**:Typeoff logo,左键拉起主界面,右键同款菜单。
- **主界面**:pywebview 无边框窗口,Apple 风分组列表,左侧四 tab(设置 / 热词 / 历史 / 导入)。
- **开机自启**:走 `HKCU\...\Run` 注册表项(项名 `Typeoff`),这样任务管理器→启动、设置→应用→启动里都能看见能开关。免管理员、无控制台窗口。

---

## 使用方法

### 环境要求
- Windows 10/11
- Python 3.9+
- 显卡:任意支持 Vulkan 的 GPU(GTX 1650 4GB 起步够用);或纯 CPU(速度约慢 2 倍)。

### 1. 装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

激活后提示符前会有 `(.venv)`。

> PowerShell 报「禁止运行脚本」先跑一次:`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

### 2. 下模型(三样)

**① Qwen3-ASR GGUF**(转写主模型)

从 [Qwen3-ASR-1.7B-GGUF](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-GGUF) 或对应 ModelScope 镜像下这两个文件到 `models/Qwen3-ASR-1.7B-GGUF/`:
- `Qwen3-ASR-1.7B-IQ4_NL.gguf`(解码器,4bit;NormalFloat4,与训练 QLoRA nf4 码本对齐,训推一致)
- `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf`(音频编码器)

> 国内 HF 慢:`$env:HF_ENDPOINT = "https://hf-mirror.com"`;或直接用 ModelScope + curl 拉,别开代理 TUN(会假死)。

**② 唤醒词模型(sherpa-onnx KWS)**

```powershell
curl -L -o kws.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
tar xjf kws.tar.bz2
ren sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20 kws-model
```

纯中文模型用 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`,并把 config 里 `kws_tokens_type` 改成 `ppinyin`。

**③ VAD(切句)**

```powershell
curl -L -o silero_vad.onnx https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
```

**④ 文本整理 LoRA**:仓库自带 `polish_lora/`,不用下。

### 3. 启动

一键后台(推荐,无黑窗、日志按日切分到 `logs/`):

```powershell
powershell -ExecutionPolicy Bypass -File start.ps1   # 起
powershell -ExecutionPolicy Bypass -File stop.ps1    # 停
```

想要每次开机自动跑:主界面「设置」里勾开机自启即可(写 HKCU Run 项)。

前台调试(能看到实时输出):

```powershell
.\.venv\Scripts\Activate.ps1
python -m typeoff.main
```

> ⚠️ 前台跑**不要**用 `| Select-Object` 之类的管道,会把 stdout 变 GBK,程序里 emoji `print` 抛异常让后台线程崩掉(表现为"说话没反应")。

### 4. 用它

1. 启动后右下角圆环变灰 = 「待唤醒中」。
2. 把光标点进任意输入框(Claude Code / VS Code / 网页 / 微信...)。
3. **说唤醒词**(默认 `你好小爱`),等圆环变绿。
4. 正常说中文;停顿超过 `silence_seconds` 秒自动切句转写。
5. 文字自动落到光标处;说"发送"就回车提交,或自己手改后按回车。

**退出**:圆环右键「退出程序」,或 `Ctrl+Shift+Q`(需要以管理员身份运行终端,`keyboard` 库要求)。

### 5. 常用配置

`config.json` 全项都有中文注释(顶部 `DEFAULT_CONFIG` 是权威说明),最常调这几个:

| 字段 | 说明 | 默认 |
|---|---|---|
| `wake_words` | 唤醒词。改完重启即生效(自动重生成 keywords) | `["你好小爱"]` |
| `kws_threshold` | 唤醒灵敏度,越小越易触发 | `0.25` |
| `vad_threshold` | 切句灵敏度,漏识别调低到 0.4,把噪声当人声调高到 0.6 | `0.5` |
| `sleep_after_seconds` | 静默多久后完整休眠(释放显存) | `300` |
| `device` | `auto`/`cuda` 都用 GPU(Vulkan),`cpu` 强制纯 CPU | `auto` |
| `polish_mode` | 默认整理模式:保守/深度/邮件/00后 | `保守` |
| `simplified` | 繁体统一转简体(OpenCC) | `true` |
| `paste` | `false` 时只复制到剪贴板不粘贴 | `true` |
| `overlay` | 右下角圆环 | `true` |

其余项在主界面设置面板里点点就能改,不用手撸 JSON。

---

## 架构一览

入口:`voice_input.py`(常驻主程序,状态机 + 命令词编排)。功能按模块拆:

- **音频/唤醒**:`audio_capture.py`(采集 + VAD 切句 + KWS)
- **ASR 后端**:`asr_llamacpp.py`(管 llama-server,HTTP 转写)
- **文本整理**:`polish/local.py`(挂 LoRA 在同一个 llama.cpp 上)
- **落字**:`win_focus.py`(UIA 找输入框 + Ctrl+V)
- **热词**:`hotwords.py`(自学习)+ `screen_context.py`(屏幕上下文)
- **UI**:`overlay.py`(圆环+玻璃面板)、`tray.py`(托盘)、`main_window.py`(pywebview 主界面)
- **提醒**:`reminder_mode.py` + `reminder_chat.py` + `reminders.py`
- **基建**:`paths.py` / `log_setup.py` / `tts.py` / `win/autostart.py`

详情看 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

---

## 说明与限制

- **目前仅支持 Windows **，后续会陆续支持Mac / Linux 。
- 用了 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)、[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、[silero-vad](https://github.com/snakers4/silero-vad)、[llama.cpp](https://github.com/ggml-org/llama.cpp),许可证以各自项目为准。
- 本仓库代码采用 [LICENSE](LICENSE) 中的条款。
