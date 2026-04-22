"""
x402 Mailcheck API — Email Validation for AI Agents

Validate email addresses with syntax, MX, disposable, free provider,
role-based, and typo detection checks. All powered by x402 micropayments.

Endpoints:
  POST /validate         — Full email validation (PII-safe: POST body)
  GET  /disposable       — Disposable domain check
  GET  /mx               — MX record lookup
  GET  /health           — Health check (free, no payment)
  GET  /.well-known/x402 — x402 discovery
"""

import os
import sys
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response
from pydantic import BaseModel, Field

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig, UnpaidResponseResult
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.extensions.bazaar import bazaar_resource_server_extension
from x402.server import x402ResourceServer

load_dotenv()

# --- Config ---
EVM_ADDRESS = os.getenv("EVM_ADDRESS")
NETWORK: Network = os.getenv("NETWORK", "eip155:84532")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4024"))
RAPIDAPI_PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET")
INTERNAL_KEY = os.getenv("INTERNAL_KEY", "")

if not EVM_ADDRESS:
    import warnings
    warnings.warn(
        "EVM_ADDRESS not set — payment middleware will be non-functional. "
        "Server startup will be blocked unless this is a test environment.",
        stacklevel=1,
    )
    EVM_ADDRESS = "0xDEAD000000000000000000000000000000000000"  # sentinel, never valid

# --- Business logic ---
from mailcheck import (
    check_disposable,
    check_mx,
    validate_email_full,
)

# --- Response schemas ---
class ValidateRequest(BaseModel):
    email: str = Field(max_length=254)  # RFC 5321


class ValidateResponse(BaseModel):
    email: str
    status: str
    score: float
    syntax_valid: bool
    domain: str | None
    mx_found: bool
    mx_records: list[str]
    is_disposable: bool
    is_free: bool
    is_role_based: bool
    did_you_mean: str | None
    checks_performed: list[str]


class DisposableResponse(BaseModel):
    domain: str
    is_disposable: bool


class MxResponse(BaseModel):
    domain: str
    mx_found: bool
    mx_records: list[str]


class HealthResponse(BaseModel):
    status: str
    service: str
    network: str


# --- Lifespan (startup guard: catches uvicorn main:app without .env) ---
@asynccontextmanager
async def lifespan(app):
    if not os.getenv("EVM_ADDRESS"):
        raise RuntimeError(
            "FATAL: EVM_ADDRESS not set. Payments would go to a dead address. "
            "Set EVM_ADDRESS in .env before starting the server."
        )
    yield


# --- App ---
app = FastAPI(
    title="Mailcheck API",
    description="6 email validation checks in one call — syntax, MX records, disposable detection (5,000+ domains), "
    "free provider, role-based, and typo suggestion. No API keys, no multiple service integrations.",
    version="0.1.0",
    lifespan=lifespan,
)


# --- x402 Payment Middleware ---
CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET")

if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
    from cdp.x402 import create_facilitator_config as create_cdp_config
    facilitator = HTTPFacilitatorClient(create_cdp_config())
else:
    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))

server = x402ResourceServer(facilitator)
server.register(NETWORK, ExactEvmServerScheme())
server.register_extension(bazaar_resource_server_extension)

# Payment logging — records settlements to shared SQLite DB
from payment_logger import PaymentLogger
_pay_logger = PaymentLogger("mailcheck")
server.on_after_settle(_pay_logger.log_settlement)
server.on_settle_failure(_pay_logger.log_failure)

# Solana support (Dexter market — activated by SOLANA_PAY_TO env var)
SOLANA_PAY_TO = os.getenv("SOLANA_PAY_TO", "")
if SOLANA_PAY_TO:
    from x402.mechanisms.svm.constants import SOLANA_MAINNET_CAIP2
    from x402.mechanisms.svm.exact import ExactSvmServerScheme
    server.register(SOLANA_MAINNET_CAIP2, ExactSvmServerScheme())


