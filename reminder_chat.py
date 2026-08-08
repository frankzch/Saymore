"""提醒模式的对话逻辑——把一句用户话变成「执行动作 + 一句朗读回复」。

设计要点：状态的唯一真相是 reminders.json，每轮都把它的最新快照塞进系统提示，
所以聊天历史只用来接住眼前几句的指代，可随便丢（定长 deque）。LLM 只输出 JSON。

提示词唯一真相是 finetune/reminder_prompt.txt（含时间锚点表——把日期算术搬出模型，
1.7B 提醒 LoRA 只挑不算）。build_system 是训推共用的唯一构造入口：造数据（reminder_gen.py）
和运行时推理都调它填 system，训推物理上不会错配。
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_PROMPT_FILE = Path(__file__).parent / "finetune" / "reminder_prompt.txt"


def _anchors(now):
    """时间锚点表：预算好相对/口语时间对应的具体日期时刻，模型只需引用不必推算。"""
    mon = now - timedelta(days=now.weekday())  # 本周一
    week = lambda base: " ".join(f"{_WEEKDAY[i]}={base+timedelta(days=i):%m-%d}" for i in range(7))
    return (
        "时间锚点（相对/口语时间按此表换算，勿自行推算日期）：\n"
        f"今天={now:%Y-%m-%d} 明天={now+timedelta(days=1):%Y-%m-%d} 后天={now+timedelta(days=2):%Y-%m-%d}\n"
        f"本周: {week(mon)}\n"
        f"下周: {week(mon+timedelta(days=7))}\n"
        f"当前时刻 +15分={now+timedelta(minutes=15):%H:%M} +30分={now+timedelta(minutes=30):%H:%M} +1小时={now+timedelta(hours=1):%H:%M}"
    )


def build_system(store, now=None):
    """构造提醒助手的 system。store 可以是 Reminders 实例，也可以是任何有 all()->list 的对象
    （造数据时传合成快照）。占位用 <<>> 避免和 JSON 花括号冲突。"""
    now = now or datetime.now()
    reminders = json.dumps(store.all(), ensure_ascii=False)
    tpl = _PROMPT_FILE.read_text(encoding="utf-8")
    return (tpl.replace("<<NOW>>", now.strftime("%Y-%m-%d %H:%M"))
               .replace("<<WEEKDAY>>", _WEEKDAY[now.weekday()])
               .replace("<<ANCHORS>>", _anchors(now))
               .replace("<<REMINDERS>>", reminders)).strip()


def _parse(raw):
    """从模型输出里抠出 JSON 对象，容忍 ```json 围栏和前后废话。"""
    s = raw.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        raise ValueError(f"模型未返回 JSON: {raw!r}")
    return json.loads(m.group(0))


def _execute(store, action):
    """执行一个动作，返回是否真的改动了 store。add 按 (text, due) 去重跳过重复——
    文件导入分块重叠时同一条记录可能被识别两次，靠这个兜底不建出重复提醒。"""
    op = action.get("op")
    _opt = ("repeat", "repeat_until", "repeat_count", "phrases")
    if op == "add":
        dup = any(r["text"] == action["text"] and r["due"] == action["due"] for r in store.all())
        if dup:
            return False
        extra = {k: action[k] for k in _opt if k in action}
        store.add(action["text"], action["due"], **extra)
        return True
    elif op == "update":
        fields = {k: action[k] for k in ("text", "due") + _opt if k in action}
        return store.update(action["id"], **fields)
    elif op == "delete":
        return store.delete(action["id"])
    return False


def handle(store, history, user_text, llm_cfg, chat_fn):
    """处理一轮：返回 (要朗读的话, 真正执行成功的 actions 列表)。history 是定长 deque，就地追加。
    chat_fn(messages, cfg)->str 由调用方注入（运行时=llama-server 提醒人格，造数据=教师）。
    actions 供调用方统计（如文件导入分块处理时数一共建了几条，不必每块都朗读）；被去重跳过
    或执行失败的动作不会出现在返回的 actions 里。"""
    history.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": build_system(store)}] + list(history)
    raw = chat_fn(messages, llm_cfg)
    try:
        data = _parse(raw)
    except (ValueError, json.JSONDecodeError):
        return "抱歉，我没太听懂，请再说一遍。", []
    applied = []
    for action in data.get("actions", []):
        try:
            if _execute(store, action):
                applied.append(action)
        except (KeyError, ValueError) as e:
            print(f"[warn] 执行动作失败 {action}: {e}")
    say = data.get("say", "好的。")
    history.append({"role": "assistant", "content": say})
    return say, applied


def _selfcheck():
    import tempfile, os, shutil
    from collections import deque
    from reminders import Reminders
    p = os.path.join(tempfile.gettempdir(), "reminder_chat_selftest.json")
    d = os.path.splitext(p)[0]
    if os.path.exists(p):
        os.remove(p)
    if os.path.exists(d):
        shutil.rmtree(d)
    store = Reminders(p)
    hist = deque(maxlen=8)

    # 假 LLM：第一轮新增，第二轮删除——验证 parse + execute + history 串起来
    future_due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT15:00:00")

    def fake_add(messages, cfg):
        assert messages[0]["role"] == "system"
        assert "现有提醒" in messages[0]["content"]
        return ('```json\n{"say":"好的，明天下午三点提醒你开会","actions":'
                f'[{{"op":"add","text":"开会","due":"{future_due}"}}]}}\n```')

    say, actions = handle(store, hist, "提醒我明天下午三点开会", {}, chat_fn=fake_add)
    assert "开会" in say
    assert len(actions) == 1 and actions[0]["op"] == "add"
    assert len(store.all()) == 1 and store.all()[0]["text"] == "开会", store.all()
    assert len(hist) == 2  # user + assistant

    rid = store.all()[0]["id"]

    def fake_del(messages, cfg):
        return f'{{"say":"已删除","actions":[{{"op":"delete","id":{rid}}}]}}'

    handle(store, hist, "删了它", {}, chat_fn=fake_del)
    assert store.all() == [], store.all()
    assert len(hist) == 4

    # 坏输出要兜底不崩
    say, actions = handle(store, hist, "随便说点", {}, chat_fn=lambda m, c: "我不是JSON")
    assert "没太听懂" in say and actions == []

    # —— 去重：重叠切块导致同一条记录被识别两次时，第二次别真的建出重复提醒 ——
    hist2 = deque(maxlen=8)
    dup_due = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")

    def fake_dup(messages, cfg):
        return f'{{"say":"好的","actions":[{{"op":"add","text":"军训报到","due":"{dup_due}"}}]}}'

    _say, applied1 = handle(store, hist2, "军训报到", {}, chat_fn=fake_dup)
    _say, applied2 = handle(store, hist2, "（重叠区重复识别到）军训报到", {}, chat_fn=fake_dup)
    assert len(applied1) == 1 and len(applied2) == 0, "同 (text,due) 第二次该被去重跳过"
    assert sum(1 for r in store.all() if r["text"] == "军训报到") == 1, "不该建出重复提醒"

    shutil.rmtree(d)
    print("reminder_chat 自检通过。")


if __name__ == "__main__":
    _selfcheck()
