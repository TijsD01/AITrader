import asyncio
import datetime
import logging
import time
import uuid
from typing import Dict, Any, List, Literal, Optional, Tuple
from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func

from config.settings import settings, update_env_file
from src.db.session import AsyncSessionLocal, set_kill_switch
from src.db.models import UserHolding, Position, Trade, PortfolioSnapshot, AnalysisReport, WatchlistItem, SystemConfig, MarketDigest
from src.trading.broker import active_broker as paper_broker, get_active_broker, active_broker, live_broker, check_broker_status
from src.trading import trading212
from src.trading.holdings import holdings_enabled, list_holdings
from src.trading.risk import derive_trigger_levels, entry_allocation_from_summary
from src.trading.trigger_gate import (
    TriggerGateResult, normalize_ticker, invalid_ticker_result, precheck_manual_trigger,
    get_trigger_min_score, evaluate_manual_trigger
)
from src.data.market import market_data_provider
from src.screener.scanner import market_scanner
from src.agents.committee import investment_committee
from src.agents.base import llm_client
from src.scheduler.jobs import trading_scheduler
from src.data.market_hours import get_market_statuses, check_market_hours

logger = logging.getLogger(__name__)
router = APIRouter()

class AnalyzeRequest(BaseModel):
    ticker: str

class BuyRequest(BaseModel):
    ticker: str
    allocation_eur: Optional[float] = None

class SellRequest(BaseModel):
    ticker: str
    reason: Optional[str] = "Manual Dashboard Close"

class WatchlistRequest(BaseModel):
    ticker: str
    name: Optional[str] = None
    sector: Optional[str] = None

class CreateTriggerRequest(BaseModel):
    ticker: str
    trigger_type: Optional[str] = None  # None = use the committee's trigger type
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    allocation_eur: Optional[float] = None
    ai_rationale: Optional[str] = None
    force: bool = False  # arm despite a soft block (HOLD verdict, low score); hard blocks stay blocked

class CreateTriggerFromAIRequest(BaseModel):
    ticker: str
    allocation_eur: Optional[float] = None
    force: bool = False  # arm despite a soft block (HOLD verdict, low score); hard blocks stay blocked
    report_job_id: Optional[str] = None  # reuse the committee report of this recent arm or analyze job instead of re-running it

class SettingsUpdateRequest(BaseModel):
    auto_buy_enabled: Optional[bool] = None
    score_auto_tune: Optional[bool] = None
    min_consensus_score: Optional[int] = None
    trigger_min_score: Optional[int] = None
    position_size_pct: Optional[float] = None
    max_positions: Optional[int] = None
    enforce_market_hours: Optional[bool] = None
    gemini_api_key: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    trading_mode: Optional[str] = None
    live_trading_enabled: Optional[bool] = None
    max_capital_usage_pct: Optional[float] = None
    max_deployable_capital_eur: Optional[float] = None
    clear_max_deployable_capital: Optional[bool] = None
    max_fee_drag_pct: Optional[float] = None

@router.get("/api/market-status")
async def get_market_status():
    return get_market_statuses()

@router.get("/api/broker/status")
async def get_broker_status(probe_live: bool = False, force: bool = False):
    """Returns active broker connection status, latency, and diagnostics."""
    return await check_broker_status(force=force, probe_live=probe_live)

@router.post("/api/broker/ping")
async def ping_broker(probe_live: bool = False):
    """Forces an immediate connectivity ping to the broker."""
    return await check_broker_status(force=True, probe_live=probe_live)

@router.post("/api/broker/reconnect")
async def reconnect_broker():
    """Forces an immediate disconnection and fresh reconnect to the live broker."""
    return await live_broker.reconnect()


# 1. Portfolio & Metrics
@router.get("/api/portfolio")
async def get_portfolio():
    return await active_broker.get_portfolio_summary()

