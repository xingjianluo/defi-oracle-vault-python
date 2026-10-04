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
    CircuitOpen,
    InsufficientShares,
    InvalidAmount,
    InvalidTimestamp,
    UnknownAsset,
)
from .ledger import Ledger
from .models import AggregationResult, AssetConfig, HistoryRetention, OperationReceipt, Quote
from .pricing import PriceAggregator, PriceBook

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


class VaultService:
    """对外门面：配置资产、摄入价格、存取份额、查询估值、恢复熔断、历史回看。"""

    def __init__(
        self,
        share_scale: int = 18,
        cash_scale: int = 8,
        operators: frozenset[str] | None = None,
        quote_retention: HistoryRetention | None = None,
        ledger_history_max_events: int | None = None,
    ) -> None:
        self.book = PriceBook(retention=quote_retention)
        self.aggregator = PriceAggregator(self.book)
        self.ledger = Ledger(share_scale=share_scale, history_max_events=ledger_history_max_events)
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
        if not math.isfinite(quote_ts):
            raise InvalidAmount(f"报价时间戳必须有限: {ts}")
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

    # ---- 历史回看（只读：不改熔断状态、不录观察样本、不动账本与现金） ----

    def price_of_at(
        self, asset: str, ts: float, now: float | None = None
    ) -> AggregationResult:
        """回看过去某时点的有效聚合价（只读、可重复）。

        - 只用 ts 时点（含）之前已存在的报价，绝不使用更晚的报价；
        - 判定口径与当前聚合完全一致（过期/非正/离群/最少来源数）；
        - 该时点无报价(missing)与报价已被清理(pruned)是可区分的拒绝原因；
        - 有效来源不足抛 InsufficientValidSources，从不观测抛 NoPriceObservation，
          绝不拿更早或更晚的价格顶替；
        - ts 非法（非有限/负数/晚于 now）抛 InvalidTimestamp。
        """
        config = self.get_config(asset)
        self._check_query_ts(ts, time.monotonic() if now is None else now)
        result = self.aggregator.evaluate_as_of(config, float(ts))
        logger.info(
            "price_of_at asset=%s ts=%s price=%s used=%s",
            asset, ts, result.price, result.used_sources,
        )
        return result

    def position_at(
        self, user: str, asset: str, ts: float, now: float | None = None
    ) -> dict:
        """回放某用户在过去某时点的持仓：份额余额 + 按当时聚合价折算的市值。

        只读、可重复：不改动账本、现金、熔断状态与观察样本。
        - 资产在该时点不可用：抛 NoPriceObservation / InsufficientValidSources
          （与"用户当时还没持仓"是可区分的两种结论）；
        - 用户当时还没持仓：has_position=False 且 shares=0（不硬凑数字）；
        - 账本历史被压实：抛 LedgerHistoryPruned。
        """
        config = self.get_config(asset)
        self._check_query_ts(ts, time.monotonic() if now is None else now)
        with self._lock:
            result = self.aggregator.evaluate_as_of(config, float(ts))
            shares = self.ledger.balance_at(asset, user, float(ts))
            has_position = self.ledger.has_position_at(asset, user, float(ts))
            with localcontext() as ctx:
                ctx.prec = 60
                value = shares * result.price
            snapshot = {
                "user": user,
                "asset": asset,
                "ts": float(ts),
                "shares": shares,
                "has_position": has_position,
                "nav": result.price,
                "value": value,
                "used_sources": result.used_sources,
                "rejected": result.rejected,
            }
        logger.info(
            "position_at user=%s asset=%s ts=%s shares=%s has_position=%s nav=%s value=%s",
            user, asset, ts, shares, has_position, result.price, value,
        )
        return snapshot

    def reconcile_position(
        self, user: str, asset: str, ts: float, now: float | None = None
    ) -> dict:
        """历史时点与当前持仓对账（只读）。

        恒等式：当前份额 == 历史时点份额 + 之后存入合计 - 之后提取合计。
        返回净变动明细，差额可逐笔解释；matches=False 表示账本内部不一致。
        """
        self.get_config(asset)
        self._check_query_ts(ts, time.monotonic() if now is None else now)
        with self._lock:
            shares_at = self.ledger.balance_at(asset, user, float(ts))
            deposited, withdrawn, events = self.ledger.net_change_after(
                asset, user, float(ts)
            )
            current = self.ledger.balance_of(asset, user)
        expected = shares_at + deposited - withdrawn
        report = {
            "user": user,
            "asset": asset,
            "ts": float(ts),
            "shares_at": shares_at,
            "deposited_after": deposited,
            "withdrawn_after": withdrawn,
            "net_change_after": deposited - withdrawn,
            "events_after": events,
            "current_shares": current,
            "expected_current": expected,
            "matches": current == expected,
        }
        logger.info("reconcile_position %s", report)
        return report

    @staticmethod
    def _check_query_ts(ts: float, now: float) -> None:
        """校验历史查询时点：必须有限、非负且不晚于参考时刻 now。"""
        try:
            value = float(ts)
        except (TypeError, ValueError):
            raise InvalidTimestamp(ts, "时间点必须是数值") from None
        if not math.isfinite(value):
            raise InvalidTimestamp(ts, "时间点必须有限")
        if value < 0:
            raise InvalidTimestamp(ts, "时间点不能为负")
        if value > now:
            raise InvalidTimestamp(ts, f"时间点晚于当前参考时刻 {now}（不允许查询未来）")

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
            self.ledger.deposit(asset, user, shares, ts=ts, kind="deposit")
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

            self.ledger.deposit(asset, user, shares, ts=ts, kind="deposit_value")
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
            self.ledger.withdraw(asset, user, shares, ts=ts, kind="withdraw")
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
