"""HTTP 层：成功路径与可区分的业务错误码。"""

import pytest
from fastapi.testclient import TestClient

from defi_oracle_vault.api import create_app
from defi_oracle_vault.vault import VaultService


@pytest.fixture
def client():
    svc = VaultService(share_scale=6, operators=frozenset({"admin"}))
    return TestClient(create_app(svc)), svc


def configure(c, asset="BTC", **kw):
    body = {
        "sources": ["s1", "s2", "s3"],
        "min_valid_sources": 2,
        "max_age_seconds": 30.0,
        "outlier_deviation": "0.05",
        "volatility_window_seconds": 100.0,
        "volatility_threshold": "0.10",
        "unavailable_grace_seconds": 40.0,
    }
    body.update(kw)
    r = c.post(f"/assets/{asset}/configure", json=body)
    assert r.status_code == 200, r.text


def quote(c, source, value, ts, asset="BTC"):
    r = c.post(f"/assets/{asset}/quotes", json={"source": source, "value": value, "ts": ts})
    assert r.status_code == 200, r.text


def test_health(client):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}


def test_full_flow_and_price_rationale(client):
    c, _ = client
    configure(c)
    quote(c, "s1", "100", 0)
    quote(c, "s2", "101", 0)
    quote(c, "s3", "200", 0)  # 离群
    r = c.get("/assets/BTC/price?ts=0")
    data = r.json()
    assert data["price"] == "100.5"
    assert data["used_sources"] == ["s1", "s2"]
    rejected = {(x["source"], x["reason_code"]) for x in data["rejected"]}
    assert rejected == {("s3", "outlier")}

    r = c.post("/assets/BTC/deposit", json={"user": "alice", "shares": "2", "ts": 0})
    assert r.status_code == 200
    assert r.json()["balance_after"] == "2"

    r = c.post("/assets/BTC/deposit-value", json={"user": "alice", "value": "250", "ts": 0})
    assert r.status_code == 200

    r = c.get("/users/alice/positions/BTC?ts=0")
    assert r.status_code == 200
    assert r.json()["nav"] == "100.5"

    audit = c.get("/assets/BTC/audit").json()
    assert audit["shares_conserved"] is True


def test_error_codes_are_distinguished(client):
    c, _ = client
    configure(c)
    # 未知资产
    assert c.post("/assets/NOPE/deposit", json={"user": "u", "shares": "1", "ts": 0}).status_code == 404
    # 无价格 -> 503 AssetUnavailable
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "1", "ts": 0})
    assert r.status_code == 503
    assert r.json()["error_code"] == "AssetUnavailable"

    quote(c, "s1", "100", 0)
    quote(c, "s2", "101", 0)
    c.post("/assets/BTC/deposit", json={"user": "u", "shares": "1", "ts": 0})
    # 份额不足 -> 409
    r = c.post("/assets/BTC/withdraw", json={"user": "u", "shares": "5", "ts": 0})
    assert r.status_code == 409
    assert r.json()["error_code"] == "InsufficientShares"
    # 非法金额 -> 422
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "0", "ts": 0})
    assert r.status_code == 422
    assert r.json()["error_code"] == "InvalidAmount"


def test_circuit_and_recovery_rbac(client):
    c, _ = client
    configure(c)
    quote(c, "s1", "100", 0)
    quote(c, "s2", "101", 0)
    c.get("/assets/BTC/price?ts=0")
    quote(c, "s1", "120", 50)
    quote(c, "s2", "121", 50)
    c.get("/assets/BTC/price?ts=50")  # 波动熔断

    assert c.get("/assets/BTC/circuit").json()["state"] == "OPEN"
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "1", "ts": 0})
    assert r.status_code == 409
    assert r.json()["error_code"] == "CircuitOpen"
    assert "volatility" in r.json()["trigger"]

    # 越权恢复
    r = c.post("/assets/BTC/recover", json={"operator": "mallory", "ts": 50})
    assert r.status_code == 403
    assert r.json()["error_code"] == "UnauthorizedRecovery"
    # 条件不满足
    r = c.post("/assets/BTC/recover", json={"operator": "admin", "ts": 50})
    assert r.status_code == 409
    assert r.json()["error_code"] == "RecoveryConditionsNotMet"
    assert r.json()["unmet_reasons"]
    # 条件满足后人工恢复
    quote(c, "s1", "100", 200)
    quote(c, "s2", "101", 200)
    r = c.post("/assets/BTC/recover", json={"operator": "admin", "ts": 200})
    assert r.status_code == 200 and r.json()["state"] == "CLOSED"


def test_stale_quote_returns_503_with_rejections(client):
    c, _ = client
    configure(c)
    quote(c, "s1", "100", 0)
    quote(c, "s2", "101", 0)
    r = c.get("/assets/BTC/price?ts=100")
    assert r.status_code == 503
    assert r.json()["error_code"] == "AssetUnavailable"
