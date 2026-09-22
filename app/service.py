"""FastAPI HTTP 服务：把本地资产库暴露为可调用接口。

路由
----
- POST /assets/{symbol}/prices       摄入多来源报价，返回聚合判定
- GET  /assets/{symbol}/status       资产状态（熔断/价格/份额/触发依据）
- POST /assets/{symbol}/deposit      按份额存入
- POST /assets/{symbol}/withdraw     按份额提取
- POST /assets/{symbol}/recover      授权操作者关闭熔断
- GET  /healthz

所有业务失败都返回统一结构 ``{"error": {"code", "message"}}``，
HTTP 状态码：400 账本/参数类、409 熔断/估值不可用、403 越权、404 未配置。
"""

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .demo import build_demo_vault
from .errors import (
    InvalidAmountError,
    VaultError,
)
from .models import PriceQuote
from .price import safe_decimal
from .vault import Vault

# 错误码 -> HTTP 状态码
_STATUS = {
    "P000": 404,  # 资产未配置
    "P001": 422,
    "P002": 422,
    "P003": 422,
    "P004": 422,
    "P005": 409,  # 有效来源不足
    "P006": 409,  # 当前无有效估值
    "L001": 400,  # 份额不足
    "L002": 400,  # 非法金额
    "C001": 409,  # 熔断中
    "C002": 409,  # 熔断未触发
    "C003": 409,  # 恢复条件不满足
    "C004": 403,  # 越权
}


# ---------------------------------------------------------------- 请求模型 --


class QuoteIn(BaseModel):
    source: str
    price: str | None = None  # 字符串保 Decimal 精度；None=缺失
    timestamp: float
    observed_at: float | None = None


class PricesIn(BaseModel):
    quotes: list[QuoteIn]


class SharesIn(BaseModel):
    user: str
    shares: str


class RecoverIn(BaseModel):
    operator: str


# ---------------------------------------------------------------- 应用 -----


def create_app(vault: Vault | None = None) -> FastAPI:
    app = FastAPI(title="defi-oracle-vault", version="0.1.0")
    app.state.vault = vault or build_demo_vault()

    # ------------------------------------------------------------ 错误处理 --

    @app.exception_handler(VaultError)
    async def _on_vault_error(_: Request, exc: VaultError) -> JSONResponse:
        status = _STATUS.get(exc.code, 400)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    # ------------------------------------------------------------ 路由 -----

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/assets/{symbol}/prices")
    def post_prices(symbol: str, payload: PricesIn) -> dict[str, Any]:
        vault_: Vault = app.state.vault
        now = vault_._clock()
        quotes = []
        for q in payload.quotes:
            price = safe_decimal(q.price)  # 无法解析 -> None -> 缺失/非正路径
            quotes.append(
                PriceQuote(
                    source=q.source,
                    price=price,
                    timestamp=q.timestamp,
                    observed_at=q.observed_at if q.observed_at is not None else now,
                )
            )
        result = vault_.update_prices(symbol, quotes)
        return _aggregation_json(result)

    @app.get("/assets/{symbol}/status")
    def get_status(symbol: str) -> dict[str, Any]:
        st = app.state.vault.status(symbol)
        return {
            "symbol": st.symbol,
            "breaker": st.breaker.value,
            "usable": st.usable,
            "last_price": str(st.last_price) if st.last_price is not None else None,
            "last_observed_at": st.last_observed_at,
            "total_shares": str(st.total_shares),
            "net_cash": str(st.net_cash),
            "triggers": [
                {"kind": t.kind, "at": t.at, "detail": t.detail}
                for t in st.triggers
            ],
        }

    @app.post("/assets/{symbol}/deposit")
    def post_deposit(symbol: str, payload: SharesIn) -> dict[str, Any]:
        receipt = app.state.vault.deposit(
            payload.user, symbol, _parse_shares(payload.shares)
        )
        return _receipt_json(receipt)

    @app.post("/assets/{symbol}/withdraw")
    def post_withdraw(symbol: str, payload: SharesIn) -> dict[str, Any]:
        receipt = app.state.vault.withdraw(
            payload.user, symbol, _parse_shares(payload.shares)
        )
        return _receipt_json(receipt)

    @app.post("/assets/{symbol}/recover")
    def post_recover(symbol: str, payload: RecoverIn) -> dict[str, Any]:
        record = app.state.vault.recover(symbol, payload.operator)
        return {
            "recovered": True,
            "at": record.at,
            "operator": record.operator,
            "previous_trigger": {
                "kind": record.previous_trigger.kind,
                "at": record.previous_trigger.at,
                "detail": record.previous_trigger.detail,
            },
            "consecutive_valid": record.consecutive_valid,
            "detail": record.detail,
        }

    return app


def _parse_shares(raw: str) -> Decimal:
    dec = safe_decimal(raw)
    if dec is None:
        raise InvalidAmountError(f"份额无法解析: {raw!r}")
    return dec


def _aggregation_json(result) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "usable": result.usable,
        "price": str(result.price) if result.price is not None else None,
        "median": str(result.median) if result.median is not None else None,
        "accepted": {k: str(v) for k, v in result.accepted.items()},
        "rejections": [
            {"source": r.source, "reason": r.reason.value, "detail": r.detail}
            for r in result.rejections
        ],
        "failure_code": result.failure_code,
        "observed_at": result.observed_at,
    }


def _receipt_json(receipt) -> dict[str, Any]:
    e = receipt.entry
    return {
        "tx_id": e.tx_id,
        "user": e.user,
        "symbol": e.symbol,
        "kind": e.kind,
        "shares": str(e.shares),
        "price": str(receipt.price),
        "cash_delta": str(e.cash_delta),
        "at": e.at,
        "breaker_state": receipt.breaker_state.value,
    }


app = create_app()
