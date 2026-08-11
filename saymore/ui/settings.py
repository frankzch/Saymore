# -*- coding: utf-8 -*-
"""设置面板（数据 + 后端）：精简后设置界面只剩「各种语音命令词」+「热词」，由 main_window 的
「设置」tab 渲染，本模块不再自己开窗。

对外：CATEGORIES（界面显示哪些项）、settings_data(cfg, cfg_dir)（喂前端的结构）、
save(config_path, payload)。
精简原则见 CLAUDE：日常不碰的项（灵敏度/切句/整理开关/面板尺寸/高级等）一律「删界面行、
留 config 键」——它们仍在 config.json 里带默认值，只是不在界面上露，power user 改 json 即可。
保存只覆盖界面出现过的键，其余键原样保留。

wordlist 类型（专业术语 / 历史热词）：值不落 config，直接读写外部 .txt 文件（每行一词）。
选项字典里 file 指定文件名（相对 config 所在目录），或 file_key 指定从 config 里取路径。"""
import json
import os
from pathlib import Path

# 每类：(标题, 注解, [(key, 标签, 说明, 类型, 选项)])。当前全是 list（一组命令词，前端用小方块展示）。
# 两类按作用域分门别类：整程序 / 语音输入。注解让用户一眼分清这类是干什么的。
# 选项(第 5 项)：select 用它列候选；list 可传 {"single": True} 表示只保留一个方块（如唤醒词）。
CATEGORIES = [
    ("通用设置", "", [
        ("autostart", "开机自启", "登录 Windows 时自动在后台启动 Saymore",
         "bool", None),
        ("input_device", "麦克风", "系统里所有输入设备都在下拉里，选完对着绿条说两句能跳就是选对了；改动重启生效", "mic", None),
        ("device", "计算设备", "语音识别 + 文本整理都用这个跑（同一个本地模型进程）", "select",
         [("cuda", "GPU", "显卡加速，速度快,推荐"),
          ("cpu", "CPU", "纯 CPU 推理，不挑显卡但会变慢，具体幅度因 CPU 型号而异")]),
        ("wake_words", "唤醒词", "唤醒程序，进入听写模式", "list", {"single": True}),
        ("sleep_words", "休眠词", "让程序进入休眠状态，释放显存", "list", None),
        ("quit_words", "退出词", "退出应用程序", "list", None),
    ]),
    ("语音输入", "", [
        ("polish_mode", "整理模式", "停顿数秒后自动按此模式整理面板文字", "select",
         [("小范围整理", "轻度整理", "仅对原文做小幅度的改动，尽量保持原意"),
          ("深度整理", "深度整理", "对全文重排、润色，转为书面化风格"),
          ("邮件整理", "邮件整理", "整成得体的邮件格式"),
          ("00后整理", "00后整理", "改成年轻化网感语气")]),
        ("send_words", "发送指令词", "将当前缓存下来已转换的文字，填入当前光标处的输入框", "list",
         {"toggle": {"key": "auto_enter", "label": "自动回车",
                     "desc": "关闭，则只把缓存下来的文字放入光标所在的输入框，不按回车提交"}}),
        ("undo_words", "回退指令词", "删掉面板里最近攒进去的一句", "list", None),
        ("clear_words", "清空指令词", "将缓存面板里全部文字清掉", "list", None),
        ("polish_words", "文本整理指令词", "不等停顿倒计时，立即按当前整理模式整理面板文字", "list", None),
    ]),
]

# 热词单独一个顶级菜单项（词条多、编辑场景不同），前端渲染到 p-hotwords panel。
HOTWORD_CATEGORIES = [
    ("热词", "作为上下文喂给识别模型，让专业术语识别更准。改动实时生效，无需重启。", [
        ("terms_file", "专业术语",
         "自己维护的专有名词、行业术语、产品名等。每行一个词。",
         "wordlist", {"file_key": "qwen_context_file", "default": "terms.txt"}),
        ("hotwords_file", "历史热词",
         "程序从你的口述历史里自动提取的高频专业词，可手动增删。每行一个词。",
         "wordlist", {"file": "hotwords.txt"}),
    ]),
]

