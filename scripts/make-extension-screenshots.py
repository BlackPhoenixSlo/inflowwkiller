#!/usr/bin/env python3
"""Render Chrome Web Store screenshots (1280x800, 24-bit PNG) for both extensions.

Each scene embeds the extension's real popup HTML inside a branded backdrop with
a caption, then re-saves the screenshot as RGB (no alpha) per store requirements.
"""
import base64
import re
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
LOGIN = ROOT / "loginExtension"
NOTELE = ROOT / "noTeleportLoginExtension"
DIST = ROOT / "dist"
SHOTS = DIST / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)


def read_popup(folder: Path) -> str:
    return (folder / "popup.html").read_text()


def extract_styles_and_body(html: str) -> tuple[str, str]:
    # Naive but sufficient: pull <style>...</style> + everything inside <body>
    style_start = html.find("<style>")
    style_end = html.find("</style>")
    styles = html[style_start + len("<style>") : style_end] if style_start != -1 else ""
    # Strip the global html/body reset — it clobbers our stage layout.
    # The popup is 360–400px wide; we let .popup-frame width handle that.
    styles = re.sub(
        r"html\s*,\s*body\s*\{[^}]*\}",
        "",
        styles,
    )
    body_start = html.find("<body>") + len("<body>")
    body_end = html.find("</body>")
    body = html[body_start:body_end]
    # Strip popup.js script tag — we'll inject our own state
    body = body.replace('<script src="popup.js"></script>', "")
    return styles, body


def icon_data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def render_scene(html: str, out_path: Path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        page = ctx.new_page()
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(150)
        raw = out_path.with_suffix(".raw.png")
        page.screenshot(path=str(raw), full_page=False, type="png", omit_background=False)
        browser.close()
    # Re-encode as 24-bit RGB PNG to guarantee no alpha channel
    img = Image.open(raw).convert("RGB")
    img.save(out_path, format="PNG", optimize=True)
    raw.unlink()


# ---------- shared scene chrome ----------
def scene_html(
    popup_styles: str,
    popup_body: str,
    bg_top: str,
    bg_bottom: str,
    title: str,
    subtitle: str,
    bullets: list[str],
    popup_width: int,
    icon_path: Path,
):
    icon_uri = icon_data_uri(icon_path)
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
*,*::before,*::after {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; width:1280px; height:800px; overflow:hidden;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  background: linear-gradient(135deg, {bg_top} 0%, {bg_bottom} 100%);
  color: #fff;
}}
.stage {{
  width:1280px; height:800px;
  display:grid; grid-template-columns: 1fr {popup_width + 100}px; gap: 0;
  align-items:center;
}}
.left {{ padding: 80px 60px 80px 80px; }}
.left .logo {{ display:flex; align-items:center; gap:14px; margin-bottom: 28px; }}
.left .logo img {{ width:56px; height:56px; border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,0.25); display:block; }}
.left .logo .brand {{ font-size: 14px; font-weight: 600; letter-spacing: 1.2px; text-transform: uppercase; opacity: 0.85; }}
.left h2 {{ font-size: 40px; line-height: 1.1; font-weight: 700; margin: 0 0 18px; letter-spacing: -0.5px; max-width: 520px; }}
.left p.sub {{ font-size: 17px; line-height: 1.5; opacity: 0.92; margin: 0 0 28px; max-width: 540px; }}
.left ul {{ list-style:none; padding:0; margin:0; }}
.left li {{
  position:relative; padding-left: 28px; font-size: 15px; line-height: 1.55;
  opacity: 0.95; margin-bottom: 12px; max-width: 540px;
}}
.left li::before {{
  content:''; position:absolute; left:0; top:8px; width:14px; height:14px;
  border-radius: 50%; background: rgba(255,255,255,0.22);
  box-shadow: inset 0 0 0 2px rgba(255,255,255,0.8);
}}
.right {{
  display:flex; align-items:flex-start; justify-content:center;
  height: 800px; padding: 60px 50px 60px 0;
}}
.popup-frame {{
  width: {popup_width}px;
  max-height: 680px;
  background: #0e1116;
  color: #e6edf3;
  border-radius: 12px;
  box-shadow: 0 24px 60px rgba(0,0,0,0.45), 0 4px 12px rgba(0,0,0,0.3);
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.08);
  position: relative;
}}
.popup-frame::after {{
  content:''; position:absolute; left:0; right:0; bottom:0; height: 60px;
  background: linear-gradient(to bottom, rgba(14,17,22,0), rgba(14,17,22,1));
  pointer-events: none;
}}
/* embedded popup styles (html/body resets stripped) */
{popup_styles}
</style></head><body>
<div class="stage">
  <div class="left">
    <div class="logo">
      <img src="{icon_uri}" alt="">
      <span class="brand">Fastt</span>
    </div>
    <h2>{subtitle}</h2>
    <p class="sub">{title}</p>
    <ul>
      {''.join(f'<li>{b}</li>' for b in bullets)}
    </ul>
  </div>
  <div class="right">
    <div class="popup-frame">{popup_body}</div>
  </div>
