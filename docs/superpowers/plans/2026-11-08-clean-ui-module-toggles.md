# Clean UI with Module Toggles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Calmer, Apple-style web UI that follows the system light/dark theme, with Screener, Debate, Shadow and Memory hidden until switched on.

**Architecture:** Restyle in place. A new `src/web/static/features.js` owns the module registry and visibility; `app.js` asks it which tabs are allowed. Theme colors move into CSS variables that the Tailwind CDN config reads, so existing class names follow the theme.

**Tech Stack:** Vanilla JS, Tailwind CDN 3.4.1, Chart.js 4.4.1, FastAPI static serving, pytest, `node --test` (Node 22), Playwright (pre-installed, node).

**Spec:** `docs/superpowers/specs/2026-11-08-clean-ui-module-toggles-design.md`

## Global Constraints

- Frontend only. No change to API routes, server settings or bot behavior.
- Module ids: `screener`, `debate`, `shadow`, `memory`. All `defaultOn: false`.
- Always-on tabs: `portfolio` (Home), `triggers`, `advice`, `history`, `settings`.
- Storage key: `alphaagent.modules.v1`, JSON `{ "<id>": bool }`. Every `localStorage` access in `try/catch`.
- If `features.js` fails to load, every module stays visible.
- Theme follows `prefers-color-scheme`; tokens switch by media query, without JS.
- Contrast: text ≥ 4.5:1, data marks ≥ 3:1, both themes. Gain/loss keep sign or arrow.
- Motion: `--dur-in: 200ms`, `--dur-out: 160ms`; only `transform`/`opacity` animate; `prefers-reduced-motion: reduce` → opacity only, no stagger.
- Touch targets ≥ 44px on mobile.
- Every id in `tests/test_web_home_contract.py::HOME_IDS` stays in the page.
- Large files: grep the symbol, then read narrow line ranges (`app.js` 2.9k lines, `index.html` 1k lines).

## Review Focus

1. Stored tab or deep link (`openSettingsGroup`, a Home shortcut, `resumePendingJob`) points at a disabled module → user lands on Home, never on a blank page. Test: Task 2, `fallbackTab` cases.
2. `localStorage` holds garbage (`"null"`, an array, `{"screener":"yes"}`, unknown ids) → defaults apply, no exception. Test: Task 2, `resolveModules` cases.
3. All four modules off on mobile → "More" button becomes a direct Settings button; the sheet never opens empty. Test: Task 2 Playwright check.
4. Light theme: hard-coded dark colors in `app.js` templates (`text-white`, `#121722` doughnut border) stay unreadable on white. Check: Task 3 grep step + Task 6 screenshots.
5. Switching a module off while its tab is open → user moves to Home and the tab's 30 s refresh does not fetch for it. Test: Task 2 `fallbackTab` + Playwright check.

---

## Model assignment (orchestration)

| Task | Model | Why |
|---|---|---|
| 1 Markup + contract test | Sonnet 5.5 | Mechanical attribute and markup work |
| 2 `features.js` + `app.js` wiring | Opus 5.5 | State, navigation edge cases |
| 3 Theme tokens light/dark | Opus 5.5 | Tailwind var mapping, contrast across 2.9k-line JS |
| 4 Layout restyle | Opus 5.5 | Visual judgment, contract ids |
| 5 Motion | Sonnet 5.5 | Clear token recipe from skill |
| 6 Visual verification | Sonnet 5.5 | Screenshots + small fixes |
| Final review | Opus 5.5, medium effort | Whole branch |

Tasks run in order 1 → 6 (all touch `index.html`/`style.css`).

---

### Task 1: Module markup and contract test

**Files:**
- Modify: `src/web/static/index.html` (header nav ~l.104-163, sections `tab-screener` ~l.420, `tab-debate` ~l.439, `tab-shadow` ~l.692, `tab-memory` ~l.708, `tab-settings` ~l.789, script tag ~l.1035)
- Modify: `src/web/app.py:95` (`VERSIONED_ASSETS`)
- Create: `tests/test_web_modules_contract.py`

