import os
import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from config.settings import settings
from src.db.session import init_db
from src.scheduler.jobs import trading_scheduler
from src.telegram.bot import telegram_bot
from src.trading.broker import live_broker
from src.web.routes import router
from src.web.security import install_web_security
from src.memory.store import memory_store
from src.memory.routes import memory_router
from src.memory.proxy import proxy_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs incl. Telegram bot token
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup actions
    logger.info("Initializing database...")
    await init_db()

    logger.info("Initializing TencentDB Agent Memory storage...")
    await memory_store.init_db()
    
    logger.info("Starting background scheduler...")
    trading_scheduler.start()
    asyncio.create_task(trading_scheduler.check_and_run_startup_sessions())

    logger.info("Starting Telegram bot background worker...")
    telegram_task = asyncio.create_task(telegram_bot.start_polling())

    yield

    # Shutdown actions
    logger.info("Stopping scheduler...")
    trading_scheduler.stop()
    await telegram_bot.stop()
    telegram_task.cancel()
    logger.info("Disconnecting broker clients...")
    await live_broker.disconnect()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan
)

# Only LAN clients (or HTTP Basic auth when WEB_PASSWORD is set), same-origin writes, no open CORS
install_web_security(app)

app.include_router(router)
app.include_router(memory_router)
app.include_router(proxy_router)

# Mount static directory
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR, exist_ok=True)

class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate (cheap 304 via ETag) instead of
    reusing a stale copy. Without this, iOS Safari kept serving an old style.css
    after a dashboard update, so the new page rendered without its colours."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=str(STATIC_DIR)), name="static")

# Assets referenced from index.html get a ?v=<content hash> so a changed file is a new URL
VERSIONED_ASSETS = ("style.css", "app.js")


def _asset_version(name: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in VERSIONED_ASSETS:
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={_asset_version(name)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
