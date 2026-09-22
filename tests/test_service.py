"""HTTP 服务测试：状态码与可区分错误码。"""


import pytest
from fastapi.testclient import TestClient

from app.demo import build_demo_vault
from app.service import create_app


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def client():
    clk = Clock(1000.0)
    vault = build_demo_vault(clock=clk)
    app = create_app(vault)
    app.state.clock = clk
    return TestClient(app), clk


def _feed(c, prices, symbol="BTC", ts=999.0, now=1000.0):
    sources = ("binance", "coinbase", "kraken")
    body = {
        "quotes": [
            {"source": s, "price": str(p), "timestamp": ts, "observed_at": now}
            for s, p in zip(sources, prices)
        ]
    }
    return c.post(f"/assets/{symbol}/prices", json=body)


def test_health(client):
    c, _ = client
    assert c.get("/healthz").json() == {"status": "ok"}


def test_price_ingestion_rejections_distinguishable(client):
    c, _ = client
    r = _feed(c, ["100", "0", "200"])  # binance 有效, coinbase 非正, kraken 离群
    data = r.json()
    # 有效来源=1 < 要求2 => 不可用，且两类拒绝原因可区分
    assert data["usable"] is False and data["failure_code"] == "P005"
    reasons = {x["source"]: x["reason"] for x in data["rejections"]}
    assert reasons["coinbase"] == "non_positive"
    assert reasons["kraken"] == "outlier"


def test_full_flow_over_http(client):
    c, _clk = client
    assert _feed(c, ["100", "100", "100"]).json()["usable"] is True

    r = c.post("/assets/BTC/deposit", json={"user": "alice", "shares": "2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shares"] == "2.00000000" and body["price"] == "100.00000000"

    r = c.post("/assets/BTC/withdraw", json={"user": "alice", "shares": "0.5"})
    assert r.status_code == 200
    assert r.json()["cash_delta"] == "-50.0000000000000000"


def test_error_codes_over_http(client):
    c, _clk = client

    # 未配置资产 P000 -> 404
    r = c.post("/assets/DOGE/deposit", json={"user": "u", "shares": "1"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "P000"

    # 无有效估值 P006 -> 409
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "P006"

    _feed(c, ["100", "100", "100"])
    # 份额不足 L001 -> 400
    r = c.post("/assets/BTC/withdraw", json={"user": "nobody", "shares": "1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "L001"

    # 非法金额 L002 -> 400
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "0"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "L002"
    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "not-a-number"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "L002"

    # 无法解析的报价 -> 作为非正数拒绝，不产生 500（独立用例见下）

    # 熔断未开时恢复 -> C002
    r = c.post("/assets/BTC/recover", json={"operator": "risk_admin"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "C002"


def test_malformed_inputs_never_500(client):
    c, _ = client

    r = c.post("/assets/BTC/deposit", json={"user": "u", "shares": "not-a-number"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "L002"

    r = c.post("/assets/BTC/prices", json={"quotes": [
        {"source": "binance", "price": "abc", "timestamp": 999.0, "observed_at": 1000.0},
        {"source": "coinbase", "price": "100", "timestamp": 999.0, "observed_at": 1000.0},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["usable"] is False and data["failure_code"] == "P005"
    reasons = {x["source"]: x["reason"] for x in data["rejections"]}
    # 无法解析的报价在 HTTP 边界被安全转为缺失，绝不产生 500
    assert reasons["binance"] == "missing"


def test_breaker_and_authorized_recovery_over_http(client):
    c, clk = client
    _feed(c, ["100", "100", "100"])
    c.post("/assets/BTC/deposit", json={"user": "alice", "shares": "1"})

    # 价格跳涨 20% 触发熔断
    clk.t = 1100.0
    _feed(c, ["120", "120", "120"], ts=1099.0, now=1100.0)
    st = c.get("/assets/BTC/status").json()
    assert st["breaker"] == "open" and st["triggers"][0]["kind"] == "volatility"

    # 存款被拒 C001
    r = c.post("/assets/BTC/deposit", json={"user": "bob", "shares": "1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "C001"

    # 越权 C004
    r = c.post("/assets/BTC/recover", json={"operator": "eve"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "C004"

    # 3 次稳定有效估值后授权恢复
    for t in (1200.0, 1300.0, 1400.0):
        clk.t = t
        _feed(c, ["100", "100", "100"], ts=t - 1, now=t)
    r = c.post("/assets/BTC/recover", json={"operator": "risk_admin"})
    assert r.status_code == 200, r.text
    assert c.get("/assets/BTC/status").json()["breaker"] == "closed"
