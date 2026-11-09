# Ticker detail sheet: design

Date: 2026-11-09
Status: draft, awaiting review

## Goal

Every ticker shown in the dashboard can be tapped. The tap opens a full-screen sheet with a price chart and the trade levels for that ticker drawn on it: trigger limit, stop loss (including the ratcheted trailing stop), take profit, and entry/exit points. Levels that are not live (not armed, unfunded, simulated or historical) still show, as "would be here" lines.

Success: from any list, one tap shows where the price is relative to every level the bot holds or would use for that ticker, on a phone.

## What the user asked for (verbatim intent)

- Tickers in Triggers, Shadow "and such" become tappable.
- The tap opens a view with a chart and info about the trigger, trailing stop and take profit.
- The levels are drawn in the chart.
- For triggers that are not armed, the lines show where the trigger would be.

Answers from brainstorming:
- "Not armed" covers: advice/screener picks (latest report), unfunded triggers, shadow pending entries, past triggers (cancelled, expired, triggered).
- Opens as a full-screen sheet over the current tab. Closing returns to the same list and scroll position. No new nav button.
- Line chart of closes, range switch 1D / 1M / 3M / 1Y, default 3M.
- Approach A: one server endpoint aggregates history and levels.

## Assumptions (not stated by the user)

- Taps are added in: Triggers (pending cards, history table), Shadow (open positions, pending entries, recently closed), Home holdings/positions, Advice cards, Screener cards, trade History table.
- Existing buttons in those cards (Cancel, Arm, Analyze, Buy, Close, owner switch, expand toggles) keep their current behavior. Only the ticker text becomes the tap target, so no existing action is shadowed.
- Prices and levels are shown in the ticker's native currency, as the lists already do.
- Positions and triggers are filtered to the active broker mode (`is_live` matches whether `active_broker` is the live broker), the same way `/api/portfolio/history` does it.

## Backend

### `MarketDataProvider.get_price_history(ticker, range)` in `src/data/market.py`

Returns `[[iso_time, close], ...]` or `None` on failure.

| range | yfinance call | points |
|---|---|---|
| 1D | `history(period="1d", interval="5m", prepost=True)` | intraday |
| 1M | `history(period="1mo", interval="1d")` | daily |
| 3M | `history(period="3mo", interval="1d")` | daily |
| 1Y | `history(period="1y", interval="1d")` | daily |

Cached in a module dict keyed by `(ticker, range)`, reusing the existing `_CACHE_TTL_OPEN` / `_CACHE_TTL_CLOSED` TTLs. Unknown range falls back to 3M. Runs via `asyncio.to_thread` from the route.

### `GET /api/ticker/{ticker}/detail?range=3M` in `src/web/routes.py`

Ticker is validated with `trigger_gate.normalize_ticker`; invalid → 400.

Response:

```json
{
  "ticker": "ASML.AS",
  "name": "ASML Holding",
  "currency": "EUR",
  "current_price": 712.4,
  "range": "3M",
  "history": [["2026-08-10T00:00", 690.1], ...],
  "level_sets": [ ... ],
  "trades": [{"side": "BUY", "price": 701.0, "at": "2026-10-02T14:31", "source": "main"}]
}
```

`history` is `[]` when price data fails; the rest still returns.

Each entry in `level_sets` is one thing that owns levels:

```json
{
  "id": "trigger:42",
  "kind": "position | trigger | report | shadow_position | shadow_trigger",
  "label": "Pending trigger",
  "state": "live | unfunded | would_be | past | simulated",
  "status": "PENDING",
  "trigger_type": "LIMIT_PULLBACK",
  "levels": {
    "entry": 701.0,
    "limit": 698.5,
    "stop": 672.0,
    "take_profit": 760.0,
    "high": 730.2,
    "breakeven_at": 718.5
  },
  "info": {"allocation_eur": 450.0, "planned_shares": 0.64, "score": 78, "rationale": "...", "created_at": "...", "exit_reason": null, "pnl_pct": null}
}
```

Missing levels are `null`. Sources, newest first within each kind:

| kind | source | state | levels |
|---|---|---|---|
| position | open `Position` for ticker (active mode) | live | entry, stop (current, already ratcheted), take_profit, high, breakeven_at |
| trigger | PENDING trigger from `active_broker.get_triggers()` | live, or unfunded when `allocation_eur == 0` | limit, stop, take_profit |
| trigger | non-PENDING trigger from the same call, last 5 | past | limit, stop, take_profit |
| report | latest `AnalysisReport.raw_signals` | would_be | limit (`entry_trigger_price`), stop, take_profit (`target_price`) |
| shadow_position | `ShadowPosition` OPEN, plus last 3 CLOSED | simulated / past | entry, stop, take_profit, high |
| shadow_trigger | `ShadowTrigger` PENDING | would_be | limit, stop, take_profit |

`breakeven_at = entry * (1 + BREAKEVEN_TRIGGER_PCT)` from `SystemConfig` (default 0.025), only while the stop is still below entry. It tells the user the price at which the stop ratchets to breakeven + 1%. Past that point the stop shown is the trailing stop itself.

