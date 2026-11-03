# Mobile-first Home Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Home tab readable and usable at 390px without changing behaviour, API or business logic.

**Architecture:** Edit in place: `index.html` Home markup, `style.css` mobile rules, and the position-row template in `app.js` `loadPortfolio()`. A pytest pins the DOM contract `app.js` depends on; a throwaway Playwright harness takes before/after screenshots with mocked API data.

**Tech Stack:** Vanilla JS, Tailwind CDN classes, project CSS tokens in `style.css`, FastAPI `TestClient`, Node Playwright (preinstalled; `require('playwright')`).

**Spec:** `docs/superpowers/specs/2026-11-03-mobile-home-redesign-design.md`

## Global Constraints

- No framework, no build step, no new dependency.
- Do not change backend, REST API, colour tokens in `:root`, or other tabs.
- Every Home element ID keeps its name: `brokerDisconnectBanner bannerBrokerName brokerDisconnectDetail brokerConnectedStrip stripBrokerName stripBrokerEndpoint stripBrokerLatency stripBrokerAccount metricTotalVal metricReturnBadge allocationBar metricEquity metricTotalCash allocationLegend mobileAutoBuyPill mobileAutoBuyText mobileAutoBuyDot killSwitchBtn mobileMarketStatusPill mobileMarketDot mobileMarketStatusText capitalLimitBadge metricCash metricCashSub metricUnrealizedPnl metricPosCount metricUnrealizedPnlSub pendingTriggersSummary pendingTriggersSummaryText pendingTriggersSummarySub positionsTitle positionsCount positionsContainer allocationChart`.
- `#killSwitchBtn` keeps `onclick="toggleKillSwitch()"` and a child `.kill-switch-label` (read by `app.js:1473`). Kill switch and auto-buy stay one tap away on Home.
- `data-action="toggle-position"` / `data-action="close-position"` with `data-ticker` stay on position rows.
- Tap targets >= 44px on mobile; bottom nav and sticky bars pad with `env(safe-area-inset-bottom)`.
- P&L shows sign or arrow, never colour alone. `prefers-reduced-motion` honoured.
- Desktop (>= 1024px) keeps the allocation doughnut aside.

## Review Focus

1. LIVE mode, broker offline: `#brokerDisconnectBanner` visible above everything and its Reconnect button reachable. Test: Task 1 contract test asserts banner precedes status block in DOM.
2. Kill switch engaged: label and tile state still update (class/label from `app.js:1473`). Test: Task 1 asserts `.kill-switch-label` inside `#killSwitchBtn`.
3. Long ticker / long name / big EUR value (e.g. `€1,234,567.89`) at 390px: no horizontal scroll. Test: Task 2 harness asserts `scrollWidth <= innerWidth`.
4. Zero positions: empty state still renders. Test: Task 2 harness runs with an empty-positions fixture too.
5. Negative P&L: arrow `▼` and minus sign present. Test: Task 2 fixture includes a losing position; screenshot checked in Task 5.

---

### Task 1: Pin Home DOM contract (junior, Sonnet)

**Files:**
- Create: `tests/test_web_home_contract.py`

**Interfaces:**
- Produces: a guard later tasks run after every edit.

