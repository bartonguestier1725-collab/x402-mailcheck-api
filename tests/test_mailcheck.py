"""Unit tests for mailcheck business logic.

All tests run without network access (DNS tests are marked integration).
"""

from unittest.mock import patch

import dns.resolver
import pytest

from mailcheck import (
    _edit_distance,
    calculate_score,
    check_disposable,
    check_free,
    check_mx,
    check_role_based,
    check_syntax,
    suggest_typo,
    validate_email_full,
)


class TestCheckSyntax:
    def test_valid_email(self):
        result = check_syntax("user@gmail.com")
        assert result["valid"] is True
        assert result["normalized"] == "user@gmail.com"
        assert result["local_part"] == "user"
        assert result["domain"] == "gmail.com"

    def test_email_with_plus(self):
        result = check_syntax("user+tag@gmail.com")
        assert result["valid"] is True
        assert result["local_part"] == "user+tag"

    def test_invalid_no_at(self):
        result = check_syntax("notanemail")
        assert result["valid"] is False
        assert result["error"]

    def test_invalid_empty(self):
        result = check_syntax("")
        assert result["valid"] is False

    def test_invalid_double_at(self):
        result = check_syntax("user@@gmail.com")
        assert result["valid"] is False

    def test_unicode_local_part(self):
        # email-validator supports internationalized local parts
        result = check_syntax("用户@gmail.com")
        assert result["valid"] is True

    def test_case_normalization(self):
        result = check_syntax("User@Gmail.COM")
        assert result["valid"] is True
        assert result["domain"] == "gmail.com"


class TestCheckDisposable:
    def test_known_disposable(self):
        assert check_disposable("guerrillamail.com") is True

    def test_normal_domain(self):
        assert check_disposable("gmail.com") is False

    def test_case_insensitive(self):
        assert check_disposable("GUERRILLAMAIL.COM") is True

    def test_empty_domain(self):
        assert check_disposable("") is False


class TestCheckFree:
    def test_gmail(self):
        assert check_free("gmail.com") is True

    def test_yahoo(self):
        assert check_free("yahoo.com") is True

    def test_corporate_domain(self):
        assert check_free("example.com") is False

    def test_case_insensitive(self):
        assert check_free("Gmail.COM") is True


class TestCheckRoleBased:
    def test_admin(self):
        assert check_role_based("admin") is True

    def test_info(self):
        assert check_role_based("info") is True

    def test_support(self):
        assert check_role_based("support") is True

    def test_postmaster(self):
        assert check_role_based("postmaster") is True

    def test_normal_name(self):
        assert check_role_based("john") is False

    def test_case_insensitive(self):
        assert check_role_based("Admin") is True

    def test_empty(self):
        assert check_role_based("") is False


class TestEditDistance:
    def test_identical(self):
        assert _edit_distance("gmail.com", "gmail.com") == 0

    def test_transposition(self):
        # gmial→gmail is a transposition (2 ops in Levenshtein: delete i, insert i)
        assert _edit_distance("gmial.com", "gmail.com") == 2

    def test_one_char_insertion(self):
        assert _edit_distance("gmal.com", "gmail.com") == 1

    def test_one_char_deletion(self):
        assert _edit_distance("gmal.com", "gmail.com") == 1

    def test_completely_different(self):
        dist = _edit_distance("example.com", "gmail.com")
        assert dist > 3


class TestSuggestTypo:
    def test_exact_match_no_suggestion(self):
        assert suggest_typo("gmail.com") is None

    def test_gmial_suggests_gmail(self):
        assert suggest_typo("gmial.com") == "gmail.com"

    def test_yaho_suggests_yahoo(self):
        assert suggest_typo("yaho.com") == "yahoo.com"

    def test_outlookk_suggests_outlook(self):
        assert suggest_typo("outlookk.com") == "outlook.com"

    def test_completely_different_no_suggestion(self):
        assert suggest_typo("verylongcustomdomain.io") is None

    def test_case_insensitive(self):
        assert suggest_typo("GMIAL.COM") == "gmail.com"