@router.get("/api/portfolio/history")
async def get_portfolio_history(days: int = 30):
    """Total value over time for the Home chart: [[iso_time, value_eur], ...], at most ~200 points.

    Raw account value, so a deposit shows as a step up. The all-time return next to it excludes deposits.
    """
    days = max(1, min(days, 365))
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    live = get_active_broker() is live_broker
    q = select(PortfolioSnapshot.timestamp, PortfolioSnapshot.total_value_eur).where(
        PortfolioSnapshot.is_live == live, PortfolioSnapshot.timestamp >= since)
    if live:  # a broker switch starts a new history
        q = q.where(PortfolioSnapshot.broker == (settings.LIVE_BROKER or "").lower())
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(q.order_by(PortfolioSnapshot.timestamp))).all()
    step = max(1, len(rows) // 200)
    points = rows[::step]
    if rows and (len(rows) - 1) % step:
        points.append(rows[-1])  # always end on the latest value
    return [[ts.isoformat(timespec="minutes"), round(v, 2)] for ts, v in points]

@router.api_route("/api/portfolio/sync", methods=["GET", "POST"])
async def sync_portfolio():
    return await active_broker.sync_account_state()

@router.get("/api/positions")
async def get_positions():
    return await active_broker.get_positions()

@router.get("/api/trades")
async def get_trades(limit: int = 30):
    async with AsyncSessionLocal() as session:
        is_live = settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED
        res = await session.execute(
            select(Trade).where(Trade.is_live == is_live).order_by(desc(Trade.executed_at)).limit(limit)
        )
        trades = res.scalars().all()
        return [
            {
                "id": t.id,
                "ticker": t.ticker,
                "name": t.name,
                "order_type": t.order_type,
                "shares": round(t.shares, 4),
                "price_native": round(t.price_native, 2),
                "price_eur": round(t.price_eur, 2),
                "currency": t.currency,
                "total_eur": round(t.total_eur, 2),
                "fee_eur": round(t.fee_eur, 2) if hasattr(t, "fee_eur") and t.fee_eur is not None else 0.0,
                "realized_pnl_eur": round(t.realized_pnl_eur, 2) if t.realized_pnl_eur is not None else None,
                "realized_pnl_pct": round(t.realized_pnl_pct, 2) if t.realized_pnl_pct is not None else None,
                "consensus_score": t.consensus_score,
                "rationale": t.rationale,
                "executed_at": t.executed_at.strftime("%Y-%m-%d %H:%M") if t.executed_at else ""
            }
            for t in trades
        ]

# 2. Recommendations & Reports
@router.get("/api/recommendations")
async def get_recommendations(limit: int = 15):
    async with AsyncSessionLocal() as session:
        # One card per ticker: only the newest report for each stock
        latest_ids = select(func.max(AnalysisReport.id)).group_by(AnalysisReport.ticker)
        res = await session.execute(
            select(AnalysisReport)
            .where(AnalysisReport.id.in_(latest_ids))
            .order_by(desc(AnalysisReport.created_at), desc(AnalysisReport.id))
            .limit(limit)
        )
        reports = res.scalars().all()
        return [
            {
                "id": r.id,
                "ticker": r.ticker,
                "action": r.action,
                "consensus_score": r.consensus_score,
                "target_price": r.target_price,
                "stop_loss": r.stop_loss,
                "summary": r.summary,
                "technical_summary": r.technical_summary,
                "fundamental_summary": r.fundamental_summary,
                "sentiment_summary": r.sentiment_summary,
                "risk_summary": r.risk_summary,
                "debate_transcript": r.debate_transcript,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
            }
            for r in reports
        ]

# Committee runs take minutes, so they run as background jobs that survive the client disconnecting.
# shortcut: in-memory only, a server restart loses jobs (the client gets a 404), upgrade to a DB table if that bites.
_jobs: Dict[str, Dict[str, Any]] = {}
_JOB_TTL_SECONDS = 3600
_REPORT_REUSE_SECONDS = 900  # older verdicts are re-run before arming

async def _run_job(job: Dict[str, Any], coro) -> None:
    try:
        job["body"], job["status_code"] = await coro, 200
    except HTTPException as e:
        job["status_code"], job["body"] = e.status_code, {"detail": e.detail}
    except Exception:
        logging.getLogger(__name__).exception("Background job %s (%s) failed", job["id"], job["kind"])
        job["status_code"], job["body"] = 500, {"detail": "Internal error while running the committee."}
    job["status"], job["finished_at"] = "done", time.time()

def _start_job(kind: str, ticker: str, make_coro) -> JSONResponse:
    now = time.time()
    for jid in [j for j, v in _jobs.items() if v["finished_at"] and now - v["finished_at"] > _JOB_TTL_SECONDS]:
        del _jobs[jid]
    job = {"id": uuid.uuid4().hex, "kind": kind, "ticker": ticker, "status": "running", "status_code": None,
           "body": None, "report": None, "finished_at": None}
    _jobs[job["id"]] = job
    job["task"] = asyncio.create_task(_run_job(job, make_coro(job)))
    return JSONResponse({"job_id": job["id"]}, status_code=202)

@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (the server may have restarted).")
    return {k: job[k] for k in ("status", "kind", "ticker", "status_code", "body")}

# 3. On-Demand Analysis
@router.post("/api/analyze")
async def analyze_ticker(req: AnalyzeRequest):
    return _start_job("analyze", req.ticker.strip().upper(), lambda job: _analyze(req, job))

async def _analyze(req: AnalyzeRequest, job: Dict[str, Any]):
    ticker = req.ticker.strip().upper()
    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
    if not data:
        raise HTTPException(status_code=404, detail=f"Market data could not be found for ticker '{ticker}'.")

    summary = await paper_broker.get_portfolio_summary()
    report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])
    job["report"] = report  # lets "Arm trigger" on the debate card skip a second committee run

    await trading_scheduler._save_report(ticker, report)

    from src.trading.shadow import shadow_portfolios
    await shadow_portfolios.follow_report(report, data, followed_main=False)
    return report

# 4. Market Screener
@router.get("/api/screener")
async def get_screener_candidates(market: str = "ALL"):
    return await asyncio.to_thread(market_scanner.scan_universe, market=market, limit=12, extra_tickers=await market_scanner.load_watchlist_tickers())

# 5. Trading Actions
def _calculate_safe_allocation(
    summary: Dict[str, Any],
    requested_alloc: Optional[float],
    ticker: Optional[str] = None,
    currency: Optional[str] = None
) -> float:
    # The slot every pending trigger gets, so this matches the dashboard and the order
    return entry_allocation_from_summary(summary, ticker, currency, requested_alloc=requested_alloc)

