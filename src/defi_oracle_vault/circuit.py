"""风控熔断与受控恢复。

触发（任一满足，且当前 CLOSED 时打开，触发依据完整保留）：
1. volatility: 观察窗口内有效聚合价 (max-min)/min 严格超过 volatility_threshold；
2. prolonged_unavailable: 价格持续不可用时间严格超过 unavailable_grace_seconds。

恢复（不静默自动恢复，必须人工介入，且同时满足）：
1. operator 在授权名单内，否则 UnauthorizedRecovery；
2. 当前能产出有效聚合价（有效来源充足）；
3. 最近一个观察窗口（含当前价）内波动不超过阈值。
条件不满足时逐条列出原因 -> RecoveryConditionsNotMet。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext

from .errors import (
    CircuitOpen,
    RecoveryConditionsNotMet,
    UnauthorizedRecovery,
)
from .models import (
    AggregationResult,
    AssetConfig,
    CircuitState,
    CircuitStatus,
    TriggerEvidence,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


@dataclass
class _PriceSample:
    ts: float
    price: Decimal


class RiskMonitor:
    """逐资产维护价格样本窗口与熔断状态。"""

    def __init__(self, operators: frozenset[str] | None = None) -> None:
        self._status: dict[str, CircuitStatus] = {}
        self._samples: dict[str, list[_PriceSample]] = {}
        self._unavailable_since: dict[str, float] = {}
        self._history: dict[str, list[TriggerEvidence]] = {}
        self._operators: frozenset[str] = operators if operators is not None else frozenset()
        self._lock = threading.RLock()

    def register(self, config: AssetConfig) -> None:
        """注册资产的熔断跟踪（幂等）。"""
        with self._lock:
            self._status.setdefault(config.asset, CircuitStatus(asset=config.asset))
            self._samples.setdefault(config.asset, [])
            self._history.setdefault(config.asset, [])

    def status(self, asset: str) -> CircuitStatus:
        """获取熔断状态快照（未注册资产返回默认 CLOSED 快照）。"""
        with self._lock:
            st = self._status.get(asset)
            if st is None:
                return CircuitStatus(asset=asset)
            return replace(st)

    def history(self, asset: str) -> list[TriggerEvidence]:
        """该资产历次熔断触发依据（含已恢复的），按时间顺序。"""
        with self._lock:
            return list(self._history.get(asset, ()))

    def ensure_not_open(self, asset: str) -> None:
        """若熔断生效则抛 CircuitOpen。"""
        st = self.status(asset)
        if st.state is CircuitState.OPEN:
            assert st.evidence is not None
            raise CircuitOpen(asset, f"[{st.evidence.code}] {st.evidence.detail}")

    def record_available(self, result: AggregationResult, config: AssetConfig, now: float) -> None:
        """记录一次有效聚合结果，滚动样本并判定波动熔断。"""
        asset = config.asset
        with self._lock:
            self.register(config)
            self._unavailable_since.pop(asset, None)
            samples = self._samples[asset]
            samples.append(_PriceSample(now, result.price))
            self._prune(asset, config, now)

            st = self._status[asset]
            st.last_good_price = result.price
            st.last_good_ts = now

            vol = self._window_volatility(asset, config, now)
            logger.info(
                "risk.available asset=%s price=%s window_volatility=%s state=%s",
                asset,
                result.price,
                vol,
                st.state.value,
            )
            if st.state is CircuitState.CLOSED and vol is not None and vol > config.volatility_threshold:
                self._open(
                    asset,
                    TriggerEvidence(
                        code="volatility",
                        detail=(
                            f"窗口{config.volatility_window_seconds}s内波动 {vol} "
                            f"> 阈值 {config.volatility_threshold} "
                            f"(样本数={len(samples)}, 区间=[{now - config.volatility_window_seconds}, {now}])"
                        ),
                        opened_ts=now,
                    ),
                )

    def record_unavailable(self, config: AssetConfig, now: float) -> None:
        """记录一次价格不可用；持续超宽限则触发熔断。"""
        asset = config.asset
        with self._lock:
            self.register(config)
            since = self._unavailable_since.get(asset)
            if since is None:
                self._unavailable_since[asset] = now
                since = now
            duration = now - since
            st = self._status[asset]
            logger.info(
                "risk.unavailable asset=%s unavailable_for=%ss grace=%ss state=%s",
                asset,
                duration,
                config.unavailable_grace_seconds,
                st.state.value,
            )
            if (
                st.state is CircuitState.CLOSED
                and duration > config.unavailable_grace_seconds
            ):
                self._open(
                    asset,
                    TriggerEvidence(
                        code="prolonged_unavailable",
                        detail=(
                            f"价格持续不可用 {duration}s > 宽限 {config.unavailable_grace_seconds}s"
                        ),
                        opened_ts=now,
                    ),
                )

    def recover(
        self,
        operator: str,
        config: AssetConfig,
        latest: AggregationResult | None,
        now: float,
    ) -> CircuitStatus:
        """受控恢复：先鉴权，再逐条校验条件，成功才转 CLOSED。"""
        with self._lock:
            if operator not in self._operators:
                raise UnauthorizedRecovery(operator)

            self.register(config)
            st = self._status[config.asset]
            if st.state is CircuitState.CLOSED:
                return replace(st)

            reasons: list[str] = []
            if latest is None:
                reasons.append("价格仍不可用：无观测或有效来源不足，不允许恢复")
            else:
                # 当前价临时纳入窗口后再评估波动（不污染样本序列），
                # 保证"最近一个观察窗口"含最新事实
                vol = self._window_volatility(
                    config.asset, config, now, include=_PriceSample(now, latest.price)
                )
                if vol is None:
                    vol = ZERO
                if vol > config.volatility_threshold:
                    reasons.append(
                        f"窗口{config.volatility_window_seconds}s内波动 {vol} "
                        f"> 阈值 {config.volatility_threshold}，尚未平复"
                    )

            if reasons:
                logger.warning(
                    "risk.recover_denied asset=%s operator=%s reasons=%s",
                    config.asset,
                    operator,
                    reasons,
                )
                raise RecoveryConditionsNotMet(config.asset, reasons)

            if latest is not None:
                self._samples[config.asset].append(_PriceSample(now, latest.price))
                self._prune(config.asset, config, now)
            evidence = st.evidence
            st.state = CircuitState.CLOSED
            st.evidence = None
            st.last_good_price = latest.price if latest else st.last_good_price
            st.last_good_ts = now
            self._unavailable_since.pop(config.asset, None)
            logger.info(
                "risk.recovered asset=%s operator=%s evidence=%s price=%s",
                config.asset,
                operator,
                evidence,
                latest.price if latest else None,
            )
            return replace(st)

    # ---- 内部 ----

    def _open(self, asset: str, evidence: TriggerEvidence) -> None:
        st = self._status[asset]
        st.state = CircuitState.OPEN
        st.evidence = evidence
        self._history[asset].append(evidence)
        logger.warning("risk.OPEN asset=%s evidence=%s", asset, evidence)

    def _prune(self, asset: str, config: AssetConfig, now: float) -> None:
        cutoff = now - config.volatility_window_seconds
        samples = self._samples[asset]
        samples[:] = [s for s in samples if s.ts >= cutoff]

    def _window_volatility(
        self,
        asset: str,
        config: AssetConfig,
        now: float,
        include: _PriceSample | None = None,
    ) -> Decimal | None:
        """窗口内 (max-min)/min；窗口中不足 2 个样本返回 None。

        include 提供一个不落库的临时样本（用于恢复前评估）。
        """
        cutoff = now - config.volatility_window_seconds
        samples = [s for s in self._samples.get(asset, ()) if s.ts >= cutoff]
        if include is not None and include.ts >= cutoff:
            samples.append(include)
        if len(samples) < 2:
            return None
        prices = [s.price for s in samples]
        hi, lo = max(prices), min(prices)
        with localcontext() as ctx:
            ctx.prec = 60
            return (hi - lo) / lo
