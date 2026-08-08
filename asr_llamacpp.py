# -*- coding: utf-8 -*-
"""Qwen3-ASR 的 llama.cpp 后端：管理常驻 llama-server（Vulkan GPU 或纯 CPU），HTTP 转写。

用法（voice_input.py 的 asr_engine="qwen_gguf" 时）：
    asr = LlamaASR(exe=..., model=..., mmproj=..., port=8901, ngl=99,  # 0=纯CPU，99=全部丢GPU
                   loras={"basic": "cleanup-lora.gguf", "deep": "deep-lora.gguf"})
    asr.start()                 # 启动 llama-server 并等就绪（模型加载约几秒）
    text, conf = asr.transcribe(audio_f32, 16000, context="术语表", language="Chinese")
    # conf: 生成 token 平均 logprob 的 exp（0..1，越高越有把握）；服务端没返回 logprobs 时为 None
    clean, conf = asr.cleanup("嗯就是那个...", system=提示词, role="basic")  # 整理人格（需 loras）
    asr.stop()                  # 休眠卸载：杀进程释放显存

与 qwen_asr(PyTorch) 的 prompt 约定一致：system=context 热词偏置，user=音频，
输出形如 "language Chinese<asr_text>识别文本"，本模块负责剥掉前缀只回文本。

多人格（finetune/ROUNDS.md I1/J1 轮）：同一份 ASR 权重挂多个整理 LoRA，按请求切——
loras={角色: gguf} 载入顺序即 id 序（basic=保守 id0，deep=深度 id1）。转写请求所有
LoRA scale=0（必须显式带上：挂了 --lora 后缺省是 scale=1，会污染转写），整理请求只把
对应角色那个开成 scale=1。loras 空时行为与纯转写一致。
"""
import base64
import io
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave

import numpy as np


