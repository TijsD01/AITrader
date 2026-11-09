# Ticker detail sheet: implementation progress

Spec: `docs/superpowers/specs/2026-11-09-ticker-detail-sheet-design.md` (approved 2026-11-09).
Branch: `claude/cool-dirac-8fw46c`. Execution: orchestration over sub-agents (Opus medium for hard units, Sonnet for easy ones), per user.

Baseline before work: `python -m pytest tests/ -q` → 693 passed, 1 failed (`tests/test_antigravity_agent.py::test_antigravity_llm_client`, pre-existing, unrelated).

## Units

| # | Unit | Model | Files | Status |
|---|---|---|---|---|
| 1 | Backend: `get_price_history` + `/api/ticker/{ticker}/detail` + tests | opus, medium | `src/data/market.py`, `src/web/routes.py`, `tests/test_ticker_detail.py` | dispatched |
| 2 | Frontend sheet: dialog, chart + level plugin, info panel, motion, `tickerLink` + `open-ticker` handler | opus, medium | `src/web/static/index.html`, `src/web/static/app.js` (new code only), `src/web/static/style.css`, `tests/test_web_modules_contract.py` | dispatched |
| 3 | Wire `tickerLink` into the six lists | sonnet | `src/web/static/app.js` (list renderers only) | waits on 2 |

## Decisions made during implementation

- No `--faint` CSS token exists; use `--text-3` (the `.ui-faint` colour) for high/breakeven lines.
- Holdings and bot positions render the ticker inside the row's expand `<button class="pos-head">`. Nested buttons are invalid HTML, so those rows get a "Chart" button inside the expanded `.pos-detail` instead of a tappable ticker.
- Shadow summary payload (`src/trading/shadow.py` ~line 817) has no row ids; unit 3 adds `"id"` to open_positions, pending_triggers and recent_closed so `source` can preselect the tapped shadow item.
- Unit 3 site list (app.js, verified by grep at commit a66e076): advice cards ~940, debate report header ~1152, trades table ~1329, screener ~1005, pending trigger cards ~2099, trigger history ~2146, shadow lists ~3002/3011/3020, holdings ~632 and positions ~879 (Chart button). Review/learning lists (~2416, 2537, 2552, 2714) are optional extras.
