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
        price = t.get("price", stock_data.get("price", 0.0))
        ema20 = t.get("ema20", price)
        ema50 = t.get("ema50", price)
        sma200 = t.get("sma200", price)
        above_ema20 = t.get("above_ema20", price >= ema20)
        above_ema50 = t.get("above_ema50", price >= ema50)
        above_sma200 = t.get("above_sma200", price >= sma200)
        rsi14 = t.get("rsi14", 50.0)
        macd = t.get("macd", 0.0)
        macd_signal = t.get("macd_signal", 0.0)
        macd_hist = t.get("macd_hist", 0.0)
        support_20d = t.get("support_20d", round(price * 0.95, 2))
        resistance_20d = t.get("resistance_20d", round(price * 1.05, 2))
        return_5d_pct = t.get("return_5d_pct", 0.0)
        return_1m_pct = t.get("return_1m_pct", 0.0)
        volume_surge_ratio = t.get("volume_surge_ratio", 1.0)

        system_prompt = (
            "You are a Senior Quantitative Technical Analyst at a top Wall Street & European hedge fund. "
            "You analyze price action, moving averages (EMA 20, 50, SMA 200), RSI-14, MACD, and volume profiles. "
            "Evaluate whether this setup is favorable for a multi-day to multi-week swing trade."
        )

        user_prompt = f"""
Ticker: {ticker}
Current Price: {price} {stock_data.get('currency', 'USD')}
Technical Indicators:
- EMA 20: {ema20}, EMA 50: {ema50}, SMA 200: {sma200}
- Price vs EMAs: Above EMA20={above_ema20}, Above EMA50={above_ema50}, Above SMA200={above_sma200}
- RSI (14): {rsi14}
- MACD Line: {macd}, Signal: {macd_signal}, Histogram: {macd_hist}
- Support (20d): {support_20d}, Resistance (20d): {resistance_20d}
- 5-Day Return: {return_5d_pct}%, 1-Month Return: {return_1m_pct}%
- Volume Surge: {volume_surge_ratio}x average

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
        if above_ema50 and above_sma200:
            score += 20
            drivers.append(f"Golden Trend: Price is trading above both 50 EMA ({ema50}) and 200 SMA ({sma200}).")
        elif above_ema20:
            score += 10
            drivers.append(f"Short-term bullish momentum above 20 EMA ({ema20}).")
        else:
            score -= 15
            drivers.append("Bearish price action trading below short-term moving averages.")

        rsi = rsi14
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

        if macd_hist > 0:
            score += 10
            drivers.append("Positive MACD histogram confirms expanding upward momentum.")

        if volume_surge_ratio >= 1.2:
            score += 5
            drivers.append(f"Institutional volume surge at {volume_surge_ratio}x average.")

        bias = "BULLISH" if score >= 65 else ("BEARISH" if score <= 40 else "NEUTRAL")
        confidence = min(95, max(10, score))

        summary = (
            f"{bias} outlook with {confidence}% confidence. "
            f"{' '.join(drivers[:2])} Immediate key resistance sits at {resistance_20d} "
            f"with tactical support at {support_20d}."
        )

        return {
            "bias": bias,
            "confidence": confidence,
            "key_drivers": drivers,
            "support_level": support_20d,
            "resistance_level": resistance_20d,
            "analysis_summary": summary
        }
