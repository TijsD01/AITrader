import os
import asyncio
from pathlib import Path
import pytest

# Point to isolated test database files
TEST_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "test_trading_bot.db"
TEST_MEMORY_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "test_memory.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"

from config.settings import settings, Settings
# Start from the code defaults, not the real .env, so results don't depend on the owner's config
for _name, _field in Settings.model_fields.items():
    if not _field.is_required():
        setattr(settings, _name, _field.get_default(call_default_factory=True))
settings.DATABASE_URL = f"sqlite+aiosqlite:///{TEST_DB_FILE}"
settings.TRADING_MODE = "PAPER"
settings.LIVE_TRADING_ENABLED = False
settings.ENFORCE_MARKET_HOURS = True
settings.REGIME_FILTER_ENABLED = False  # offline tests: no index lookups; covered in test_research_strategy.py

from src.memory.store import memory_store
memory_store.db_path = TEST_MEMORY_DB_FILE
memory_store._initialized = False

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
    if TEST_MEMORY_DB_FILE.exists():
        try:
            TEST_MEMORY_DB_FILE.unlink()
        except Exception:
            pass
    memory_store._initialized = False

    async def _init():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await db_session_module.init_db()
        await memory_store.init_db()

    asyncio.run(_init())
    yield
    async def _cleanup():
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await test_engine.dispose()
    asyncio.run(_cleanup())
    for db_file in (TEST_DB_FILE, TEST_MEMORY_DB_FILE, Path(f"{TEST_MEMORY_DB_FILE}-wal"), Path(f"{TEST_MEMORY_DB_FILE}-shm")):
        if db_file.exists():
            try:
                db_file.unlink()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _no_real_env_writes(monkeypatch):
    """POST /api/settings persists to .env; tests must never rewrite the real one."""
    monkeypatch.setattr("src.web.routes.update_env_file", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _telegram_owner_chat(monkeypatch):
    """The Telegram bot only accepts commands from the configured owner chat (12345 in tests)."""
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
