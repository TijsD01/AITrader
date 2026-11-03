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
    {"ticker": "ADYEN.AS", "name": "Adyen", "sector": "Financials", "country": "NL"},
    {"ticker": "PRX.AS", "name": "Prosus", "sector": "Communication", "country": "NL"},
    {"ticker": "KPN.AS", "name": "KPN", "sector": "Communication", "country": "NL"},
    {"ticker": "NN.AS", "name": "NN Group", "sector": "Financials", "country": "NL"},
    {"ticker": "ABN.AS", "name": "ABN AMRO", "sector": "Financials", "country": "NL"},
    {"ticker": "RAND.AS", "name": "Randstad", "sector": "Industrials", "country": "NL"},
    {"ticker": "IMCD.AS", "name": "IMCD", "sector": "Basic Materials", "country": "NL"},
    {"ticker": "EXO.AS", "name": "Exor", "sector": "Financials", "country": "NL"},

    # Germany (DAX)
    {"ticker": "SAP.DE", "name": "SAP SE", "sector": "Technology", "country": "DE"},
    {"ticker": "SIE.DE", "name": "Siemens AG", "sector": "Industrials", "country": "DE"},
    {"ticker": "ALV.DE", "name": "Allianz SE", "sector": "Financials", "country": "DE"},
    {"ticker": "DTE.DE", "name": "Deutsche Telekom", "sector": "Communication", "country": "DE"},
    {"ticker": "BMW.DE", "name": "BMW AG", "sector": "Consumer Cyclical", "country": "DE"},
    {"ticker": "MBG.DE", "name": "Mercedes-Benz Group", "sector": "Consumer Cyclical", "country": "DE"},
    {"ticker": "AIR.PA", "name": "Airbus SE", "sector": "Industrials", "country": "FR/DE"},
    {"ticker": "RHM.DE", "name": "Rheinmetall", "sector": "Industrials", "country": "DE"},
    {"ticker": "MUV2.DE", "name": "Munich Re", "sector": "Financials", "country": "DE"},
    {"ticker": "IFX.DE", "name": "Infineon Technologies", "sector": "Semiconductors", "country": "DE"},
    {"ticker": "DBK.DE", "name": "Deutsche Bank", "sector": "Financials", "country": "DE"},
    {"ticker": "ADS.DE", "name": "Adidas", "sector": "Consumer Cyclical", "country": "DE"},
    {"ticker": "DHL.DE", "name": "DHL Group", "sector": "Industrials", "country": "DE"},
    {"ticker": "ENR.DE", "name": "Siemens Energy", "sector": "Industrials", "country": "DE"},
    {"ticker": "BAS.DE", "name": "BASF", "sector": "Basic Materials", "country": "DE"},

    # France (CAC 40)
    {"ticker": "MC.PA", "name": "LVMH Moët Hennessy", "sector": "Consumer Cyclical", "country": "FR"},
    {"ticker": "TTE.PA", "name": "TotalEnergies", "sector": "Energy", "country": "FR"},
    {"ticker": "SAN.PA", "name": "Sanofi", "sector": "Healthcare", "country": "FR"},
    {"ticker": "OR.PA", "name": "L'Oréal", "sector": "Consumer Defensive", "country": "FR"},
    {"ticker": "SU.PA", "name": "Schneider Electric", "sector": "Industrials", "country": "FR"},
    {"ticker": "BNP.PA", "name": "BNP Paribas", "sector": "Financials", "country": "FR"},
    {"ticker": "RMS.PA", "name": "Hermès International", "sector": "Consumer Cyclical", "country": "FR"},
    {"ticker": "SAF.PA", "name": "Safran", "sector": "Industrials", "country": "FR"},
    {"ticker": "AI.PA", "name": "Air Liquide", "sector": "Basic Materials", "country": "FR"},
    {"ticker": "EL.PA", "name": "EssilorLuxottica", "sector": "Healthcare", "country": "FR"},
    {"ticker": "CS.PA", "name": "AXA", "sector": "Financials", "country": "FR"},
    {"ticker": "DG.PA", "name": "Vinci", "sector": "Industrials", "country": "FR"},

    # Spain, Italy, Belgium
    {"ticker": "ITX.MC", "name": "Inditex", "sector": "Consumer Cyclical", "country": "ES"},
    {"ticker": "IBE.MC", "name": "Iberdrola", "sector": "Utilities", "country": "ES"},
    {"ticker": "SAN.MC", "name": "Banco Santander", "sector": "Financials", "country": "ES"},
    {"ticker": "ENEL.MI", "name": "Enel", "sector": "Utilities", "country": "IT"},
    {"ticker": "UCG.MI", "name": "UniCredit", "sector": "Financials", "country": "IT"},
    {"ticker": "RACE.MI", "name": "Ferrari", "sector": "Consumer Cyclical", "country": "IT"},
    {"ticker": "ABI.BR", "name": "Anheuser-Busch InBev", "sector": "Consumer Defensive", "country": "BE"},
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

    # More tech / semis / software
    {"ticker": "ORCL", "name": "Oracle Corp.", "sector": "Technology", "country": "US"},
    {"ticker": "ADBE", "name": "Adobe Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "MU", "name": "Micron Technology", "sector": "Semiconductors", "country": "US"},
    {"ticker": "ARM", "name": "Arm Holdings", "sector": "Semiconductors", "country": "US"},
    {"ticker": "ANET", "name": "Arista Networks", "sector": "Technology", "country": "US"},
    {"ticker": "INTU", "name": "Intuit Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "CRWD", "name": "CrowdStrike", "sector": "Technology", "country": "US"},
    {"ticker": "SHOP", "name": "Shopify Inc.", "sector": "Technology", "country": "US"},
    {"ticker": "NFLX", "name": "Netflix Inc.", "sector": "Communication", "country": "US"},

    # More financials / healthcare
    {"ticker": "MA", "name": "Mastercard Inc.", "sector": "Financials", "country": "US"},
    {"ticker": "GS", "name": "Goldman Sachs", "sector": "Financials", "country": "US"},
    {"ticker": "BAC", "name": "Bank of America", "sector": "Financials", "country": "US"},
    {"ticker": "BLK", "name": "BlackRock Inc.", "sector": "Financials", "country": "US"},
    {"ticker": "ABBV", "name": "AbbVie Inc.", "sector": "Healthcare", "country": "US"},
    {"ticker": "ISRG", "name": "Intuitive Surgical", "sector": "Healthcare", "country": "US"},
    {"ticker": "MRK", "name": "Merck & Co.", "sector": "Healthcare", "country": "US"},
    {"ticker": "VRTX", "name": "Vertex Pharmaceuticals", "sector": "Healthcare", "country": "US"},

    # Industrials / defense
    {"ticker": "GE", "name": "GE Aerospace", "sector": "Industrials", "country": "US"},
    {"ticker": "RTX", "name": "RTX Corp.", "sector": "Industrials", "country": "US"},
    {"ticker": "LMT", "name": "Lockheed Martin", "sector": "Industrials", "country": "US"},
    {"ticker": "HON", "name": "Honeywell", "sector": "Industrials", "country": "US"},
    {"ticker": "DE", "name": "Deere & Co.", "sector": "Industrials", "country": "US"},

    # Consumer / energy / utilities
    {"ticker": "WMT", "name": "Walmart Inc.", "sector": "Consumer Defensive", "country": "US"},
    {"ticker": "HD", "name": "Home Depot", "sector": "Consumer Cyclical", "country": "US"},
    {"ticker": "MCD", "name": "McDonald's Corp.", "sector": "Consumer Cyclical", "country": "US"},
    {"ticker": "KO", "name": "Coca-Cola Co.", "sector": "Consumer Defensive", "country": "US"},
    {"ticker": "PG", "name": "Procter & Gamble", "sector": "Consumer Defensive", "country": "US"},
    {"ticker": "CVX", "name": "Chevron Corp.", "sector": "Energy", "country": "US"},
    {"ticker": "NEE", "name": "NextEra Energy", "sector": "Utilities", "country": "US"},
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
