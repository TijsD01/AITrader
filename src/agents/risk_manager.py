import logging
from typing import Dict, Any
from config.settings import settings
from src.agents.base import llm_client

logger = logging.getLogger(__name__)

class RiskManagerAgent:
    name: str = "Chief Risk Officer (Capital Preservation)"

    @staticmethod
    async def evaluate_risk(
        stock_data: Dict[str, Any],
        technical_rep: Dict[str, Any],
        fundamental_rep: Dict[str, Any],
        portfolio_equity_eur: float
    ) -> Dict[str, Any]:
        ticker = stock_data["ticker"]
        price = stock_data["price"]
        t = stock_data["technicals"]
        atr = t["atr14"]
        support = technical_rep.get("support_level") or t["support_20d"]
        resistance = technical_rep.get("resistance_level") or t["resistance_20d"]

        # 1. Fetch trade learning context and dynamic calibrated parameters
        try:
            from src.trading.learner import trade_learner
            learning_context = await trade_learner.get_learning_context_for_agents()
        except Exception:
            learning_context = ""

        atr_mult = 1.75
        tp_rr_target = 2.1
        base_size_pct = settings.POSITION_SIZE_PCT
        try:
            from src.db.session import AsyncSessionLocal
            from src.db.models import SystemConfig
            from sqlalchemy import select
            async with AsyncSessionLocal() as session:
                cfg_res = await session.execute(select(SystemConfig))
                cfgs = {c.key: c.value for c in cfg_res.scalars().all()}
                atr_mult = float(cfgs.get("ATR_STOP_MULTIPLIER", 1.75))
                tp_rr_target = float(cfgs.get("TAKE_PROFIT_RR", 2.1))
                base_size_pct = float(cfgs.get("POSITION_SIZE_PCT", settings.POSITION_SIZE_PCT))
                be_pct = float(cfgs.get("BREAKEVEN_TRIGGER_PCT", 0.025))
        except Exception:
            be_pct = 0.025

        system_prompt = (
            "You are the Chief Risk Officer of an institutional hedge fund. "
            "Your sole focus is capital preservation, strict stop-loss rules, position sizing, "
            "and calculating high-conviction entry price triggers (limit pullback or breakout orders). "
            "Incorporate lessons learned from past winning and losing trades into your risk assessment. "
            f"Calculate exact stop-loss, take-profit, and AI entry trigger price levels with a minimum 1:{tp_rr_target} risk-to-reward."
        )

        user_prompt = f"""
Ticker: {ticker}
Current Price: {price}
14-Day ATR: {atr}
Key Technical Support: {support}
Key Technical Resistance: {resistance}
EMA 20: {t.get('ema20')}
Fundamental Health: {fundamental_rep.get('health')}
Beta: {stock_data['fundamentals'].get('beta', 1.0)}
Portfolio Equity: €{portfolio_equity_eur}

{learning_context}

Active Risk Parameters: Target R:R 1:{tp_rr_target}, Base Sizing {base_size_pct*100:.1f}%, ATR Buffer {atr_mult}x, Trailing Breakeven at +{be_pct*100:.1f}%.

Return JSON with exactly these keys:
{{
  "approved": boolean,
  "entry_trigger_price": float (optimal AI entry price - either pullback support or breakout resistance),
  "trigger_type": "LIMIT_PULLBACK" or "BREAKOUT_BUY",
  "trigger_rationale": string,
  "stop_loss_price": float,
  "take_profit_price": float,
  "risk_reward_ratio": float,
  "recommended_size_pct": float (decimal fraction between 0.02 and 0.12, e.g. 0.08 for 8%),
  "risk_flags": [list of strings],
  "risk_summary": string
}}
"""
        if llm_client.has_active_key():
            res = await llm_client.generate_json(system_prompt, user_prompt)
            if res and "stop_loss_price" in res and res.get("stop_loss_price") < price:
                if "entry_trigger_price" not in res:
                    res["entry_trigger_price"] = round(price * 0.985, 2)
                    res["trigger_type"] = "LIMIT_PULLBACK"
                    res["trigger_rationale"] = f"Pullback limit entry to {res['entry_trigger_price']} near key support."

                # Normalize recommended_size_pct: if > 1.0 (e.g. 8.5 for 8.5%), divide by 100
                size = float(res.get("recommended_size_pct", base_size_pct))
                if size > 1.0:
                    size = size / 100.0
                size = max(0.02, min(settings.MAX_POSITION_SIZE_PCT, size))
                res["recommended_size_pct"] = round(size, 4)

                # Economic viability & fee drag check on LLM response
                try:
                    from src.trading.fees import evaluate_trade_economic_viability, calculate_round_trip_fee
                    from src.data.currency import currency_service
                    from src.trading.risk import calculate_position_allocation

                    curr_code = stock_data.get("currency", "EUR")
                    est_alloc_eur = calculate_position_allocation(
                        available_cash=portfolio_equity_eur if portfolio_equity_eur > 0 else 100.0,
                        current_positions_count=0,
                        recommended_size_pct=size,
                        full_budget_utilization=settings.FULL_BUDGET_UTILIZATION,
                        ticker=ticker,
                        currency=curr_code
                    )
                    if est_alloc_eur <= 0:
                        est_alloc_eur = 100.0

                    price_eur = currency_service.to_eur(price, curr_code)
                    est_shares = round(est_alloc_eur / price_eur, 4) if price_eur > 0 else 1.0
                    tp_price = res.get("take_profit_price") or round(price * (1.0 + settings.DEFAULT_TAKE_PROFIT_PCT), 2)

                    rt_info = calculate_round_trip_fee(
                        ticker=ticker,
                        shares=est_shares,
                        entry_price=price,
                        exit_price=tp_price,
                        currency=curr_code
                    )
                    res["estimated_fee_eur"] = rt_info["round_trip_fee_eur"]
                    res["fee_drag_pct"] = rt_info["round_trip_drag_pct"]

                    viability = evaluate_trade_economic_viability(
                        ticker=ticker,
                        allocation_eur=est_alloc_eur,
                        price_native=price,
                        currency=curr_code,
                        stop_loss_native=res.get("stop_loss_price"),
                        take_profit_native=tp_price,
                        broker=settings.LIVE_BROKER if settings.TRADING_MODE == "LIVE" else "paper",
                        pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                        max_fee_drag_pct=getattr(settings, "MAX_FEE_DRAG_PCT", 3.8),
                        max_profit_erosion_pct=getattr(settings, "MAX_PROFIT_EROSION_PCT", 48.0)
                    )
                    if not viability["viable"]:
                        res["approved"] = False
                        if "risk_flags" not in res or not isinstance(res["risk_flags"], list):
                            res["risk_flags"] = []
                        res["risk_flags"].append(viability["reason"])
                        res["risk_summary"] = f"Risk Officer REJECTED trade: {viability['reason']}"
                except Exception as e:
                    logger.warning(f"Error checking fee viability in LLM risk path: {e}")

                return res

        # Deterministic Risk Math & AI Trigger Calculation
        # Dynamic Stop Loss using adapted ATR multiplier: Max of (Support level, Price - atr_mult * ATR, Price * (1 - DEFAULT_STOP_LOSS_PCT))
        atr_stop = price - (atr_mult * atr)
        pct_stop = price * (1.0 - settings.DEFAULT_STOP_LOSS_PCT)
        
        # Stop loss should never be closer than 2.5% (to avoid market noise) and never wider than 7%
        min_stop_price = price * 0.93
        max_stop_price = price * 0.975

        candidate_stop = max(support, atr_stop, pct_stop)
        stop_loss = max(min_stop_price, min(max_stop_price, candidate_stop))

        # AI Entry Trigger Determination
        # If stock is within 1% of 20d resistance with bullish momentum, set a breakout trigger
        ema20 = t.get("ema20", price)
        if resistance > price and (price / resistance) >= 0.99 and technical_rep.get("bias") == "BULLISH":
            trigger_type = "BREAKOUT_BUY"
            entry_trigger_price = round(resistance * 1.003, 2)
            trigger_rationale = (
                f"Breakout buy trigger set at {entry_trigger_price} (+{round(((entry_trigger_price/price)-1)*100, 1)}%) "
                f"to confirm bullish continuation above 20-day resistance ({resistance})."
            )
        else:
            trigger_type = "LIMIT_PULLBACK"
            # Optimal pullback entry: tactical support or EMA20, between 1.5% and 4% below current price
            candidate_pullback = max(support, ema20, price - (0.85 * atr))
            if candidate_pullback >= price or candidate_pullback < price * 0.94:
                entry_trigger_price = round(price * 0.982, 2)  # ~1.8% pullback
            else:
                entry_trigger_price = round(candidate_pullback, 2)
            
            trigger_dist_pct = round(((entry_trigger_price / price) - 1) * 100, 1)
            trigger_rationale = (
                f"Tactical limit pullback trigger at {entry_trigger_price} ({trigger_dist_pct}%) "
                f"anchored at key support/EMA20 ({round(candidate_pullback, 2)}) to maximize R:R."
            )
        
        risk_per_share = price - stop_loss
        risk_pct = (risk_per_share / price) * 100

        # Calibrated Risk to Reward
        reward_per_share = risk_per_share * tp_rr_target
        take_profit = round(price + reward_per_share, 2)
        rr_ratio = round(reward_per_share / risk_per_share, 2) if risk_per_share > 0 else tp_rr_target

        # Adapted Position Sizing
        size_pct = base_size_pct
        beta = stock_data["fundamentals"].get("beta") or 1.0
        if beta > 1.5:
            size_pct = max(0.05, size_pct * 0.8)  # Reduce size for high volatility stocks
        elif beta < 0.8:
            size_pct = min(0.10, size_pct * 1.1)

        risk_flags = []
        if beta > 1.4:
            risk_flags.append(f"High beta asset ({beta:.2f})")
        if risk_pct > 5.5:
            risk_flags.append(f"Wide volatility band (risk {risk_pct:.1f}%)")

        # Assess broker commission, fee drag, and economic viability
        from src.trading.fees import evaluate_trade_economic_viability, calculate_round_trip_fee
        from src.data.currency import currency_service
        from src.trading.risk import calculate_position_allocation

        curr_code = stock_data.get("currency", "EUR")
        est_alloc_eur = calculate_position_allocation(
            available_cash=portfolio_equity_eur if portfolio_equity_eur > 0 else 100.0,
            current_positions_count=0,
            recommended_size_pct=size_pct,
            full_budget_utilization=settings.FULL_BUDGET_UTILIZATION,
            ticker=ticker,
            currency=curr_code
        )
        if est_alloc_eur <= 0:
            est_alloc_eur = 100.0

        price_eur = currency_service.to_eur(price, curr_code)
        est_shares = round(est_alloc_eur / price_eur, 4) if price_eur > 0 else 1.0

        rt_info = calculate_round_trip_fee(
            ticker=ticker,
            shares=est_shares,
            entry_price=price,
            exit_price=take_profit,
            currency=curr_code
        )
        rt_fee_eur = rt_info["round_trip_fee_eur"]
        rt_drag_pct = rt_info["round_trip_drag_pct"]

        max_drag = getattr(settings, "MAX_FEE_DRAG_PCT", 3.8)
        viability = evaluate_trade_economic_viability(
            ticker=ticker,
            allocation_eur=est_alloc_eur,
            price_native=price,
            currency=curr_code,
            stop_loss_native=stop_loss,
            take_profit_native=take_profit,
            broker=settings.LIVE_BROKER if settings.TRADING_MODE == "LIVE" else "paper",
            pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
            max_fee_drag_pct=max_drag,
            max_profit_erosion_pct=getattr(settings, "MAX_PROFIT_EROSION_PCT", 48.0)
        )

        approved = True
        if not viability["viable"]:
            approved = False
            risk_flags.append(viability["reason"])
        elif rt_drag_pct > max_drag:
            approved = False
            risk_flags.append(f"Excessive IBKR fee drag ({rt_drag_pct:.1f}% on est. €{est_alloc_eur:.0f} size > {max_drag}%)")
        elif rt_drag_pct > 2.5:
            risk_flags.append(f"Elevated IBKR fee drag ({rt_drag_pct:.1f}% on est. €{est_alloc_eur:.0f} size)")

        if approved:
            summary = (
                f"Trade structure approved with {round(size_pct * 100, 1)}% allocation. "
                f"AI Trigger: {trigger_type} @ {entry_trigger_price}. "
                f"Stop-Loss anchored at {round(stop_loss, 2)} (-{risk_pct:.1f}%), "
                f"Take-Profit set at {take_profit} (+{round(((take_profit/price)-1)*100, 1)}%), "
                f"delivering a favorable {rr_ratio}:1 reward-to-risk ratio. "
                f"Est. IBKR round-trip commission: €{rt_fee_eur:.2f} ({rt_drag_pct:.1f}% drag)."
            )
        else:
            summary = (
                f"Trade structure REJECTED by Risk Officer: {viability.get('reason', 'Excessive risk/fee drag')}. "
                f"AI Trigger: {trigger_type} @ {entry_trigger_price}. "
                f"Est. IBKR round-trip commission: €{rt_fee_eur:.2f} ({rt_drag_pct:.1f}% drag on est. €{est_alloc_eur:.0f} size)."
            )

        return {
            "approved": approved,
            "entry_trigger_price": round(entry_trigger_price, 2),
            "trigger_type": trigger_type,
            "trigger_rationale": trigger_rationale,
            "stop_loss_price": round(stop_loss, 2),
            "take_profit_price": round(take_profit, 2),
            "risk_reward_ratio": rr_ratio,
            "recommended_size_pct": round(size_pct, 3),
            "estimated_fee_eur": rt_fee_eur,
            "fee_drag_pct": rt_drag_pct,
            "risk_flags": risk_flags,
            "risk_summary": summary
        }
