import logging
from typing import Dict, Any, Optional
from src.agents.base import llm_client
from src.agents.technical import TechnicalAnalystAgent
from src.agents.fundamental import FundamentalAnalystAgent
from src.agents.sentiment import SentimentAnalystAgent
from src.agents.risk_manager import RiskManagerAgent

logger = logging.getLogger(__name__)

class InvestmentCommittee:
    name: str = "Hedge Fund Investment Committee"

    @staticmethod
    async def evaluate_ticker(stock_data: Dict[str, Any], portfolio_equity_eur: float = 10000.0) -> Dict[str, Any]:
        ticker = stock_data["ticker"]
        price = stock_data["price"]
        curr = stock_data.get("currency", "USD")

        # 1. Fetch TencentDB Agent Memory Cold Start & trade learning context
        memory_context = ""
        try:
            from src.memory.client import agent_memory_client
            cold_start = await agent_memory_client.get_cold_start_loadout(query=ticker)
            memory_context = cold_start.full_prompt_context
        except Exception as e:
            logger.debug(f"Memory client context load note: {e}")

        try:
            from src.trading.learner import trade_learner
            learning_context = await trade_learner.get_learning_context_for_agents()
        except Exception:
            learning_context = ""

        # Fetch active weights and thresholds from SystemConfig
        tech_w, fund_w, sent_w, risk_w = 0.35, 0.30, 0.15, 0.20
        min_buy_score = 68
        try:
            from src.db.session import AsyncSessionLocal
            from src.db.models import SystemConfig
            from sqlalchemy import select
            async with AsyncSessionLocal() as session:
                cfg_res = await session.execute(select(SystemConfig))
                cfgs = {c.key: c.value for c in cfg_res.scalars().all()}
                tech_w = float(cfgs.get("WEIGHT_TECHNICAL", 0.35))
                fund_w = float(cfgs.get("WEIGHT_FUNDAMENTAL", 0.30))
                sent_w = float(cfgs.get("WEIGHT_SENTIMENT", 0.15))
                risk_w = float(cfgs.get("WEIGHT_RISK", 0.20))
                min_buy_score = int(cfgs.get("MIN_CONSENSUS_SCORE", 70))
        except Exception:
            pass

        # 2. Run Specialist Agents in parallel or sequence
        tech_rep = await TechnicalAnalystAgent.analyze(stock_data)
        fund_rep = await FundamentalAnalystAgent.analyze(stock_data)
        sent_rep = await SentimentAnalystAgent.analyze(stock_data)
        risk_rep = await RiskManagerAgent.evaluate_risk(stock_data, tech_rep, fund_rep, portfolio_equity_eur)

        # 3. Conduct LLM Committee Debate (if LLM active)
        system_prompt = (
            "You are the Lead Portfolio Manager presiding over the Investment Committee of a top hedge fund. "
            "You have received specialized reports from Technical, Fundamental, Sentiment, and Risk analysts, "
            "along with historical lessons learned from previous winning and losing trades and full memory loadout. "
            "Simulate an authentic, insightful debate between these agents discussing trade-offs, factoring in "
            "where previous trades went wrong or succeeded, and render your final verdict."
        )

        user_prompt = f"""
Ticker: {ticker} ({stock_data.get('name')}) at {price} {curr}

{memory_context}

{learning_context}

Analyst Reports:
1. Technical Analyst:
   Bias: {tech_rep.get('bias')} ({tech_rep.get('confidence')}%)
   Summary: {tech_rep.get('analysis_summary')}
   Drivers: {tech_rep.get('key_drivers')}

2. Fundamental Analyst:
   Valuation: {fund_rep.get('valuation')} ({fund_rep.get('confidence')}%)
   Health: {fund_rep.get('health')}
   Summary: {fund_rep.get('analysis_summary')}

3. Sentiment Analyst:
   Mood: {sent_rep.get('mood')} (Score: {sent_rep.get('score')})
   Summary: {sent_rep.get('sentiment_summary')}

4. Risk Officer:
   Approved: {risk_rep.get('approved')}
   Stop Loss: {risk_rep.get('stop_loss_price')} | Take Profit: {risk_rep.get('take_profit_price')}
   Size: {risk_rep.get('recommended_size_pct')*100}% | R:R: {risk_rep.get('risk_reward_ratio')}:1
   Summary: {risk_rep.get('risk_summary')}

Synthesize the debate and return JSON with:
{{
  "action": "STRONG_BUY" | "BUY" | "HOLD" | "SELL",
  "consensus_score": integer between 0 and 100,
  "verdict_summary": string,
  "debate_transcript": string formatted multi-turn debate between Technical, Fundamental, Sentiment, and Risk agents concluding with PM decision
}}
"""
        if llm_client.has_active_key():
            res = await llm_client.generate_json(system_prompt, user_prompt)
            if res and "action" in res and "consensus_score" in res:
                res_action = res["action"]
                try:
                    res_score = int(res["consensus_score"])
                except (TypeError, ValueError):
                    res_score = 0
                # Risk Officer veto: downgrade and cap the score, since triggers are armed on score alone
                if not risk_rep.get("approved", True):
                    if res_action in ("BUY", "STRONG_BUY"):
                        res_action = "HOLD"
                    res_score = min(res_score, 55)
                
                # Asynchronously log decision to TencentDB Agent Memory L0 and trigger asset extraction
                try:
                    import asyncio
                    from src.memory.client import agent_memory_client
                    import time
                    asyncio.create_task(agent_memory_client.log_interaction(
                        session_id=f"committee_{ticker}_{int(time.time())}",
                        role="assistant",
                        content=f"Deliberation on {ticker} ({res_action}, Score {res_score}/100):\n{res.get('debate_transcript', '')}\n\nVerdict: {res.get('verdict_summary', '')}",
                        metadata={"ticker": ticker, "action": res_action, "consensus_score": res_score, "price": price},
                        trigger_extraction=True
                    ))
                except Exception:
                    pass

                return {
                    "ticker": ticker,
                    "name": stock_data.get("name"),
                    "currency": curr,
                    "price": price,
                    "action": res_action,
                    "consensus_score": res_score,
                    "summary": res.get("verdict_summary", ""),
                    "debate_transcript": res.get("debate_transcript", ""),
                    "entry_trigger_price": risk_rep.get("entry_trigger_price", price),
                    "trigger_type": risk_rep.get("trigger_type", "LIMIT_PULLBACK"),
                    "trigger_rationale": risk_rep.get("trigger_rationale", ""),
                    "technical": tech_rep,
                    "fundamental": fund_rep,
                    "sentiment": sent_rep,
                    "risk": risk_rep,
                }

        # Structured Synthetic Debate Generator Fallback with Dynamic Weights
        tech_score = tech_rep["confidence"] if tech_rep["bias"] == "BULLISH" else (30 if tech_rep["bias"] == "BEARISH" else 50)
        fund_score = fund_rep["confidence"] if fund_rep["valuation"] in ("UNDERVALUED", "FAIR") else 35
        sent_score = int(50 + (sent_rep["score"] * 40))
        
        # Weighted score with dynamically adapted weights
        final_score = int((tech_score * tech_w) + (fund_score * fund_w) + (sent_score * sent_w) + (75 * risk_w))
        final_score = min(98, max(20, final_score))

        buy_threshold = max(65, min_buy_score)
        strong_buy_threshold = max(80, buy_threshold + 10)

        if final_score >= strong_buy_threshold:
            action = "STRONG_BUY"
        elif final_score >= buy_threshold:
            action = "BUY"
        elif final_score >= 45:
            action = "HOLD"
        else:
            action = "SELL"

        # Risk Officer Veto Power (e.g. excessive fee drag or unviable trade economics)
        risk_veto = not risk_rep.get("approved", True)
        if risk_veto and action in ("BUY", "STRONG_BUY"):
            action = "HOLD"
            final_score = min(final_score, 55)

        trigger_pr = risk_rep.get("entry_trigger_price", price)
        trig_type = risk_rep.get("trigger_type", "LIMIT_PULLBACK")

        risk_officer_line = (
            f"\"Risk parameters allow this setup. {risk_rep['risk_summary']}\""
            if not risk_veto
            else f"\"VETO: Setup rejected due to risk / fee constraints. {risk_rep['risk_summary']}\""
        )

        transcript = (
            f"**[Technical Analyst]**: \"We are seeing a {tech_rep['bias']} posture with {tech_rep['confidence']}% conviction. "
            f"{tech_rep['analysis_summary']}\"\n\n"
            f"**[Fundamental Analyst]**: \"From a balance sheet perspective, valuation is {fund_rep['valuation']} and solvency is {fund_rep['health']}. "
            f"{fund_rep['analysis_summary']}\"\n\n"
            f"**[Sentiment Analyst]**: \"News sentiment registers as {sent_rep['mood']}. {sent_rep['sentiment_summary']}\"\n\n"
            f"**[Risk Officer]**: {risk_officer_line}\n\n"
            f"**[Portfolio Manager]**: \"Committee consensus reached: **{action}** with score {final_score}/100. "
            f"{'Trade downgraded to HOLD due to Risk Officer veto. ' if risk_veto else ''}"
            f"AI Trigger armed: {trig_type} at {trigger_pr} with Stop-Loss at {risk_rep['stop_loss_price']} and Take-Profit at {risk_rep['take_profit_price']}.\""
        )

        summary = (
            f"Committee assigns **{action}** (Score {final_score}/100) for {ticker}. "
            f"{'Vetoed to HOLD by Risk Officer. ' if risk_veto else ''}"
            f"AI Trigger: {trig_type} @ {trigger_pr}. Technical: {tech_rep['bias']}; "
            f"Fundamental: {fund_rep['valuation']}; Sentiment: {sent_rep['mood']}. "
            f"Target R:R is {risk_rep['risk_reward_ratio']}:1."
        )

        # Asynchronously log decision to TencentDB Agent Memory L0 and trigger asset extraction
        try:
            import asyncio
            from src.memory.client import agent_memory_client
            import time
            asyncio.create_task(agent_memory_client.log_interaction(
                session_id=f"committee_{ticker}_{int(time.time())}",
                role="assistant",
                content=f"Deliberation on {ticker} ({action}, Score {final_score}/100):\n{transcript}\n\nSummary: {summary}",
                metadata={"ticker": ticker, "action": action, "consensus_score": final_score, "price": price},
                trigger_extraction=True
            ))
        except Exception:
            pass

        return {
            "ticker": ticker,
            "name": stock_data.get("name"),
            "currency": curr,
            "price": price,
            "action": action,
            "consensus_score": final_score,
            "summary": summary,
            "debate_transcript": transcript,
            "entry_trigger_price": trigger_pr,
            "trigger_type": trig_type,
            "trigger_rationale": risk_rep.get("trigger_rationale", ""),
            "technical": tech_rep,
            "fundamental": fund_rep,
            "sentiment": sent_rep,
            "risk": risk_rep,
        }

investment_committee = InvestmentCommittee()
