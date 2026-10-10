import sys
import argparse
import uvicorn
from config.settings import settings

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Antigravity Multi-Agent Trading System")
    parser.add_argument("--host", type=str, default=settings.HOST, help="Host address to bind")
    parser.add_argument("--port", type=int, default=settings.PORT, help="Port to bind")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    tailscale_ip = None

    print("==================================================================")
    print(" [*] Starting Multi-Agent Investment & Trading System")
    print(f" [*] Local URL:     http://localhost:{args.port}")
    if tailscale_ip:
        print(f" [*] Tailscale URL: http://{tailscale_ip}:{args.port}")
    print(f" [*] Base Currency: {settings.BASE_CURRENCY}")
    print(f" [*] AI Engine:    {settings.LLM_PROVIDER.upper()}")
    print("==================================================================")

    uvicorn.run(
        "src.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )

if __name__ == "__main__":
    main()