**Interfaces:**
- Produces: every element of a module (desktop `.tab-btn`, `.mobile-dropdown-item`, `section.tab-content`) has `data-module="<id>"`.
- Produces: Settings section `<details class="ui-list adv-details fold" data-fold="settings-modules" open>` as the first group, containing `<div id="moduleToggleList" class="ui-list"></div>` (filled by Task 2).
- Produces: `<script src="/static/features.js"></script>` directly before the `app.js` script tag; `features.js` exists (stub: `// Module toggles, see Task 2`).

- [ ] **Step 1: Write the failing tests** in `tests/test_web_modules_contract.py`, reusing the `_html()` helper pattern from `tests/test_web_home_contract.py`:

```python
MODULES = ("screener", "debate", "shadow", "memory")

def test_features_js_versioned_and_before_app_js():
    html = _html()
    f = re.search(r'src="/static/features\.js\?v=[0-9a-f]{10}"', html)
    a = re.search(r'src="/static/app\.js\?v=[0-9a-f]{10}"', html)
    assert f and a and f.start() < a.start()

def test_every_module_element_is_tagged():
    html = _html()
    for m in MODULES:
        assert re.search(rf'<section\b[^>]*id="tab-{m}"[^>]*data-module="{m}"', html), m
        assert len(re.findall(rf'data-tab="{m}"[^>]*data-module="{m}"|data-module="{m}"[^>]*data-tab="{m}"', html)) == 2, m

def test_core_tabs_are_not_modules():
    html = _html()
    for t in ("portfolio", "triggers", "advice", "history", "settings"):
        assert not re.search(rf'data-tab="{t}"[^>]*data-module=', html), t

def test_settings_has_modules_group():
    html = _html()
    assert 'data-fold="settings-modules"' in html
    assert 'id="moduleToggleList"' in html
    assert html.index('data-fold="settings-modules"') < html.index('data-fold="settings-trading"')

def test_features_js_is_served():
    assert _client().get("/static/features.js").status_code == 200
```

- [ ] **Step 2: Run** `python -m pytest tests/test_web_modules_contract.py -q` → expect 5 failures.
- [ ] **Step 3: Implement** the attributes, the Settings group (summary title "Modules", help text "Extra sections. Off by default; only this browser."), the stub file, the script tag and `VERSIONED_ASSETS = ("style.css", "features.js", "app.js")`.
- [ ] **Step 4: Run** `python -m pytest tests/test_web_modules_contract.py tests/test_web_home_contract.py tests/test_web_static_caching.py -q` → all pass.
- [ ] **Step 5: Commit** `feat(web): tag module sections and add Modules settings group`.

---

### Task 2: Module registry and navigation wiring

**Files:**
- Modify: `src/web/static/features.js`
- Modify: `src/web/static/app.js` (`MORE_TABS` l.264, `switchTabUi` l.266, `switchTab` l.277, `openSettingsGroup` l.348, `DOMContentLoaded` l.118)
- Create: `tests/js/features.test.mjs`
- Modify: `tests/test_web_modules_contract.py` (one test that runs node)

**Interfaces:**
- Consumes: Task 1 markup (`data-module`, `#moduleToggleList`).
- Produces (global `window.Features`, also `module.exports` when `module` exists):
  - `MODULES: Array<{id, label, icon, description, defaultOn:false}>`
  - `resolveModules(raw: unknown) -> {[id]: boolean}` — pure; defaults for anything not a boolean, unknown ids dropped.
  - `isEnabled(id: string) -> boolean` — non-module ids return `true`.
  - `fallbackTab(tabId: string, state: {[id]: boolean}) -> string` — pure; `'portfolio'` when `tabId` is a disabled module, else `tabId`.
  - `setEnabled(id: string, on: boolean) -> void` — saves, applies, dispatches `document` event `modules:changed` with `detail = state`.
  - `applyModules() -> void` — toggles `hidden` on every `[data-module]`, renders `#moduleToggleList` switches (reuse `.set-row` + `.check-wrap.set-toggle` + `.ui-switch` markup from the Automated buying row).
- `app.js` reads `window.Features` defensively: `const F = window.Features || { isEnabled: () => true, fallbackTab: t => t };` (spec: everything visible when `features.js` fails).