@router.post("/api/trade/buy")
async def manual_buy(req: BuyRequest):
    ticker = req.ticker.strip().upper()

    # Fast market hours check (No trades when market is closed)
    is_open, open_msg = check_market_hours(ticker)
    if not is_open:
        raise HTTPException(status_code=400, detail=open_msg)

    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
    if not data:
        raise HTTPException(status_code=404, detail=f"Stock data not found for '{ticker}'.")
    on_broker, why = await trading212.broker_can_buy(ticker, data.get("currency"))
    if not on_broker:
        raise HTTPException(status_code=400, detail=why)

    summary = await active_broker.get_portfolio_summary()
    available_cash = summary.get("available_cash_eur", 0.0)
    if available_cash < 20.0:
        raise HTTPException(status_code=400, detail=f"Insufficient available cash (€{available_cash:.2f}) to execute buy order.")

    report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])
    alloc_eur = _calculate_safe_allocation(
        summary,
        req.allocation_eur,
        ticker=ticker,
        currency=data.get("currency")
    )

    res = await active_broker.execute_buy(
        ticker=ticker,
        name=data.get("name", ticker),
        price_native=data["price"],
        currency=data["currency"],
        stop_loss_native=report["risk"]["stop_loss_price"],
        take_profit_native=report["risk"]["take_profit_price"],
        allocation_eur=alloc_eur,
        consensus_score=report["consensus_score"],
        rationale=f"Executed via Dashboard ({report['action']})",
        is_manual=True
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@router.post("/api/trade/sell")
async def manual_sell(req: SellRequest):
    ticker = req.ticker.strip().upper()

    # Fast market hours check (No selling when market is closed)
    is_open, open_msg = check_market_hours(ticker)
    if not is_open:
        raise HTTPException(status_code=400, detail=open_msg)

    res = await active_broker.execute_sell(ticker, reason=req.reason)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

# 5b. AI Order Triggers (Limit Pullback & Breakout Buy Orders)
@router.get("/api/triggers")
async def get_triggers(status: Optional[str] = None):
    return await active_broker.get_triggers(status=status)

def _trigger_precheck_error(result: TriggerGateResult) -> HTTPException:
    """A check that failed before the committee ran: 404 for missing market data, else 400."""
    return HTTPException(status_code=404 if result.code == "NO_MARKET_DATA" else 400, detail=result.to_detail())

def _trigger_blocked_error(result: TriggerGateResult, report: Dict[str, Any]) -> HTTPException:
    """The committee's verdict does not allow this trigger: 422, with its summary so the UI can show why."""
    detail = {**result.to_detail(), "summary": str(report.get("summary") or "")[:400]}
    return HTTPException(status_code=422, detail=detail)

async def _precheck_trigger(raw_ticker: str) -> Tuple[str, Dict[str, Any], Dict[str, Any], float]:
    """
    Cheap checks that run before the expensive committee: ticker format, market data, instrument type,
    no open position or pending trigger, enough cash. Returns (ticker, data, portfolio summary, available cash).
    """
    ticker = normalize_ticker(raw_ticker)
    if not ticker:
        raise _trigger_precheck_error(invalid_ticker_result(raw_ticker))
    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
    summary = await active_broker.get_portfolio_summary() if data else {}
    available_cash = summary.get("available_cash_eur", 0.0)
    blocked = await precheck_manual_trigger(ticker, data, available_cash)
    if blocked:
        raise _trigger_precheck_error(blocked)
    return ticker, data, summary, available_cash

@router.post("/api/triggers")
async def create_trigger(req: CreateTriggerRequest):
    ticker, data, summary, available_cash = await _precheck_trigger(req.ticker)

    report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])

    target_price = req.target_price or report.get("entry_trigger_price", data["price"])
    trigger_type = req.trigger_type or report.get("trigger_type", "LIMIT_PULLBACK")
    alloc_eur = _calculate_safe_allocation(
        summary,
        req.allocation_eur,
        ticker=ticker,
        currency=data.get("currency")
    )
    sl = req.stop_loss or report["risk"]["stop_loss_price"]
    tp = req.take_profit or report["risk"]["take_profit_price"]
    rationale = req.ai_rationale or report.get("trigger_rationale", report["summary"])

    gate = await evaluate_manual_trigger(
        ticker, data, report, target_price, alloc_eur, await get_trigger_min_score(), force=req.force,
        stop_loss=sl, portfolio_value_eur=summary.get("total_value_eur", 0.0),
        free_cash_eur=summary.get("available_cash_eur", 0.0)
    )
    if not gate.allowed:
        raise _trigger_blocked_error(gate, report)

    res = await active_broker.create_price_trigger(
        ticker=ticker,
        name=data.get("name", ticker),
        currency=data["currency"],
        trigger_type=trigger_type,
        target_price=target_price,
        current_price_at_creation=data["price"],
        stop_loss=sl,
        take_profit=tp,
        allocation_eur=alloc_eur,
        consensus_score=report["consensus_score"],
        ai_rationale=rationale,
        forced=gate.code == "OK_FORCED"
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return {**res, "gate": gate.to_detail()}

@router.post("/api/triggers/from-ai")
async def create_trigger_from_ai(req: CreateTriggerFromAIRequest):
    """Convenience endpoint: Runs AI committee and arms optimal limit trigger directly (as a background job)."""
    return _start_job("arm", req.ticker.strip().upper(), lambda job: _arm_from_ai(req, job))

async def _arm_from_ai(req: CreateTriggerFromAIRequest, job: Dict[str, Any]):
    ticker, data, summary, available_cash = await _precheck_trigger(req.ticker)

    prev = _jobs.get(req.report_job_id or "")
    if (prev and prev["kind"] in ("arm", "analyze") and prev["ticker"] == job["ticker"] and prev["report"]
            and prev["finished_at"] and time.time() - prev["finished_at"] < _REPORT_REUSE_SECONDS):
        report = prev["report"]
    else:
        report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])
    job["report"] = report

    trigger_type, trigger_price, sl, tp = derive_trigger_levels(report, data["price"])
    alloc_eur = _calculate_safe_allocation(
        summary,
        req.allocation_eur,
        ticker=ticker,
        currency=data.get("currency")
    )
    rationale = report.get("trigger_rationale", report["summary"])

    gate = await evaluate_manual_trigger(
        ticker, data, report, trigger_price, alloc_eur, await get_trigger_min_score(), force=req.force,
        stop_loss=sl, portfolio_value_eur=summary.get("total_value_eur", 0.0),
        free_cash_eur=summary.get("available_cash_eur", 0.0)
    )
    if not gate.allowed:
        raise _trigger_blocked_error(gate, report)

    res = await active_broker.create_price_trigger(
        ticker=ticker,
        name=data.get("name", ticker),
        currency=data["currency"],
        trigger_type=trigger_type,
        target_price=trigger_price,
        current_price_at_creation=data["price"],
        stop_loss=sl,
        take_profit=tp,
        allocation_eur=alloc_eur,
        consensus_score=report["consensus_score"],
        ai_rationale=rationale,
        forced=gate.code == "OK_FORCED"
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    
    # Broadcast to Telegram
    from src.telegram.bot import telegram_bot
    await telegram_bot.notify_trigger_armed(res)

    return {**res, "gate": gate.to_detail()}

@router.delete("/api/triggers/{trigger_id}")
async def cancel_trigger(trigger_id: int):
    res = await active_broker.cancel_price_trigger(trigger_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

# 6. Watchlist
@router.get("/api/watchlist")
async def get_watchlist():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(WatchlistItem).where(WatchlistItem.is_active == True))
        items = res.scalars().all()
        return [
            {
                "id": w.id,
                "ticker": w.ticker,
                "name": w.name,
                "sector": w.sector,
                "country": w.country,
                "added_at": w.added_at.strftime("%Y-%m-%d") if w.added_at else ""
            }
            for w in items
        ]

@router.post("/api/watchlist")
async def add_watchlist(req: WatchlistRequest):
    ticker = req.ticker.strip().upper()
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(WatchlistItem).where(WatchlistItem.ticker == ticker))
        item = res.scalars().first()
        if item:
            item.is_active = True
        else:
            session.add(WatchlistItem(ticker=ticker, name=req.name or ticker, sector=req.sector or "General"))
        await session.commit()
    return {"success": True, "ticker": ticker}

@router.delete("/api/watchlist/{ticker}")
async def remove_watchlist(ticker: str):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(WatchlistItem).where(WatchlistItem.ticker == ticker.upper()))
        item = res.scalars().first()
        if item:
            item.is_active = False
            await session.commit()
    return {"success": True, "ticker": ticker}

