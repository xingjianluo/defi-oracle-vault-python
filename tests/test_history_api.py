"""HTTP 层历史回看端点：成功体与可区分失败码（410/404/503/422）。"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from defi_oracle_vault.api import create_app
from defi_oracle_vault.models import AssetConfig
from defi_oracle_vault.vault import VaultService


@pytest.fixture
def client():
    svc = VaultService(quote_retention_seconds=1000.0)
    svc.configure(
        AssetConfig(
            asset="BTC",
            sources=("s1", "s2"),
            min_valid_sources=2,
            max_age_seconds=100.0,
        )
    )
    return TestClient(create_app(svc)), svc


def feed_and_deposit(svc):
    svc.ingest_quote("BTC", "s1", Decimal(100), 0)
    svc.ingest_quote("BTC", "s2", Decimal(101), 0)
    svc.deposit("alice", "BTC", "10", now=0)


def test_history_price_endpoint(client):
    c, svc = client
    feed_and_deposit(svc)
    r = c.get("/assets/BTC/history/price", params={"ts": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["price"] == "100.5"
    assert set(body["used_sources"]) == {"s1", "s2"}
    assert body["queried_ts"] == 0
    assert body["oldest_retained_ts"] == 0


def test_history_position_endpoint(client):
    c, svc = client
    feed_and_deposit(svc)
    r = c.get("/users/alice/positions/BTC/history", params={"ts": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["shares"] == "10" and body["price"] == "100.5"
    assert body["value"] == "1005.0"


def test_history_reconcile_endpoint(client):
    c, svc = client
    feed_and_deposit(svc)
    svc.withdraw("alice", "BTC", "4", now=10)
    r = c.get(
        "/users/alice/positions/BTC/reconcile", params={"historical_ts": 0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] is True
    assert body["historical_shares"] == "10"
    assert body["current_shares"] == "6"
    assert body["net_delta"] == "-4"
    assert len(body["events"]) == 1 and body["events"][0]["kind"] == "withdraw"


def test_history_error_codes_are_distinct(client):
    c, svc = client
    feed_and_deposit(svc)
    # 无持仓 -> 404 NoPositionAtTime
    r = c.get("/users/bob/positions/BTC/history", params={"ts": 0})
    assert r.status_code == 404 and r.json()["error_code"] == "NoPositionAtTime"
    # 越界 -> 410 HistoryOutOfRetention
    r = c.get("/assets/BTC/history/price", params={"ts": -10**9})
    assert r.status_code == 410 and r.json()["error_code"] == "HistoryOutOfRetention"
    # 留存窗内但当时无报价 -> 503 NoPriceObservation（与 410 可区分）
    from decimal import Decimal

    svc.configure(AssetConfig(asset="ETH", sources=("s1",), min_valid_sources=1))
    svc.ingest_quote("ETH", "s1", Decimal(1), 0)
    r = c.get("/assets/ETH/history/price", params={"ts": -50})
    assert r.status_code == 503 and r.json()["error_code"] == "NoPriceObservation"
    # 未配置资产 -> 404
    r = c.get("/assets/DOGE/history/price", params={"ts": 0})
    assert r.status_code == 404 and r.json()["error_code"] == "UnknownAsset"
    # 非法时间点 -> 422
    r = c.get("/assets/BTC/history/price", params={"ts": "not-a-number"})
    assert r.status_code == 422
    # 缺少必填 ts -> 422（FastAPI 校验）
    assert c.get("/assets/BTC/history/price").status_code == 422
