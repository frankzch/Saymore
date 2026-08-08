"""LLM 传输层——OpenAI 兼容 /chat/completions，云端/本地共用一套。复用已装的 requests，零 SDK。

云端示例 base_url: https://dashscope.aliyuncs.com/compatible-mode/v1 (阿里百炼)
本地示例 base_url: http://localhost:11434/v1 (ollama) —— 换 base_url/model/api_key 即切换。
"""
import os
import requests


def chat(messages, cfg, usage=None):
    """发一轮对话，返回模型输出的文本。
    cfg: {base_url, model, api_key?/api_key_env?, temperature?, timeout?}
    key 优先用 api_key（明文），否则从 api_key_env 指定的环境变量读（不落盘）。
    传入 usage（dict）则把本轮 token 用量累加进去（API 真实返回，本地模型可能没有则跳过）。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env", ""), "")
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.2),
    }
    if cfg.get("max_tokens"):  # Anthropic OAI-compat 需要，DeepSeek 也接受
        payload["max_tokens"] = cfg["max_tokens"]
    if cfg.get("reasoning_effort"):  # 推理模型关思考:"none" 让其直接出正文(否则思考吃光 token,content 空)
        payload["reasoning_effort"] = cfg["reasoning_effort"]
    r = requests.post(url, headers=headers, json=payload, timeout=cfg.get("timeout", 60))
    r.raise_for_status()
    data = r.json()
    if usage is not None:
        u = data.get("usage") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[k] = usage.get(k, 0) + u.get(k, 0)
    return data["choices"][0]["message"]["content"]