# 7. System Settings & Manual Trigger
@router.get("/api/settings")
async def get_settings():
    async with AsyncSessionLocal() as session:
        cfg_res = await session.execute(select(SystemConfig))
        configs = {c.key: c.value for c in cfg_res.scalars().all()}
        raw_cap = configs.get("MAX_DEPLOYABLE_CAPITAL_EUR")
        cap_val = float(raw_cap) if raw_cap not in (None, "", "null", "None") else settings.MAX_DEPLOYABLE_CAPITAL_EUR

        return {
            "auto_buy_enabled": configs.get("AUTO_BUY_ENABLED", str(settings.AUTO_BUY_ENABLED)).lower() == "true",
            "score_auto_tune": configs.get("SCORE_AUTO_TUNE", str(settings.SCORE_AUTO_TUNE)).lower() == "true",
            "min_consensus_score": int(configs.get("MIN_CONSENSUS_SCORE", settings.MIN_CONSENSUS_SCORE)),
            "trigger_min_score": int(configs.get("TRIGGER_MIN_SCORE", settings.TRIGGER_MIN_SCORE)),
            "position_size_pct": float(configs.get("POSITION_SIZE_PCT", settings.POSITION_SIZE_PCT)),
            "max_capital_usage_pct": float(configs.get("MAX_CAPITAL_USAGE_PCT", settings.MAX_CAPITAL_USAGE_PCT)),
            "max_deployable_capital_eur": cap_val,
            "atr_stop_multiplier": float(configs.get("ATR_STOP_MULTIPLIER", 1.75)),
            "take_profit_rr": float(configs.get("TAKE_PROFIT_RR", 2.1)),
            "breakeven_trigger_pct": float(configs.get("BREAKEVEN_TRIGGER_PCT", 0.025)),
            "enforce_market_hours": configs.get("ENFORCE_MARKET_HOURS", str(settings.ENFORCE_MARKET_HOURS)).lower() == "true",
            "base_currency": settings.BASE_CURRENCY,
            "max_positions": int(configs.get("MAX_POSITIONS", settings.MAX_POSITIONS)),
            "llm_provider": settings.LLM_PROVIDER,
            "trading_mode": configs.get("TRADING_MODE", settings.TRADING_MODE),
            "live_trading_enabled": configs.get("LIVE_TRADING_ENABLED", str(settings.LIVE_TRADING_ENABLED)).lower() == "true",
            "max_fee_drag_pct": float(configs.get("MAX_FEE_DRAG_PCT", getattr(settings, "MAX_FEE_DRAG_PCT", 3.5))),
            "has_antigravity": bool(settings.LLM_PROVIDER == "antigravity" and llm_client.has_active_key()),
            "has_gemini_key": bool(settings.GEMINI_API_KEY and not settings.GEMINI_API_KEY.startswith("your_")),
            "has_telegram": bool(settings.TELEGRAM_BOT_TOKEN and not settings.TELEGRAM_BOT_TOKEN.startswith("your_"))
        }

@router.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    env_updates: Dict[str, Any] = {}
    was_live = get_active_broker() is live_broker
    async with AsyncSessionLocal() as session:
        if req.auto_buy_enabled is not None:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "AUTO_BUY_ENABLED"))
            cfg = res.scalars().first()
            val = str(req.auto_buy_enabled).lower()
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="AUTO_BUY_ENABLED", value=val))
            settings.AUTO_BUY_ENABLED = bool(req.auto_buy_enabled)
            env_updates["AUTO_BUY_ENABLED"] = bool(req.auto_buy_enabled)

        if req.score_auto_tune is not None:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "SCORE_AUTO_TUNE"))
            cfg = res.scalars().first()
            val = str(req.score_auto_tune).lower()
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="SCORE_AUTO_TUNE", value=val))
            settings.SCORE_AUTO_TUNE = bool(req.score_auto_tune)
            env_updates["SCORE_AUTO_TUNE"] = bool(req.score_auto_tune)

        if req.min_consensus_score is not None:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MIN_CONSENSUS_SCORE"))
            cfg = res.scalars().first()
            val = str(req.min_consensus_score)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="MIN_CONSENSUS_SCORE", value=val))
            settings.MIN_CONSENSUS_SCORE = int(req.min_consensus_score)
            env_updates["MIN_CONSENSUS_SCORE"] = int(req.min_consensus_score)

        if req.trigger_min_score is not None:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
            cfg = res.scalars().first()
            val = str(req.trigger_min_score)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="TRIGGER_MIN_SCORE", value=val))
            settings.TRIGGER_MIN_SCORE = int(req.trigger_min_score)
            env_updates["TRIGGER_MIN_SCORE"] = int(req.trigger_min_score)

        if req.position_size_pct is not None:
            pos_val = max(0.01, min(0.15, float(req.position_size_pct)))
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "POSITION_SIZE_PCT"))
            cfg = res.scalars().first()
            val = str(pos_val)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="POSITION_SIZE_PCT", value=val))
            settings.POSITION_SIZE_PCT = pos_val
            env_updates["POSITION_SIZE_PCT"] = pos_val

        if req.max_positions is not None:
            pos_count = max(1, min(50, int(req.max_positions)))
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MAX_POSITIONS"))
            cfg = res.scalars().first()
            val = str(pos_count)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="MAX_POSITIONS", value=val))
            settings.MAX_POSITIONS = pos_count
            env_updates["MAX_POSITIONS"] = pos_count

        if req.max_capital_usage_pct is not None:
            pct_val = max(10.0, min(100.0, float(req.max_capital_usage_pct)))
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MAX_CAPITAL_USAGE_PCT"))
            cfg = res.scalars().first()
            val = str(pct_val)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="MAX_CAPITAL_USAGE_PCT", value=val))
            settings.MAX_CAPITAL_USAGE_PCT = pct_val
            env_updates["MAX_CAPITAL_USAGE_PCT"] = pct_val

        if req.clear_max_deployable_capital:
            settings.MAX_DEPLOYABLE_CAPITAL_EUR = None
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MAX_DEPLOYABLE_CAPITAL_EUR"))
            cfg = res.scalars().first()
            if cfg:
                cfg.value = ""
            env_updates["MAX_DEPLOYABLE_CAPITAL_EUR"] = ""
        elif req.max_deployable_capital_eur is not None:
            if req.max_deployable_capital_eur <= 0:
                settings.MAX_DEPLOYABLE_CAPITAL_EUR = None
                val = ""
            else:
                settings.MAX_DEPLOYABLE_CAPITAL_EUR = float(req.max_deployable_capital_eur)
                val = str(settings.MAX_DEPLOYABLE_CAPITAL_EUR)
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MAX_DEPLOYABLE_CAPITAL_EUR"))
            cfg = res.scalars().first()
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="MAX_DEPLOYABLE_CAPITAL_EUR", value=val))
            env_updates["MAX_DEPLOYABLE_CAPITAL_EUR"] = val

        if req.enforce_market_hours is not None:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "ENFORCE_MARKET_HOURS"))
            cfg = res.scalars().first()
            val = str(req.enforce_market_hours).lower()
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="ENFORCE_MARKET_HOURS", value=val))
            settings.ENFORCE_MARKET_HOURS = bool(req.enforce_market_hours)
            env_updates["ENFORCE_MARKET_HOURS"] = bool(req.enforce_market_hours)

        if req.trading_mode is not None:
            mode_val = req.trading_mode.strip().upper()
            if mode_val in ("PAPER", "LIVE"):
                settings.TRADING_MODE = mode_val
                env_updates["TRADING_MODE"] = mode_val
                if mode_val == "LIVE" and req.live_trading_enabled is None:
                    settings.LIVE_TRADING_ENABLED = True
                    env_updates["LIVE_TRADING_ENABLED"] = True
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "TRADING_MODE"))
                cfg = res.scalars().first()
                if cfg:
                    cfg.value = mode_val
                else:
                    session.add(SystemConfig(key="TRADING_MODE", value=mode_val))

        if req.live_trading_enabled is not None:
            settings.LIVE_TRADING_ENABLED = bool(req.live_trading_enabled)
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "LIVE_TRADING_ENABLED"))
            cfg = res.scalars().first()
            val = str(req.live_trading_enabled).lower()
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="LIVE_TRADING_ENABLED", value=val))
            env_updates["LIVE_TRADING_ENABLED"] = bool(req.live_trading_enabled)

        if req.max_fee_drag_pct is not None:
            drag_val = float(req.max_fee_drag_pct)
            if 0.5 <= drag_val <= 15.0:
                settings.MAX_FEE_DRAG_PCT = drag_val
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "MAX_FEE_DRAG_PCT"))
                cfg = res.scalars().first()
                if cfg:
                    cfg.value = str(drag_val)
                else:
                    session.add(SystemConfig(key="MAX_FEE_DRAG_PCT", value=str(drag_val)))
                env_updates["MAX_FEE_DRAG_PCT"] = drag_val

        if req.gemini_api_key is not None:
            gk = req.gemini_api_key.strip()
            if gk:
                settings.GEMINI_API_KEY = gk
                env_updates["GEMINI_API_KEY"] = gk

        if req.telegram_bot_token is not None:
            tt = req.telegram_bot_token.strip()
            if tt:
                settings.TELEGRAM_BOT_TOKEN = tt
                env_updates["TELEGRAM_BOT_TOKEN"] = tt

        if req.telegram_chat_id is not None:
            tc = req.telegram_chat_id.strip()
            if tc:
                settings.TELEGRAM_CHAT_ID = tc
                env_updates["TELEGRAM_CHAT_ID"] = tc

        await session.commit()
        await active_broker.adjust_triggers_dynamically()

    if env_updates:
        update_env_file(env_updates)

    if was_live and get_active_broker() is not live_broker:
        # Nothing syncs resting T212 entry orders once live is disarmed: cancel them now
        await live_broker.cancel_resting_entries("live trading disarmed")

    return {"success": True, "message": "Settings updated and saved successfully."}

