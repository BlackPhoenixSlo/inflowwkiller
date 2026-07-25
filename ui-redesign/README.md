# UI Redesign — Competitor Frontend Studies

Frontend-only recreations of how **infloww.com** and **onlymonster.ai** look, built as a
design sandbox for our own next-gen UI. **No backend, no data, no network calls** — pure HTML/CSS
(inline SVG for every icon/chart, zero image files). Open any `.html` directly in a browser.

## Why this exists
We want to redesign our own UI "aside of what we have" — keep what's useful, drop the irrelevant,
make it genuinely user-friendly — and, as a joke, offer an **"old Infloww" retro reskin** variant.
Step one is faithfully studying how the two market leaders present themselves and their in-product
cockpits. These files are that study.

## Structure
```
ui-redesign/
├── index.html              → hub / gallery — start here
├── README.md
├── assets/                 → drop old-Infloww screenshots here for the joke reskin
├── infloww/
│   ├── index.html          → Infloww marketing landing clone (blue / light SaaS)
│   └── app.html            → Infloww "Messages Pro" agency chat cockpit (3-column inbox)
└── onlymonster/
    ├── index.html          → OnlyMonster marketing landing clone (premium gold / cream)
    └── app.html            → OnlyMonster big-data analytics dashboard
```

## How to view
Just open `ui-redesign/index.html` in a browser and click through — everything is relative-linked.
Or serve locally:
```
cd ui-redesign && python3 -m http.server 8080   # → http://localhost:8080
```

## Design tokens observed (for reuse in our own UI)
| | Infloww | OnlyMonster |
|---|---|---|
| Mode | Light | Light (warm) |
| Accent | Bright blue `#2F6BFF` on navy `#12172B` | Gold `#CCB994`/`#B8975A` on cream `#FFFEF9` |
| Feel | Clean, techy, rounded | Premium, editorial, luxe |
| Signature | Messages Pro™, Smart Lists™, Vault Pro™, AI Copilot | Big-Data dashboard, ARPPU/ARPNU/APV/APC, gold charts |

## Next: the "old Infloww" joke reskin
Not built yet — it needs the **classic-version screenshots** (you mentioned you have videos of the
old UI). Drop stills into `assets/` and we'll theme a variant of our own screens to match that
older look. There's a placeholder card for it on the hub page.

---
*Design mockups only. Not affiliated with, endorsed by, or connected to Infloww or OnlyMonster.
All names, numbers, and logos shown are placeholders.*
