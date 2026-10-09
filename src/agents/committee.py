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

        # 1. Fetch trade learning context and dynamic calibrated parameters
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
            "along with historical lessons learned from previous winning and losing trades. "
            "Simulate an authentic, insightful debate between these agents discussing trade-offs, factoring in "
            "where previous trades went wrong or succeeded, and render your final verdict."
        )

        user_prompt = f"""
Ticker: {ticker} ({stock_data.get('name')}) at {price} {curr}

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
                return {
                    "ticker": ticker,
                    "name": stock_data.get("name"),
                    "currency": curr,
                    "price": price,
                    "action": res["action"],
                    "consensus_score": int(res["consensus_score"]),
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

        trigger_pr = risk_rep.get("entry_trigger_price", price)
        trig_type = risk_rep.get("trigger_type", "LIMIT_PULLBACK")

        transcript = (
            f"**[Technical Analyst]**: \"We are seeing a {tech_rep['bias']} posture with {tech_rep['confidence']}% conviction. "
            f"{tech_rep['analysis_summary']}\"\n\n"
            f"**[Fundamental Analyst]**: \"From a balance sheet perspective, valuation is {fund_rep['valuation']} and solvency is {fund_rep['health']}. "
            f"{fund_rep['analysis_summary']}\"\n\n"
            f"**[Sentiment Analyst]**: \"News sentiment registers as {sent_rep['mood']}. {sent_rep['sentiment_summary']}\"\n\n"
            f"**[Risk Officer]**: \"Risk parameters allow this setup. {risk_rep['risk_summary']}\"\n\n"
            f"**[Portfolio Manager]**: \"Committee consensus reached: **{action}** with score {final_score}/100. "
            f"AI Trigger armed: {trig_type} at {trigger_pr} with Stop-Loss at {risk_rep['stop_loss_price']} and Take-Profit at {risk_rep['take_profit_price']}.\""
        )

        summary = (
            f"Committee assigns **{action}** (Score {final_score}/100) for {ticker}. "
            f"AI Trigger: {trig_type} @ {trigger_pr}. Technical: {tech_rep['bias']}; "
            f"Fundamental: {fund_rep['valuation']}; Sentiment: {sent_rep['mood']}. "
            f"Target R:R is {risk_rep['risk_reward_ratio']}:1."
        )

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