@router.post("/api/scheduler/trigger")
async def trigger_scan(market: str = "ALL"):
    """Allows triggering an immediate market session scan from the UI."""
    asyncio.create_task(trading_scheduler.run_market_session(market=market, session="MANUAL_SCAN"))
    return {"success": True, "message": f"Market scan triggered for {market} in background."}

@router.post("/api/market-data/refresh")
async def refresh_market_data():
    """Manually pulls fresh market data and runs the trigger and stop/target checks immediately."""
    return await trading_scheduler.refresh_market_data_now()

class MarketSessionRequest(BaseModel):
    market: Optional[str] = "EU"
    session: Optional[str] = "OPEN"

@router.get("/api/market-digest/latest")
async def get_latest_market_digest(market: Optional[str] = None):
    """Returns the most recent market opening/closing digest."""
    from src.data.review_tracker import review_tracker, fmt_digest_time
    next_info = review_tracker.get_next_session_info()
    async with AsyncSessionLocal() as session:
        query = select(MarketDigest).order_by(desc(MarketDigest.timestamp))
        if market:
            query = query.where(MarketDigest.market == market.upper())
        res = await session.execute(query)
        digest = res.scalars().first()
        if not digest:
            return {
                "found": False,
                "message": "No market digests recorded yet.",
                "next_session": next_info
            }
        
        return {
            "found": True,
            "id": digest.id,
            "market": digest.market,
            "session": digest.session,
            "headline": digest.headline,
            "content": digest.content,
            "top_picks": digest.top_picks or [],
            "portfolio_state": digest.portfolio_state or {},
            "review_text": digest.review_text or "",
            "timestamp": fmt_digest_time(digest.timestamp),
            "next_session": next_info,
            "trigger_min_score": await get_trigger_min_score()
        }

@router.get("/api/reviews/weekly")
async def get_weekly_reviews(days: int = 7):
    """Returns archived market reviews from the past N days."""
    from src.data.review_tracker import review_tracker
    return await review_tracker.get_weekly_reviews(days=days)

@router.get("/api/reviews/compare")
async def get_reviews_comparison(market: str = "EU", session: str = "OPEN", days: int = 7):
    """Compares past-week reviews, pick follow-through, and portfolio trajectory against live quotes."""
    from src.data.review_tracker import review_tracker
    return await review_tracker.compare_past_week_reviews(current_market=market, current_session=session, days=days)

@router.post("/api/market-session/run")
async def run_market_session_endpoint(req: MarketSessionRequest):
    """Allows manual/on-demand execution of Opening Market Review or Closing Session."""
    market = (req.market or "EU").upper()
    session = (req.session or "OPEN").upper()
    asyncio.create_task(trading_scheduler.run_market_session(market=market, session=session))
    return {"success": True, "message": f"Triggered {market} {session} market session in background."}

@router.get("/api/learning/insights")
async def get_learning_insights():
    """Returns telemetry on trade post-mortems, win/loss stats, and actively adapted parameters."""
    from src.trading.learner import trade_learner
    return await trade_learner.get_learning_insights()

@router.post("/api/learning/calibrate")
async def calibrate_learning_parameters():
    """Forces an immediate re-evaluation and calibration of internal parameters."""
    from src.trading.learner import trade_learner
    return await trade_learner.calibrate_internal_parameters()

