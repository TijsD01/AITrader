import os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from config.settings import settings
from config.default_universe import DEFAULT_WATCHLIST, EU_STOCKS, US_STOCKS
from src.db.models import Base, WatchlistItem, PortfolioSnapshot, SystemConfig

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

def _run_migrations(connection):
    migrations = [
        ("positions", "is_live", "BOOLEAN DEFAULT 0"),
        ("positions", "broker_order_id", "VARCHAR(100)"),
        ("positions", "fees_eur", "FLOAT DEFAULT 0.0"),
        ("trades", "is_live", "BOOLEAN DEFAULT 0"),
        ("trades", "broker_order_id", "VARCHAR(100)"),
        ("trades", "fee_eur", "FLOAT DEFAULT 0.0"),
        ("order_triggers", "is_live", "BOOLEAN DEFAULT 0"),
        ("order_triggers", "broker_order_id", "VARCHAR(100)"),
        ("portfolio_snapshots", "is_live", "BOOLEAN DEFAULT 0")
    ]
    for table, col, col_type in migrations:
        try:
            res = connection.exec_driver_sql(f"PRAGMA table_info({table});")
            cols = [r[1] for r in res.fetchall()]
            if col not in cols:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")
        except Exception:
            pass

async def init_db():
    # Ensure parent dir exists
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_migrations)
        
    async with AsyncSessionLocal() as session:
        # Check if portfolio snapshot exists
        res = await session.execute(select(PortfolioSnapshot))
        snapshot = res.scalars().first()
        if not snapshot:
            init_snapshot = PortfolioSnapshot(
                cash_eur=settings.INITIAL_PAPER_CAPITAL,
                equity_eur=0.0,
                total_value_eur=settings.INITIAL_PAPER_CAPITAL,
                daily_pnl_eur=0.0,
                daily_pnl_pct=0.0,
                open_positions_count=0
            )
            session.add(init_snapshot)

        # Check if watchlist is seeded
        res = await session.execute(select(WatchlistItem))
        items = res.scalars().all()
        if not items:
            stock_lookup = {s["ticker"]: s for s in EU_STOCKS + US_STOCKS}
            for ticker in DEFAULT_WATCHLIST:
                meta = stock_lookup.get(ticker, {"name": ticker, "sector": "General", "country": "US"})
                item = WatchlistItem(
                    ticker=ticker,
                    name=meta.get("name"),
                    sector=meta.get("sector"),
                    country=meta.get("country"),
                    is_active=True
                )
                session.add(item)
                
        # Check and seed system configs
        cfg_res = await session.execute(select(SystemConfig))
        db_configs = {c.key: c.value for c in cfg_res.scalars().all()}

        # Seed missing keys with settings defaults
        defaults_to_seed = {
            "AUTO_BUY_ENABLED": str(settings.AUTO_BUY_ENABLED).lower(),
            "KILL_SWITCH_ACTIVE": str(settings.KILL_SWITCH_ACTIVE).lower(),
            "MIN_CONSENSUS_SCORE": str(settings.MIN_CONSENSUS_SCORE),
            "TRIGGER_MIN_SCORE": str(settings.TRIGGER_MIN_SCORE),
            "POSITION_SIZE_PCT": str(settings.POSITION_SIZE_PCT),
            "ENFORCE_MARKET_HOURS": str(settings.ENFORCE_MARKET_HOURS).lower(),
            "MAX_CAPITAL_USAGE_PCT": str(settings.MAX_CAPITAL_USAGE_PCT),
            "MAX_DEPLOYABLE_CAPITAL_EUR": str(settings.MAX_DEPLOYABLE_CAPITAL_EUR) if settings.MAX_DEPLOYABLE_CAPITAL_EUR is not None else "",
            "MAX_POSITIONS": str(settings.MAX_POSITIONS),
            "TRADING_MODE": settings.TRADING_MODE,
            "LIVE_TRADING_ENABLED": str(settings.LIVE_TRADING_ENABLED).lower(),
            "IBKR_HOST": settings.IBKR_HOST,
            "IBKR_PORT": str(settings.IBKR_PORT),
            "IBKR_CLIENT_ID": str(settings.IBKR_CLIENT_ID),
            "IBKR_ACCOUNT_ID": settings.IBKR_ACCOUNT_ID or "",
        }

        seeded_any = False
        for k, v in defaults_to_seed.items():
            if k not in db_configs:
                session.add(SystemConfig(key=k, value=v))
                db_configs[k] = v
                seeded_any = True

        if seeded_any:
            await session.commit()

        # Synchronize all persisted DB configurations into in-memory settings singleton
        if "AUTO_BUY_ENABLED" in db_configs:
            settings.AUTO_BUY_ENABLED = db_configs["AUTO_BUY_ENABLED"].strip().lower() == "true"
        if "KILL_SWITCH_ACTIVE" in db_configs:
            settings.KILL_SWITCH_ACTIVE = db_configs["KILL_SWITCH_ACTIVE"].strip().lower() == "true"
        if "MIN_CONSENSUS_SCORE" in db_configs:
            try:
                settings.MIN_CONSENSUS_SCORE = int(db_configs["MIN_CONSENSUS_SCORE"])
            except Exception:
                pass
        if "TRIGGER_MIN_SCORE" in db_configs:
            try:
                settings.TRIGGER_MIN_SCORE = int(db_configs["TRIGGER_MIN_SCORE"])
            except Exception:
                pass
        if "POSITION_SIZE_PCT" in db_configs:
            try:
                # Cap stale DB values (e.g. old 0.55 from the 100% slider); matches web endpoint cap
                settings.POSITION_SIZE_PCT = min(0.15, float(db_configs["POSITION_SIZE_PCT"]))
            except Exception:
                pass
        if "ENFORCE_MARKET_HOURS" in db_configs:
            settings.ENFORCE_MARKET_HOURS = db_configs["ENFORCE_MARKET_HOURS"].strip().lower() == "true"
        if "MAX_CAPITAL_USAGE_PCT" in db_configs:
            try:
                settings.MAX_CAPITAL_USAGE_PCT = float(db_configs["MAX_CAPITAL_USAGE_PCT"])
            except Exception:
                pass
        if "MAX_DEPLOYABLE_CAPITAL_EUR" in db_configs:
            try:
                v = db_configs["MAX_DEPLOYABLE_CAPITAL_EUR"].strip()
                settings.MAX_DEPLOYABLE_CAPITAL_EUR = float(v) if v and v not in ("None", "null") else None
            except Exception:
                pass
        if "MAX_POSITIONS" in db_configs:
            try:
                settings.MAX_POSITIONS = max(1, int(db_configs["MAX_POSITIONS"]))
            except Exception:
                pass
        if "TRADING_MODE" in db_configs:
            m = db_configs["TRADING_MODE"].strip().upper()
            if m in ("PAPER", "LIVE"):
                settings.TRADING_MODE = m
        if "LIVE_TRADING_ENABLED" in db_configs:
            settings.LIVE_TRADING_ENABLED = db_configs["LIVE_TRADING_ENABLED"].strip().lower() == "true"
        if "IBKR_HOST" in db_configs:
            h = db_configs["IBKR_HOST"].strip()
            if h:
                settings.IBKR_HOST = h
        if "IBKR_PORT" in db_configs:
            try:
                p = int(db_configs["IBKR_PORT"])
                if p > 0:
                    settings.IBKR_PORT = p
            except Exception:
                pass
        if "IBKR_CLIENT_ID" in db_configs:
            try:
                settings.IBKR_CLIENT_ID = int(db_configs["IBKR_CLIENT_ID"])
            except Exception:
                pass
        if "IBKR_ACCOUNT_ID" in db_configs:
            aid = db_configs["IBKR_ACCOUNT_ID"].strip()
            settings.IBKR_ACCOUNT_ID = aid if aid else None

        await session.commit()



async def set_kill_switch(active: bool) -> bool:
    """Set the emergency kill switch in memory and persist it so it survives restarts."""
    active = bool(active)
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "KILL_SWITCH_ACTIVE"))
        cfg = res.scalars().first()
        if cfg:
            cfg.value = str(active).lower()
        else:
            session.add(SystemConfig(key="KILL_SWITCH_ACTIVE", value=str(active).lower()))
        await session.commit()
    settings.KILL_SWITCH_ACTIVE = active
    return active