- [ ] **Step 1: Write failing node tests** `tests/js/features.test.mjs` using `node:test` + `node:assert` and `createRequire` to load `src/web/static/features.js`:
  - `resolveModules(null)`, `resolveModules("x")`, `resolveModules([])` → all four `false`.
  - `resolveModules({screener: true, debate: "yes", bogus: true})` → `{screener: true, debate: false, shadow: false, memory: false}`.
  - `fallbackTab("shadow", {shadow: false, ...})` → `"portfolio"`; `fallbackTab("shadow", {shadow: true, ...})` → `"shadow"`; `fallbackTab("triggers", allOff)` → `"triggers"`; `fallbackTab("settings", allOff)` → `"settings"`.
- [ ] **Step 2: Add pytest wrapper** `test_features_js_unit_tests` in `tests/test_web_modules_contract.py`: `subprocess.run(["node", "--test", "tests/js/features.test.mjs"])`, `pytest.skip` if `shutil.which("node")` is None, assert returncode 0.
- [ ] **Step 3: Run** `node --test tests/js/features.test.mjs` → fails (stub).
- [ ] **Step 4: Implement `features.js`.** Load state once at script run (`try/catch` around `localStorage`), call `applyModules()` on `DOMContentLoaded`. File must not touch `document` at top level when `typeof document === 'undefined'` (node tests).
- [ ] **Step 5: Wire `app.js`.**
  - `switchTab(tabId)`: first line `tabId = F.fallbackTab(tabId, …)` (use `F.isEnabled`).
  - `MORE_TABS` becomes a function `moreTabs()` returning enabled modules + `'settings'`; update both call sites.
  - When `moreTabs()` is `['settings']`: the mobile More button (`#mobileMenuDropdownBtn`) calls `switchTab('settings')` instead of opening the sheet, icon `fa-solid fa-sliders`, label `Settings`.
  - Listen for `modules:changed`: re-run `switchTabUi(currentTab)`; if `currentTab` is now disabled, `switchTab('portfolio')`.
  - Per-tab loaders in `switchTab` already run only for the shown tab; no change needed beyond the fallback.
- [ ] **Step 6: Run** `node --test tests/js/features.test.mjs` and `python -m pytest tests/test_web_modules_contract.py tests/test_web_home_contract.py -q` → pass.
- [ ] **Step 7: Playwright check (not committed)** script in the scratchpad: start `python run.py` (or `uvicorn src.web.app:app --port 8765`), at 390×844: (a) default → no Screener/Debate/Shadow/Memory buttons visible, More shows Settings; (b) enable Shadow in Settings → Shadow tile appears without reload; (c) open Shadow, disable it → page shows Home; (d) `localStorage.setItem('alphaagent.modules.v1','[1]')` + reload → no console error. Report results.
- [ ] **Step 8: Commit** `feat(web): hide optional modules until switched on`.

---

### Task 3: Light and dark theme tokens

**Files:**
- Modify: `src/web/static/style.css` (`:root` l.1-45 and every literal color below it)
- Modify: `src/web/static/index.html` head (l.2-47: `class="dark"`, metas, Tailwind config, font links)
- Modify: `src/web/static/app.js` (`ALLOC_COLORS`/`ALLOC_CASH_COLOR` ~l.367, doughnut `borderColor` ~l.386, shadow chart colors ~l.2845)
- Modify: `tests/test_web_static_caching.py` only if its `--bg:` assertion breaks (it should not)

**Interfaces:**
- Produces CSS tokens used by Tasks 4-5: existing names (`--bg`, `--surface`, `--surface-2`, `--surface-inset`, `--line`, `--line-strong`, `--text`, `--text-2`, `--text-3`, `--accent`, `--accent-strong`, `--accent-soft`, `--accent-ink`, `--gain`, `--loss`, `--warn`, `--info`, soft variants, danger set) plus `--shadow-card`, and RGB triplets `--rgb-<name>` for every color Tailwind maps (`bg`, `surface`, `line`, `accent`, `slate-50`…`slate-950`).
- Produces JS helper in `app.js`: `cssVar(name: string) -> string` (trimmed `getComputedStyle(document.documentElement).getPropertyValue(name)`), and chart colors read through it at chart creation and on theme change.
- Produces: `matchMedia('(prefers-color-scheme: dark)')` listener that toggles `<html class="dark">`, updates `meta[name=theme-color]` to `cssVar('--bg')`, and re-colors existing charts.