# 8. Real Money Execution & Emergency Controls
@router.post("/api/killswitch")
async def toggle_killswitch(active: bool):
    """Emergency halt switch to immediately block or resume all automated buying."""
    await set_kill_switch(active)
    return {
        "success": True,
        "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
        "trading_mode": settings.TRADING_MODE,
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED
    }
# Deposits and withdrawals on the live account (kept out of the portfolio gain)
class CashFlowRequest(BaseModel):
    amount_eur: float  # Positive = deposit, negative = withdrawal
    note: Optional[str] = None

@router.get("/api/cash-flows")
async def get_cash_flows():
    from src.trading.cash_flows import scan_live_cash_flows, list_cash_flows, net_cash_flows_eur
    await scan_live_cash_flows()
    return {
        "net_deposits_eur": await net_cash_flows_eur(is_live=True),
        "min_detect_eur": settings.CASH_FLOW_MIN_EUR,
        "flows": await list_cash_flows(is_live=True),
    }

@router.post("/api/cash-flows")
async def add_cash_flow(req: CashFlowRequest):
    """Records a deposit or withdrawal the automatic detection missed."""
    from src.trading.cash_flows import add_cash_flow as _add
    if not req.amount_eur or abs(req.amount_eur) > 10_000_000:
        raise HTTPException(status_code=400, detail="Enter a non-zero amount.")
    return {"success": True, **(await _add(req.amount_eur, req.note))}

@router.delete("/api/cash-flows/{flow_id}")
async def remove_cash_flow(flow_id: int):
    """Removes a deposit or withdrawal, e.g. one that was detected by mistake."""
    from src.trading.cash_flows import delete_cash_flow
    if not await delete_cash_flow(flow_id):
        raise HTTPException(status_code=404, detail="No such deposit or withdrawal.")
    return {"success": True}

# 9. Shadow paper portfolios (simulated strategy variants next to the main portfolio)
@router.get("/api/shadow")
async def get_shadow_comparison():
    """Main portfolio versus every enabled shadow strategy, each measured since the shadow started."""
    from src.trading.shadow import shadow_portfolios
    main_is_live = settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED
    try:
        main_summary = await active_broker.get_portfolio_summary()
    except Exception as e:
        logger.warning(f"Shadow comparison: main portfolio summary unavailable: {e}")
        main_summary = {}
    return await shadow_portfolios.get_comparison(main_summary, main_is_live)

class ShadowCreate(BaseModel):
    label: str = Field(min_length=1)
    params: Dict[str, Any] = {}
    base: str = "baseline"
    description: str = ""

class ShadowCopy(BaseModel):
    label: str = Field(min_length=1)

