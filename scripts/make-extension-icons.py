#!/usr/bin/env python3
"""Generate Chrome Web Store icons + promo tile for the two Fastt extensions."""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN = ROOT / "loginExtension"
NOTELE = ROOT / "noTeleportLoginExtension"
DIST = ROOT / "dist"
DIST.mkdir(exist_ok=True)

ICON_SIZES = [16, 32, 48, 128]
PROMO_SIZE = (440, 280)


def rounded_square(size, radius_ratio, color):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * radius_ratio)
    d.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=r, fill=color)
    return img


def vertical_gradient(size, top, bottom, radius_ratio=0.22):
    w, h = size, size
    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = grad.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=int(w * radius_ratio), fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def chat_bubble(size, color=(255, 255, 255, 235)):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(size * 0.18)
    body_top = pad
    body_bottom = int(size * 0.70)
    body_left = pad
    body_right = size - pad
    radius = int(size * 0.18)
    d.rounded_rectangle(
        [(body_left, body_top), (body_right, body_bottom)],
        radius=radius,
        fill=color,
    )
    # tail
    tail_x = int(size * 0.32)
    tail_y_top = body_bottom - int(size * 0.04)
    tail_w = int(size * 0.16)
    tail_h = int(size * 0.16)
    d.polygon(
        [
            (tail_x, tail_y_top),
            (tail_x + tail_w, tail_y_top),
            (tail_x + int(tail_w * 0.25), tail_y_top + tail_h),
        ],
        fill=color,
    )
    return img


def draw_key(draw, size, cx, cy, color):
    # Key: circular bow + rectangular shaft + two teeth
    bow_r = int(size * 0.10)
    draw.ellipse(
        [(cx - bow_r, cy - bow_r), (cx + bow_r, cy + bow_r)],
        outline=color,
        width=max(2, int(size * 0.035)),
    )
    inner = int(size * 0.04)
    draw.ellipse(
        [(cx - inner, cy - inner), (cx + inner, cy + inner)],
        fill=(0, 0, 0, 0),
        outline=color,
        width=max(1, int(size * 0.02)),
    )
    shaft_left = cx + bow_r - int(size * 0.01)
    shaft_right = shaft_left + int(size * 0.22)
    shaft_top = cy - int(size * 0.025)
    shaft_bottom = cy + int(size * 0.025)
    draw.rectangle([(shaft_left, shaft_top), (shaft_right, shaft_bottom)], fill=color)
    tooth_w = int(size * 0.035)
    tooth_h = int(size * 0.055)
    draw.rectangle(
        [(shaft_right - tooth_w * 3, shaft_bottom), (shaft_right - tooth_w * 2, shaft_bottom + tooth_h)],
        fill=color,
    )
    draw.rectangle(
        [(shaft_right - tooth_w, shaft_bottom), (shaft_right, shaft_bottom + tooth_h)],
        fill=color,
    )


def draw_shield(draw, size, cx, cy, color):
    w = int(size * 0.32)
    h = int(size * 0.38)
    top = cy - h // 2
    bottom = cy + h // 2
    left = cx - w // 2
    right = cx + w // 2
    points = [
        (cx, top),
        (right, top + int(h * 0.18)),
        (right, top + int(h * 0.55)),
        (cx, bottom),
        (left, top + int(h * 0.55)),
        (left, top + int(h * 0.18)),
    ]
    draw.polygon(points, fill=color)
    # check mark
    check_color = (255, 255, 255, 255)
    cw = max(2, int(size * 0.04))
    p1 = (cx - int(w * 0.20), cy + int(h * 0.02))
    p2 = (cx - int(w * 0.02), cy + int(h * 0.18))
    p3 = (cx + int(w * 0.24), cy - int(h * 0.18))
    draw.line([p1, p2], fill=check_color, width=cw)
    draw.line([p2, p3], fill=check_color, width=cw)