- [ ] **Step 1: Write failing test** in `tests/test_web_modules_contract.py`:

```python
def test_theme_follows_system():
    html = _html()
    css = _client().get("/static/style.css").text
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "@media (prefers-color-scheme: dark)" in css
    assert "rgb(var(--rgb-" in html  # Tailwind colors read CSS vars
```

- [ ] **Step 2: Run** it → FAIL.
- [ ] **Step 3: Implement tokens.** `:root` = light palette (white/near-white surfaces, `--text` near-black, accent a deeper indigo that keeps 4.5:1 on white, gain/loss darkened for white). Dark block = today's values. Tailwind `slate` scale inverts in light mode (so `text-slate-100` = dark text on light). Font stack: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, "Segoe UI", sans-serif`; keep IBM Plex Mono for numbers and drop the IBM Plex Sans font link.
- [ ] **Step 4: Sweep literals.** `grep -nE "#[0-9A-Fa-f]{3,6}\b|rgba?\(" src/web/static/style.css src/web/static/app.js` and `grep -nE "text-white|bg-black|text-black" src/web/static/app.js src/web/static/index.html`; replace each with a token or a Tailwind class mapped to a token. `text-white` on accent buttons stays only where the background is the accent in both themes.
- [ ] **Step 5: Run** `python -m pytest tests/test_web_modules_contract.py tests/test_web_home_contract.py tests/test_web_static_caching.py -q` → pass.
- [ ] **Step 6: Contrast check (scratchpad script, not committed):** compute WCAG ratios for `--text`, `--text-2`, `--text-3`, `--accent`, `--gain`, `--loss`, `--warn` on `--bg` and `--surface` in both themes; every text token ≥ 4.5:1, `--text-3` allowed ≥ 3:1 only if used for non-essential meta. Paste the table in the task report.
- [ ] **Step 7: Commit** `feat(web): light and dark theme that follows the system`.

---

### Task 4: Apple-style layout

**Files:**
- Modify: `src/web/static/index.html` (header l.52-99, desktop nav l.152-163, `main` l.166, Home section l.170-306)
- Modify: `src/web/static/style.css` (`.app-header`, `.tab-btn`, `.ui-card`, `.ui-page-title`, `.ui-section-title`, bottom nav)

**Interfaces:**
- Consumes: Task 3 tokens; Task 2 `data-module` hiding (do not remove those attributes).
- Produces classes used by Task 5: `.seg-nav` (desktop segmented control container), `.seg-btn` (its buttons, replaces visual role of `.tab-btn`; keep `.tab-btn` class and `data-tab` because `switchTab` queries them), `.hero-value` (Home portfolio value).

- [ ] **Step 1: Header.** Sticky, `background: color-mix(in srgb, var(--bg) 72%, transparent)`, `backdrop-filter: saturate(180%) blur(20px)`, hairline bottom border only when scrolled (`.is-scrolled`, toggled by a passive scroll listener in `app.js`). Keep every existing id in the header.
- [ ] **Step 2: Desktop nav** as centered `.seg-nav` pill group; active `.seg-btn` = raised surface, no green text (remove `text-emerald-400`/`text-slate-400` toggling in `switchTab`, use `.active-tab` only).
- [ ] **Step 3: Home.** Portfolio value `.hero-value` ~44px desktop / 36px mobile, tight tracking, mono digits; KPI row below with no card borders; section spacing `space-y-8` on ≥ md. Status/kill-switch block visually quieter (smaller, `--text-2`), not removed.
- [ ] **Step 4: Cards and titles.** `.ui-card`/`.ui-list`: `--radius-lg`, `--shadow-card`, border `transparent` in light, `--line` hairline in dark. `.ui-page-title` 28px/600, `.ui-section-title` 13px uppercase-free, `--text-2`.
- [ ] **Step 5: Mobile bottom nav**: translucent like header, `padding-bottom: env(safe-area-inset-bottom)`, buttons min-height 44px.
- [ ] **Step 6: Run** `python -m pytest tests/test_web_home_contract.py tests/test_web_modules_contract.py tests/test_web_static_caching.py -q` → pass.
- [ ] **Step 7: Screenshot** Home at 390px and 1280px, light and dark (scratchpad Playwright, `deviceScaleFactor: 1`), inspect, fix overflow or clipping.
- [ ] **Step 8: Commit** `feat(web): calmer Apple-style header, nav and home`.