</div>
</body></html>"""


# ---------- per-extension state injections ----------

def login_states(body: str) -> list[tuple[str, str]]:
    """Return (label, body_variant) tuples."""
    # State 1: idle (default)
    idle = body
    # State 2: capture success
    success = body.replace(
        '<div id="status-line">Waiting for sign-in…</div>',
        '<div id="status-line" class="ok">Captured · ready to copy</div>',
    ).replace(
        '<div class="meta" id="meta"></div>',
        '<div class="meta" id="meta">'
        '<div><span class="label">user-id:</span> 412***928</div>'
        '<div><span class="label">x-of-rev:</span> 202605211442-7c…f0a3</div>'
        '<div><span class="label">x-bc:</span> b9e1…d2c4 (78 chars)</div>'
        '<div><span class="label">captured:</span> 3s ago</div>'
        "</div>",
    ).replace(
        '<button id="copy-btn" class="secondary" disabled>Copy curl</button>',
        '<button id="copy-btn" class="secondary">Copy curl</button>',
    )
    # add a toast on success scene
    success = success.replace(
        '<div class="toast" id="toast"></div>',
        '<div class="toast show" id="toast" style="background:rgba(46,160,67,0.12);'
        'border:1px solid rgba(46,160,67,0.4);color:#2ea043;">Curl copied to clipboard.</div>',
    )
    return [("idle", idle), ("captured", success)]


def noteleport_states(body: str) -> list[tuple[str, str]]:
    # State 1: proxy off, default
    idle = body
    # State 2: proxy on + captured
    on = body.replace(
        '<div class="proxy-state off" id="proxy-state">proxy: off</div>',
        '<div class="proxy-state on" id="proxy-state">proxy: on · socks5://relay-eu1:1080</div>',
    ).replace(
        '<div id="status-line">Waiting…</div>',
        '<div id="status-line" class="ok">Captured through proxy · ready</div>',
    ).replace(
        '<div class="meta" id="meta"></div>',
        '<div class="meta" id="meta">'
        '<div>user-id: 412***928</div>'
        '<div>x-of-rev: 202605211442-7c…f0a3</div>'
        '<div>egress IP: 185.***.***.42 (matches relay)</div>'
        '<div>static_param: ok</div>'
        "</div>",
    ).replace(
        '<button id="copy-btn" class="secondary" disabled>Copy curl</button>',
        '<button id="copy-btn" class="secondary">Copy curl</button>',
    ).replace(
        'value=""',
        'value="http://127.0.0.1:8787"',
        1,
    )
    on = on.replace(
        '<input id="relay-url" placeholder="http://127.0.0.1:8787">',
        '<input id="relay-url" value="http://127.0.0.1:8787" placeholder="http://127.0.0.1:8787">',
    ).replace(
        '<input id="proxy-url" placeholder="socks5://user:pass@host:port  or  http://user:pass@host:port">',
        '<input id="proxy-url" value="socks5://****@relay-eu1.chatterly:1080" '
        'placeholder="socks5://user:pass@host:port  or  http://user:pass@host:port">',
    )
    return [("idle", idle), ("active", on)]


# ---------- build scenes ----------

def build():
    login_html = read_popup(LOGIN)
    notele_html = read_popup(NOTELE)
    login_styles, login_body = extract_styles_and_body(login_html)
    notele_styles, notele_body = extract_styles_and_body(notele_html)

    # Login Capture: indigo/purple
    login_scenes = [
        {
            "label": "01-idle",
            "title": "One click opens OnlyFans in a fresh, signed-in session.",
            "subtitle": "Sign in once. Copy the curl.",
            "bullets": [
                "Captures user-id, x-bc, x-of-rev, sign, time, user-agent and cookies.",
                "Packaged as a ready-to-paste curl for the Fastt relay.",
                "When x-of-rev drifts, re-login and capture a fresh one.",
            ],
            "state": login_states(login_body)[0][1],
        },
        {
            "label": "02-captured",
            "title": "Fresh credentials, captured the moment OF first signs a request.",
            "subtitle": "Captured · ready to paste",
            "bullets": [
                "Watches for the first signed /api2/v2/ request after login.",
                "Bundles every header your relay needs in one curl.",
                "Local only — nothing leaves your browser.",
            ],
            "state": login_states(login_body)[1][1],
        },
    ]
    for s in login_scenes:
        html = scene_html(
            popup_styles=login_styles,
            popup_body=s["state"],
            bg_top="#667eea",
            bg_bottom="#764ba2",
            title=s["title"],
            subtitle=s["subtitle"],
            bullets=s["bullets"],
            popup_width=360,
            icon_path=LOGIN / "icons" / "icon128.png",
        )
        out = SHOTS / f"login-{s['label']}-1280x800.png"
        render_scene(html, out)
        print("wrote", out)

    # No-Teleport: emerald
    notele_scenes = [
        {
            "label": "01-idle",
            "title": "Sign in to OnlyFans through your relay's IP — before OF mints any cookie.",
            "subtitle": "No teleport. Same IP, start to finish.",
            "bullets": [
                "Routes onlyfans.com through your relay's proxy before login.",
                "Cookies are minted on the exact IP the relay will use later.",
                "Proxy stays off for the rest of your browsing.",
            ],
            "state": noteleport_states(notele_body)[0][1],
        },
        {
            "label": "02-active",
            "title": "Proxy active. Credentials captured. Egress IP matches the relay.",
            "subtitle": "Captured through the relay's IP",
            "bullets": [
                "Verifies the live egress IP at capture time.",
                "Static param fingerprint included for OF's sign() algorithm.",
                "Auto-loads proxy list from your relay if SHARE_TOKEN is set.",
            ],
            "state": noteleport_states(notele_body)[1][1],
        },
    ]
    for s in notele_scenes:
        html = scene_html(
            popup_styles=notele_styles,
            popup_body=s["state"],
            bg_top="#10b981",
            bg_bottom="#047857",
            title=s["title"],
            subtitle=s["subtitle"],
            bullets=s["bullets"],
            popup_width=400,
            icon_path=NOTELE / "icons" / "icon128.png",
        )
        out = SHOTS / f"noteleport-{s['label']}-1280x800.png"
        render_scene(html, out)
        print("wrote", out)


if __name__ == "__main__":
    build()
