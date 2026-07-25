# Infloww — Exactness Kit (sampled from the real app, dark mode, v5.7.14)

Colors pulled from actual pixels of the running app (window sized 1440×900, 2× retina).

## Core palette
| Token | Hex | Where |
|---|---|---|
| Accent blue (brand) | `#4166f6` | Online Fans btn, UNLOCK PPV btn, Send, Total-earnings figure |
| Success green | `#67d1ae` | Notifications button, online dots |
| Sidebar / far rail | `#000000` | main-window left nav, pure black |
| App canvas | `#262626` | dashboard background |
| Card surface | `#262626` | dashboard cards (subtle 1px border ~`#333`) |
| Messages thread bg | `#151515` | center conversation pane + composer input |
| Side panel bg | `#232323` | Fan-insights right rail, conversation list panel |
| Row selected / hover | `#353535` | selected conversation row |
| Border / divider | ~`#2e2e2e`–`#353535` | card + panel borders |
| Text primary | `#ffffff` | |
| Text muted | ~`#8a8a8a` / `#b0b0b0` | handles, timestamps, labels |
| Danger / unsub badge | red circle avatar (❌) ~`#f0483e` | expired/unsub fans |
| Spend badge | grey pill `$0`, green-ish for spenders | per-row |

## Type
Sans-serif, looks like **Inter / system UI**. Use `Inter, -apple-system, "SF Pro Text", system-ui, sans-serif`.
Radius: cards ~12px, buttons ~8–10px, pills/chips fully rounded.

## Notes
- App is **dark mode**. Overall feel: near-black sidebar, dark-grey canvas, blue accent.
- Reference screenshots (session-local, NOT committed): `scratchpad/assets/infloww/*.png`
- All clones use **placeholder fan data** — never real handles/PPV copy from the captures.
