import sys
import argparse
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.memory.store import memory_store
from src.memory.routes import memory_router
from src.memory.proxy import proxy_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] TencentDB-Agent-Memory: %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing TencentDB Agent Memory storage...")
    await memory_store.init_db()
    logger.info("TencentDB Agent Memory ready: L0-L3 layers, Skills, Wiki, CodeGraph initialized.")
    yield
    logger.info("TencentDB Agent Memory shutting down...")

def create_memory_app() -> FastAPI:
    app = FastAPI(
        title="TencentDB Agent Memory Server",
        version="2.0.0",
        description="Team-level memory hub & proxy for AI Agents (Chat Memory, Skill, LLM-Wiki, Code-Graph)",
        lifespan=lifespan
    )

    # Same protection as the dashboard: LAN-only or Basic auth, same-origin writes, no open CORS
    from src.web.security import install_web_security
    install_web_security(app)

    # Include memory v3 endpoints and LLM proxy
    app.include_router(memory_router)
    app.include_router(proxy_router)

    return app

app = create_memory_app()

def main():
    parser = argparse.ArgumentParser(description="TencentDB Agent Memory Server")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind (default: localhost only)")
    parser.add_argument("--port", type=int, default=8420, help="Port to bind (default 8420)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    print("==================================================================")
    print(" [*] TencentDB Agent Memory Hub & Proxy Server")
    print(f" [*] Local Endpoint:       http://localhost:{args.port}")
    print(f" [*] Health Check:         http://localhost:{args.port}/health")
    print(f" [*] Cold-Start Recap:     http://localhost:{args.port}/v3/memory/cold-start")
    print(f" [*] Memory Assets Catalog: http://localhost:{args.port}/v3/memory/assets")
    print(f" [*] OpenAI Proxy URL:     http://localhost:{args.port}/v1/chat/completions")
    print(f" [*] Claude Proxy URL:     http://localhost:{args.port}/v1/messages")
    print("==================================================================")

    uvicorn.run(
        "src.memory.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )

if __name__ == "__main__":
    main()
