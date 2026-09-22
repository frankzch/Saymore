"""胶囊标题栏：同一套细线图标、命中区域与悬停说明。"""
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont


ACTIONS = ("copy", "edit", "polish", "send")
LABELS = {"copy": "复制全文", "edit": "修改文字", "polish": "整理全文", "send": "发送"}
HEADER_HEIGHT = 36
MIN_WIDTH = 300  # 编辑态说明"Enter 保存 · Esc 放弃"至少要放得下


def button_rects(width, scale):
    size, gap, pad = round(26 * scale), round(4 * scale), round(8 * scale)
    x = width - pad - 4 * size - 3 * gap
    y = round(5 * scale)
    return {action: (x + i * (size + gap), y, x + i * (size + gap) + size, y + size)
            for i, action in enumerate(ACTIONS)}


def hit_button(rects, x, y):
    return next((action for action, (l, t, r, b) in rects.items() if l <= x < r and t <= y < b), None)


@lru_cache(maxsize=32)
def _icon(action, size, color):
    scale = 3
    image = Image.new("RGBA", (size * scale, size * scale))
    draw = ImageDraw.Draw(image)
    unit = size * scale / 24
    xy = lambda points: [(round(x * unit), round(y * unit)) for x, y in points]
    stroke = max(2, round(1.5 * unit))

    def line(points):
        points = xy(points)
        draw.line(points, fill=color, width=stroke, joint="curve")
        for x, y in (points[0], points[-1]):
            r = stroke / 2
            draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    if action == "copy":
        line([(7, 15), (5, 15), (5, 4), (15, 4), (15, 6)])
        draw.rounded_rectangle((8 * unit, 8 * unit, 19 * unit, 20 * unit),
                               radius=2 * unit, outline=color, width=stroke)
    elif action == "edit":
        line([(5, 19), (6, 14), (16, 4), (20, 8), (10, 18), (5, 19)])
        line([(14, 6), (18, 10)])
    elif action == "polish":  # 整理：一大一小两颗四角星（"魔法整理"）
        line([(10, 4), (12, 10), (18, 12), (12, 14), (10, 20), (8, 14), (2, 12), (8, 10), (10, 4)])
        line([(19, 3), (19, 8)])
        line([(16.5, 5.5), (21.5, 5.5)])
    elif action == "send":
        line([(3, 10), (21, 3), (14, 21), (11, 13), (3, 10)])
        line([(11, 13), (21, 3)])
    return image.resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def _font(size):
    for face in ("msyh.ttc", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(face, size)
        except OSError:
            continue
    return ImageFont.load_default()


def is_active(action, enabled=True, editing=False, sending=False):
    """编辑中只留编辑键（再点一下＝保存退出）；发送中或无正文时都不响应。"""
    if editing:
        return action == "edit"
    return enabled and not sending


def draw_toolbar(canvas, scale, hover=None, enabled=True, editing=False, sending=False, opacity=1,
                 notice=""):
    layer = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(layer)
    rects = button_rects(canvas.width, scale)
    # 平时标题栏留空；操作反馈(已复制)/悬停/编辑/发送时在猫与按钮之间居中显示说明，放不下就退到更短的版本。
    labels = ((notice,) if notice else
              (LABELS[hover],) if hover in LABELS and not editing and is_active(hover, enabled, editing, sending) else
              ("编辑中 · Enter 保存 · Esc 放弃", "Enter 保存 · Esc 放弃", "Enter 保存") if editing else
              ("发送中…",) if sending else ())
    label_x, header = round(42 * scale), round(HEADER_HEIGHT * scale)
    room = rects["copy"][0] - label_x - round(8 * scale)
    font = _font(max(10, round(11 * scale)))
    label = next((s for s in labels if draw.textlength(s, font=font) <= room), None)
    if label:
        draw.text(((label_x + rects["copy"][0]) / 2, header / 2), label, font=font,
                  fill=(96, 118, 104, 255), anchor="mm")
    for action, (l, t, r, b) in rects.items():
        active = is_active(action, enabled, editing, sending)
        selected = editing and action == "edit"
        if selected:  # 编辑键实心绿底白图标，一眼看出正处于编辑模式
            draw.rounded_rectangle((l, t, r, b), radius=round(7 * scale), fill=(52, 168, 83, 255))
        elif action == hover and active:
            draw.rounded_rectangle((l, t, r, b), radius=round(7 * scale), fill=(200, 234, 212, 255))
        # 有意为之：无正文只禁止操作，不改变标题栏配色；编辑中其余键才变灰。
        color = ((255, 255, 255, 255) if selected else (170, 184, 176, 255) if editing else
                 (52, 168, 83, 255) if action == "send" else (51, 65, 58, 255))
        icon_size = round(17 * scale)
        layer.alpha_composite(_icon(action, icon_size, color),
                              (l + (r - l - icon_size) // 2, t + (b - t - icon_size) // 2))
    draw.line((round(12 * scale), header, canvas.width - round(12 * scale), header),
              fill=(190, 225, 202, 255), width=max(1, round(scale)))
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda a: round(a * max(0, opacity))))
    canvas.alpha_composite(layer)
    return rects
