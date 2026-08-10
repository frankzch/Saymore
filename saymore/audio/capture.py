"""麦克风采集与切句 + 语音唤醒前端。

Recorder 常驻采集、按静音/VAD 切句，每句回调 on_segment(在 PortAudio 线程，只入队)。
build_vad 造 Silero VAD、build_keyword_spotter 造 sherpa-onnx KWS 唤醒器。
从 voice_input.py 拆出——参数由主程序传入，路径解析复用 paths._resolve。
"""
import re
import sys

import numpy as np
import sounddevice as sd

from saymore.paths import _resolve


class Recorder:
    """常驻采集麦克风音频，并按静音停顿切句。

    每检测到一段满足时长的语音后跟随足够长的静音，就调用 on_segment(audio)
    把这一句交出去（在 PortAudio 回调线程，必须轻量，只入队不做转写）。
    stop()（退出程序时）把缓冲里剩余的语音作为最后一句补发。
    """

    def __init__(self, sample_rate, on_segment, silence_rms, silence_seconds, min_segment_seconds,
                 max_segment_seconds=15.0, on_block=None, on_speech=None, vad=None, device=None):
        self.sample_rate = sample_rate
        self.device = device  # None=系统默认；否则按名字匹配（不匹配则退默认）
        self.on_segment = on_segment
        self.on_block = on_block   # 每个原始音频块都回调一次（待唤醒时喂给 KWS）
        self.on_speech = on_speech # 检测到说话声时回调（用于刷新休眠倒计时，避免长句中途休眠）
        self.silence_rms = silence_rms
        self.silence_samples = int(silence_seconds * sample_rate)
        self.min_samples = int(min_segment_seconds * sample_rate)
        self.max_samples = int(max_segment_seconds * sample_rate)
        # sherpa-onnx VoiceActivityDetector 或 None（None 时回退 RMS 能量判静音 + 自管切句）
        self.vad = vad
        self._stream = None
        self._reset_buf()

    def _reset_buf(self):
        # RMS 回退路径的状态
        self._buf = []
        self._buf_len = 0
        self._trailing_silence = 0
        self._has_speech = False
        # VAD 路径的状态：跟踪 is_speech_detected 的边沿，用来只在起说时触发 on_speech
        self._was_detecting = False
        if self.vad is not None:
            self.vad.reset()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        block = indata[:, 0].copy()
        if block.size == 0:
            return
        if self.on_block is not None:
            self.on_block(block)

        if self.vad is not None:
            # VAD 路径：把音频喂给 VoiceActivityDetector，它内部按 min_silence_duration 平滑，
            # 抖一两下不会归零静音倒计时。直接吐"完整语音段"给我们，不用自己管切句。
            self.vad.accept_waveform(block)
            detecting = self.vad.is_speech_detected()
            # 起说边沿（False→True）：刷休眠倒计时
            if detecting and not self._was_detecting and self.on_speech is not None:
                self.on_speech()
            self._was_detecting = detecting
            # 排空所有已完成段
            while not self.vad.empty():
                seg = np.asarray(self.vad.front.samples, dtype=np.float32)
                self.vad.pop()
                if seg.size >= self.min_samples:
                    self.on_segment(seg)
            return

        # RMS 回退路径（原逻辑）
        self._buf.append(block)
        self._buf_len += block.size
        rms = float(np.sqrt(np.mean(block ** 2)))
        if rms >= self.silence_rms:
            self._has_speech = True
            self._trailing_silence = 0
            if self.on_speech is not None:
                self.on_speech()
        else:
            self._trailing_silence += block.size
            if (self._has_speech
                    and self._trailing_silence >= self.silence_samples
                    and self._buf_len >= self.min_samples):
                self._flush_rms()
        if self._has_speech and self._buf_len >= self.max_samples:
            self._flush_rms()
        elif not self._has_speech and self._buf_len > self.max_samples:
            last = self._buf[-1]
            self._buf = [last]
            self._buf_len = last.size
            self._trailing_silence = min(self._trailing_silence, last.size)

    def _flush_rms(self):
        seg = np.concatenate(self._buf).astype(np.float32)
        self._buf = []
        self._buf_len = 0
        self._trailing_silence = 0
        self._has_speech = False
        self.on_segment(seg)

    def discard_current(self):
        """丢弃当前累积的缓冲（唤醒瞬间调用，避免把唤醒词本身当正文转写）。"""
        self._reset_buf()

    def start(self):
        self._reset_buf()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self._resolve_device(),
            callback=self._callback,
        )
        self._stream.start()

    def _resolve_device(self):
        """把配置里的设备名解析成 sounddevice 的设备索引；找不到就 None（走系统默认）。"""
        if not self.device:
            return None
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and d["name"] == self.device:
                    return i
        except Exception:
            pass
        print(f"[warn] 输入设备 {self.device!r} 不存在，回退到系统默认", file=sys.stderr)
        return None

    def stop(self):
        """停止采集，把剩余语音作为最后一句补发（若够长）。"""
        if self._stream is not None:
            self._stream.stop()   # 等回调跑完，之后缓冲不再变化
            self._stream.close()
            self._stream = None
        if self.vad is not None:
            self.vad.flush()
            while not self.vad.empty():
                seg = np.asarray(self.vad.front.samples, dtype=np.float32)
                self.vad.pop()
                if seg.size >= self.min_samples:
                    self.on_segment(seg)
            self._reset_buf()
            return
        if self._has_speech and self._buf_len >= self.min_samples:
            self._flush_rms()
        else:
            self._reset_buf()


