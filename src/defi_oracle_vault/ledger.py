"""本地份额账本。

守恒约定（任何操作序列后成立）：
    total_shares(asset) == sum(各用户份额) + dust(asset)

- 账本只记录份额；1 份额 = 1 单位资产，NAV 由服务层用聚合价格推导。
- 存入份额增加、提取份额扣减；不允许透支（InsufficientShares）。
- 份额采用定点小数（share_scale 位），非法精度直接拒绝（InvalidAmount），
  服务层的价值->份额换算残差以计价货币找零形式保留，不会进入或吞噬资产。

历史回放：每次存取追加一条不可变 ShareEvent（含单调序号 seq）。
balance_at / has_position_at / net_change_after 只读回放，可重复。
可选 history_max_events 上限：超出时把最旧事件压实进基线余额并推进
回放边界 horizon；查询 horizon 之前的时点抛 LedgerHistoryPruned，
与"当时无持仓"是可区分的两种结论。默认 None 表示不压实（事件有界性
由部署方根据体量自行配置）。
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from .errors import InsufficientShares, InvalidAmount, LedgerHistoryPruned, UnknownAsset
from .models import ShareEvent

ZERO = Decimal(0)


class Ledger:
    """线程安全的内存账本（含追加式份额事件日志）。"""

    def __init__(self, share_scale: int = 18, history_max_events: int | None = None) -> None:
        self._balances: dict[str, dict[str, Decimal]] = {}
        self._dust: dict[str, Decimal] = {}
        # asset -> 追加式份额事件（按 seq 单调）
        self._events: dict[str, list[ShareEvent]] = {}
        # asset -> user -> 被压实事件的净额基线（键存在即表示该用户曾有持仓）
        self._baselines: dict[str, dict[str, Decimal]] = {}
        # asset -> 回放边界：ts 严格小于该值的查询不可精确回放
        self._horizon: dict[str, float] = {}
        self._seq = 0
        self._lock = threading.RLock()
        self.share_scale = share_scale
        self.history_max_events = history_max_events

    def register_asset(self, asset: str) -> None:
        """注册资产账本（幂等）。"""
        with self._lock:
            self._balances.setdefault(asset, {})
            self._dust.setdefault(asset, ZERO)
            self._events.setdefault(asset, [])
            self._baselines.setdefault(asset, {})

    def is_registered(self, asset: str) -> bool:
        with self._lock:
            return asset in self._balances

    def deposit(
        self, asset: str, user: str, shares: Decimal, ts: float | None = None, kind: str = "deposit"
    ) -> Decimal:
        """存入份额，返回操作后该用户余额。"""
        shares = self._check_share_amount(shares)
        with self._lock:
            if asset not in self._balances:
                raise UnknownAsset(asset)
            balances = self._balances[asset]
            balances[user] = balances.get(user, ZERO) + shares
            self._record(asset, user, shares, ts, kind)
            return balances[user]

    def withdraw(
        self, asset: str, user: str, shares: Decimal, ts: float | None = None, kind: str = "withdraw"
    ) -> Decimal:
        """提取份额，余额不足抛 InsufficientShares；返回操作后余额。"""
        shares = self._check_share_amount(shares)
        with self._lock:
            if asset not in self._balances:
                raise UnknownAsset(asset)
            balances = self._balances[asset]
            current = balances.get(user, ZERO)
            if current < shares:
                raise InsufficientShares(user, str(shares), str(current))
            new_balance = current - shares
            if new_balance == 0:
                balances.pop(user, None)
            else:
                balances[user] = new_balance
            self._record(asset, user, -shares, ts, kind)
            return new_balance

    # ---- 历史回放（只读） ----

    def balance_at(self, asset: str, user: str, ts: float) -> Decimal:
        """回放 ts 时点（含）的份额余额；边界之前被压实抛 LedgerHistoryPruned。"""
        with self._lock:
            self._check_horizon(asset, ts)
            total = self._baselines.get(asset, {}).get(user, ZERO)
            for e in self._events.get(asset, ()):
                if e.user == user and e.ts <= ts:
                    total += e.delta
            return total

    def has_position_at(self, asset: str, user: str, ts: float) -> bool:
        """该用户在 ts 时点（含）之前是否建立过持仓（与余额是否为 0 无关）。"""
        with self._lock:
            if user in self._baselines.get(asset, {}):
                return True
            self._check_horizon(asset, ts)
            return any(e.user == user and e.ts <= ts for e in self._events.get(asset, ()))

    def net_change_after(self, asset: str, user: str, ts: float) -> tuple[Decimal, Decimal, int]:
        """ts 时点之后的净变动：(存入合计, 提取合计, 事件数)，用于对账。

        满足恒等式：balance_of(当前) == balance_at(ts) + 存入合计 - 提取合计。
        """
        with self._lock:
            self._check_horizon(asset, ts)
            deposited = ZERO
            withdrawn = ZERO
            count = 0
            for e in self._events.get(asset, ()):
                if e.user == user and e.ts > ts:
                    count += 1
                    if e.delta > 0:
                        deposited += e.delta
                    else:
                        withdrawn -= e.delta
            return deposited, withdrawn, count

    def _record(
        self, asset: str, user: str, delta: Decimal, ts: float | None, kind: str
    ) -> None:
        event_ts = time.monotonic() if ts is None else float(ts)
        self._seq += 1
        events = self._events[asset]
        events.append(ShareEvent(seq=self._seq, ts=event_ts, user=user, delta=delta, kind=kind))
        self._compact(asset, events)

    def _compact(self, asset: str, events: list[ShareEvent]) -> None:
        """超出 history_max_events 时把最旧事件压实进基线余额。"""
        limit = self.history_max_events
        if limit is None or len(events) <= limit:
            return
        fold = events[: len(events) - limit]
        del events[: len(events) - limit]
        baseline = self._baselines[asset]
        for e in fold:
            baseline[e.user] = baseline.get(e.user, ZERO) + e.delta
        # 回放边界推进到最后一条被压实事件的 ts
        self._horizon[asset] = max(self._horizon.get(asset, 0.0), fold[-1].ts)

    def _check_horizon(self, asset: str, ts: float) -> None:
        horizon = self._horizon.get(asset)
        if horizon is not None and ts < horizon:
            raise LedgerHistoryPruned(asset, ts, horizon)

    def balance_of(self, asset: str, user: str) -> Decimal:
        """查询用户份额（未注册资产或用户返回 0）。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return self._balances[asset].get(user, ZERO)

    def total_shares(self, asset: str) -> Decimal:
        """份额总量（含尘埃）。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return sum(self._balances[asset].values(), ZERO) + self._dust[asset]

    def user_shares_sum(self, asset: str) -> Decimal:
        """所有用户份额之和。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return sum(self._balances[asset].values(), ZERO)

    def dust(self, asset: str) -> Decimal:
        with self._lock:
            return self._dust.get(asset, ZERO)

    def holders(self, asset: str) -> dict[str, Decimal]:
        """返回用户份额快照拷贝。"""
        with self._lock:
            if asset not in self._balances:
                return {}
            return dict(self._balances[asset])

    def _check_share_amount(self, shares: Decimal) -> Decimal:
        """校验份额为有限正数且精度合法（小数位不超过 share_scale）。"""
        if not isinstance(shares, Decimal):
            shares = Decimal(shares)
        if not shares.is_finite() or shares <= 0:
            raise InvalidAmount(f"份额必须为有限正数, 收到 {shares}")
        exponent = shares.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -self.share_scale:
            raise InvalidAmount(
                f"份额精度超出 share_scale={self.share_scale}: {shares}"
            )
        return shares