PRICE = "$0.01"
PAYMENT = PaymentOption(scheme="exact", pay_to=EVM_ADDRESS, price=PRICE, network=NETWORK)
ACCEPTS = [PAYMENT]
if SOLANA_PAY_TO:
    ACCEPTS.append(PaymentOption(scheme="exact", pay_to=SOLANA_PAY_TO, price=PRICE, network=SOLANA_MAINNET_CAIP2))


# --- 402 Sample Responses (show agents what they'd get if they paid) ---
_ALTERNATIVES = {
    "mcp_gateway": {
        "info": "Access this and 2300+ x402 APIs via a single MCP server. Payment brokered for you — supports API keys (no wallet), free trial (World ID), or x402.",
        "url": "https://mcp.hugen.tokyo/mcp",
        "transport": "streamable-http",
    },
    "api_key": {
        "info": "Get a free API key with $0.05 credit — covers search and broker calls, no wallet needed",
        "create": "POST https://discovery.hugen.tokyo/keys/create",
        "then": "POST /broker/call with X-API-Key header to call any x402 API",
        "docs": "https://discovery.hugen.tokyo/llms.txt",
    },
    "sdk": {
        "info": "One-line Python access — no wallet, no setup",
        "install": "pip install x402-pay",
        "usage": "import x402_pay; r = x402_pay.get('https://mailcheck.hugen.tokyo/mailcheck/disposable?email=test@mailinator.com')",
    },
    "intel": {
        "info": "Need deeper analysis? Intel combines 4+ data sources with AI risk verdict in one call ($0.50)",
        "example": "https://intel.hugen.tokyo/intel/token-report?address=0xdac17f958d2ee523a2206206994597c13d831ec7&chain=base",
    },
}


def _sample(example: dict):
    """Factory: returns unpaid_response_body callback with sample data."""
    body = {"_notice": f"Payment required ({PRICE} USDC on Base). Sample response below.", "_alternatives": _ALTERNATIVES, **example}
    def callback(_ctx):
        return UnpaidResponseResult(content_type="application/json", body=body)
    return callback


routes = {
    "POST /mailcheck/validate": RouteConfig(
        accepts=ACCEPTS,
        mime_type="application/json",
        description="6 email validation checks in one call — syntax (RFC 5322), MX record verification, "
        "disposable detection (5,000+ domains), free provider identification, role-based address detection (admin@, info@), "
        "and typo suggestion. Returns a 0-1 confidence score with detailed per-check results. "
        "POST method protects email addresses from access logs. "
        "No API keys, no multiple service integrations needed. Accepts USDC payments on Base and Solana",
        unpaid_response_body=_sample({
            "email": "user@gmail.com", "status": "valid", "score": 0.95,
            "syntax_valid": True, "domain": "gmail.com", "mx_found": True,
            "mx_records": ["gmail-smtp-in.l.google.com"],
            "is_disposable": False, "is_free": True, "is_role_based": False,
            "did_you_mean": None,
            "checks_performed": ["syntax", "mx", "disposable", "free", "role", "typo"],
        }),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "email-validation",
                "tags": ["email", "mx", "disposable"],
                "info": {
                    "input": {
                        "type": "http",
                        "bodyType": "json",
                        "example": {"email": "user@gmail.com"},
                    },
                    "output": {
                        "type": "json",
                        "example": {
                            "email": "user@gmail.com",
                            "status": "valid",
                            "score": 0.95,
                            "syntax_valid": True,
                            "domain": "gmail.com",
                            "mx_found": True,
                            "mx_records": ["gmail-smtp-in.l.google.com"],
                            "is_disposable": False,
                            "is_free": True,
                            "is_role_based": False,
                            "did_you_mean": None,
                            "checks_performed": ["syntax", "mx", "disposable", "free", "role", "typo"],
                        },
                    },
                },
            },
        },
    ),
    "GET /mailcheck/disposable": RouteConfig(
        accepts=ACCEPTS,
        mime_type="application/json",
        description="Instant disposable email detection — checks against 5,000+ known temporary email domains "
        "(Guerrilla Mail, Mailinator, Temp Mail, 10MinuteMail, and more). Returns boolean result. "
        "Maintaining this blocklist yourself requires weekly updates from multiple sources. "
        "Accepts USDC payments on Base and Solana",
        unpaid_response_body=_sample({
            "domain": "guerrillamail.com", "is_disposable": True,
        }),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "email-validation",
                "tags": ["email", "mx", "disposable"],
                "info": {
                    "input": {
                        "type": "http",
                        "queryParams": {"domain": "guerrillamail.com"},
                    },
                    "output": {
                        "type": "json",
                        "example": {
                            "domain": "guerrillamail.com",
                            "is_disposable": True,
                        },
                    },
                },
            },
        },
    ),
    "GET /mailcheck/mx": RouteConfig(
        accepts=ACCEPTS,
        mime_type="application/json",
        description="MX record lookup for any domain — returns whether mail servers exist and their hostnames sorted by priority. "
        "Verify email deliverability before sending. "
        "Supports all TLDs including ccTLDs and new gTLDs. No DNS library setup needed. "
        "Accepts USDC payments on Base and Solana",
        unpaid_response_body=_sample({
            "domain": "gmail.com", "mx_found": True,
            "mx_records": ["gmail-smtp-in.l.google.com", "alt1.gmail-smtp-in.l.google.com"],
        }),
        extensions={
            "bazaar": {
                "discoverable": True,
                "category": "email-validation",
                "tags": ["email", "mx", "disposable"],
                "info": {
                    "input": {
                        "type": "http",
                        "queryParams": {"domain": "gmail.com"},
                    },
                    "output": {
                        "type": "json",
                        "example": {
                            "domain": "gmail.com",
                            "mx_found": True,
                            "mx_records": ["gmail-smtp-in.l.google.com", "alt1.gmail-smtp-in.l.google.com"],
                        },
                    },
                },
            },
        },
    ),
}