class TestCalculateScore:
    def test_perfect_score(self):
        score = calculate_score(
            syntax_valid=True, mx_found=True,
            is_disposable=False, is_free=False,
            is_role_based=False, has_typo_suggestion=False,
        )
        assert score == 1.0

    def test_invalid_syntax_zero(self):
        score = calculate_score(
            syntax_valid=False, mx_found=True,
            is_disposable=False, is_free=False,
            is_role_based=False, has_typo_suggestion=False,
        )
        assert score == 0.0

    def test_no_mx(self):
        score = calculate_score(
            syntax_valid=True, mx_found=False,
            is_disposable=False, is_free=False,
            is_role_based=False, has_typo_suggestion=False,
        )
        assert score == 0.60

    def test_disposable_penalty(self):
        score = calculate_score(
            syntax_valid=True, mx_found=True,
            is_disposable=True, is_free=False,
            is_role_based=False, has_typo_suggestion=False,
        )
        assert score == 0.85

    def test_role_based_penalty(self):
        score = calculate_score(
            syntax_valid=True, mx_found=True,
            is_disposable=False, is_free=False,
            is_role_based=True, has_typo_suggestion=False,
        )
        assert score == 0.90

    def test_typo_penalty(self):
        score = calculate_score(
            syntax_valid=True, mx_found=True,
            is_disposable=False, is_free=False,
            is_role_based=False, has_typo_suggestion=True,
        )
        assert score == 0.95

    def test_free_email_no_penalty(self):
        score = calculate_score(
            syntax_valid=True, mx_found=True,
            is_disposable=False, is_free=True,
            is_role_based=False, has_typo_suggestion=False,
        )
        assert score == 1.0


class TestValidateEmailFull:
    """Test the full validation pipeline (uses DNS → marked integration for MX)."""

    def test_invalid_syntax(self):
        result = validate_email_full("not-an-email")
        assert result["status"] == "invalid"
        assert result["score"] == 0.0
        assert result["syntax_valid"] is False
        assert result["checks_performed"] == ["syntax"]

    def test_empty_email(self):
        result = validate_email_full("")
        assert result["status"] == "invalid"
        assert result["score"] == 0.0


class TestValidateEmailFullIntegration:
    """Full validation with real DNS lookups."""

    pytestmark = pytest.mark.integration

    def test_gmail_valid(self):
        result = validate_email_full("user@gmail.com")
        assert result["status"] == "valid"
        assert result["syntax_valid"] is True
        assert result["mx_found"] is True
        assert result["is_free"] is True
        assert result["is_disposable"] is False
        assert result["score"] >= 0.9

    def test_role_based_email(self):
        result = validate_email_full("admin@gmail.com")
        assert result["is_role_based"] is True

    def test_typo_domain(self):
        result = validate_email_full("user@gmial.com")
        assert result["did_you_mean"] == "user@gmail.com"


class TestCheckMxDnsErrorDistinction:
    """Verify check_mx distinguishes NXDOMAIN (definitive) from infra errors."""

    def test_nxdomain_has_no_error_key(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
            result = check_mx("nonexistent.example")
        assert result["mx_found"] is False
        assert "error" not in result

    def test_no_answer_has_no_error_key(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.NoAnswer):
            result = check_mx("no-mx.example")
        assert result["mx_found"] is False
        assert "error" not in result

    def test_timeout_has_error_key(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.LifetimeTimeout):
            result = check_mx("slow.example")
        assert result["mx_found"] is False
        assert "error" in result
        assert "LifetimeTimeout" in result["error"]

    def test_no_nameservers_has_error_key(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.NoNameservers):
            result = check_mx("broken.example")
        assert result["mx_found"] is False
        assert "error" in result
        assert "NoNameservers" in result["error"]


class TestValidateEmailFullDnsError:
    """Verify validate_email_full returns 'unknown' status on DNS infra failure."""

    def test_dns_timeout_returns_unknown(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.LifetimeTimeout):
            result = validate_email_full("user@example.com")
        assert result["status"] == "unknown"
        assert result["syntax_valid"] is True
        assert result["mx_found"] is False

    def test_nxdomain_returns_invalid(self):
        with patch("mailcheck.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
            result = validate_email_full("user@nonexistent-domain-xyz.com")
        assert result["status"] == "invalid"


class TestCheckMxIntegration:
    """Real DNS MX lookups."""

    pytestmark = pytest.mark.integration

    def test_gmail_has_mx(self):
        result = check_mx("gmail.com")
        assert result["mx_found"] is True
        assert len(result["mx_records"]) > 0
        assert "error" not in result

    def test_nonexistent_domain(self):
        result = check_mx("this-domain-definitely-does-not-exist-12345.com")
        assert result["mx_found"] is False
        assert result["mx_records"] == []
        assert "error" not in result  # NXDOMAIN is definitive, not an error