Main-bot triggers come from `active_broker.get_triggers()` filtered to the ticker, the same call `/api/triggers` makes. That gives allocation, planned shares and the live/paper split for free; unfunded means `allocation_eur == 0`. That call can re-adjust pending triggers (`adjust_triggers_dynamically`), exactly as opening the Triggers tab already does today; the endpoint adds no other writes.

`trades` lists main-bot `Trade` rows (active mode) and shadow entries/exits for the ticker inside the chart range, for entry/exit dots.

No orders, no LLM calls. The only side effect is the trigger re-adjustment noted above.

## Frontend

### Tap targets

A shared helper renders the ticker as a button:

```js
tickerLink(ticker, source)  // <button type="button" data-action="open-ticker" data-ticker=".." data-source="..">
```

`source` is the `level_sets[].id` the user tapped (e.g. `trigger:42`, `shadow_position:17`, `report:ASML.AS`), or empty. The existing `document` click delegation in `app.js` handles `open-ticker`. The button keeps the current ticker typography; it adds only a focus ring and a 44px min hit area.

### Sheet

One `<dialog id="tickerSheet">` in `index.html`, opened with `showModal()` so focus trapping, Escape and backdrop work natively.

Layout, top to bottom:
1. Header: ticker, name, current price, close button.
2. Range switch: 1D / 1M / 3M / 1Y segmented control (reuses `seg-btn` styling).
3. Chart (Chart.js, already loaded): close line, horizontal level lines, entry/exit dots.
4. Level set picker: chips for each `level_set` (e.g. "Position", "Trigger #42", "Advice", "Shadow · tight stop"). The tapped `source` is preselected; others can be toggled on. Default when no source: the position if any, else the pending trigger, else the report.
5. Info panel for the selected set: status badge, each level with its distance from the current price in %, allocation and shares, score, rationale, armed/closed time, exit reason and P&L for past ones.

Escape, the close button, and the browser back gesture close it. Opening pushes a history state so Android/iOS back closes the sheet instead of leaving the page.

### Chart lines

Drawn with a small inline Chart.js plugin (no new dependency): horizontal lines across the plot, each with a short right-edge label.

| level | colour token | style |
|---|---|---|
| limit | `--accent` | solid if live, dashed otherwise |
| stop | `--loss` | solid if live, dashed otherwise |
| take_profit | `--gain` | solid if live, dashed otherwise |
| entry | `--text` muted | solid |
| high | `--faint` | dotted |
| breakeven_at | `--faint` | dotted |

Y-axis range expands to include every visible level, so a level far from the current price stays on screen. A legend below the chart explains solid vs dashed ("live" vs "would be"). Prices use `tabular-nums`.

### Motion

- Sheet enters from the bottom: `translateY(100%) → 0`, 220 ms ease-out. Exits in 170 ms ease-in.
- Range switch crossfades the line (Chart.js update animation, 200 ms). No stagger, no bounce.
- `prefers-reduced-motion`: opacity fade only, chart updates without animation.

### States

- Loading: chart-height skeleton block plus disabled range switch.
- No price data: "No price data for this range" in the chart area; levels and info still render.
- No level sets: chart only, and the line "No trigger, position or advice for this ticker yet."
- Fetch error: inline error in the sheet with a Retry button. No toast.

## Out of scope

- Editing levels or cancelling from the sheet (existing buttons stay where they are).
- Candlesticks, indicators, volume.
- Live-updating price inside the open sheet.
- A shareable URL per ticker.

## Testing

Backend, new `tests/test_ticker_detail.py` (yfinance mocked, no network, no LLM):
- Open position yields a `position` set with entry, stop, take_profit, high, and `breakeven_at` while the stop is below entry.
- Pending trigger with allocation → `live`; with zero allocation → `unfunded`.
- Cancelled trigger → `past`.
- Latest report → `report` set with `limit` from `raw_signals.entry_trigger_price`.
- Shadow pending trigger and open shadow position appear.
- Positions from the other mode (`is_live` mismatch) are excluded.
- Price history failure → `history == []`, still 200.
- Invalid ticker → 400.

Frontend contract, extend `tests/test_web_modules_contract.py`: `index.html` has `#tickerSheet`; `app.js` handles `open-ticker` and calls `/api/ticker/`.

Manual check: open the sheet from each list on a phone-width viewport (Playwright screenshot at 390px), with reduced motion on and off.

## Files touched

- `src/data/market.py`: `get_price_history`.
- `src/web/routes.py`: `/api/ticker/{ticker}/detail`.
- `src/web/static/index.html`: `<dialog id="tickerSheet">`.
- `src/web/static/app.js`: `tickerLink`, `open-ticker` handler, sheet render, chart plugin; ticker cells in the six lists use `tickerLink`.
- `src/web/static/style.css`: sheet, level legend, motion and reduced-motion rules.
- `tests/test_ticker_detail.py`, `tests/test_web_modules_contract.py`.

No DB schema change, so no migration.