---

### Task 5: Motion

**Files:**
- Modify: `src/web/static/style.css` (motion tokens near l.40, new motion section at the end)
- Modify: `src/web/static/app.js` (`switchTab` content swap; list render helpers that should stagger: positions, triggers, advice picks)

**Interfaces:**
- Consumes: Task 4 classes (`.seg-btn`, `.ui-card`), Task 2 event `modules:changed`.
- Produces: tokens `--dur-in: 200ms`, `--dur-out: 160ms`, `--ease-out` (exists), `--ease-in-out: cubic-bezier(0.65,0,0.35,1)`, `--ease-in: cubic-bezier(0.4,0,1,1)`; utility class `.stagger` whose direct children get `--i` index and `animation-delay: calc(min(var(--i), 8) * 30ms)`; helper `staggerChildren(el: Element) -> void` in `app.js`.

- [ ] **Step 1: Tab change.** In `switchTab`, the target section gets class `tab-enter` (keyframe: opacity 0→1, `translateY(8px)`→0, `--dur-in`, `--ease-out`); remove after `animationend`. Outgoing section hides at once (no blank frame because incoming starts at opacity 0 for only 200ms).
- [ ] **Step 2: Stagger** positions list, pending triggers list, advice picks via `staggerChildren` after render; skip when the list re-renders on the 30 s refresh (only first render per tab visit).
- [ ] **Step 3: Press feedback** `button:active, .ui-btn:active, .seg-btn:active { transform: scale(.97) }` with `transition: transform 120ms var(--ease-out)`.
- [ ] **Step 4: Switch thumb** stretch: `.ui-switch::after` widens ~12% (`scaleX(1.12)`) while `:active`, transform-origin on the travel side.
- [ ] **Step 5: Folds** `details[open] > :not(summary)` animate opacity + `translateY(4px)` on open, `--dur-in`.
- [ ] **Step 6: Module on/off** nav items get `tab-enter` on show after `modules:changed`.
- [ ] **Step 7: Reduced motion** `@media (prefers-reduced-motion: reduce)`: all keyframes become opacity-only, `animation-delay: 0`, no `scale` on press.
- [ ] **Step 8: Run** contract tests (same command as Task 4 Step 6) → pass; Playwright: switch tabs 10× quickly at 390px, confirm no section stays at opacity 0 (`getComputedStyle(...).opacity === '1'` after 400ms).
- [ ] **Step 9: Commit** `feat(web): restrained motion with reduced-motion fallback`.

---

### Task 6: Visual verification and fixes

**Files:**
- Modify: only what screenshots show broken (`style.css`, `index.html`, `app.js`)
- Screenshots: scratchpad only, not committed

- [ ] **Step 1:** Start the app (`uvicorn src.web.app:app --port 8765`; lifespan side effects are acceptable in PAPER mode, else use the `TestClient`-free static route). Capture every enabled tab at 390×844 and 1280×800, `colorScheme: 'light'` and `'dark'`, modules all off and all on, plus one run with `reducedMotion: 'reduce'`. `deviceScaleFactor: 2`.
- [ ] **Step 2:** Check each shot against Global Constraints: unreadable text, dark-on-dark or light-on-light, horizontal scroll, clipped nav, touch targets < 44px, missing gain/loss signs.
- [ ] **Step 3:** Fix findings, re-shoot the affected view.
- [ ] **Step 4: Run** `python -m pytest tests/ -q` → full suite passes.
- [ ] **Step 5: Commit** `fix(web): polish found in visual pass` (skip if nothing changed). Report the list of screenshot paths.

---

### Final review

Fresh Opus 5.5 reviewer, medium effort, over `git diff origin/main...HEAD`: spec coverage, Review Focus items, contrast, no lost ids or handlers, no server-side change.