class LlamaASR:
    def __init__(self, exe, model, mmproj, port=8901, ngl=99, loras=None):
        # Windows CreateProcess 不认含正斜杠的相对路径，统一转绝对路径
        self.exe = os.path.abspath(str(exe))
        self.model = os.path.abspath(str(model))
        self.mmproj = os.path.abspath(str(mmproj))
        self.port = int(port)
        self.ngl = int(ngl)
        # 整理 LoRA：{角色: {"path":绝对路径,"id":载入序号}}。载入顺序即 llama-server 的 lora id。
        self.loras = {}
        for role, path in (loras or {}).items():
            if path:
                self.loras[role] = {"path": os.path.abspath(str(path)), "id": len(self.loras)}
        self.lora = bool(self.loras)  # 真值：有没有挂整理 LoRA（voice_input 判断用）
        self.proc = None
        self.base = f"http://127.0.0.1:{self.port}"
        self._start_lock = threading.Lock()  # 预热线程与转写线程可能同时 start()，串行化防双开

    def _lora_scales(self, active=None):
        """构造 llama-server 的 lora 数组：只有 active 角色 scale=1，其余全 0（含转写=None 全 0）。"""
        return [{"id": r["id"], "scale": 1.0 if k == active else 0.0}
                for k, r in self.loras.items()]

    # ---------- 进程管理 ----------
    def start(self, timeout=120):
        """启动 llama-server 并阻塞等 /health 就绪。已在跑则直接返回。"""
        if self.alive():
            return
        with self._start_lock:
            if self.alive():  # 双检：等锁期间别的线程可能已把 server 拉起
                return
            return self._start(timeout)

    def _start(self, timeout):
        # ponytail: 端口写死单实例；要多实例再说
        cmd = [
            self.exe, "-m", self.model, "--mmproj", self.mmproj,
            "--port", str(self.port), "--host", "127.0.0.1",
            "-ngl", str(self.ngl), "-c", "4096", "-np", "1", "--no-webui",  # ctx 限 4096 + 单槽(串行用，默认 4 槽白吃 3 份 KV≈1GB)：4GB 卡省 KV 显存
        ]
        for r in self.loras.values():  # 按 id 序挂多个整理 LoRA（basic id0 / deep id1）
            cmd += ["--lora", r["path"]]
        # 无黑窗后台跑；stdout 丢弃（llama-server 日志很吵）
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 启动即退出，exit={self.proc.returncode}")
            if self._health():
                return
            time.sleep(0.3)
        self.stop()
        raise TimeoutError(f"llama-server {timeout}s 内未就绪")

    def stop(self):
        if self.proc is not None:
            self.proc.kill()
            self.proc = None
        elif self._health():
            # 复用的旧 server 不是自己的子进程，按镜像名杀（端口写死单实例，不会误伤）
            subprocess.run(["taskkill", "/im", os.path.basename(self.exe), "/f"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    def alive(self):
        # 只认端口健康：旧 voice_input 留下的 server 也复用，避免重复加载显存
        return self._health()

    def _health(self):
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    # ---------- 转写 ----------
    def transcribe(self, audio, sr, context="", language=None, timeout=60):
        """audio: float32 numpy 数组(-1..1)。返回 (识别文本, 置信度)。
        置信度 = 生成 token 平均 logprob 的 exp(0..1)；预填了 <asr_text> 前缀，
        logprobs 恰好只覆盖正文，无需再剔除前缀 token。拿不到 logprobs 时为 None。"""
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm16.tobytes())
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        messages = [
            {"role": "system", "content": context or ""},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
            ]},
        ]
        if language:
            # 与 qwen_asr 一致：预填 assistant 前缀强制语言、只出正文（llama-server 支持续写末尾 assistant 消息）
            messages.append({"role": "assistant", "content": f"language {language}<asr_text>"})
        payload = {"messages": messages, "temperature": 0,
                   "logprobs": True, "top_logprobs": 1}
        if self.loras:
            payload["lora"] = self._lora_scales(active=None)  # 转写：所有整理 LoRA 显式 scale=0（缺省是全量应用，会污染转写）
        choice = self._chat(payload, timeout)
        text = choice["message"]["content"] or ""
        return self._strip_prefix(text), self._confidence(choice)

    # ---------- 整理 ----------
    def cleanup(self, text, system, role="basic", timeout=120):
        """整理人格：同一份权重 + 对应角色的整理 LoRA(scale=1) 把口语原文整理。
        role: basic=保守整理 / deep=深度整理。system 用该 LoRA 训练时的提示词（训推一致）。
        未挂 loras 时原样返回；role 不存在则退到 basic（或任一已挂角色）。
        返回 (整理文本, 置信度)：置信度算法与 transcribe() 一致（exp(平均 logprob)），
        取不到/空输出兜底回原文时为 None。"""
        text = text.strip()
        if not text or not self.loras:
            return text, None
        if role not in self.loras:
            role = "basic" if "basic" in self.loras else next(iter(self.loras))
        if not self.alive():
            self.start()  # 休眠刚杀掉 server 又要整理（如收尾回填）时拉起来
        payload = {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": text}],
            "temperature": 0,
            "max_tokens": max(256, len(text) * 2),  # 预算跟随输入，防长段截断
            "lora": self._lora_scales(active=role),
            "logprobs": True, "top_logprobs": 1,
        }
        _t0 = time.perf_counter()
        choice = self._chat(payload, timeout)
        reply = self._strip_prefix((choice["message"]["content"] or "").strip())
        print(f"   [整理:{role}] 耗时 {time.perf_counter()-_t0:.2f}s（{len(text)}字→{len(reply)}字）")
        return (reply, self._confidence(choice)) if reply else (text, None)  # 空输出兜底回原文

    # ---------- 通用文本对话（屏幕提词等杂用） ----------
    def extract(self, text, system, role="basic", timeout=60, max_tokens=32):
        """挂一个整理 LoRA(scale=1)发一轮纯文本对话，返回模型文本。给屏幕提词用。
        必须挂 LoRA：base(全 scale=0)会锁死在 ASR 模式，只回显提示词干不了文本活；
        任一人格 scale=1 即解锁指令跟随(实测 basic 提术语已很准)。role 缺省 basic，
        不存在退到任一已挂角色。未挂 loras 时返回空。llama-server 单槽串行，勿频繁调。
        server 没活着直接跳过——提词是可选增益，绝不为它启动/复活 server(否则休眠杀了又被拉起，显存不释放)。"""
        text = (text or "").strip()
        if not text or not self.loras or not self.alive():
            return ""
        if role not in self.loras:
            role = "basic" if "basic" in self.loras else next(iter(self.loras))
        payload = {
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": text}],
            # 32 够挑出几个短术语；1.7B 提词爱不早停拖到上限,调大会跑满霸占单槽拖慢转写
            # (见 screen_context 说明),而超出部分本就被 parse_terms 丢弃
            "temperature": 0, "max_tokens": max_tokens,
            "lora": self._lora_scales(active=role),
        }
        choice = self._chat(payload, timeout)
        return self._strip_prefix((choice["message"]["content"] or "").strip())

    # ---------- 提醒解析 ----------
    def reminder_chat(self, messages, timeout=60):
        """提醒解析人格：整串 messages(system+历史+本轮 user) → 模型出 JSON 文本。
        reminder LoRA(scale=1)、其余人格全 scale=0。未挂 reminder 角色时 fail fast——
        否则会退成裸 base(ASR 前缀空转)。返回原始文本，交 reminder_chat._parse 抠 JSON。"""
        if "reminder" not in self.loras:
            raise RuntimeError("reminder 人格未挂载（loras 缺 reminder 角色）")
        if not self.alive():
            self.start()
        # 温度 0（greedy）：提醒解析要唯一正确的 JSON，采样无益；训练评测也是 greedy，训推一致。
        payload = {"messages": messages, "temperature": 0.0, "top_k": 1, "max_tokens": 1024,
                   "lora": self._lora_scales(active="reminder")}
        choice = self._chat(payload, timeout)
        return self._strip_prefix((choice["message"]["content"] or "").strip())

    def _chat(self, payload, timeout):
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]

    @staticmethod
    def _confidence(choice):
        """从 choice.logprobs.content 算 exp(平均 logprob)；缺失/为空返回 None。"""
        try:
            lps = [t["logprob"] for t in choice["logprobs"]["content"]]
            return float(np.exp(np.mean(lps))) if lps else None
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _strip_prefix(text):
        """剥掉 'language Chinese<asr_text>' 式前缀，只留正文。"""
        if "<asr_text>" in text:
            return text.split("<asr_text>", 1)[1].strip()
        return text.strip()


if __name__ == "__main__":
    # 最小自检：不起服务，只验证 wav 编码与前缀剥离逻辑
    a = LlamaASR.__new__(LlamaASR)
    assert LlamaASR._strip_prefix("language Chinese<asr_text>你好。") == "你好。"
    assert LlamaASR._strip_prefix("你好。") == "你好。"
    x = np.zeros(1600, dtype=np.float32)
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    assert pcm.dtype == np.int16 and pcm.size == 1600
    c = LlamaASR._confidence({"logprobs": {"content": [
        {"logprob": -0.1}, {"logprob": -0.3}]}})
    assert abs(c - float(np.exp(-0.2))) < 1e-6
    assert LlamaASR._confidence({}) is None
    assert LlamaASR._confidence({"logprobs": None}) is None
    assert LlamaASR._confidence({"logprobs": {"content": []}}) is None
    print("self-check ok")
