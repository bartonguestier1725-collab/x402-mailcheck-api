"""Tests for API endpoints — x402 middleware and route behavior.

x402 PaymentMiddleware returns 402 for all paid routes before the route
handler runs. These tests verify middleware behavior via TestClient
(no external network calls needed for 402 checks).

These require the x402 facilitator client to initialize (network access),
guarded by 10s timeout via pyproject.toml.
"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.timeout(10)


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "mailcheck"
        assert "network" in data


class TestPaidEndpointsReturn402:
    """x402 middleware should return 402 for all paid routes."""

    def test_validate_post(self, client):
        resp = client.post(
            "/validate",
            json={"email": "test@gmail.com"},
        )
        assert resp.status_code == 402

    def test_disposable_get(self, client):
        resp = client.get("/disposable?domain=tempmail.com")
        assert resp.status_code == 402

    def test_mx_get(self, client):
        resp = client.get("/mx?domain=gmail.com")
        assert resp.status_code == 402


class TestHeadGuard:
    """HEAD requests to paid endpoints should return 402."""

    def test_head_validate(self, client):
        resp = client.head("/validate")
        assert resp.status_code == 402

    def test_head_disposable(self, client):
        resp = client.head("/disposable?domain=test.com")
        assert resp.status_code == 402

    def test_head_mx(self, client):
        resp = client.head("/mx?domain=test.com")
        assert resp.status_code == 402

    def test_head_health_allowed(self, client):
        resp = client.head("/health")
        # FastAPI may return 200 or 405 for HEAD on GET-only routes
        assert resp.status_code in (200, 405)


class TestX402Discovery:
    def test_well_known_returns_resources(self, client):
        resp = client.get("/.well-known/x402")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        resources = data["resources"]
        assert len(resources) == 3
        # Check all 3 paid endpoints are listed
        paths = [r.split("/", 3)[-1] for r in resources]
        assert "validate" in paths
        assert "disposable" in paths
        assert "mx" in paths

    def test_well_known_has_instructions(self, client):
        resp = client.get("/.well-known/x402")
        data = resp.json()
        assert "instructions" in data
        assert "POST /validate" in data["instructions"]