# --- x402 + RapidAPI Bypass (combined ASGI middleware) ---
# When x-rapidapi-proxy-secret matches, skip x402 payment entirely.
# Pattern from scout-mcp (server.ts:272), adapted for ASGI.
class PaymentWithRapidAPIBypass:
    """Wraps PaymentMiddlewareASGI with RapidAPI proxy secret bypass.

    Safety net: if the facilitator is unreachable (DNS failure, timeout),
    return 502 instead of letting the unhandled exception become a 500.
    """

    def __init__(self, app_inner, *, routes, server):
        self.raw_app = app_inner
        self.payment_app = PaymentMiddlewareASGI(app_inner, routes=routes, server=server)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and INTERNAL_KEY:
            headers = dict(scope.get("headers", []))
            key = headers.get(b"x-internal-key", b"").decode(errors="replace")
            if key and key == INTERNAL_KEY:
                scope.setdefault("state", {})["_channel"] = "mcp-gateway"
                return await self.raw_app(scope, receive, send)
        if scope["type"] == "http" and RAPIDAPI_PROXY_SECRET:
            headers = dict(scope.get("headers", []))
            secret = headers.get(b"x-rapidapi-proxy-secret", b"").decode(errors="replace")
            if secret and secret == RAPIDAPI_PROXY_SECRET:
                return await self.raw_app(scope, receive, send)
        try:
            return await self.payment_app(scope, receive, send)
        except Exception as exc:
            import json as _json
            print(f"[x402] Facilitator error: {exc}", file=sys.stderr)
            body = _json.dumps({"error": "Payment service temporarily unavailable"}).encode()
            await send({"type": "http.response.start", "status": 502, "headers": [
                [b"content-type", b"application/json"],
                [b"retry-after", b"30"],
            ]})
            await send({"type": "http.response.body", "body": body})


