import base64
import ipaddress
import logging
import secrets
from typing import Iterable, Optional
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from config.settings import settings

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_private_client(host: Optional[str]) -> bool:
    if not host:
        return False
    if host in ("localhost", "testclient"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return any(ip in net for net in _trusted_networks())


def _trusted_networks():
    nets = []
    for cidr in (settings.WEB_TRUSTED_NETWORKS or "").split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning(f"Ignoring invalid WEB_TRUSTED_NETWORKS entry: {cidr}")
    return nets


def _check_basic_auth(header: Optional[str]) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except Exception:
        return False
    user, _, password = decoded.partition(":")
    user_ok = secrets.compare_digest(user.encode(), (settings.WEB_USERNAME or "").encode())
    pass_ok = secrets.compare_digest(password.encode(), (settings.WEB_PASSWORD or "").encode())
    return user_ok and pass_ok


def _allowed_origins() -> Iterable[str]:
    return [o.strip().rstrip("/") for o in (settings.CORS_ALLOW_ORIGINS or "").split(",") if o.strip()]


def _same_origin(request: Request) -> bool:
    """
    For state-changing requests, a browser-supplied Origin (or Referer) must match the Host we
    were reached on. Blocks cross-site requests from other web pages (CSRF). Requests without
    either header (curl, scripts) are allowed; they still pass the network/auth checks.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source != "null"
    parts = urlsplit(source)
    origin = f"{parts.scheme}://{parts.netloc}".rstrip("/")
    if origin in _allowed_origins():
        return True
    host = request.headers.get("host", "")
    return parts.netloc.lower() == host.lower()


class WebSecurityMiddleware(BaseHTTPMiddleware):
    """Network restriction or HTTP Basic auth, plus an Origin check on state-changing requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        client_host = request.client.host if request.client else None

        if settings.WEB_PASSWORD:
            if not _check_basic_auth(request.headers.get("authorization")):
                return PlainTextResponse(
                    "Authentication required.",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Trading Bot", charset="UTF-8"'},
                )
        elif settings.WEB_ALLOW_PRIVATE_NETWORK_ONLY and not _is_private_client(client_host):
            logger.warning(f"Blocked request from non-local client {client_host} (set WEB_PASSWORD to allow remote access).")
            return PlainTextResponse("Forbidden: only local network clients are allowed.", status_code=403)

        if request.method.upper() not in SAFE_METHODS and not _same_origin(request):
            logger.warning(f"Blocked cross-origin {request.method} {request.url.path} from {request.headers.get('origin') or request.headers.get('referer')}")
            return PlainTextResponse("Forbidden: cross-origin request.", status_code=403)

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def install_web_security(app) -> None:
    """Adds the security middleware and, only if configured, CORS for explicit extra origins."""
    origins = list(_allowed_origins())
    if origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(WebSecurityMiddleware)
