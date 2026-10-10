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
        ("trades", "is_live", "BOOLEAN DEFAULT 0"),
        ("trades", "broker_order_id", "VARCHAR(100)"),
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
                
        # Check system config
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "AUTO_BUY_ENABLED"))
        cfg = res.scalars().first()
        if not cfg:
            session.add(SystemConfig(key="AUTO_BUY_ENABLED", value=str(settings.AUTO_BUY_ENABLED).lower()))
            session.add(SystemConfig(key="MIN_CONSENSUS_SCORE", value=str(settings.MIN_CONSENSUS_SCORE)))
            session.add(SystemConfig(key="TRIGGER_MIN_SCORE", value=str(settings.TRIGGER_MIN_SCORE)))
            session.add(SystemConfig(key="POSITION_SIZE_PCT", value=str(settings.POSITION_SIZE_PCT)))
        else:
            # Check if TRIGGER_MIN_SCORE already exists
            trig_cfg = await session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
            if not trig_cfg.scalars().first():
                session.add(SystemConfig(key="TRIGGER_MIN_SCORE", value=str(settings.TRIGGER_MIN_SCORE)))

            # Check if ENFORCE_MARKET_HOURS already exists in DB
            emh_cfg = await session.execute(select(SystemConfig).where(SystemConfig.key == "ENFORCE_MARKET_HOURS"))
            if not emh_cfg.scalars().first():
                session.add(SystemConfig(key="ENFORCE_MARKET_HOURS", value="true"))

        await session.commit()
