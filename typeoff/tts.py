"""文本转语音。优先用 edge-tts 的微软神经语音（在线、音质好），失败/没网回退系统 SAPI（离线）。

休眠时也能播：纯音频播放，不依赖 GPU/ASR。提醒线程和对话线程都会调 speak，用锁串行化。
edge-tts 已在 requirements；mp3 用 Windows 自带 MCI 播放，零新依赖。
"""
import asyncio
import ctypes
import json
import os
import tempfile
import threading
import time

import comtypes.client

_lock = threading.Lock()
playing = False

# 音色：可在 config.json 改 tts_voice。常用 zh-CN-XiaoxiaoNeural(女)/zh-CN-YunxiNeural(男)。
try:
    with open(os.path.join(os.path.dirname(__file__), "config.json"), encoding="utf-8") as f:
        _VOICE = json.load(f).get("tts_voice", "zh-CN-XiaoxiaoNeural")
except Exception:
    _VOICE = "zh-CN-XiaoxiaoNeural"


def _speak_sapi(text):
    """系统自带、离线兜底。"""
    global playing
    try:
        comtypes.CoInitialize()
    except Exception:
        pass  # 已初始化过的线程会抛 S_FALSE，忽略
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    playing = True
    try:
        voice.Speak(text)
    finally:
        playing = False


def _play_mp3(path):
    """用 Windows 自带 MCI(winmm)同步播 mp3，阻塞到放完。用轮询播放状态判断放完，
    而不是按 mp3 的 length 元数据算 sleep 时长——部分文件（尤其 ffmpeg 转码过的）
    length 读数不准，按它 sleep 会提前切断播放。"""
    global playing
    alias = "edgetts_play"
    mci = ctypes.windll.winmm.mciSendStringW
    buf = ctypes.create_unicode_buffer(64)
    mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    try:
        playing = True
        mci(f"play {alias}", None, 0, None)
        while True:
            mci(f"status {alias} mode", buf, 64, None)
            if buf.value.strip().lower() != "playing":
                break
            time.sleep(0.02)
    finally:
        playing = False
        mci(f"close {alias}", None, 0, None)


def _speak_edge(text):
    """edge-tts 合成到临时 mp3 再播放。失败会抛异常，由 speak 回退 SAPI。"""
    import edge_tts

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        asyncio.run(edge_tts.Communicate(text, _VOICE).save(path))
        if os.path.getsize(path) == 0:
            raise RuntimeError("edge-tts 返回空音频")
        _play_mp3(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_cue_dir = os.path.join(tempfile.gettempdir(), "voiceinput_cues")

# 短提示语变速倍数：ffmpeg atempo 只变速不变调（MCI 原生变速会变调，听着像捏着嗓子说话，弃用）。
# 装了 ffmpeg 才会加速；没装就原速播放（慢但音色正常，优先保证不失真）。
_CUE_TEMPO = 1.4


def _speedup_mp3(src, dst, tempo):
    """用 ffmpeg atempo 变速不变调，写到 dst。没装 ffmpeg 或转码失败返回 False。"""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", src, "-filter:a", f"atempo={tempo}", dst],
            check=True, capture_output=True,
        )
        return os.path.getsize(dst) > 0
    except Exception:
        return False


def play_cue(text):
    """播放一个固定的短提示语（如"嗯"/"好的"）。首次合成后缓存到本地，之后直接放缓存，
    避免每说一句都走一次网络合成而拖慢即时反馈。合成失败回退 SAPI。"""
    text = (text or "").strip()
    if not text:
        return
    import hashlib
    key = hashlib.md5(f"{_VOICE}:{_CUE_TEMPO}:{text}".encode("utf-8")).hexdigest()
    path = os.path.join(_cue_dir, key + ".mp3")
    with _lock:
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            try:
                import edge_tts
                os.makedirs(_cue_dir, exist_ok=True)
                raw = path + ".raw.mp3"
                asyncio.run(edge_tts.Communicate(text, _VOICE).save(raw))
                if os.path.getsize(raw) == 0:
                    raise RuntimeError("edge-tts 返回空音频")
                if not _speedup_mp3(raw, path, _CUE_TEMPO):
                    os.replace(raw, path)  # 没有 ffmpeg：原样用原速文件，保证音色正常
                else:
                    os.remove(raw)
            except Exception as e:
                print(f"[tts] cue 合成失败，回退 SAPI：{e}")
                _speak_sapi(text)
                return
        _play_mp3(path)


def speak(text):
    """同步播报一段中文。多线程调用排队，不会撞车。"""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        try:
            _speak_edge(text)
        except Exception as e:
            print(f"[tts] edge-tts 失败，回退 SAPI：{e}")
            _speak_sapi(text)


if __name__ == "__main__":
    speak("语音提醒测试，现在用的是微软神经语音。")
    print("已播报，听到声音即为正常。")
