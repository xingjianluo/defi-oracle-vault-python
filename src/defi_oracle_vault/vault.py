"""资产库服务：基于聚合估值的份额存取。

估值约定：
- 1 份额对应 1 单位资产；每份额净值 NAV = 聚合中位数价格。
- deposit(shares)：按份额存入，数量即份额（调用方自行按价格换算）。
- deposit_value(value)：按计价货币价值存入，
  shares = floor(value / NAV, share_scale)；
  找零 change = value - shares*NAV（>=0，以 cash_scale 截断后计入用户现金余额，
  不足一位现金精度的残差计入资产级 cash_dust）。资产不被凭空产生或吞掉。
- withdraw(shares)：按份额扣减；NAV*份额仅用于估值展示与对账。
- 熔断 OPEN 时：存入抛 CircuitOpen（暂停新增敞口）；
  提取仍要求当前价格有效，价格不可用抛 AssetUnavailable（不按陈旧价成交），
  熔断本身不阻止提取。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from decimal import ROUND_DOWN, Decimal, localcontext

from .circuit import RiskMonitor
from .errors import (
    AssetUnavailable,
    InsufficientShares,
    InvalidAmount,
    InvalidHistoryQuery,
    NoPositionAtTime,
    UnknownAsset,
)
from .ledger import Ledger
from .models import (
    AggregationResult,
    AssetConfig,
    HistoricalAggregationResult,
    HistoricalPosition,
    OperationReceipt,
    PositionReconciliation,
    Quote,
)
from .pricing import PriceAggregator, PriceBook

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


class VaultService:
    """对外门面：配置资产、摄入价格、存取份额、查询估值、恢复熔断。"""

    def __init__(
        self,
        share_scale: int = 18,
        cash_scale: int = 8,
        operators: frozenset[str] | None = None,
        quote_retention_seconds: float | None = 3600.0,
        max_quotes_per_source: int | None = 1000,
    ) -> None:
        self.book = PriceBook(
            retention_seconds=quote_retention_seconds,
            max_quotes_per_source=max_quotes_per_source,
        )
        self.aggregator = PriceAggregator(self.book)
        self.ledger = Ledger(share_scale=share_scale)
        self.risk = RiskMonitor(operators=operators)
        self._configs: dict[str, AssetConfig] = {}
        # asset -> user -> 计价货币找零余额
        self._cash: dict[str, dict[str, Decimal]] = {}
        # asset -> 现金记账的次级尘埃（不足一位现金精度的残差）
        self._cash_dust: dict[str, Decimal] = {}
        self.share_scale = share_scale
        self.cash_scale = cash_scale
        self._lock = threading.RLock()

    # ---- 配置 & 摄入 ----

    def configure(self, config: AssetConfig) -> None:
        """注册/更新资产配置。"""
        with self._lock:
            if not config.sources:
                raise InvalidAmount(f"资产 {config.asset!r} 必须配置至少一个价格源")
            if config.min_valid_sources < 1:
                raise InvalidAmount("min_valid_sources 必须 >= 1")
            if config.min_valid_sources > len(config.sources):
                raise InvalidAmount("min_valid_sources 不能超过来源数量")
            self._configs[config.asset] = config
            self.ledger.register_asset(config.asset)
            self.risk.register(config)
            self._cash.setdefault(config.asset, {})
            self._cash_dust.setdefault(config.asset, ZERO)

    def get_config(self, asset: str) -> AssetConfig:
        try:
            return self._configs[asset]
        except KeyError:
            raise UnknownAsset(asset) from None

    def ingest_quote(
        self,
        asset: str,
        source: str,
        value: Decimal | str,
        ts: float | None = None,
    ) -> None:
        """摄入一条报价（ts 缺省取当前单调时间）。"""
        # 先校验资产存在，避免脏报价进入价格簿
        self.get_config(asset)
        price = value if isinstance(value, Decimal) else Decimal(str(value))
        if not price.is_finite():
            raise InvalidAmount(f"报价必须有限: {value}")
        quote_ts = time.monotonic() if ts is None else float(ts)
        self.book.ingest(
            Quote(asset=asset, source=source, value=price, ts=quote_ts)
        )

    # ---- 估值 ----

    def _resolve_price(self, asset: str, now: float) -> AggregationResult:
        """聚合估值并同步风控状态；不可用时抛 AssetUnavailable。"""
        config = self.get_config(asset)
        result = self.aggregator.try_evaluate(config, now)
        if result is None:
            self.risk.record_unavailable(config, now)
            st = self.risk.status(asset)
            reason = "无价格观测" if not self.book.has_any(asset) else "有效来源不足/全部被拒"
            extra = f"；且熔断已生效({st.evidence.code})" if st.evidence else ""
            raise AssetUnavailable(asset, f"{reason}{extra}")
        self.risk.record_available(result, config, now)
        return result

    def price_of(self, asset: str, now: float | None = None) -> AggregationResult:
        """取当前有效聚合价并更新风控样本；不可用抛 AssetUnavailable。"""
        with self._lock:
            return self._resolve_price(asset, time.monotonic() if now is None else now)

    def nav(self, asset: str, now: float | None = None) -> Decimal:
        """每份额净值（= 聚合价格）。"""
        return self.price_of(asset, now).price

    def valuation(self, user: str, asset: str, now: float | None = None) -> dict:
        """用户持仓估值快照（份额、NAV、市值）。"""
        with self._lock:
            result = self._resolve_price(asset, time.monotonic() if now is None else now)
            shares = self.ledger.balance_of(asset, user)
            with localcontext() as ctx:
                ctx.prec = 60
                value = shares * result.price
            return {
                "user": user,
                "asset": asset,
                "shares": shares,
                "nav": result.price,
                "value": value,
                "cash": self._cash.get(asset, {}).get(user, ZERO),
                "used_sources": result.used_sources,
                "rejected": result.rejected,
                "ts": result.computed_ts,
            }

    # ---- 历史回看（纯只读：不改熔断状态、风控样本、账本与现金） ----

    @staticmethod
    def _check_hist_ts(ts: float) -> float:
        t = float(ts)
        if math.isnan(t) or math.isinf(t):
            raise InvalidHistoryQuery(f"历史查询时点必须为有限数, 收到 {ts!r}")
        return t

    def historical_price(
        self, asset: str, ts: float
    ) -> HistoricalAggregationResult:
        """回放 ts 时点的有效聚合价与完整判定依据（used/rejected 逐条可解释）。

        - 只用 ts 当时已经存在的报价（ts' <= ts），绝不拿更晚报价倒推；
        - 只读：不调用风控，不改变熔断状态与波动观察样本，重复查询结果一致；
        - 失败语义可区分：
          * InvalidHistoryQuery：入参非法；
          * HistoryOutOfRetention：时点早于留存下界（数据已淘汰）；
          * NoPriceObservation：留存可覆盖，但当时确实没有任何报价；
          * InsufficientValidSources：当时有效来源不足，明确不可用。
        """
        t = self._check_hist_ts(ts)
        config = self.get_config(asset)
        return self.aggregator.evaluate_at(config, t)

    def historical_position(
        self, user: str, asset: str, ts: float
    ) -> HistoricalPosition:
        """回放用户在 ts 时点的份额余额与按当时聚合价计算的持仓市值。

        纯只读、可重复，不触碰当前账本、现金与熔断。无持仓（ts 前没有任何
        份额变动）抛 NoPositionAtTime；资产当时不可用的异常原样向上传播，
        两种结论互不混淆。
        """
        t = self._check_hist_ts(ts)
        config = self.get_config(asset)
        shares, had_position = self.ledger.balance_at(asset, user, t)
        if not had_position:
            raise NoPositionAtTime(user, asset, t)
        result = self.aggregator.evaluate_at(config, t)
        with localcontext() as ctx:
            ctx.prec = 60
            value = shares * result.price
        logger.info(
            "historical_position user=%s asset=%s ts=%s shares=%s price=%s value=%s used=%s",
            user, asset, t, shares, result.price, value, result.used_sources,
        )
        return HistoricalPosition(
            user=user,
            asset=asset,
            queried_ts=t,
            shares=shares,
            price=result.price,
            value=value,
            used_sources=result.used_sources,
            rejected=result.rejected,
        )

    def reconcile_position(
        self,
        user: str,
        asset: str,
        historical_ts: float,
        current_ts: float | None = None,
    ) -> PositionReconciliation:
        """历史份额与当前份额对账（纯只读）。

        恒等式：current_shares == historical_shares + net_delta，
        net_delta 为 (historical_ts, current_ts] 内该用户全部存取的净变化，
        events 逐笔给出，差额可解释。matches=False 即为对账不平。
        current_ts 缺省取该用户事件流最新时间。
        """
        t = self._check_hist_ts(historical_ts)
        self.get_config(asset)
        rec = self.ledger.reconcile(asset, user, t, current_ts)
        logger.info(
            "reconcile_position user=%s asset=%s hist_ts=%s hist=%s current=%s "
            "net_delta=%s matches=%s events=%d",
            user, asset, t, rec.historical_shares, rec.current_shares,
            rec.net_delta, rec.matches, len(rec.events),
        )
        return rec

    # ---- 份额存取 ----

    def deposit(
        self,
        user: str,
        asset: str,
        shares: Decimal | str,
        now: float | None = None,
    ) -> OperationReceipt:
        """按份额存入（熔断 OPEN 时抛 CircuitOpen，暂停新增敞口）。"""
        shares = self._to_decimal(shares, "份额")
        with self._lock:
            ts = time.monotonic() if now is None else now
            self.risk.ensure_not_open(asset)
            result = self._resolve_price(asset, ts)
            self.ledger.deposit(asset, user, shares, ts)
            return self._receipt("deposit", user, asset, shares, result)

    def deposit_value(
        self,
        user: str,
        asset: str,
        value: Decimal | str,
        now: float | None = None,
    ) -> OperationReceipt:
        """按计价货币价值存入：份额=floor(value/NAV)，找零入用户现金余额。"""
        value = self._to_decimal(value, "价值")
        if value <= 0:
            raise InvalidAmount(f"存入价值必须为正数, 收到 {value}")
        with self._lock:
            ts = time.monotonic() if now is None else now
            self.risk.ensure_not_open(asset)
            result = self._resolve_price(asset, ts)
            nav = result.price

            with localcontext() as ctx:
                ctx.prec = 60
                raw_shares = value / nav
                shares = raw_shares.quantize(
                    Decimal(1).scaleb(-self.share_scale), rounding=ROUND_DOWN
                )
                if shares <= 0:
                    raise InvalidAmount(
                        f"价值 {value} 按 NAV {nav} 折算不足 1 个份额精度单位"
                    )
                change = value - shares * nav
                cash_quantum = Decimal(1).scaleb(-self.cash_scale)
                cash = change.quantize(cash_quantum, rounding=ROUND_DOWN)
                cash_residual = change - cash

            self.ledger.deposit(asset, user, shares, ts)
            cashbook = self._cash[asset]
            cashbook[user] = cashbook.get(user, ZERO) + cash
            self._cash_dust[asset] += cash_residual
            logger.info(
                "deposit_value user=%s asset=%s value=%s nav=%s shares=%s change=%s dust=%s",
                user, asset, value, nav, shares, cash, cash_residual,
            )
            return self._receipt("deposit_value", user, asset, shares, result)

    def withdraw(
        self,
        user: str,
        asset: str,
        shares: Decimal | str,
        now: float | None = None,
    ) -> OperationReceipt:
        """按份额提取。

        - 资产不可用（无有效价）-> AssetUnavailable（不按陈旧价结算估值）
        - 份额不足 -> InsufficientShares
        - 熔断 OPEN 不阻止提取（只暂停新增敞口）
        """
        shares = self._to_decimal(shares, "份额")
        with self._lock:
            ts = time.monotonic() if now is None else now
            result = self._resolve_price(asset, ts)
            # 先估值后扣账：余额不足直接失败，账本无任何变化
            self.ledger.withdraw(asset, user, shares, ts)
            return self._receipt("withdraw", user, asset, shares, result)

    def cash_balance(self, user: str, asset: str) -> Decimal:
        """用户在该资产上的计价货币找零余额。"""
        self.get_config(asset)
        with self._lock:
            return self._cash.get(asset, {}).get(user, ZERO)

    def withdraw_cash(self, user: str, asset: str, amount: Decimal | str) -> Decimal:
        """提走找零现金，返回提走后余额。"""
        amount = self._to_decimal(amount, "金额")
        if amount <= 0:
            raise InvalidAmount(f"提现金额必须为正数, 收到 {amount}")
        self.get_config(asset)
        with self._lock:
            book = self._cash.get(asset, {})
            current = book.get(user, ZERO)
            if current < amount:
                raise InsufficientShares(user, str(amount), str(current))
            current -= amount
            if current == 0:
                book.pop(user, None)
            else:
                book[user] = current
            return current

    # ---- 对账 ----

    def audit(self, asset: str) -> dict:
        """守恒对账：用户份额和 + 尘埃 == 总量；现金总额 = 用户现金 + 现金尘埃。"""
        with self._lock:
            user_sum = self.ledger.user_shares_sum(asset)
            dust = self.ledger.dust(asset)
            total = self.ledger.total_shares(asset)
            cashbook = self._cash.get(asset, {})
            cash_sum = sum(cashbook.values(), ZERO)
            cash_dust = self._cash_dust.get(asset, ZERO)
            return {
                "asset": asset,
                "total_shares": total,
                "user_shares_sum": user_sum,
                "share_dust": dust,
                "shares_conserved": user_sum + dust == total,
                "user_cash_sum": cash_sum,
                "cash_dust": cash_dust,
                "holders": len(self.ledger.holders(asset)),
            }

    # ---- 恢复（权限操作，转发给风控） ----

    def recover_circuit(
        self,
        operator: str,
        asset: str,
        now: float | None = None,
    ):
        """授权操作者在条件满足时手动恢复熔断；不静默自动恢复。"""
        with self._lock:
            ts = time.monotonic() if now is None else now
            config = self.get_config(asset)
            latest = self.aggregator.try_evaluate(config, ts)
            return self.risk.recover(operator, config, latest, ts)

    # ---- 内部 ----

    def _receipt(
        self,
        kind: str,
        user: str,
        asset: str,
        shares: Decimal,
        result: AggregationResult,
    ) -> OperationReceipt:
        return OperationReceipt(
            kind=kind,
            user=user,
            asset=asset,
            shares=shares,
            price=result.price,
            ts=result.computed_ts,
            balance_after=self.ledger.balance_of(asset, user),
            total_shares_after=self.ledger.total_shares(asset),
        )

    @staticmethod
    def _to_decimal(value: Decimal | str, label: str) -> Decimal:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        if not d.is_finite() or d <= 0:
            raise InvalidAmount(f"{label}必须为有限正数, 收到 {value}")
        return d