# --- Access Log (analytics) ---
class AccessLogMiddleware:
    """ASGI middleware — logs requests to paid endpoints for analytics."""

    _SKIP = frozenset({"/health", "/.well-known/x402", "/openapi.json", "/llms.txt", "/docs", "/redoc"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "/")
        if path in self._SKIP or scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)

        t0 = time.monotonic()
        status = 0

        async def _send(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        except Exception:
            status = 500
            raise
        finally:
            ms = (time.monotonic() - t0) * 1000
            hdrs = dict(scope.get("headers", []))
            raw_ip = hdrs.get(b"x-forwarded-for", b"").decode(errors="replace")
            ip = raw_ip.split(",")[0].strip() if raw_ip else "direct"
            ua = hdrs.get(b"user-agent", b"").decode(errors="replace")[:80]
            qs = scope.get("query_string", b"").decode(errors="replace")
            url = f"{path}?{qs}" if qs else path

            ik = hdrs.get(b"x-internal-key", b"").decode(errors="replace")
            if ik and ik == INTERNAL_KEY:
                ch = "mcp-gateway"
            elif RAPIDAPI_PROXY_SECRET and hdrs.get(b"x-rapidapi-proxy-secret", b"").decode(errors="replace") == RAPIDAPI_PROXY_SECRET:
                ch = "rapidapi"
            elif status == 402:
                ch = "no-pay"
            else:
                ch = "x402"

            print(
                f"[access] {scope.get('method', '?')} {url} {status} "
                f"{ms:.0f}ms from={ip} ch={ch} ua={ua}",
                file=sys.stderr,
            )


app.add_middleware(PaymentWithRapidAPIBypass, routes=routes, server=server)
app.add_middleware(AccessLogMiddleware)


# HEAD guard: prevent health-check bots from triggering free API execution.
# FastAPI routes HEAD→GET by default, so HEAD bypasses x402 payment.
@app.middleware("http")
async def head_guard(request: Request, call_next):
    if request.method == "HEAD" and request.url.path in ("/validate", "/disposable", "/mx"):
        return Response(status_code=402)
    return await call_next(request)


# --- x402 Discovery ---
@app.get("/.well-known/x402")
async def x402_discovery(request: Request):
    """x402 discovery document — lists all paid endpoints for auto-cataloging."""
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return {
        "version": 1,
        "resources": [
            f"{origin}/mailcheck/validate",
            f"{origin}/mailcheck/disposable",
            f"{origin}/mailcheck/mx",
        ],
        "instructions": (
            "# Mailcheck API\n\n"
            "6 email validation checks in one call. No API keys, no multiple service integrations.\n\n"
            "## Why use this?\n"
            "- Individual checks require separate libraries (DNS, disposable blocklist, typo detection)\n"
            "- This API combines all 6 checks with a single confidence score\n"
            "- POST method protects email PII from access logs\n\n"
            "## Endpoints\n"
            "- `POST /mailcheck/validate` — Full email validation (JSON body: {\"email\": \"user@example.com\"})\n"
            "- `GET /mailcheck/disposable?domain=example.com` — Disposable domain check\n"
            "- `GET /mailcheck/mx?domain=example.com` — MX record lookup\n\n"
            "## Pricing\n"
            "All endpoints: $0.01/request (USDC on Base)\n"
        ),
    }


# --- RapidAPI OpenAPI spec (optimized for import) ---
@app.get("/rapidapi.json", include_in_schema=False)
async def rapidapi_spec():
    import json
    from pathlib import Path
    from fastapi.responses import JSONResponse
    spec = json.loads(Path(__file__).parent.joinpath("rapidapi-openapi.json").read_text())
    return JSONResponse(spec)


# --- Routes ---
@app.get("/health")
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="mailcheck", network=NETWORK)


