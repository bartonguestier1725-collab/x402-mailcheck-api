"""
Email validation business logic.

All functions are pure (no side effects except DNS lookups in check_mx).
Each check can be called independently or combined via validate_email_full().
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import dns.resolver
from disposable_email_domains import blocklist as _disposable_set
from email_validator import EmailNotValidError
from email_validator import validate_email as _validate_syntax
from free_email_domains import whitelist as _free_set

# ── Role-based prefixes ──────────────────────────
_ROLE_PREFIXES_FILE = Path(__file__).parent / "data" / "role_prefixes.txt"
_ROLE_PREFIXES: set[str] = set()
if _ROLE_PREFIXES_FILE.exists():
    _ROLE_PREFIXES = {
        line.strip().lower()
        for line in _ROLE_PREFIXES_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }

# ── Common domains for typo suggestion ───────────
_COMMON_DOMAINS = [
    "gmail.com",
    "yahoo.com",
    "yahoo.co.jp",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "zoho.com",
    "mail.com",
    "gmx.com",
    "yandex.com",
    "live.com",
    "me.com",
    "msn.com",
    "comcast.net",
    "verizon.net",
    "att.net",
]


def check_syntax(email: str) -> dict[str, Any]:
    """Validate email syntax per RFC 5322. Returns normalized email + parts."""
    try:
        result = _validate_syntax(email, check_deliverability=False)
        return {
            "valid": True,
            "normalized": result.normalized,
            "local_part": result.local_part,
            "domain": result.domain,
        }
    except EmailNotValidError as e:
        return {
            "valid": False,
            "error": str(e),
            "normalized": None,
            "local_part": None,
            "domain": None,
        }


def check_disposable(domain: str) -> bool:
    """Check if domain is a known disposable/temporary email provider."""
    return domain.lower() in _disposable_set


def check_mx(domain: str) -> dict[str, Any]:
    """Query DNS MX records for domain. Returns found flag + record list.

    The "error" key is only present when DNS infrastructure fails (timeout,
    no nameservers). Definitive "no MX" (NXDOMAIN, NoAnswer) omits it.
    """
    try:
        answers = dns.resolver.resolve(domain, "MX")
        records = sorted(
            [(r.preference, str(r.exchange).rstrip(".")) for r in answers],
            key=lambda x: x[0],
        )
        return {
            "mx_found": True,
            "mx_records": [rec[1] for rec in records],
        }
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        # Definitive: domain exists but has no MX, or domain doesn't exist
        return {"mx_found": False, "mx_records": []}
    except (
        dns.resolver.NoNameservers,
        dns.resolver.LifetimeTimeout,
        dns.exception.DNSException,
    ) as e:
        # Infrastructure failure: can't determine MX status
        return {"mx_found": False, "mx_records": [], "error": f"dns_error: {type(e).__name__}"}


def check_free(domain: str) -> bool:
    """Check if domain is a known free email provider (gmail, yahoo, etc.)."""
    return domain.lower() in _free_set


def check_role_based(local_part: str) -> bool:
    """Check if local part is a role-based address (admin, info, support, etc.)."""
    return local_part.lower() in _ROLE_PREFIXES


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings."""
    if len(a) < len(b):
        return _edit_distance(b, a)
    if len(b) == 0:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[len(b)]


def suggest_typo(domain: str) -> str | None:
    """Suggest a correction if domain is close to a common email domain."""
    domain_lower = domain.lower()
    if domain_lower in _COMMON_DOMAINS:
        return None  # exact match, no typo

    best_match = None
    best_dist = 3  # max distance threshold
    for common in _COMMON_DOMAINS:
        dist = _edit_distance(domain_lower, common)
        if dist < best_dist:
            best_dist = dist
            best_match = common

    return best_match


def calculate_score(
    syntax_valid: bool,
    mx_found: bool,
    is_disposable: bool,
    is_free: bool,
    is_role_based: bool,
    has_typo_suggestion: bool,
) -> float:
    """Calculate email quality score (0.0 to 1.0).

    Weights:
      syntax_valid:       +0.30 (mandatory base)
      mx_found:           +0.40 (strongest signal)
      not disposable:     +0.15
      not role_based:     +0.10
      no typo suggestion: +0.05
      is_free:            no penalty (free email is valid)
    """
    if not syntax_valid:
        return 0.0

    score = 0.30  # syntax valid
    if mx_found:
        score += 0.40
    if not is_disposable:
        score += 0.15
    if not is_role_based:
        score += 0.10
    if not has_typo_suggestion:
        score += 0.05

    return round(score, 2)


def validate_email_full(email: str) -> dict[str, Any]:
    """Run all checks and return a comprehensive validation result."""
    syntax = check_syntax(email)

    if not syntax["valid"]:
        return {
            "email": email,
            "status": "invalid",
            "score": 0.0,
            "syntax_valid": False,
            "domain": None,
            "mx_found": False,
            "mx_records": [],
            "is_disposable": False,
            "is_free": False,
            "is_role_based": False,
            "did_you_mean": None,
            "checks_performed": ["syntax"],
        }

    domain = syntax["domain"]
    local_part = syntax["local_part"]

    mx = check_mx(domain)
    is_disposable = check_disposable(domain)
    is_free = check_free(domain)
    is_role = check_role_based(local_part)
    typo = suggest_typo(domain)

    score = calculate_score(
        syntax_valid=True,
        mx_found=mx["mx_found"],
        is_disposable=is_disposable,
        is_free=is_free,
        is_role_based=is_role,
        has_typo_suggestion=typo is not None,
    )

    if is_disposable:
        status = "disposable"
    elif "error" in mx:
        status = "unknown"  # DNS infrastructure failure — can't determine validity
    elif not mx["mx_found"]:
        status = "invalid"
    elif score >= 0.7:
        status = "valid"
    else:
        status = "risky"

    did_you_mean = None
    if typo:
        did_you_mean = f"{local_part}@{typo}"

    return {
        "email": syntax["normalized"],
        "status": status,
        "score": score,
        "syntax_valid": True,
        "domain": domain,
        "mx_found": mx["mx_found"],
        "mx_records": mx["mx_records"],
        "is_disposable": is_disposable,
        "is_free": is_free,
        "is_role_based": is_role,
        "did_you_mean": did_you_mean,
        "checks_performed": ["syntax", "mx", "disposable", "free", "role", "typo"],
    }
