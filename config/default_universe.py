"""
Pre-configured liquid stock universes for US and European markets.
These serve as the candidate pool for the daily algorithmic screener.
"""

from typing import List, Dict

EU_STOCKS: List[Dict[str, str]] = [
    # Amsterdam (AEX)
    {"ticker": "ASML.AS", "name": "ASML Holding", "sector": "Semiconductors", "country": "NL"},
    {"ticker": "INGA.AS", "name": "ING Groep", "sector": "Financials", "country": "NL"},
    {"ticker": "AD.AS", "name": "Ahold Delhaize", "sector": "Consumer Defensive", "country": "NL"},
    {"ticker": "PHIA.AS", "name": "Philips", "sector": "Healthcare", "country": "NL"},
    {"ticker": "HEIA.AS", "name": "Heineken", "sector": "Consumer Defensive", "country": "NL"},
    {"ticker": "SHEL.AS", "name": "Shell", "sector": "Energy", "country": "NL/UK"},
    {"ticker": "ASM.AS", "name": "ASM International", "sector": "Semiconductors", "country": "NL"},
    {"ticker": "BESI.AS", "name": "BE Semiconductor", "sector": "Semiconductors", "country": "NL"},
    {"ticker": "UNA.AS", "name": "Unilever", "sector": "Consumer Defensive", "country": "NL/UK"},
    {"ticker": "WKL.AS", "name": "Wolters Kluwer", "sector": "Publishing", "country": "NL"},

    # Germany (DAX)
    {"ticker": "SAP.DE", "name": "SAP SE", "sector": "Technology", "country": "DE"},
    {"ticker": "SIE.DE", "name": "Siemens AG", "sector": "Industrials", "country": "DE"},
    {"ticker": "ALV.DE", "name": "Allianz SE", "sector": "Financials", "country": "DE"},
    {"ticker": "DTE.DE", "name": "Deutsche Telekom", "sector": "Communication", "country": "DE"},
    {"ticker": "BMW.DE", "name": "BMW AG", "sector": "Consumer Cyclical", "country": "DE"},
    {"ticker": "MBG.DE", "name": "Mercedes-Benz Group", "sector": "Consumer Cyclical", "country": "DE"},
    {"ticker": "AIR.PA", "name": "Airbus SE", "sector": "Industrials", "country": "FR/DE"},

    # France (CAC 40)
    {"ticker": "MC.PA", "name": "LVMH Moët Hennessy", "sector": "Consumer Cyclical", "country": "FR"},
    {"ticker": "TTE.PA", "name": "TotalEnergies", "sector": "Energy", "country": "FR"},
    {"ticker": "SAN.PA", "name": "Sanofi", "sector": "Healthcare", "country": "FR"},
    {"ticker": "OR.PA", "name": "L'Oréal", "sector": "Consumer Defensive", "country": "FR"},
    {"ticker": "SU.PA", "name": "Schneider Electric", "sector": "Industrials", "country": "FR"},
    {"ticker": "BNP.PA", "name": "BNP Paribas", "sector": "Financials", "country": "FR"},
    {"ticker": "RMS.PA", "name": "Hermès International", "sector": "Consumer Cyclical", "country": "FR"},
]

US_STOCKS: List[Dict[str, str]] = [
    # Mega-Cap Tech / Growth
    {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "MSFT", "name": "Microsoft Corp.", "sector": "Technology", "country": "US"},
    {"ticker": "NVDA", "name": "NVIDIA Corp.", "sector": "Semiconductors", "country": "US"},
    {"ticker": "AMZN", "name": "Amazon.com Inc.", "sector": "Consumer Cyclical", "country": "US"},
    {"ticker": "GOOGL", "name": "Alphabet Inc.", "sector": "Communication", "country": "US"},
    {"ticker": "META", "name": "Meta Platforms", "sector": "Communication", "country": "US"},
    {"ticker": "TSLA", "name": "Tesla Inc.", "sector": "Consumer Cyclical", "country": "US"},
    {"ticker": "AVGO", "name": "Broadcom Inc.", "sector": "Semiconductors", "country": "US"},

    # Growth / Semis / Software
    {"ticker": "AMD", "name": "Advanced Micro Devices", "sector": "Semiconductors", "country": "US"},
    {"ticker": "QCOM", "name": "Qualcomm Inc.", "sector": "Semiconductors", "country": "US"},
    {"ticker": "CRM", "name": "Salesforce Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "NOW", "name": "ServiceNow Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "PLTR", "name": "Palantir Technologies", "sector": "Technology", "country": "US"},
    {"ticker": "UBER", "name": "Uber Technologies", "sector": "Technology", "country": "US"},
    {"ticker": "PANW", "name": "Palo Alto Networks", "sector": "Technology", "country": "US"},

    # Cyclicals / Financials / Healthcare / Industrial
    {"ticker": "JPM", "name": "JPMorgan Chase", "sector": "Financials", "country": "US"},
    {"ticker": "V", "name": "Visa Inc.", "sector": "Financials", "country": "US"},
    {"ticker": "LLY", "name": "Eli Lilly and Co.", "sector": "Healthcare", "country": "US"},
    {"ticker": "UNH", "name": "UnitedHealth Group", "sector": "Healthcare", "country": "US"},
    {"ticker": "CAT", "name": "Caterpillar Inc.", "sector": "Industrials", "country": "US"},
    {"ticker": "XOM", "name": "Exxon Mobil Corp.", "sector": "Energy", "country": "US"},
    {"ticker": "COST", "name": "Costco Wholesale", "sector": "Consumer Defensive", "country": "US"},
]

DEFAULT_WATCHLIST = [
    "ASML.AS",
    "INGA.AS",
    "SAP.DE",
    "NVDA",
    "AAPL",
    "MSFT",
    "PLTR",
    "AMZN"
]