class ShadowPatch(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    learning_strength: Optional[int] = None
    enabled: Optional[bool] = None

async def _shadow_call(coro):
    """Maps the shadow store's errors to HTTP: bad input is 422, an unknown strategy or change is 404."""
    try:
        return await coro
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

# Declared before any /api/shadow/{key} GET so "schema" is not read as a key
@router.get("/api/shadow/schema")
async def get_shadow_schema():
    from src.trading.strategy_params import to_json_schema
    return to_json_schema()

@router.post("/api/shadow")
async def create_shadow(body: ShadowCreate):
    from src.trading.shadow import PRESETS, create_strategy
    if body.base not in PRESETS:
        raise HTTPException(status_code=422, detail=f"base: unknown preset '{body.base}'")
    params = {**PRESETS[body.base].to_params(), **body.params}
    return {"success": True, "key": await _shadow_call(create_strategy(body.label, params, body.description))}

@router.post("/api/shadow/{strategy_key}/copy")
async def copy_shadow(strategy_key: str, body: ShadowCopy):
    from src.trading.shadow import copy_strategy
    return {"success": True, "key": await _shadow_call(copy_strategy(strategy_key, body.label))}

@router.patch("/api/shadow/{strategy_key}")
async def edit_shadow(strategy_key: str, body: ShadowPatch):
    from src.trading.shadow import edit_strategy
    changes = await _shadow_call(edit_strategy(
        strategy_key, label=body.label, description=body.description, params=body.params,
        learning_strength=body.learning_strength, enabled=body.enabled))
    return {"success": True, "changes": changes}

@router.delete("/api/shadow/{strategy_key}")
async def delete_shadow(strategy_key: str):
    from src.trading.shadow import delete_strategy
    if not await delete_strategy(strategy_key):
        raise HTTPException(status_code=404, detail=f"No shadow portfolio named '{strategy_key}'.")
    return {"success": True}

@router.get("/api/shadow/{strategy_key}/history")
async def shadow_history(strategy_key: str):
    from src.db.models import ShadowParamChange
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(ShadowParamChange).where(ShadowParamChange.strategy == strategy_key)
            .order_by(desc(ShadowParamChange.id)).limit(50))).scalars().all()
    return [{"id": r.id, "source": r.source, "changes": r.changes, "reason": r.reason,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]

@router.post("/api/shadow/{strategy_key}/history/{change_id}/undo")
async def undo_shadow_change(strategy_key: str, change_id: int = Path(ge=1, le=2**63 - 1)):
    from src.db.models import ShadowParamChange
    from src.trading.shadow import undo_param_change
    async with AsyncSessionLocal() as session:
        change = await session.get(ShadowParamChange, change_id)
    if not change or change.strategy != strategy_key:
        raise HTTPException(status_code=404, detail=f"No change {change_id} for '{strategy_key}'.")
    return {"success": True, "changes": await _shadow_call(undo_param_change(change_id))}

@router.post("/api/shadow/{strategy_key}/learn")
async def learn_shadow(strategy_key: str):
    """Runs one learning pass now. The LLM call can take minutes, so this simply awaits it."""
    from src.trading.shadow import get_strategies
    from src.trading.shadow_learner import shadow_learner
    if strategy_key not in {s.key for s in get_strategies()}:
        raise HTTPException(status_code=404, detail=f"No shadow portfolio named '{strategy_key}'.")
    return await shadow_learner.learn(strategy_key, manual=True)

@router.post("/api/shadow/{strategy_key}/reset")
async def reset_shadow(strategy_key: str):
    """Restarts one shadow portfolio from fresh simulated capital. Never touches the main portfolio."""
    from src.trading.shadow import shadow_portfolios
    if not await shadow_portfolios.reset(strategy_key.strip().lower()):
        raise HTTPException(status_code=404, detail=f"No shadow portfolio named '{strategy_key}'.")
    return {"success": True, "message": f"Shadow portfolio '{strategy_key}' was reset."}

# 10. Promote: the owner copies a shadow's rules onto the main bot (rules only; positions and triggers keep their levels)
class PromoteRequest(BaseModel):
    confirm: Any = None       # must be exactly true; checked in promote()
    confirm_text: Any = ""    # "PROMOTE LIVE" when a LIVE promote newly turns on a risky rule

async def _promote_call(coro):
    """Maps promote errors to HTTP: a refused promote is 400, an unknown shadow is 404."""
    try:
        return await coro
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.get("/api/shadow/{strategy_key}/promote/preview")
async def preview_promote(strategy_key: str):
    from src.trading.promote import preview
    return await _promote_call(preview(strategy_key))

@router.post("/api/shadow/{strategy_key}/promote")
async def promote_shadow(strategy_key: str, body: PromoteRequest):
    from src.trading.promote import promote
    return await _promote_call(promote(strategy_key, body.confirm, body.confirm_text))

@router.get("/api/main/rules")
async def get_main_rules_status():
    from src.trading.promote import status
    return await status()

@router.post("/api/main/rules/revert")
async def revert_main_rules_route():
    from src.trading.promote import revert
    if not await revert():
        raise HTTPException(status_code=400, detail="Nothing to revert: no previous main rule set is stored.")
    return {"success": True}

@router.post("/api/main/learner/resume")
async def resume_main_learner():
    from src.trading.promote import set_learner_paused
    await set_learner_paused(False)
    return {"success": True}


class OwnerRequest(BaseModel):
    owner: Literal["mine", "bot"]

@router.get("/api/holdings")
async def get_holdings():
    return await list_holdings()

@router.post("/api/holdings/{ticker}/owner")
async def set_holding_owner(ticker: str, body: OwnerRequest):
    """Flips a stock between the user (bot never trades it) and the bot."""
    if not holdings_enabled():
        raise HTTPException(status_code=409, detail="Only for live Trading 212.")
    method = live_broker.release_to_user if body.owner == "mine" else live_broker.take_from_user
    try:
        result = await method(ticker)
    except Exception:
        logger.exception("Holding owner flip failed for %s", ticker)
        raise HTTPException(status_code=409, detail="Something went wrong. Nothing was confirmed; check the Home tab.")
    if not result["success"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return {"ok": True, "message": result["message"]}


# 10. Ticker detail sheet: price history plus every set of levels the app knows for one ticker
# shortcut: trade-dot window approximates the yfinance period by days, use the first history point if dots fall off the chart edge
_DETAIL_RANGE_DAYS = {"1D": 1, "1M": 31, "3M": 92, "1Y": 366}


def _r2(v):
    return round(float(v), 2) if v is not None else None


def _level_set(id_, kind, label, state, status=None, trigger_type=None, levels=None, info=None):
    lv = {k: None for k in ("entry", "limit", "stop", "take_profit", "high", "breakeven_at")}
    lv.update({k: _r2(v) for k, v in (levels or {}).items()})
    inf = {k: None for k in ("allocation_eur", "planned_shares", "score", "rationale", "created_at", "exit_reason", "pnl_pct")}
    inf.update(info or {})
    return {"id": id_, "kind": kind, "label": label, "state": state, "status": status,
            "trigger_type": trigger_type, "levels": lv, "info": inf}


def _iso(ts):
    return ts.isoformat(timespec="minutes") if ts else None


@router.get("/api/ticker/{ticker}/detail")
async def get_ticker_detail(ticker: str, range: str = "3M"):
    """Read-only: chart history, level sets (positions, triggers, report, shadows) and trade dots for one ticker.

    Only side effect: active_broker.get_triggers() may re-adjust pending triggers, as the Triggers tab does.
    """
    from src.db.models import ShadowPosition, ShadowTrigger
    from src.trading import main_strategy
    from src.trading.levels import effective_breakeven_pct
    from src.trading.shadow import PRESETS, get_strategies

    sym = normalize_ticker(ticker)
    if not sym:
        raise HTTPException(status_code=400, detail=invalid_ticker_result(ticker).message)
    range_key = range.upper() if range and range.upper() in _DETAIL_RANGE_DAYS else "3M"
    since = datetime.datetime.utcnow() - datetime.timedelta(days=_DETAIL_RANGE_DAYS[range_key])
    live = get_active_broker() is live_broker

    history, stock = await asyncio.gather(
        asyncio.to_thread(market_data_provider.get_price_history, sym, range_key),
        asyncio.to_thread(market_data_provider.get_stock_data, sym),
        return_exceptions=True,
    )
    history = history if isinstance(history, list) else []
    stock = stock if isinstance(stock, dict) else {}

    level_sets: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    fallback_name = fallback_currency = None
    shadow_labels = {**{k: p.label for k, p in PRESETS.items()}, **{s.key: s.label for s in get_strategies()}}

    async with AsyncSessionLocal() as session:
        be_row = (await session.execute(
            select(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT"))).scalars().first()
        be_pct = float(be_row.value) if be_row else 0.025
        rules = await main_strategy.get_main_rules()
        rules_strategy = rules.as_strategy()

        positions = (await session.execute(
            select(Position).where(Position.ticker == sym, Position.status == "OPEN", Position.is_live == live)
            .order_by(desc(Position.opened_at)))).scalars().all()
        for p in positions:
            fallback_name, fallback_currency = fallback_name or p.name, fallback_currency or p.currency
            entry, stop = p.entry_price_native, p.stop_loss_native
            pos_be = be_pct
            if p.rules_version is not None and p.rules_version == rules.version:  # same rule as the brokers' ratchet
                pos_be = effective_breakeven_pct(rules_strategy, rules.breakeven_pct, entry, p.initial_stop_native)
            # breakeven_at only matters until the stop has ratcheted up to entry
            breakeven = round(entry * (1 + pos_be), 2) if entry and (stop is None or stop < entry) else None
            level_sets.append(_level_set(
                f"position:{p.id}", "position", "Open position", "live", status=p.status,
                levels={"entry": entry, "stop": stop, "take_profit": p.take_profit_native,
                        "high": p.highest_price_native, "breakeven_at": breakeven},
                info={"created_at": _iso(p.opened_at), "pnl_pct": _r2(p.unrealized_pnl_pct)}))

        report = (await session.execute(
            select(AnalysisReport).where(AnalysisReport.ticker == sym)
            .order_by(desc(AnalysisReport.created_at)).limit(1))).scalars().first()

        # shortcut: loads every shadow position for the ticker to pick OPEN + last 3 CLOSED, add a limit if a ticker gets hundreds
        shadow_positions = (await session.execute(
            select(ShadowPosition).where(ShadowPosition.ticker == sym)
            .order_by(desc(ShadowPosition.opened_at)))).scalars().all()
        shadow_triggers = (await session.execute(
            select(ShadowTrigger).where(ShadowTrigger.ticker == sym, ShadowTrigger.status == "PENDING")
            .order_by(desc(ShadowTrigger.created_at)))).scalars().all()

        main_trades = (await session.execute(
            select(Trade).where(Trade.ticker == sym, Trade.is_live == live, Trade.executed_at >= since)
            .order_by(Trade.executed_at))).scalars().all()

    triggers = [t for t in await active_broker.get_triggers() if t.get("ticker") == sym]
    past_count = 0
    for t in triggers:
        fallback_name, fallback_currency = fallback_name or t.get("name"), fallback_currency or t.get("currency")
        if t.get("status") == "PENDING":
            state = "live" if (t.get("allocation_eur") or 0) > 0 else "unfunded"
            label = "Pending trigger" if state == "live" else "Pending trigger (no budget)"
        else:
            if past_count >= 5:
                continue
            past_count += 1
            state, label = "past", f"Trigger ({str(t.get('status', '')).lower()})"
        level_sets.append(_level_set(
            f"trigger:{t['id']}", "trigger", label, state, status=t.get("status"), trigger_type=t.get("trigger_type"),
            levels={"limit": t.get("target_price"), "stop": t.get("stop_loss"), "take_profit": t.get("take_profit")},
            info={"allocation_eur": t.get("allocation_eur"), "planned_shares": t.get("planned_shares"),
                  "score": t.get("consensus_score"), "rationale": t.get("ai_rationale"),
                  "created_at": t.get("created_at")}))

    if report is not None:
        raw = report.raw_signals if isinstance(report.raw_signals, dict) else {}
        limit = raw.get("entry_trigger_price")
        stop, target = report.stop_loss, report.target_price
        if limit is not None or stop is not None or target is not None:
            level_sets.append(_level_set(
                f"report:{sym}", "report", f"Latest analysis · {report.action}", "would_be", status=report.action,
                trigger_type=raw.get("trigger_type"),
                levels={"limit": limit, "stop": stop, "take_profit": target},
                info={"score": report.consensus_score, "rationale": raw.get("trigger_rationale") or report.summary,
                      "created_at": _iso(report.created_at)}))

    closed_count = 0
    for sp in shadow_positions:
        label = f"Shadow · {shadow_labels.get(sp.strategy, sp.strategy)}"
        if sp.status == "OPEN":
            state = "simulated"
        elif closed_count < 3:
            closed_count += 1
            state = "past"
        else:
            state = None
        if state:
            level_sets.append(_level_set(
                f"shadow_position:{sp.id}", "shadow_position", label, state, status=sp.status,
                levels={"entry": sp.entry_price_native, "stop": sp.stop_loss_native,
                        "take_profit": sp.take_profit_native, "high": sp.highest_price_native},
                info={"score": sp.consensus_score, "created_at": _iso(sp.opened_at),
                      "exit_reason": sp.exit_reason, "pnl_pct": _r2(sp.realized_pnl_pct),
                      "closed_at": _iso(sp.closed_at)}))
        source = f"shadow:{sp.strategy}"
        if sp.opened_at and sp.opened_at >= since:
            trades.append({"side": "BUY", "price": _r2(sp.entry_price_native), "at": _iso(sp.opened_at), "source": source})
        if sp.closed_at and sp.closed_at >= since and sp.exit_price_native is not None:
            trades.append({"side": "SELL", "price": _r2(sp.exit_price_native), "at": _iso(sp.closed_at), "source": source})

    for st in shadow_triggers:
        level_sets.append(_level_set(
            f"shadow_trigger:{st.id}", "shadow_trigger", f"Shadow trigger · {shadow_labels.get(st.strategy, st.strategy)}",
            "would_be", status=st.status, trigger_type=st.trigger_type,
            levels={"limit": st.target_price, "stop": st.stop_loss, "take_profit": st.take_profit},
            info={"score": st.consensus_score, "rationale": st.note, "created_at": _iso(st.created_at)}))

    trades.extend({"side": t.order_type, "price": _r2(t.price_native), "at": _iso(t.executed_at), "source": "main"}
                  for t in main_trades)
    trades.sort(key=lambda t: t["at"] or "")

    currency = stock.get("currency") or fallback_currency
    current_price = stock.get("price") or (history[-1][1] if history else None)
    price_source = "yahoo"
    # Trading 212 holds the shares, so its own price is the most accurate; Yahoo stays the fallback
    client = trading212.get_client() if settings.LIVE_BROKER.lower() == "trading212" else None
    if client:
        try:
            # Only tickers the account holds (a user holding or an open live bot position): no T212 calls otherwise
            async with AsyncSessionLocal() as session:
                t212_ticker = (await session.execute(
                    select(UserHolding.t212_ticker).where(UserHolding.ticker == sym))).scalars().first()
                live_held = (await session.execute(select(Position.id).where(
                    Position.ticker == sym, Position.status == "OPEN", Position.is_live == True))).first()
            if not t212_ticker and live_held:
                t212_ticker = await client.resolve_ticker(sym, currency)
            if t212_ticker:
                # shortcut: one positions call per sheet load, cache if T212 starts returning 429
                held = {bp["instrument"]["ticker"]: bp.get("currentPrice") for bp in await client.positions()}
                t212_price = float(held.get(t212_ticker) or 0)
                if t212_price > 0:
                    current_price, price_source = t212_price, "trading212"
                    now = datetime.datetime.utcnow().isoformat(timespec="minutes")
                    if history and history[-1][0] < now:
                        history = list(history) + [[now, t212_price]]  # copy: the provider returns its cached list
        except Exception as e:
            logger.warning(f"Trading 212 price for {sym} failed, using Yahoo: {e}")
    return {
        "ticker": sym,
        "name": stock.get("name") or fallback_name or sym,
        "currency": currency,
        "current_price": _r2(current_price),
        "price_source": price_source,
        "range": range_key,
        "history": history,
        "level_sets": level_sets,
        "trades": trades,
    }