- [ ] **Step 1: Write the test.** Load `/` with `TestClient(app, client=("192.168.1.20", 5000))` (same as `tests/test_web_static_caching.py`). Assertions:
  - every ID in the Global Constraints list appears as `id="<ID>"` in the HTML;
  - `#killSwitchBtn` element contains `onclick="toggleKillSwitch()"` and a descendant with class `kill-switch-label` (regex over the button's outer HTML is fine);
  - `brokerDisconnectBanner` index < `metricTotalVal` index in the HTML text;
  - `src/web/static/app.js` text still contains `data-action="toggle-position"` and `data-action="close-position"`.
- [ ] **Step 2: Run** `python -m pytest tests/test_web_home_contract.py -q`. Expected: PASS on current code (it is a guard, not a red test).
- [ ] **Step 3: Commit** `test(web): pin Home DOM contract`.

### Task 2: Screenshot harness + baseline (junior, Sonnet)

**Files:**
- Create (scratch, not committed): `$SCRATCH/ui/dump.py`, `$SCRATCH/ui/shots.js`, fixtures `$SCRATCH/ui/portfolio.json`, `$SCRATCH/ui/portfolio_empty.json`. `$SCRATCH` = session scratchpad dir.

**Interfaces:**
- Produces: `node $SCRATCH/ui/shots.js <label> [empty]` → `$SCRATCH/ui/<label>-390.png`, `<label>-1280.png`; exits 1 and prints offenders if `document.documentElement.scrollWidth > innerWidth` or any console error.

- [ ] **Step 1:** `dump.py` writes `GET /` HTML from `TestClient` to `$SCRATCH/ui/index.html`.
- [ ] **Step 2:** `shots.js` opens `http://app.test/`, routes: `/` → dumped HTML, `/static/*` → files from `src/web/static/` (strip `?v=`), `/api/portfolio` → fixture, every other `/api/*` → `{}` 200. Allow CDN requests through. Viewports 390x844 (`isMobile`, `hasTouch`) and 1280x900. Wait for `#positionsContainer` to have no spinner.
- [ ] **Step 3:** Fixture shape: read the fields `loadPortfolio()` uses (`app.js:364-540`). Include 3 positions: one gain, one loss (`▼`), one with ticker `VERYLONGTICKER.AS` and name 60 chars; total value `1234567.89`.
- [ ] **Step 4:** Run `before` and `before-empty`. Expected: PNGs written; record any existing overflow as baseline (do not fix here).
- [ ] **Step 5:** No commit (scratch only). Report paths.

### Task 3: Status block (senior, Opus)

**Files:**
- Modify: `src/web/static/index.html:189-247` (broker strip, Controls, Market status sections)
- Modify: `src/web/static/style.css` (new `.status-block` rules near `.control-tile`)

**Interfaces:**
- Consumes: Task 1 test, Task 2 harness.
- Produces: one `<section class="status-block" aria-label="Status and controls">` placed directly above the hero, containing: market chip + cash-limit chip row, broker strip (still `hidden` by default, toggled by `app.js`), and the two control tiles.

- [ ] **Step 1:** Move the three blocks into the new section, IDs and handlers untouched. Banner stays first in the tab.
- [ ] **Step 2:** Mobile: chips row + 2-col tiles, total height ≤ 150px at 390px when broker strip hidden. Desktop: same order, no width change to the 2/3 column.
- [ ] **Step 3:** Run `python -m pytest tests/test_web_home_contract.py tests/test_web_static_caching.py -q` → PASS; `node $SCRATCH/ui/shots.js t3` → exit 0.
- [ ] **Step 4:** Commit `feat(web): merge Home status and controls into one block`.

### Task 4: Hero + compact KPI/trigger row (junior, Sonnet)

**Files:**
- Modify: `src/web/static/index.html:206-273` (hero, KPIs, trigger link)
- Modify: `src/web/static/style.css` (`.hero-value`, `.kpi`, `.trigger-link`)

- [ ] **Step 1:** Hero padding 16px mobile; `.hero-value` 36px mobile (44px ≥ 768px unchanged).
- [ ] **Step 2:** KPIs and trigger link in one compact row: on mobile a 3-col grid (Deployable cash, Open P&L, Triggers count tile linking to `switchTab('triggers')` via the existing `#pendingTriggersSummary` button). Keep `#pendingTriggersSummarySub` (may be visually hidden on mobile with `sr-only`).
- [ ] **Step 3:** Acceptance at 390x844 with broker strip hidden: hero + status block + KPI row + first position header visible without scrolling.
- [ ] **Step 4:** Tests + `shots.js t4` → PASS / exit 0. Commit `feat(web): compact Home hero and KPI row`.

### Task 5: Position cards (junior, Sonnet)

**Files:**
- Modify: `src/web/static/app.js` position template inside `loadPortfolio()` (~`app.js:502-534`)
- Modify: `src/web/static/style.css` (`.pos-row`, `.pos-head`, `.pos-mono`)

- [ ] **Step 1:** Card: ticker 16px semibold, value right, P&L line shows `signedEur(pnl)` and `pctText` (arrow kept), plus weight `% of portfolio` computed from `pos.current_value_eur / total value` already in scope of `loadPortfolio()` (use the same total `metricTotalVal` is set from). Min head height 64px.
- [ ] **Step 2:** Long ticker/name truncate; no horizontal overflow.
- [ ] **Step 3:** Tests + `shots.js t5` and `shots.js t5-empty empty` → PASS / exit 0. Commit `feat(web): larger Home position cards`.

### Task 6: Accessibility + design pass (senior, Opus)

**Files:** whatever Tasks 3-5 touched; no new files.

- [ ] **Step 1:** Use `design:accessibility-review` and `design-superpowers:design-review` on `t5-390.png` vs `before-390.png`. Check contrast of new text on `--surface`, focus rings, `aria-label`s, 44px targets, safe-area padding.
- [ ] **Step 2:** Fix findings only within Home scope. Tests + `shots.js final` → PASS / exit 0. Commit `fix(web): Home a11y polish`.

### Review gates

After each Task 3-6: fresh reviewer runs `ponytail-review` (Opus, medium) on that task's commit. After Task 6: one whole-branch `ponytail-review`. Then `superpowers:verification-before-completion`: full `python -m pytest tests/ -q`.
