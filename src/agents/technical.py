import logging
from typing import Dict, Any
from src.agents.base import llm_client

logger = logging.getLogger(__name__)

class TechnicalAnalystAgent:
    name: str = "Technical Analyst (Quant & Chart Patterns)"

    @staticmethod
    async def analyze(stock_data: Dict[str, Any]) -> Dict[str, Any]:
        ticker = stock_data["ticker"]
        t = stock_data["technicals"]
        price = t["price"]

        system_prompt = (
            "You are a Senior Quantitative Technical Analyst at a top Wall Street & European hedge fund. "
            "You analyze price action, moving averages (EMA 20, 50, SMA 200), RSI-14, MACD, and volume profiles. "
            "Evaluate whether this setup is favorable for a multi-day to multi-week swing trade."
        )

        user_prompt = f"""
Ticker: {ticker}
Current Price: {price} {stock_data.get('currency', 'USD')}
Technical Indicators:
- EMA 20: {t['ema20']}, EMA 50: {t['ema50']}, SMA 200: {t['sma200']}
- Price vs EMAs: Above EMA20={t['above_ema20']}, Above EMA50={t['above_ema50']}, Above SMA200={t['above_sma200']}
- RSI (14): {t['rsi14']}
- MACD Line: {t['macd']}, Signal: {t['macd_signal']}, Histogram: {t['macd_hist']}
- Support (20d): {t['support_20d']}, Resistance (20d): {t['resistance_20d']}
- 5-Day Return: {t['return_5d_pct']}%, 1-Month Return: {t['return_1m_pct']}%
- Volume Surge: {t['volume_surge_ratio']}x average

Return JSON with exactly these keys:
{{
  "bias": "BULLISH" | "NEUTRAL" | "BEARISH",
  "confidence": integer between 0 and 100,
  "key_drivers": [list of strings],
  "support_level": float,
  "resistance_level": float,
  "analysis_summary": string
}}
"""
        if llm_client.has_active_key():
            res = await llm_client.generate_json(system_prompt, user_prompt)
            if res and "bias" in res:
                return res

        # High-precision Quantitative Fallback
        score = 50
        drivers = []
        if t["above_ema50"] and t["above_sma200"]:
            score += 20
            drivers.append(f"Golden Trend: Price is trading above both 50 EMA ({t['ema50']}) and 200 SMA ({t['sma200']}).")
        elif t["above_ema20"]:
            score += 10
            drivers.append(f"Short-term bullish momentum above 20 EMA ({t['ema20']}).")
        else:
            score -= 15
            drivers.append("Bearish price action trading below short-term moving averages.")

        rsi = t["rsi14"]
        if 48 <= rsi <= 68:
            score += 15
            drivers.append(f"Healthy bullish momentum on RSI-14 ({rsi}).")
        elif 35 <= rsi < 48:
            score += 10
            drivers.append(f"Constructive pullback / consolidation zone (RSI {rsi}).")
        elif rsi > 70:
            score -= 5
            drivers.append(f"Approaching short-term overbought conditions (RSI {rsi}).")
        else:
            score -= 10
            drivers.append(f"Weak momentum with depressed RSI ({rsi}).")

        if t["macd_hist"] > 0:
            score += 10
            drivers.append("Positive MACD histogram confirms expanding upward momentum.")

        if t["volume_surge_ratio"] >= 1.2:
            score += 5
            drivers.append(f"Institutional volume surge at {t['volume_surge_ratio']}x average.")

        bias = "BULLISH" if score >= 65 else ("BEARISH" if score <= 40 else "NEUTRAL")
        confidence = min(95, max(10, score))

        summary = (
            f"{bias} outlook with {confidence}% confidence. "
            f"{' '.join(drivers[:2])} Immediate key resistance sits at {t['resistance_20d']} "
            f"with tactical support at {t['support_20d']}."
        )

        return {
            "bias": bias,
            "confidence": confidence,
            "key_drivers": drivers,
            "support_level": t["support_20d"],
            "resistance_level": t["resistance_20d"],
            "analysis_summary": summary
        }