def build_vad(cfg):
    """构建 sherpa-onnx VoiceActivityDetector（Silero VAD v5，高级接口带内置平滑）。
    模型文件不存在则返回 None，Recorder 回退到 RMS。
    用高级接口而非 VadModel.is_speech：低级接口每 32ms 一个原始概率会抖动，安静时也会不定期过阈，
    我们自管静音倒计时就永远归零、段永远不 flush；高级接口按 min_silence_duration 平滑，抗抖动。
    """
    path_str = cfg.get("vad_model", "")
    if not path_str:
        return None
    path = _resolve(path_str)
    if not path.exists():
        print(f"[warn] Silero VAD 模型不存在 ({path})，切句回退到 RMS 能量阈值。"
              f"下载：https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx")
        return None
    try:
        import sherpa_onnx
        silero = sherpa_onnx.SileroVadModelConfig(
            model=str(path),
            threshold=float(cfg.get("vad_threshold", 0.5)),
            min_silence_duration=float(cfg.get("silence_seconds", 1.0)),
            # 硬编码 0.15s：只挡真正的脉冲噪声（键盘啪一下），不牵连"继续/发送"这类 250~350ms 短词。
            # 与 min_segment_seconds（Recorder 后置过滤）解耦——后者太大会把整段短词丢掉。
            min_speech_duration=0.15,
            window_size=512,
            max_speech_duration=float(cfg.get("max_segment_seconds", 15.0)),
        )
        vad_cfg = sherpa_onnx.VadModelConfig(
            silero_vad=silero,
            sample_rate=int(cfg.get("sample_rate", 16000)),
            num_threads=1,
            provider="cpu",
        )
        detector = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=30)
        print(f"[info] 已加载 Silero VAD（阈值 {cfg.get('vad_threshold', 0.5)}，"
              f"静音 {cfg.get('silence_seconds', 1.0)}s，最短 {cfg.get('min_segment_seconds', 0.4)}s）")
        return detector
    except Exception as e:
        print(f"[warn] 加载 Silero VAD 失败，回退到 RMS：{e}")
        return None


def normalize_wake_word(w):
    """中英混合模型要求：英文须大写、且英文与中文之间用空格分隔。
    把用户随手写的 'Hi小发' 规整成 'HI 小发'，避免静默生成空关键词。"""
    w = re.sub(r"[A-Za-z]+", lambda m: m.group(0).upper(), w.strip())
    w = re.sub(r"(?<=[A-Z0-9])(?=[一-鿿])", " ", w)
    w = re.sub(r"(?<=[一-鿿])(?=[A-Z0-9])", " ", w)
    return re.sub(r"\s+", " ", w)


