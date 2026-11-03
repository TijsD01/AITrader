# Mobile-first redesign: Home (phase 1)

## Goal
Make the Home tab (`#tab-portfolio`) fast to read and use on a phone (390px). Same files, vanilla JS, no framework, no build step. Other tabs follow in later phases.

## Scope
In: `src/web/static/index.html` (Home markup), `src/web/static/style.css`, minimal render markup in `app.js` for position cards.
Out: backend, REST API, `app.js` business logic, kill switch / auto-buy behaviour, other tabs, colour tokens.

## Design
1. **Status bar above hero.** Merge market-status chip, cash-limit chip, broker strip (LIVE only) and the Controls tiles into one compact block. Auto-buy and kill switch stay one tap away. Broker-offline banner stays visible and unchanged in behaviour.
2. **Hero** keeps value, return badge and allocation bar. Tighter spacing so hero plus status fit above the fold at 390x844.
3. **Positions are the main content.** Larger cards: ticker, P&L with sign and arrow (colour is never the only signal), weight. Tap opens details (existing behaviour). KPI tiles and pending-trigger link shrink to one compact row.
4. **Touch targets.** Min 44px height for buttons and bottom-nav items. Bottom nav and sticky bars respect `env(safe-area-inset-bottom)`.
5. **Tokens.** Reuse existing `:root` variables. Only spacing, type scale and card style change.

## Constraints
- Element IDs used by `app.js` stay unchanged (`metricTotalVal`, `positionsContainer`, `killSwitchBtn`, `allocationBar`, etc.).
- Kill switch and live-broker UI must not lose any state or confirmation. See `live-money-safety`.
- Desktop (>=1024px) layout keeps the allocation doughnut aside.
- `prefers-reduced-motion` honoured.

## Verification
- Playwright screenshots at 390px and 1280px, before and after.
- Check no console errors; all `getElementById` targets still exist.
- `python -m pytest tests/ -q` passes.

## Skills for build
`frontend-design`, `dataviz` (KPI/allocation), `design-review`, `accessibility-review`, `ponytail` (code), `verification-before-completion`.

## Non-goals
Framework migration, new features, API changes, reordering other tabs.
