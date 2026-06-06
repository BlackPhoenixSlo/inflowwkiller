#!/usr/bin/env python3
"""Generate 1280x800 RGB (no-alpha) screenshot templates for both extensions.

These are *templates*, not real screenshots. Chrome Web Store reviewers
prefer screenshots that show the actual popup UI. Use these only if you
plan to swap in real captures later.
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DIST.mkdir(exist_ok=True)

W, H = 1280, 800


def font(path_bold, path_reg):
    return path_bold, path_reg


F_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
F_REG = "/System/Library/Fonts/Supplemental/Arial.ttf"


def load(p, size):
    try:
        return ImageFont.truetype(p, size)
    except OSError:
        return ImageFont.load_default()


def vertical_gradient(w, h, top, bottom):
    img = Image.new("RGB", (w, h), top)
    px = img.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def draw_popup_card(d, x, y, w, h, title, lines, accent):
    # Card
    d.rounded_rectangle([(x, y), (x + w, y + h)], radius=18, fill=(255, 255, 255))
    # Title bar
    d.rounded_rectangle(
        [(x, y), (x + w, y + 64)],
        radius=18,
        fill=accent,
    )
    d.rectangle([(x, y + 32), (x + w, y + 64)], fill=accent)
    f_title = load(F_BOLD, 22)
    d.text((x + 24, y + 18), title, fill=(255, 255, 255), font=f_title)

    # Body lines
    f_label = load(F_BOLD, 15)
    f_value = load(F_REG, 14)
    cy = y + 96
    for label, value in lines:
        d.text((x + 24, cy), label, fill=(60, 60, 70), font=f_label)
        d.text((x + 24, cy + 22), value, fill=(20, 20, 28), font=f_value)
        cy += 56

    # Buttons
    btn_y = y + h - 76
    btn_w = (w - 24 * 2 - 24) // 3
    btn_h = 44
    btn_labels = [("Log in", accent, (255, 255, 255)),
                  ("Copy curl", (240, 240, 244), (40, 40, 50)),
                  ("Reset", (250, 230, 230), (170, 30, 30))]
    bx = x + 24
    for lab, bg, fg in btn_labels:
        d.rounded_rectangle([(bx, btn_y), (bx + btn_w, btn_y + btn_h)], radius=10, fill=bg)
        bbox = d.textbbox((0, 0), lab, font=f_label)
        tw = bbox[2] - bbox[0]
        d.text((bx + (btn_w - tw) // 2, btn_y + 12), lab, fill=fg, font=f_label)
        bx += btn_w + 12


def make_screenshot(variant, idx, heading, sub, popup_title, popup_lines):
    if variant == "login":
        top, bottom = (102, 126, 234), (118, 75, 162)
        accent = (102, 126, 234)
    else:
        top, bottom = (16, 185, 129), (5, 150, 105)
        accent = (16, 185, 129)
    img = vertical_gradient(W, H, top, bottom)
    d = ImageDraw.Draw(img)

    # Heading
    f_h = load(F_BOLD, 56)
    f_s = load(F_REG, 24)
    d.text((80, 80), heading, fill=(255, 255, 255), font=f_h)
    d.text((80, 160), sub, fill=(245, 245, 255), font=f_s)

    # Popup card on the right
    card_w, card_h = 420, 520
    card_x, card_y = W - card_w - 80, (H - card_h) // 2 + 30
    # shadow
    shadow = Image.new("RGB", (W, H), (0, 0, 0))
    # simulate shadow via translucent rectangle on rgb (just darker offset rect)
    sd = ImageDraw.Draw(img)
    sd.rounded_rectangle(
        [(card_x + 8, card_y + 14), (card_x + card_w + 8, card_y + card_h + 14)],
        radius=18,
        fill=(0, 0, 0),
    )
    # overlay base bg slightly to soften shadow visual
    draw_popup_card(d, card_x, card_y, card_w, card_h, popup_title, popup_lines, accent)

    out = DIST / f"{variant}-screenshot-{idx}-1280x800.png"
    # Save as RGB (no alpha)
    img.convert("RGB").save(out, format="PNG", optimize=True)


# Login Capture screenshots
make_screenshot(
    "login", 1,
    "Capture your OnlyFans session",
    "One click. Curl on your clipboard. Paste into the Chatterly relay.",
    "Chatterly Login Capture",
    [
        ("Status", "Captured (with signing rules)"),
        ("user-id", "1234567"),
        ("x-of-rev", "20260520T103245Z-204-..."),
        ("static_param", "9f6a3d4e2c1b8a..."),
        ("Captured at", "2026-05-21 01:02:33"),
    ],
)
make_screenshot(
    "login", 2,
    "Headers, signing rules, cookies",
    "The relay gets everything it needs in a single curl command.",
    "Chatterly Login Capture",
    [
        ("Status", "Waiting for sign-in..."),
        ("Hint", "Click Log in, sign in to OnlyFans, open a chat."),
        ("Captures", "headers + sign() rules + cookies"),
        ("Stored at", "chrome.storage.local (this device only)"),
        ("Output", "curl command to system clipboard"),
    ],
)

# No-Teleport screenshots
make_screenshot(
    "noteleport", 1,
    "No teleport. Same egress IP.",
    "Mint OnlyFans cookies through your relay's proxy. Nothing else routed.",
    "Chatterly No-Teleport Login",
    [
        ("Proxy", "active (http)"),
        ("Host", "relay.example.com:8443"),
        ("Routed", "onlyfans.com only (PAC)"),
        ("All other sites", "DIRECT"),
        ("Status", "Captured (with signing rules)"),
    ],
)
make_screenshot(
    "noteleport", 2,
    "Scoped to OnlyFans by PAC",
    "Every other host goes DIRECT. Your normal browsing is unaffected.",
    "Chatterly No-Teleport Login",
    [
        ("PAC rule", "if host endsWith onlyfans.com -> proxy"),
        ("Else", "return DIRECT"),
        ("Auth", "fed via onAuthRequired"),
        ("On uninstall", "Chrome auto-reverts proxy"),
        ("Credentials", "stored locally only"),
    ],
)

print("done")