@app.get("/llms.txt")
async def llms_txt():
    """Machine-readable API description for LLM agents."""
    content = """\
# Mailcheck API — 6 Email Checks in One Call

> Validate email addresses with syntax, MX, disposable (5,000+ domains), free provider, role-based, and typo detection — all in one call. No API keys, no library setup.

## API Base URL

https://mailcheck.hugen.tokyo

## Authentication

x402 micropayments (USDC on Base, eip155:8453).

## Why This Instead of Doing It Yourself?

Individual checks require separate libraries — DNS resolution, a disposable domain blocklist (5,000+ entries, needs weekly updates), typo detection, and free provider lists. This API combines all 6 checks into one call with a single 0-1 confidence score. POST method protects email PII from access logs.

## Endpoints — $0.01/request

- POST /mailcheck/validate — Full email validation (syntax + MX + disposable + free + role-based + typo suggestion). Send JSON body: {"email": "user@example.com"}
- GET /mailcheck/disposable?domain={domain} — Check if a domain is a known disposable/temporary email provider
- GET /mailcheck/mx?domain={domain} — MX record lookup and validation
"""
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.post("/mailcheck/validate")
async def validate_email_endpoint(request: Request) -> ValidateResponse:
    """Accept JSON body, form data, or query parameter — robust against
    RapidAPI Playground which may send non-JSON Content-Type."""
    email = None
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        data = await request.json()
        email = data.get("email") if isinstance(data, dict) else None
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        email = form.get("email")
    else:
        # Try JSON first (RapidAPI sometimes omits Content-Type)
        raw = await request.body()
        if raw:
            import json as _json
            try:
                data = _json.loads(raw)
                email = data.get("email") if isinstance(data, dict) else None
            except (ValueError, AttributeError):
                email = raw.decode(errors="replace").strip()

    # Fallback: query parameter
    if not email:
        email = request.query_params.get("email")

    if not email:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": ["body", "email"], "msg": "field required", "type": "value_error.missing"}]},
        )

    # Validate length (RFC 5321: max 254)
    if len(email) > 254:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": ["body", "email"], "msg": "max length is 254", "type": "value_error"}]},
        )

    result = validate_email_full(email)
    return ValidateResponse(**result)


@app.get("/mailcheck/disposable")
async def disposable_check(
    domain: str = Query(max_length=253, description="Domain to check (e.g., guerrillamail.com)"),
) -> DisposableResponse:
    return DisposableResponse(
        domain=domain,
        is_disposable=check_disposable(domain),
    )


@app.get("/mailcheck/mx")
async def mx_check(
    domain: str = Query(max_length=253, description="Domain to look up MX records for (e.g., gmail.com)"),
) -> MxResponse:
    result = check_mx(domain)
    return MxResponse(
        domain=domain,
        mx_found=result["mx_found"],
        mx_records=result["mx_records"],
    )


# --- Legacy routes (deprecated, 410 Gone with Link header per RFC 8594) ---
from fastapi.responses import JSONResponse as _JSONResponseLegacy


def _legacy_gone(new_path: str):
    return _JSONResponseLegacy(
        status_code=410,
        headers={"Link": f'<{new_path}>; rel="successor-version"'},
        content={
            "error": "Endpoint moved to prefixed path",
            "new_path": new_path,
            "message": "Please use the prefixed path. This legacy endpoint will be removed.",
        },
    )


@app.post("/validate", deprecated=True, include_in_schema=False)
async def legacy_validate_post():
    return _legacy_gone("https://mailcheck.hugen.tokyo/mailcheck/validate")


@app.get("/validate", deprecated=True, include_in_schema=False)
async def legacy_validate_get():
    return _legacy_gone("https://mailcheck.hugen.tokyo/mailcheck/validate")


@app.get("/disposable", deprecated=True, include_in_schema=False)
async def legacy_disposable():
    return _legacy_gone("https://mailcheck.hugen.tokyo/mailcheck/disposable")


@app.get("/mx", deprecated=True, include_in_schema=False)
async def legacy_mx():
    return _legacy_gone("https://mailcheck.hugen.tokyo/mailcheck/mx")


if __name__ == "__main__":
    if not os.getenv("EVM_ADDRESS"):
        raise SystemExit("ERROR: EVM_ADDRESS not set. Configure .env before starting the server.")

    import asyncio
    import asyncio.runners

    # cdp-sdk → web3 → nest_asyncio patches asyncio.run without loop_factory support.
    # Restore the stdlib version BEFORE importing uvicorn (which captures it at import time).
    asyncio.run = asyncio.runners.run

    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
