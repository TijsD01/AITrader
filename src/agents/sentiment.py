import asyncio
import re
import logging
from typing import Dict, Any, List
from src.agents.base import llm_client, merge_llm_report
from src.data.news import news_service

logger = logging.getLogger(__name__)

class SentimentAnalystAgent:
    name: str = "Market Sentiment & News Analyst"

    @staticmethod
    async def analyze(stock_data: Dict[str, Any]) -> Dict[str, Any]:
        ticker = stock_data["ticker"]
        try:
            news_items = await asyncio.wait_for(
                asyncio.to_thread(news_service.get_ticker_news, ticker, limit=5),
                timeout=10.0
            )
        except Exception as e:
            logger.warning(f"Error or timeout fetching news for {ticker}: {e}")
            news_items = []
        headlines_text = "\n".join([f"- [{item.get('time')}] {item.get('title')} ({item.get('publisher')})" for item in news_items])
        if not headlines_text:
            headlines_text = "No major breaking negative headlines detected in current cycle."

        system_prompt = (
            "You are a Sentiment & News Catalyst Analyst for an algorithmic hedge fund. "
            "Evaluate recent headlines, media buzz, earnings sentiment, and upcoming corporate catalysts."
        )

        user_prompt = f"""
Ticker: {ticker} ({stock_data.get('name', ticker)})
Sector: {stock_data.get('sector')}
Recent Headlines:
{headlines_text}

Return JSON with exactly these keys:
{{
  "mood": "POSITIVE" | "NEUTRAL" | "NEGATIVE",
  "score": float between -1.0 and 1.0,
  "confidence": integer between 0 and 100,
  "catalysts": [list of strings],
  "sentiment_summary": string
}}
"""
        # Fallback heuristic news sentiment
        pos_words = ["surge", "jump", "record", "profit", "beat", "higher", "growth", "buy", "target", "upgrade", "lead", "rally", "strong"]
        neg_words = ["fall", "drop", "miss", "loss", "plunge", "decline", "probe", "lawsuit", "cut", "downgrade", "warning", "risk", "debt"]

        score = 0.0
        catalysts = []

        for item in news_items:
            title_words = set(re.findall(r"[a-z0-9']+", (item.get("title") or "").lower()))
            pos_hits = sum(1 for w in pos_words if w in title_words)
            neg_hits = sum(1 for w in neg_words if w in title_words)
            score += (pos_hits * 0.25) - (neg_hits * 0.35)
            if pos_hits > 0 or neg_hits > 0:
                catalysts.append(item.get("title"))

        score = max(-1.0, min(1.0, score))
        mood = "POSITIVE" if score > 0.15 else ("NEGATIVE" if score < -0.15 else "NEUTRAL")
        confidence = 65 if news_items else 50

        summary = (
            f"Sentiment is currently {mood} (score {score:+.2f}). "
            f"Analyzed {len(news_items)} recent news feeds. "
            + ("Catalyst drivers include: " + "; ".join(catalysts[:2]) if catalysts else "General sector news flow remains calm.")
        )

        fallback = {
            "mood": mood,
            "score": round(score, 2),
            "confidence": confidence,
            "catalysts": catalysts[:3],
            "sentiment_summary": summary
        }

        if llm_client.has_active_key():
            res = await llm_client.generate_json(system_prompt, user_prompt)
            return merge_llm_report(fallback, res, {
                "mood": ("enum", {"POSITIVE", "NEUTRAL", "NEGATIVE"}),
                "score": ("float", -1.0, 1.0),
                "confidence": ("int", 0, 100),
                "catalysts": "list",
                "sentiment_summary": "str",
            }, "mood")
        return fallback
