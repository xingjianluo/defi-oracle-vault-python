"""本地可运行的 HTTP 服务（FastAPI）。

端点：
- POST /assets/{asset}/configure     配置资产（来源、聚合与风控参数）
- POST /assets/{asset}/quotes        摄入一条报价
- GET  /assets/{asset}/price         当前聚合价与判定依据（含被拒来源）
- GET  /assets/{asset}/circuit       熔断状态与触发依据
- POST /assets/{asset}/recover       授权操作者手动恢复
- POST /assets/{asset}/deposit       按份额存入
- POST /assets/{asset}/deposit-value 按计价价值存入（带找零现金）
- POST /assets/{asset}/withdraw      按份额提取
- GET  /users/{user}/positions/{asset} 持仓估值
- POST /assets/{asset}/cash/withdraw 提走找零现金
- GET  /assets/{asset}/audit         守恒对账
- GET  /assets/{asset}/price/history?ts=           历史时点聚合价（只读回看）
- GET  /users/{user}/positions/{asset}/history?ts= 历史时点持仓回放（只读）
- GET  /users/{user}/positions/{asset}/reconcile?ts= 历史时点与当前持仓对账
- GET  /health                       健康检查

业务错误统一返回 4xx 与明确 error_code，失败语义可区分。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import (
    AssetUnavailable,
    CircuitOpen,
    InsufficientShares,
    InsufficientValidSources,
    InvalidAmount,
    InvalidTimestamp,
    LedgerHistoryPruned,
    NoPriceObservation,
    PriceHistoryPruned,
    RecoveryConditionsNotMet,
    UnauthorizedRecovery,
    UnknownAsset,
    VaultError,
)
from .models import AssetConfig
from .vault import VaultService

logger = logging.getLogger(__name__)


# 业务异常 -> HTTP 状态码
_STATUS: dict[type[Exception], int] = {
    UnknownAsset: 404,
    NoPriceObservation: 503,
    InsufficientValidSources: 503,
    AssetUnavailable: 503,
    CircuitOpen: 409,
    InsufficientShares: 409,
    UnauthorizedRecovery: 403,
    RecoveryConditionsNotMet: 409,
    InvalidAmount: 422,
    InvalidTimestamp: 422,
    LedgerHistoryPruned: 410,
    PriceHistoryPruned: 410,
}


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class ConfigureIn(BaseModel):
    sources: list[str]
    min_valid_sources: int = 1
    max_age_seconds: float = 60.0
    outlier_deviation: str = "0.05"
    volatility_window_seconds: float = 300.0
    volatility_threshold: str = "0.10"
    unavailable_grace_seconds: float = 120.0


class QuoteIn(BaseModel):
    source: str
    value: str
    ts: float | None = None


class DepositIn(BaseModel):
    user: str
    shares: str
    ts: float | None = None


class DepositValueIn(BaseModel):
    user: str
    value: str
    ts: float | None = None


class WithdrawIn(BaseModel):
    user: str
    shares: str
    ts: float | None = None


class RecoverIn(BaseModel):
    operator: str
    ts: float | None = None


class CashIn(BaseModel):
    user: str
    amount: str


def create_app(service: VaultService | None = None) -> FastAPI:
    """构造 FastAPI 应用；可注入自定义 VaultService（如预置授权操作者）。"""
    svc = service or VaultService()
    app = FastAPI(title="本地资产库(DeFi Oracle Vault)模拟服务", version="0.1.0")

    @app.exception_handler(VaultError)
    async def _on_vault_error(_request, exc: VaultError):
        status = _STATUS.get(type(exc), 400)
        for cls, code in _STATUS.items():
            if isinstance(exc, cls):
                status = code
                break
        payload: dict = {"error_code": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, (InsufficientValidSources, PriceHistoryPruned)):
            payload["rejected"] = [
                {"source": r.source, "reason": type(r).__name__, "detail": r.reason}
                for r in exc.rejected
            ]
        if isinstance(exc, PriceHistoryPruned):
            payload["query_ts"] = exc.ts
            payload["horizon"] = exc.horizon
            payload["pruned_sources"] = list(exc.pruned_sources)
            payload["valid"] = exc.valid
            payload["required"] = exc.required
        if isinstance(exc, CircuitOpen):
            payload["trigger"] = exc.trigger
        if isinstance(exc, RecoveryConditionsNotMet):
            payload["unmet_reasons"] = exc.reasons
        return JSONResponse(status_code=status, content=payload)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/assets/{asset}/configure")
    def configure(asset: str, body: ConfigureIn):
        config = AssetConfig(
            asset=asset,
            sources=tuple(body.sources),
            min_valid_sources=body.min_valid_sources,
            max_age_seconds=body.max_age_seconds,
            outlier_deviation=Decimal(body.outlier_deviation),
            volatility_window_seconds=body.volatility_window_seconds,
            volatility_threshold=Decimal(body.volatility_threshold),
            unavailable_grace_seconds=body.unavailable_grace_seconds,
        )
        svc.configure(config)
        return {"asset": asset, "configured": True, "sources": body.sources}

    @app.post("/assets/{asset}/quotes")
    def ingest(asset: str, body: QuoteIn):
        svc.ingest_quote(asset, body.source, Decimal(body.value), body.ts)
        return {"ingested": True, "asset": asset, "source": body.source, "value": body.value}

    @app.get("/assets/{asset}/price")
    def price(asset: str, ts: float | None = None):
        result = svc.price_of(asset, ts)
        return {
            "asset": asset,
            "price": str(result.price),
            "used_sources": list(result.used_sources),
            "rejected": [
                {"source": r.source, "reason_code": r.reason_code, "detail": r.detail}
                for r in result.rejected
            ],
            "computed_ts": result.computed_ts,
        }

    @app.get("/assets/{asset}/circuit")
    def circuit(asset: str):
        st = svc.risk.status(asset)
        return {
            "asset": asset,
            "state": st.state.value,
            "evidence": None
            if st.evidence is None
            else {
                "code": st.evidence.code,
                "detail": st.evidence.detail,
                "opened_ts": st.evidence.opened_ts,
            },
            "last_good_price": None if st.last_good_price is None else str(st.last_good_price),
            "last_good_ts": st.last_good_ts,
            "history": [
                {"code": e.code, "detail": e.detail, "opened_ts": e.opened_ts}
                for e in svc.risk.history(asset)
            ],
        }

    @app.post("/assets/{asset}/recover")
    def recover(asset: str, body: RecoverIn):
        st = svc.recover_circuit(body.operator, asset, body.ts)
        return {"asset": asset, "state": st.state.value}

    @app.post("/assets/{asset}/deposit")
    def deposit(asset: str, body: DepositIn):
        r = svc.deposit(body.user, asset, Decimal(body.shares), body.ts)
        return _receipt(r)

    @app.post("/assets/{asset}/deposit-value")
    def deposit_value(asset: str, body: DepositValueIn):
        r = svc.deposit_value(body.user, asset, Decimal(body.value), body.ts)
        resp = _receipt(r)
        resp["cash_balance"] = str(svc.cash_balance(body.user, asset))
        return resp

    @app.post("/assets/{asset}/withdraw")
    def withdraw(asset: str, body: WithdrawIn):
        r = svc.withdraw(body.user, asset, Decimal(body.shares), body.ts)
        return _receipt(r)

    @app.post("/assets/{asset}/cash/withdraw")
    def withdraw_cash(asset: str, body: CashIn):
        bal = svc.withdraw_cash(body.user, asset, Decimal(body.amount))
        return {"asset": asset, "user": body.user, "cash_balance": str(bal)}

    @app.get("/users/{user}/positions/{asset}")
    def position(user: str, asset: str, ts: float | None = None):
        v = svc.valuation(user, asset, ts)
        return {
            "user": user,
            "asset": asset,
            "shares": str(v["shares"]),
            "nav": str(v["nav"]),
            "value": str(v["value"]),
            "cash": str(v["cash"]),
            "used_sources": list(v["used_sources"]),
            "rejected": [
                {"source": r.source, "reason_code": r.reason_code, "detail": r.detail}
                for r in v["rejected"]
            ],
            "ts": v["ts"],
        }

    @app.get("/assets/{asset}/price/history")
    def price_history(asset: str, ts: float):
        result = svc.price_of_at(asset, ts)
        return {
            "asset": asset,
            "price": str(result.price),
            "used_sources": list(result.used_sources),
            "rejected": [
                {"source": r.source, "reason_code": r.reason_code, "detail": r.detail}
                for r in result.rejected
            ],
            "computed_ts": result.computed_ts,
        }

    @app.get("/users/{user}/positions/{asset}/history")
    def position_history(user: str, asset: str, ts: float):
        v = svc.position_at(user, asset, ts)
        return {
            "user": user,
            "asset": asset,
            "ts": v["ts"],
            "shares": str(v["shares"]),
            "has_position": v["has_position"],
            "nav": str(v["nav"]),
            "value": str(v["value"]),
            "used_sources": list(v["used_sources"]),
            "rejected": [
                {"source": r.source, "reason_code": r.reason_code, "detail": r.detail}
                for r in v["rejected"]
            ],
        }

    @app.get("/users/{user}/positions/{asset}/reconcile")
    def position_reconcile(user: str, asset: str, ts: float):
        r = svc.reconcile_position(user, asset, ts)
        return {
            "user": r["user"],
            "asset": r["asset"],
            "ts": r["ts"],
            "shares_at": str(r["shares_at"]),
            "deposited_after": str(r["deposited_after"]),
            "withdrawn_after": str(r["withdrawn_after"]),
            "net_change_after": str(r["net_change_after"]),
            "events_after": r["events_after"],
            "current_shares": str(r["current_shares"]),
            "expected_current": str(r["expected_current"]),
            "matches": r["matches"],
        }

    @app.get("/assets/{asset}/audit")
    def audit(asset: str):
        a = svc.audit(asset)
        return {
            "asset": asset,
            "total_shares": str(a["total_shares"]),
            "user_shares_sum": str(a["user_shares_sum"]),
            "share_dust": str(a["share_dust"]),
            "shares_conserved": a["shares_conserved"],
            "user_cash_sum": str(a["user_cash_sum"]),
            "cash_dust": str(a["cash_dust"]),
            "holders": a["holders"],
        }

    return app


def _receipt(r) -> dict:
    return {
        "kind": r.kind,
        "user": r.user,
        "asset": r.asset,
        "shares": str(r.shares),
        "price": str(r.price),
        "ts": r.ts,
        "balance_after": str(r.balance_after),
        "total_shares_after": str(r.total_shares_after),
    }
