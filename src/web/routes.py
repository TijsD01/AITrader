import asyncio
import logging
import time
import uuid
from typing import Dict, Any, List, Optional, Tuple
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, desc, func

from config.settings import settings, update_env_file
from src.db.session import AsyncSessionLocal
from src.db.models import Position, Trade, PortfolioSnapshot, AnalysisReport, WatchlistItem, SystemConfig, MarketDigest, TradeLesson
from src.trading.broker import active_broker as paper_broker, get_active_broker, active_broker, live_broker, check_broker_status
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
    report_job_id: Optional[str] = None  # reuse the committee report of this earlier arm job instead of re-running it

class SettingsUpdateRequest(BaseModel):
    auto_buy_enabled: Optional[bool] = None
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
    ibkr_host: Optional[str] = None
    ibkr_port: Optional[int] = None
    ibkr_client_id: Optional[int] = None
    ibkr_account_id: Optional[str] = None
    ibkr_pricing_tier: Optional[str] = None
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
    return _start_job("analyze", req.ticker.strip().upper(), lambda job: _analyze(req))

async def _analyze(req: AnalyzeRequest):
    ticker = req.ticker.strip().upper()
    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
    if not data:
        raise HTTPException(status_code=404, detail=f"Market data could not be found for ticker '{ticker}'.")

    summary = await paper_broker.get_portfolio_summary()
    report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])

    # Persist report
    async with AsyncSessionLocal() as session:
        entry = AnalysisReport(
            ticker=ticker,
            action=report["action"],
            consensus_score=report["consensus_score"],
            target_price=report["risk"]["take_profit_price"],
            stop_loss=report["risk"]["stop_loss_price"],
            summary=report["summary"],
            technical_summary=report["technical"]["analysis_summary"],
            fundamental_summary=report["fundamental"]["analysis_summary"],
            sentiment_summary=report["sentiment"]["sentiment_summary"],
            risk_summary=report["risk"]["risk_summary"],
            debate_transcript=report["debate_transcript"],
            raw_signals=report
        )
        session.add(entry)
        await session.commit()

    return report

# 4. Market Screener
@router.get("/api/screener")
async def get_screener_candidates(market: str = "ALL"):
    return await asyncio.to_thread(market_scanner.scan_universe, market=market, limit=12)

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
        stop_loss=sl, portfolio_value_eur=summary.get("total_value_eur", 0.0)
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
        ai_rationale=rationale
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
    if prev and prev["kind"] == "arm" and prev["ticker"] == job["ticker"] and prev["report"]:
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
        stop_loss=sl, portfolio_value_eur=summary.get("total_value_eur", 0.0)
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
        ai_rationale=rationale
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
            "ibkr_host": configs.get("IBKR_HOST", settings.IBKR_HOST),
            "ibkr_port": int(configs.get("IBKR_PORT", settings.IBKR_PORT)),
            "ibkr_client_id": int(configs.get("IBKR_CLIENT_ID", settings.IBKR_CLIENT_ID)),
            "ibkr_account_id": configs.get("IBKR_ACCOUNT_ID", settings.IBKR_ACCOUNT_ID or ""),
            "ibkr_pricing_tier": configs.get("IBKR_PRICING_TIER", getattr(settings, "IBKR_PRICING_TIER", "tiered")),
            "max_fee_drag_pct": float(configs.get("MAX_FEE_DRAG_PCT", getattr(settings, "MAX_FEE_DRAG_PCT", 3.5))),
            "has_antigravity": bool(settings.LLM_PROVIDER == "antigravity" and llm_client.has_active_key()),
            "has_gemini_key": bool(settings.GEMINI_API_KEY and not settings.GEMINI_API_KEY.startswith("your_")),
            "has_telegram": bool(settings.TELEGRAM_BOT_TOKEN and not settings.TELEGRAM_BOT_TOKEN.startswith("your_"))
        }

@router.post("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    env_updates: Dict[str, Any] = {}
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
            pos_val = max(0.01, min(1.0, float(req.position_size_pct)))
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

        if req.ibkr_host is not None:
            h = req.ibkr_host.strip()
            if h:
                settings.IBKR_HOST = h
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "IBKR_HOST"))
                cfg = res.scalars().first()
                if cfg:
                    cfg.value = h
                else:
                    session.add(SystemConfig(key="IBKR_HOST", value=h))
                env_updates["IBKR_HOST"] = h

        if req.ibkr_port is not None:
            p = int(req.ibkr_port)
            if p > 0:
                settings.IBKR_PORT = p
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "IBKR_PORT"))
                cfg = res.scalars().first()
                val = str(p)
                if cfg:
                    cfg.value = val
                else:
                    session.add(SystemConfig(key="IBKR_PORT", value=val))
                env_updates["IBKR_PORT"] = p

        if req.ibkr_client_id is not None:
            cid = int(req.ibkr_client_id)
            settings.IBKR_CLIENT_ID = cid
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "IBKR_CLIENT_ID"))
            cfg = res.scalars().first()
            val = str(cid)
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="IBKR_CLIENT_ID", value=val))
            env_updates["IBKR_CLIENT_ID"] = cid

        if req.ibkr_account_id is not None:
            acc = req.ibkr_account_id.strip()
            settings.IBKR_ACCOUNT_ID = acc or None
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "IBKR_ACCOUNT_ID"))
            cfg = res.scalars().first()
            val = acc or ""
            if cfg:
                cfg.value = val
            else:
                session.add(SystemConfig(key="IBKR_ACCOUNT_ID", value=val))
            env_updates["IBKR_ACCOUNT_ID"] = val

        if req.ibkr_pricing_tier is not None:
            tier_val = req.ibkr_pricing_tier.strip().lower()
            if tier_val in ("tiered", "fixed"):
                settings.IBKR_PRICING_TIER = tier_val
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "IBKR_PRICING_TIER"))
                cfg = res.scalars().first()
                if cfg:
                    cfg.value = tier_val
                else:
                    session.add(SystemConfig(key="IBKR_PRICING_TIER", value=tier_val))
                env_updates["IBKR_PRICING_TIER"] = tier_val

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
    from src.data.review_tracker import review_tracker
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
            "timestamp": digest.timestamp.strftime("%Y-%m-%d %H:%M CET") if digest.timestamp else "",
            "next_session": next_info
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

@router.get("/api/reviews/next-session")
async def get_next_scheduled_session():
    """Returns the upcoming chronological market session schedule."""
    from src.data.review_tracker import review_tracker
    return review_tracker.get_next_session_info()

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
    settings.KILL_SWITCH_ACTIVE = active
    return {
        "success": True,
        "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
        "trading_mode": settings.TRADING_MODE,
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED
    }

@router.post("/api/sync")
async def sync_account():
    """Syncs live positions and settled cash balance with the broker."""
    broker = get_active_broker()
    summary = await broker.sync_account_state()
    return {"success": True, "portfolio": summary}



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

@router.post("/api/shadow/{strategy_key}/reset")
async def reset_shadow(strategy_key: str):
    """Restarts one shadow portfolio from fresh simulated capital. Never touches the main portfolio."""
    from src.trading.shadow import shadow_portfolios
    if not await shadow_portfolios.reset(strategy_key.strip().lower()):
        raise HTTPException(status_code=404, detail=f"No shadow portfolio named '{strategy_key}'.")
    return {"success": True, "message": f"Shadow portfolio '{strategy_key}' was reset."}