def make_icon(size, variant):
    """variant: 'login' or 'noteleport'"""
    if variant == "login":
        top, bottom = (102, 126, 234), (118, 75, 162)  # indigo -> purple
    else:
        top, bottom = (16, 185, 129), (5, 150, 105)  # emerald
    bg = vertical_gradient(size, top, bottom)
    bubble = chat_bubble(size)
    bg.alpha_composite(bubble)
    d = ImageDraw.Draw(bg)
    cx = size // 2
    cy = int(size * 0.42)
    icon_color = top  # contrasts on white bubble
    if variant == "login":
        # shift key left a bit so it sits centered visually
        draw_key(d, size, cx - int(size * 0.10), cy, icon_color)
    else:
        draw_shield(d, size, cx, cy, icon_color)
    return bg


def make_promo(variant, label, sub):
    w, h = PROMO_SIZE
    if variant == "login":
        top, bottom = (102, 126, 234), (118, 75, 162)
    else:
        top, bottom = (16, 185, 129), (5, 150, 105)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    grad = Image.new("RGBA", (w, h))
    px = grad.load()
    for y in range(h):
        t = y / (h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b, 255)
    img.alpha_composite(grad)

    icon = make_icon(160, variant)
    img.alpha_composite(icon, (24, (h - 160) // 2))

    d = ImageDraw.Draw(img)
    text_x = 200
    max_text_w = w - text_x - 20

    def load_fit_font(text, max_w, max_size, min_size, bold=True):
        path = (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
            if bold
            else "/System/Library/Fonts/Supplemental/Arial.ttf"
        )
        for size in range(max_size, min_size - 1, -1):
            try:
                f = ImageFont.truetype(path, size)
            except OSError:
                return ImageFont.load_default()
            bbox = d.textbbox((0, 0), text, font=f)
            if bbox[2] - bbox[0] <= max_w:
                return f
        return f

    # Split label onto two lines: "Fastt" + rest
    if label.startswith("Fastt "):
        title_top = "Fastt"
        title_bottom = label[len("Fastt "):]
    else:
        title_top, title_bottom = label, ""

    font_title = load_fit_font(max(title_top, title_bottom, key=len), max_text_w, 36, 20, bold=True)
    try:
        font_sub = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
    except OSError:
        font_sub = ImageFont.load_default()

    title_h = font_title.size + 4
    d.text((text_x, 60), title_top, fill=(255, 255, 255, 255), font=font_title)
    d.text((text_x, 60 + title_h), title_bottom, fill=(255, 255, 255, 255), font=font_title)
    # wrap sub
    lines = []
    words = sub.split()
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        bbox = d.textbbox((0, 0), candidate, font=font_sub)
        if bbox[2] - bbox[0] > w - text_x - 20 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    sub_lines = []
    words = sub.split()
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        bbox = d.textbbox((0, 0), candidate, font=font_sub)
        if bbox[2] - bbox[0] > max_text_w and line:
            sub_lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        sub_lines.append(line)
    sub_y = 60 + title_h * 2 + 10
    for ln in sub_lines[:3]:
        d.text((text_x, sub_y), ln, fill=(255, 255, 255, 230), font=font_sub)
        sub_y += 22
    return img


def write_icons(target_dir, variant):
    out_dir = target_dir / "icons"
    out_dir.mkdir(exist_ok=True)
    for s in ICON_SIZES:
        icon = make_icon(s, variant)
        icon.save(out_dir / f"icon{s}.png", optimize=True)
    promo = make_promo(
        variant,
        "Fastt Login Capture" if variant == "login" else "Fastt No-Teleport Login",
        "One-click OnlyFans login that produces a relay curl command."
        if variant == "login"
        else "Mint OnlyFans cookies through your relay's egress IP. No teleport.",
    )
    promo.save(DIST / f"{variant}-promo-440x280.png", optimize=True)


write_icons(LOGIN, "login")
write_icons(NOTELE, "noteleport")
print("done")
