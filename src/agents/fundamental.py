import logging
from typing import Dict, Any
from src.agents.base import llm_client, merge_llm_report

logger = logging.getLogger(__name__)

class FundamentalAnalystAgent:
    name: str = "Fundamental & Valuation Analyst"

    @staticmethod
    async def analyze(stock_data: Dict[str, Any]) -> Dict[str, Any]:
        ticker = stock_data["ticker"]
        f = stock_data["fundamentals"]
        price = stock_data["price"]

        system_prompt = (
            "You are a Senior Fundamental & Valuation Analyst at an institutional investment fund. "
            "You assess corporate earnings power, valuation multiples (P/E, PEG, P/B), revenue growth, "
            "margin profile, balance sheet leverage, and analyst consensus price targets."
        )

        user_prompt = f"""
Ticker: {ticker} ({stock_data.get('name', ticker)})
Sector: {stock_data.get('sector', 'General')}, Industry: {stock_data.get('industry', 'General')}
Current Price: {price} {stock_data.get('currency', 'USD')}
Financial Metrics:
- Market Cap: {f.get('market_cap')}
- Trailing P/E: {f.get('trailing_pe')}, Forward P/E: {f.get('forward_pe')}, PEG Ratio: {f.get('peg_ratio')}
- Price to Book: {f.get('price_to_book')}
- Profit Margins: {f.get('profit_margins')}, Operating Margins: {f.get('operating_margins')}
- Return on Equity (ROE): {f.get('return_on_equity')}
- Revenue Growth (YoY): {f.get('revenue_growth')}
- Earnings Growth (YoY): {f.get('earnings_growth')}
- Debt to Equity: {f.get('debt_to_equity')}
- Dividend Yield: {f.get('dividend_yield')}
- Wall St Mean Target Price: {f.get('target_mean_price')}

Return JSON with exactly these keys:
{{
  "valuation": "UNDERVALUED" | "FAIR" | "OVERVALUED",
  "health": "STRONG" | "MODERATE" | "VULNERABLE",
  "confidence": integer between 0 and 100,
  "key_factors": [list of strings],
  "target_fair_value": float,
  "analysis_summary": string
}}
"""
        # Deterministic Fundamental Evaluation Fallback
        score = 50
        factors = []
        
        # 1. Growth
        rev_growth = f.get("revenue_growth")
        if rev_growth and rev_growth > 0.15:
            score += 15
            factors.append(f"Accelerating revenue expansion (+{round(rev_growth * 100, 1)}% YoY).")
        elif rev_growth and rev_growth > 0.05:
            score += 10
            factors.append(f"Steady top-line revenue growth (+{round(rev_growth * 100, 1)}% YoY).")
        elif rev_growth and rev_growth < 0:
            score -= 10
            factors.append("Top-line revenue contraction recorded in recent reports.")

        # 2. Valuation
        fwd_pe = f.get("forward_pe") or f.get("trailing_pe")
        peg = f.get("peg_ratio")
        if peg and 0 < peg < 1.5:
            score += 15
            factors.append(f"Attractive PEG ratio of {peg:.2f} signaling growth at a reasonable price.")
        elif fwd_pe and fwd_pe > 50:
            score -= 10
            factors.append(f"Premium valuation with forward P/E multiple of {round(fwd_pe, 1)}x.")
        elif fwd_pe and 0 < fwd_pe < 20:
            score += 10
            factors.append(f"Modest valuation trading at {round(fwd_pe, 1)}x earnings.")

        # 3. Profitability & ROE
        roe = f.get("return_on_equity")
        if roe and roe > 0.18:
            score += 10
            factors.append(f"Superior capital efficiency with high ROE of {round(roe * 100, 1)}%.")

        # 4. Analyst upside
        mean_target = f.get("target_mean_price")
        if mean_target and mean_target > price * 1.10:
            upside = round(((mean_target / price) - 1) * 100, 1)
            score += 10
            factors.append(f"Consensus analyst target offers {upside}% upside to fair value ({mean_target}).")
        elif not mean_target:
            mean_target = round(price * 1.12, 2)

        valuation = "UNDERVALUED" if score >= 65 else ("OVERVALUED" if score <= 40 else "FAIR")
        health = "STRONG" if score >= 60 else ("VULNERABLE" if score <= 40 else "MODERATE")
        confidence = min(95, max(15, score))

        summary = (
            f"Valuation assessed as {valuation} with {health.lower()} financial profile ({confidence}% confidence). "
            f"{' '.join(factors[:2])} Fair value estimate sits around {round(float(mean_target), 2)}."
        )

        fallback = {
            "valuation": valuation,
            "health": health,
            "confidence": confidence,
            "key_factors": factors,
            "target_fair_value": round(float(mean_target), 2),
            "analysis_summary": summary
        }

        if llm_client.has_active_key():
            res = await llm_client.generate_json(system_prompt, user_prompt)
            return merge_llm_report(fallback, res, {
                "valuation": ("enum", {"UNDERVALUED", "FAIR", "OVERVALUED"}),
                "health": ("enum", {"STRONG", "MODERATE", "VULNERABLE"}),
                "confidence": ("int", 0, 100),
                "key_factors": "list",
                "target_fair_value": ("float", 1e-9, float("inf")),
                "analysis_summary": "str",
            }, "valuation")
        return fallback