def build_keyword_spotter(cfg, wake_words):
    """加载 sherpa-onnx KWS 模型，并据 wake_words 生成关键词文件。返回 spotter 或 None。"""
    import sherpa_onnx  # 延迟导入：未装/未启用时不影响主流程

    kdir = _resolve(cfg["kws_model_dir"])
    if not kdir.exists():
        print(f"[warn] KWS 模型目录不存在: {kdir}，无法语音唤醒。下载方式见 README。")
        return None

    def pick(pattern):
        # 优先选 int8 量化文件（更快），其次普通；取第一个匹配
        files = sorted(kdir.glob(pattern), key=lambda p: ("int8" not in p.name, p.name))
        return str(files[0]) if files else None

    encoder, decoder, joiner = pick("encoder*.onnx"), pick("decoder*.onnx"), pick("joiner*.onnx")
    tokens = kdir / "tokens.txt"
    if not (encoder and decoder and joiner and tokens.exists()):
        print(f"[warn] KWS 模型文件不全（需 encoder/decoder/joiner*.onnx + tokens.txt）于 {kdir}")
        return None

    # 用自带 CLI 把中文唤醒词转成拼音 token 关键词文件（零训练，改 wake_words 即生效）。
    # sherpa_onnx.cli.cli 是个 click group,直接在本进程内调 text2token 子命令,
    # 省掉 subprocess——打包后 sys.executable 是 Saymore.exe,`-c "..."` 会被
    # PyInstaller bootloader 无视、误启一个完整语音后端(进程病毒的来源之一)。
    raw = kdir / "keywords_raw.txt"
    keywords_file = kdir / "keywords.txt"
    norm_words = [normalize_wake_word(w) for w in wake_words if w.strip()]
    raw.write_text("\n".join(norm_words) + "\n", encoding="utf-8")
    args = ["text2token",
            "--tokens", str(tokens),
            "--tokens-type", cfg["kws_tokens_type"]]
    if "phone" in cfg["kws_tokens_type"]:
        # phone+ppinyin 需要英文词→音素的 lexicon（中英模型自带 en.phone）
        lexicon = next(iter(kdir.glob("*.phone")), kdir / "lexicon.txt")
        args += ["--lexicon", str(lexicon)]
    args += [str(raw), str(keywords_file)]
    gen_failed_msg = None
    try:
        from sherpa_onnx.cli import cli
        cli.main(args=args, standalone_mode=False)
    except Exception as e:  # noqa: BLE001 click 会抛 UsageError/SystemExit,也可能是模型异常
        gen_failed_msg = str(e)
        if not keywords_file.exists():
            print(f"[error] 关键词生成失败且无现成文件，无法唤醒：{gen_failed_msg}\n"
                  f"        请手动运行：sherpa-onnx-cli text2token --tokens {tokens} "
                  f"--tokens-type {cfg['kws_tokens_type']} {raw} {keywords_file}")
            return None
        # 生成失败但磁盘上有旧 keywords.txt——先别急着"沿用"。下面统一做"标签比对"，
        # 只有旧文件里的 @原词 恰好等于当前 wake_words 才算 OK；不等则响亮报错。
        # (历史教训: sentencepiece 缺失导致 text2token 静默异常,老关键词一直被沿用,
        # 用户在设置里改词看似生效实则永远不生效。见 saymore.spec 里 hidden imports 注释)

    # 生成成功但内容为空：拼音/音素都没转出来（如英文词不在词典、或中英没分开），此时唤醒永不触发，必须报错
    if not keywords_file.exists() or not keywords_file.read_text(encoding="utf-8").strip():
        print(f"[error] 关键词文件为空，无法唤醒。检查 wake_words={norm_words}：英文须为词典内单词，"
              f"中英之间留空格（如 'HI 小发'）。原始词={wake_words}")
        return None
    # 给每行关键词补 @原词 标签：默认 get_result() 只返回拼音 token，加标签后返回原词，便于区分多组唤醒词
    try:
        lines = [ln for ln in keywords_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if gen_failed_msg is None and len(lines) == len(norm_words):
            # 只在"这轮真生成成功"时才补标签、覆盖写；失败沿用分支绝不再回写。
            labeled = [ln if "@" in ln else f"{ln} @{w}" for ln, w in zip(lines, norm_words)]
            keywords_file.write_text("\n".join(labeled) + "\n", encoding="utf-8")
            lines = labeled
    except OSError as e:
        print(f"[warn] 关键词标签写入失败，多组唤醒词可能无法区分: {e}")

    # 校验:磁盘上的 keywords.txt 里的 @原词 是否就是当前 wake_words。
    # 一致 → 静默成功;不一致 → 响亮报错但**不 return None**,而是继续用旧词的 spotter——
    # "改词没生效"是可以带病使用的软故障;"整个 spotter 建不出来"是硬故障,用户从"至少
    # 默认词能应答"退化到"什么都不响应",反而更糟。教训:响亮 + 降级 > 响亮 + 瘫痪。
    disk_labels = [ln.rsplit("@", 1)[1].strip() for ln in lines if "@" in ln]
    if set(disk_labels) != set(norm_words):
        hint = (f"pip install {gen_failed_msg.split(chr(39))[1]}"
                if gen_failed_msg and "No module named" in gen_failed_msg and chr(39) in gen_failed_msg
                else "按上面 text2token 命令手动生成 keywords.txt")
        print(f"[error] 关键词生成失败,新的 wake_words={norm_words} 未能写入 keywords.txt,"
              f"当前生效的仍是磁盘旧词={disk_labels}——说这些旧词才能唤醒!\n"
              f"        生成异常: {gen_failed_msg or '未知'}\n"
              f"        修复: {hint}。")
    elif gen_failed_msg:
        print(f"[warn] 关键词自动生成失败但磁盘旧文件与当前 wake_words 一致,沿用: {norm_words}。"
              f"       生成异常: {gen_failed_msg}")
    else:
        print(f"[info] 已生成唤醒关键词: {norm_words}")

    return sherpa_onnx.KeywordSpotter(
        tokens=str(tokens),
        encoder=encoder, decoder=decoder, joiner=joiner,
        keywords_file=str(keywords_file),
        keywords_threshold=cfg.get("kws_threshold", 0.25),
        num_threads=1, provider="cpu",
    )
