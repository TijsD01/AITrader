import logging
from typing import List, Dict, Any
import datetime
from urllib.parse import quote_plus
import yfinance as yf
import httpx
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

import sys

def get_ticker_news(ticker: str, limit: int = 6) -> List[Dict[str, Any]]:
    articles = []
    try:
        yf_ticker = yf.Ticker(ticker)
        raw_news = yf_ticker.news or []
        for item in raw_news[:limit]:
            content = item.get("content") if isinstance(item.get("content"), dict) else None
            if content is not None:
                # yfinance 1.x shape: {"id", "content": {"title", "pubDate", "provider", "canonicalUrl"}}
                title = content.get("title") or ""
                publisher = (content.get("provider") or {}).get("displayName") or "Financial Press"
                link = (content.get("canonicalUrl") or {}).get("url") or (content.get("clickThroughUrl") or {}).get("url") or ""
                pub_date = content.get("pubDate")
                time_str = "Recent"
                if pub_date:
                    try:
                        parsed = datetime.datetime.fromisoformat(str(pub_date).replace("Z", "+00:00"))
                        time_str = parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    except ValueError:
                        time_str = str(pub_date)
            else:
                title = item.get("title", "")
                publisher = item.get("publisher", "Financial Press")
                link = item.get("link", "")
                pub_time = item.get("providerPublishTime")
                time_str = (
                    datetime.datetime.fromtimestamp(pub_time, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    if pub_time
                    else "Recent"
                )
            if title:
                articles.append({
                    "title": title,
                    "publisher": publisher,
                    "link": link,
                    "time": time_str
                })
    except Exception as e:
        logger.warning(f"Error fetching yfinance news for {ticker}: {e}")

    if not articles:
        articles = get_google_news_rss(ticker, limit=limit)

    return articles


def get_google_news_rss(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    articles = []
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(str(query) + ' stock market')}&hl=en-US&gl=US&ceid=US:en"
        resp = httpx.get(url, timeout=5.0, follow_redirects=True)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:limit]:
                articles.append({
                    "title": item.findtext("title", default=""),
                    "publisher": item.findtext("source", default="News"),
                    "link": item.findtext("link", default=""),
                    "time": item.findtext("pubDate", default="Recent")
                })
    except Exception as e:
        logger.warning(f"Error fetching Google RSS news for {query}: {e}")
    return articles


news_service = sys.modules[__name__]
