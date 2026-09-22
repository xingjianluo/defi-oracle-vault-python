"""风控熔断与受控恢复。

触发条件（任一满足即熔断，OPEN 状态下暂停该资产新增敞口）：

1. volatility（波动超限）：观察窗口 observation_window_seconds 内，
   被接受的聚合价最大值相对最小值的涨幅
   ``(max - min) / min`` 超过 volatility_threshold；
2. prolonged_unavailable（长期不可用）：价格连续不可用的持续时长
   超过 unavailable_grace_seconds（从首次不可用观察算起）。

恢复必须同时满足，且只能由具备权限的操作者显式发起，系统不会静默自愈：

- 熔断当前处于 OPEN；
- 已有 >= recovery_required_valid_observations 次连续可用估值；
- 最近的这些估值落在观察窗口内，且窗口内波动不超过阈值。
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from .errors import (
    CircuitBreakerNotTrippedError,
    RecoveryConditionsNotMetError,
    UnauthorizedRecoveryError,
)
from .models import (
    AggregationResult,
    AssetConfig,
    BreakerState,
    BreakerTrigger,
    RecoveryRecord,
)

logger = logging.getLogger("vault.risk")


@dataclass(frozen=True)
class _PricePoint:
    price: Decimal
    at: float


class CircuitBreaker:
    def __init__(self, config: AssetConfig, operators: frozenset[str], clock=None):
        self.config = config
        self._operators = operators
        self._clock = clock or time.time

        self.state = BreakerState.CLOSED
        # OPEN 期间保留全部触发依据；恢复后清空。
        self.triggers: list[BreakerTrigger] = []
        # 最近一次触发（OPEN 期间用于展示恢复前原因）。
        self.last_trigger: BreakerTrigger | None = None
        self.last_recovery: RecoveryRecord | None = None

        self._points: list[_PricePoint] = []
        self._unavailable_since: float | None = None
        self._consecutive_valid = 0

    # ------------------------------------------------------------ 观察 -----

    def observe(self, result: AggregationResult) -> BreakerTrigger | None:
        now = result.observed_at
        window = self.config.observation_window_seconds

        new_trigger: BreakerTrigger | None = None

        if result.usable and result.price is not None:
            self._unavailable_since = None
            self._consecutive_valid += 1
            self._points.append(_PricePoint(result.price, now))
            # 窗口裁剪（严格早于下界的点丢弃）。
            cutoff = now - window
            self._points = [p for p in self._points if p.at >= cutoff]

            # 熔断后仍持续记录波动，作为恢复条件依据。
            trigger = self._check_volatility(now)
            if trigger is not None and self.state == BreakerState.CLOSED:
                new_trigger = self._trip(trigger)
        else:
            self._consecutive_valid = 0
            if self._unavailable_since is None:
                self._unavailable_since = now
            duration = now - self._unavailable_since
            logger.info(
                "[%s] 估值不可用, 已持续 %.3fs (宽限 %.3fs), code=%s",
                self.config.symbol, duration,
                self.config.unavailable_grace_seconds, result.failure_code,
            )
            if (
                duration > self.config.unavailable_grace_seconds
                and self.state == BreakerState.CLOSED
            ):
                trigger = BreakerTrigger(
                    kind="prolonged_unavailable",
                    at=now,
                    detail=(
                        f"价格连续不可用 {duration:.3f}s > 宽限 "
                        f"{self.config.unavailable_grace_seconds:.3f}s; "
                        f"末次失败码={result.failure_code}"
                    ),
                )
                new_trigger = self._trip(trigger)

        if self.state == BreakerState.OPEN:
            ok, unmet = self.can_recover()
            logger.info(
                "[%s] 熔断中 连续有效=%d 恢复条件满足=%s 未满足=%s",
                self.config.symbol, self._consecutive_valid, ok, unmet,
            )
        return new_trigger

    def _trip(self, trigger: BreakerTrigger) -> BreakerTrigger:
        self.state = BreakerState.OPEN
        self.last_trigger = trigger
        self.triggers.append(trigger)
        # 触发前的价格历史不再参与恢复判定：恢复只看熔断后的稳定观察。
        self._points.clear()
        self._consecutive_valid = 0
        logger.error(
            "[%s] 熔断触发 kind=%s at=%.3f detail=%s",
            self.config.symbol, trigger.kind, trigger.at, trigger.detail,
        )
        return trigger

    def _check_volatility(self, now: float) -> BreakerTrigger | None:
        pts = self._points
        if len(pts) < 2:
            return None
        prices = [p.price for p in pts]
        lo, hi = min(prices), max(prices)
        if lo <= 0:
            return None
        swing = (hi - lo) / lo
        if swing > self.config.volatility_threshold:
            return BreakerTrigger(
                kind="volatility",
                at=now,
                detail=(
                    f"观察窗口({self.config.observation_window_seconds:.0f}s)内 "
                    f"min={lo} max={hi} 波动={swing} > 阈值 "
                    f"{self.config.volatility_threshold}"
                ),
            )
        return None

    # ------------------------------------------------------------ 恢复 -----

    def can_recover(self) -> tuple[bool, list[str]]:
        """返回 (是否满足, 未满足条件的人类可读列表)。"""
        if self.state != BreakerState.OPEN:
            return True, []

        unmet: list[str] = []
        if self._consecutive_valid < self.config.recovery_required_valid_observations:
            unmet.append(
                f"连续有效估值 {self._consecutive_valid} < "
                f"要求 {self.config.recovery_required_valid_observations}"
            )

        now = self._clock()
        cutoff = now - self.config.observation_window_seconds
        in_window = [p for p in self._points if p.at >= cutoff]
        if len(in_window) < self.config.recovery_required_valid_observations:
            unmet.append("观察窗口内有效估值数量不足")
        else:
            lo = min(p.price for p in in_window)
            hi = max(p.price for p in in_window)
            swing = (hi - lo) / lo if lo > 0 else Decimal(0)
            if swing > self.config.volatility_threshold:
                unmet.append(
                    f"窗口内波动 {swing} 仍超过阈值 {self.config.volatility_threshold}"
                )
        return (len(unmet) == 0), unmet

    def recover(self, operator: str) -> RecoveryRecord:
        """受控恢复。权限、熔断状态、恢复条件任一不满足都明确拒绝。"""
        if operator not in self._operators:
            logger.warning("[%s] 恢复被拒: 操作者=%s 越权", self.config.symbol, operator)
            raise UnauthorizedRecoveryError(operator)
        if self.state != BreakerState.OPEN:
            logger.warning("[%s] 恢复被拒: 熔断未触发", self.config.symbol)
            raise CircuitBreakerNotTrippedError(self.config.symbol)

        ok, unmet = self.can_recover()
        if not ok:
            logger.warning("[%s] 恢复被拒: 条件不满足 %s", self.config.symbol, unmet)
            raise RecoveryConditionsNotMetError(unmet)

        now = self._clock()
        record = RecoveryRecord(
            at=now,
            operator=operator,
            previous_trigger=self.last_trigger,  # type: ignore[arg-type]
            consecutive_valid=self._consecutive_valid,
            detail="恢复条件全部满足，操作者确认后关闭熔断",
        )
        logger.warning(
            "[%s] 熔断恢复: 操作者=%s 连续有效=%d 原触发=%s",
            self.config.symbol, operator, self._consecutive_valid,
            self.last_trigger.kind if self.last_trigger else None,
        )
        self.state = BreakerState.CLOSED
        self.triggers.clear()
        self.last_trigger = None
        self._consecutive_valid = 0
        self.last_recovery = record
        return record

    # ------------------------------------------------------------ 查询 -----

    @property
    def is_open(self) -> bool:
        return self.state == BreakerState.OPEN
