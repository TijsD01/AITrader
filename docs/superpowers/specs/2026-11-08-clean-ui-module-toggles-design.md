# Clean UI with module toggles — design

Date: 2026-11-08
Status: approved in brainstorming, awaiting spec review

## Goal

Make the web UI calmer and closer to an Apple-style interface: more space, larger type,
fewer borders, quiet motion. Advanced modules stay hidden until the user switches them on.

Frontend only. No change to bot behavior, API routes or settings on the server.

## Decisions

| Topic | Decision |
|---|---|
| Hiding | Feature toggles per module (not only progressive disclosure) |
| Toggle storage | Per browser, `localStorage` |
| Off by default | Screener, Debate, Shadow, Memory |
| Always on | Home, Triggers, Advice, History, Settings |
| Theme | Follows system: light and dark |
| Approach | Restyle in place. No framework, no second page |

## 1. Theme and tokens

- `style.css` `:root` holds the **light** palette. `@media (prefers-color-scheme: dark)` holds the dark
  palette (today's values, softened). Both sets use the existing token names (`--bg`, `--surface`,
  `--text`, `--accent`, `--gain`, `--loss`, …).
- The Tailwind config in `index.html` maps `darkBg`, `cardBg`, `borderCol`, `slate-*` and `accent`
  to `rgb(var(--…) / <alpha-value>)`. Existing class names keep working and follow the theme.
  Each Tailwind-mapped token therefore also gets an RGB-triplet variant (e.g. `--slate-400-rgb`).
- Tokens switch through the media query alone, so the theme works without JS. `<html class="dark">`
  (only used by Tailwind `dark:` variants) and `meta theme-color` are synced from `matchMedia` at load
  and on change. `meta color-scheme` becomes `light dark`.
- Hard-coded hex colors in `app.js` (Chart.js series, inline styles) read the CSS variables through one
  helper `cssVar(name)` instead.
- Type: system font stack first (`-apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", …`),
  IBM Plex Mono kept for numbers. Page titles larger and tighter; section titles quieter.
- Cards: no visible border in light mode, a hairline in dark mode, soft shadow, `--radius-lg`.
- Contrast: text ≥ 4.5:1, data marks ≥ 3:1, in both themes. Gain and loss keep their sign or arrow.

## 2. Module toggles

New file `src/web/static/features.js`, loaded before `app.js`.

- Registry: `screener`, `debate`, `shadow`, `memory`. Each has label, icon, one-line description,
  `defaultOn: false`.
- Store: `localStorage` key `alphaagent.modules.v1`, JSON `{ id: bool }`. Every read and write is wrapped
  in `try/catch`; on failure the defaults apply.
- Markup: every tab button, bottom-nav item, "More" tile and `tab-content` section of a module gets
  `data-module="<id>"`. `features.js` sets `hidden` on all of them when the module is off.
- `switchTab(id)` falls back to `portfolio` when `id` belongs to a disabled module (also on load from a
  stored tab or URL hash).
- `MORE_TABS` is derived from the enabled modules plus `settings`. When only Settings remains, the
  mobile "More" button becomes a direct Settings button.
- Settings gets a new first section **Modules** with one iOS-style switch per module (label, icon,
  description). Switching emits a `modules:changed` event; nav updates at once, no reload.
- Data loads for a disabled module are skipped (no fetch for a hidden tab).
- Turning a module off never changes server state.

## 3. Layout

- Header: slim, sticky, translucent (`backdrop-filter: blur`), logo + status pill + Scan only.
  Mode, broker and market details stay reachable through the status pill.
- Desktop nav: centered segmented control showing only enabled tabs.
- Mobile: bottom nav stays (Home, Triggers, Advice, History, More/Settings), safe-area aware,
  touch targets ≥ 44px.
- Home: large portfolio value as the hero, KPIs below in a quiet row, then positions. Status and kill
  switch kept but visually reduced. All ids in `tests/test_web_home_contract.py::HOME_IDS` stay.
- More whitespace: section gaps up from `space-y-5` to about `space-y-8` on desktop.

## 4. Motion

Follows the `ui-animation-guidelines` skill. Tokens in `style.css`: `--dur-in: 200ms`,
`--dur-out: 160ms`, `--ease-out`, `--ease-in-out`, `--ease-in`.

- Tab change: outgoing fades, incoming fades in with 8px rise, ease-out.
- Lists and cards: 30ms stagger, capped at 8 items; rest appear at once.
- Buttons: `:active` scale 0.97.
- Switch thumb: ~12% stretch while moving.
- Module on/off: nav item fades and slides in or out.
- `<details>` folds: opacity and 4px slide on open.
- `prefers-reduced-motion: reduce`: only opacity, no transforms, no stagger.
- Only `transform` and `opacity` animate.

## Error handling

- `localStorage` blocked → defaults, no error shown.
- `features.js` fails to load → all modules visible (safe fallback: nothing becomes unreachable).
- Unknown module id in storage → ignored.

## Testing

- Existing tests stay green: `tests/test_web_home_contract.py`, `tests/test_web_static_caching.py`.
- New pytest `tests/test_web_modules_contract.py`:
  - `features.js` is served and referenced before `app.js`.
  - Every module tab, nav item and section carries `data-module`.
  - Settings contains the Modules section with one switch per module.
- Playwright check (manual, screenshots in the PR): light and dark, 390px and 1280px wide,
  modules off and on, reduced motion.

## Out of scope

- Server-side storage of toggles.
- New features or changed data on any tab.
- Splitting `app.js` beyond adding `features.js` and the `cssVar` helper.