_ALL_CATEGORIES = CATEGORIES + HOTWORD_CATEGORIES

_TYPES = {k: t for _, _, fs in _ALL_CATEGORIES for k, _, _, t, _ in fs}
_WORDLIST_OPTS = {}  # key -> options dict，save 时按此定位文件
for _, _, _fs in _ALL_CATEGORIES:
    for _k, _lbl, _desc, _t, _opts in _fs:
        if _t == "list" and isinstance(_opts, dict) and "toggle" in _opts:
            _TYPES[_opts["toggle"]["key"]] = "bool"  # list 内联开关也要能存
        if _t == "wordlist":
            _WORDLIST_OPTS[_k] = _opts or {}


def _wordlist_path(key, cfg, cfg_dir):
    """wordlist 字段的实际文件路径：file_key 从 config 读路径（含默认回退），或直接 file。"""
    opts = _WORDLIST_OPTS[key]
    if "file_key" in opts:
        name = cfg.get(opts["file_key"]) or opts.get("default", "")
    else:
        name = opts.get("file", "")
    if not name:
        return None
    p = Path(name)
    return p if p.is_absolute() else cfg_dir / p


def _read_wordlist(path):
    if path is None or not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_wordlist(path, words):
    """原子写入，避免读侧读到半截。"""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(words) + ("\n" if words else "")
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)


def _to_str(value, typ):
    if typ in ("list", "wordlist"):
        return list(value or [])
    if typ == "bool":
        return bool(value)
    return "" if value is None else str(value)


