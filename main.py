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
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response
from pydantic import BaseModel, Field

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
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
    description="Email validation API for AI agents. "
    "Checks syntax, MX records, disposable domains, free providers, role-based addresses, and typos.",
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

PRICE = "$0.001"
PAYMENT = PaymentOption(scheme="exact", pay_to=EVM_ADDRESS, price=PRICE, network=NETWORK)

routes = {
    "POST /validate": RouteConfig(
        accepts=[PAYMENT],
        mime_type="application/json",
        description="Full email validation: syntax, MX records, disposable check, "
        "free provider detection, role-based address detection, and typo suggestion. "
        "POST method protects email addresses (PII) from appearing in access logs.",
        extensions={
            "bazaar": {
                "info": {
                    "input": {
                        "type": "json",
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
    "GET /disposable": RouteConfig(
        accepts=[PAYMENT],
        mime_type="application/json",
        description="Check if a domain is a known disposable/temporary email provider. "
        "Uses a curated blocklist of 5000+ disposable domains (CC0 licensed).",
        extensions={
            "bazaar": {
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
    "GET /mx": RouteConfig(
        accepts=[PAYMENT],
        mime_type="application/json",
        description="Look up MX (Mail Exchange) DNS records for a domain. "
        "Returns whether mail servers exist and their hostnames sorted by priority.",
        extensions={
            "bazaar": {
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
    """Wraps PaymentMiddlewareASGI with RapidAPI proxy secret bypass."""

    def __init__(self, app_inner, *, routes, server):
        self.raw_app = app_inner
        self.payment_app = PaymentMiddlewareASGI(app_inner, routes=routes, server=server)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and RAPIDAPI_PROXY_SECRET:
            headers = dict(scope.get("headers", []))
            secret = headers.get(b"x-rapidapi-proxy-secret", b"").decode(errors="replace")
            if secret and secret == RAPIDAPI_PROXY_SECRET:
                return await self.raw_app(scope, receive, send)
        return await self.payment_app(scope, receive, send)


app.add_middleware(PaymentWithRapidAPIBypass, routes=routes, server=server)


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
            f"{origin}/validate",
            f"{origin}/disposable",
            f"{origin}/mx",
        ],
        "instructions": (
            "# Mailcheck API\n\n"
            "Email validation for AI agents. Checks syntax, MX records, disposable domains, "
            "free providers, role-based addresses, and typos.\n\n"
            "## Endpoints\n"
            "- `POST /validate` — Full email validation (JSON body: {\"email\": \"user@example.com\"})\n"
            "- `GET /disposable?domain=example.com` — Disposable domain check\n"
            "- `GET /mx?domain=example.com` — MX record lookup\n\n"
            "## Pricing\n"
            "All endpoints: $0.001/request (USDC on Base)\n\n"
            "## Note\n"
            "POST /validate uses POST to protect email addresses (PII) from access logs.\n\n"
            "## Contact\n"
            "GitHub: https://github.com/bartonguestier1725-collab/x402-mailcheck-api"
        ),
    }


# --- Routes ---
@app.get("/health")
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok", service="mailcheck", network=NETWORK)


@app.post("/validate")
async def validate_email_endpoint(body: ValidateRequest) -> ValidateResponse:
    result = validate_email_full(body.email)
    return ValidateResponse(**result)


@app.get("/disposable")
async def disposable_check(
    domain: str = Query(max_length=253, description="Domain to check (e.g., guerrillamail.com)"),
) -> DisposableResponse:
    return DisposableResponse(
        domain=domain,
        is_disposable=check_disposable(domain),
    )


@app.get("/mx")
async def mx_check(
    domain: str = Query(max_length=253, description="Domain to look up MX records for (e.g., gmail.com)"),
) -> MxResponse:
    result = check_mx(domain)
    return MxResponse(
        domain=domain,
        mx_found=result["mx_found"],
        mx_records=result["mx_records"],
    )


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
