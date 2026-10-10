import os
import asyncio
from pathlib import Path
import pytest

# Point to an isolated test database file
TEST_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "test_trading_bot.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"

from config.settings import settings
settings.DATABASE_URL = f"sqlite+aiosqlite:///{TEST_DB_FILE}"
settings.TRADING_MODE = "PAPER"
settings.LIVE_TRADING_ENABLED = False
settings.ENFORCE_MARKET_HOURS = True

# Re-bind session engine to the test database
from src.db import session as db_session_module
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.db.models import Base

from sqlalchemy.pool import NullPool

test_engine = create_async_engine(
    settings.DATABASE_URL,
    poolclass=NullPool,
    echo=False,
    future=True
)
test_session_maker = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

db_session_module.engine = test_engine
db_session_module.AsyncSessionLocal = test_session_maker

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    async def _init():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await db_session_module.init_db()

    asyncio.run(_init())
    yield
    async def _cleanup():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await test_engine.dispose()
    asyncio.run(_cleanup())
    if TEST_DB_FILE.exists():
        try:
            TEST_DB_FILE.unlink()
        except Exception:
            pass