def _parse(raw, typ):
    """把界面传回的值按类型转成配置值。数字非法抛 ValueError 给调用方兜底。"""
    if typ == "bool":
        return bool(raw)
    if typ == "mic":
        return str(raw or "").strip()  # 空串=系统默认；否则设备名
    if typ == "list":
        seps = str(raw).replace("，", "\n").replace(",", "\n")
        return [x.strip() for x in seps.splitlines() if x.strip()]
    if typ == "wordlist":
        # 词表专用：只按换行切（词内可含空格如 "prompt engineering"），去空行去首尾空白
        return [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    raw = str(raw).strip()
    if typ == "int":
        return int(float(raw))       # 容忍 "300.0"
    if typ == "float":
        return float(raw)
    return raw


def _norm_opts(opts, typ):
    """select 的 options 归一为 [{"v":存储值,"label":显示名,"d":选项释义}]。
    tuple 支持 (值,说明) 和 (值,显示名,说明) 两种；纯字符串则三者皆同、说明留空。
    分「存储值 / 显示名」是为了改文案时不牵动其他文件用作模式键的字符串。
    非 select（如 list 的 {"single":True}）原样返回。"""
    if typ != "select" or opts is None:
        return opts
    out = []
    for o in opts:
        if isinstance(o, tuple):
            if len(o) == 3:
                out.append({"v": o[0], "label": o[1], "d": o[2]})
            else:
                out.append({"v": o[0], "label": o[0], "d": o[1]})
        else:
            out.append({"v": o, "label": o, "d": ""})
    return out


def _norm_list_opts(opts, cfg):
    """list 的 options 若挂了内联开关（toggle），填入该开关当前值。"""
    if isinstance(opts, dict) and "toggle" in opts:
        opts = dict(opts, toggle=dict(opts["toggle"], value=bool(cfg.get(opts["toggle"]["key"], True))))
    return opts


def _field_value(k, t, cfg, cfg_dir):
    """wordlist 的值来自外部文件；其他类型来自 config。"""
    if t == "wordlist":
        return _read_wordlist(_wordlist_path(k, cfg, cfg_dir))
    return _to_str(cfg.get(k), t)


def _cats_data(cats, cfg, cfg_dir):
    return [{"title": title, "caption": caption,
             "fields": [{"key": k, "label": lbl, "desc": desc, "type": t,
                         "options": _norm_list_opts(opts, cfg) if t == "list" else _norm_opts(opts, t),
                         "value": _field_value(k, t, cfg, cfg_dir)}
                        for k, lbl, desc, t, opts in fields]}
            for title, caption, fields in cats]


def settings_data(cfg, cfg_dir):
    """喂前端两个 tab 的结构：settings=命令词、hotwords=热词。
    cfg_dir 是 config.json 所在目录，用于解析 wordlist 字段的相对文件路径。"""
    cfg_dir = Path(cfg_dir)
    return {"settings": _cats_data(CATEGORIES, cfg, cfg_dir),
            "hotwords": _cats_data(HOTWORD_CATEGORIES, cfg, cfg_dir)}


def save(config_path, payload):
    """把界面改动写回 config.json / 词表文件，只覆盖 CATEGORIES 里出现过的键。
    wordlist 类型写入外部 .txt 文件，不动 config。数字非法抛 ValueError。"""
    cfg_path = Path(config_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg_dirty = False
    for key, raw in (payload or {}).items():
        if key not in _TYPES:
            continue
        typ = _TYPES[key]
        if typ == "wordlist":
            _write_wordlist(_wordlist_path(key, cfg, cfg_path.parent), _parse(raw, typ))
        else:
            cfg[key] = _parse(raw, typ)
            cfg_dirty = True
    if cfg_dirty:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    # 无参 = 自检：往返转换 + settings_data 结构 + save 真写回文件
    import tempfile

    assert _parse("a\nb, c", "list") == ["a", "b", "c"]
    assert _parse("300.0", "int") == 300 and _parse(True, "bool") is True
    assert _to_str(["x"], "list") == ["x"] and _to_str(None, "str") == ""
    assert _parse("apple\nprompt engineering\n", "wordlist") == ["apple", "prompt engineering"]
    keys = [k for _, _, fs in _ALL_CATEGORIES for k, *_ in fs]
    assert len(keys) == len(set(keys)), "有重复配置键"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cf = td / "config.json"
        cf.write_text('{"wake_words":["旧"],"paste":true,"unknown":1,"qwen_context_file":"terms.txt"}',
                      encoding="utf-8")
        (td / "terms.txt").write_text("claude\nopenai\n", encoding="utf-8")
        data = settings_data(json.loads(cf.read_text(encoding="utf-8")), td)
        assert set(data.keys()) == {"settings", "hotwords"}
        wake_field = next(f for cat in data["settings"] for f in cat["fields"] if f["key"] == "wake_words")
        assert wake_field["value"] == ["旧"]
        terms_field = next(f for cat in data["hotwords"] for f in cat["fields"] if f["key"] == "terms_file")
        assert terms_field["value"] == ["claude", "openai"], terms_field
        hot_field = next(f for cat in data["hotwords"] for f in cat["fields"] if f["key"] == "hotwords_file")
        assert hot_field["value"] == [], hot_field  # 文件不存在→空
        # 命令词分类里不应出现 wordlist 字段
        assert not any(f["type"] == "wordlist" for cat in data["settings"] for f in cat["fields"])
        # 保存：命令词写 config，词表写文件（不动 config）
        save(cf, {"wake_words": "新词\n另一个", "bogus": "x",
                  "terms_file": "claude\nopenai\nanthropic",
                  "hotwords_file": "热词一\n热词二"})
        saved = json.loads(cf.read_text(encoding="utf-8"))
        assert saved["wake_words"] == ["新词", "另一个"] and saved["paste"] is True \
            and saved["unknown"] == 1 and "bogus" not in saved, saved
        assert (td / "terms.txt").read_text(encoding="utf-8").splitlines() \
            == ["claude", "openai", "anthropic"]
        assert (td / "hotwords.txt").read_text(encoding="utf-8").splitlines() \
            == ["热词一", "热词二"]
    print("settings_window 自检通过")
