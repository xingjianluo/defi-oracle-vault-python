"""本地份额账本。

守恒约定（任何操作序列后成立）：
    total_shares(asset) == sum(各用户份额) + dust(asset)

- 账本只记录份额；1 份额 = 1 单位资产，NAV 由服务层用聚合价格推导。
- 存入份额增加、提取份额扣减；不允许透支（InsufficientShares）。
- 份额采用定点小数（share_scale 位），非法精度直接拒绝（InvalidAmount），
  服务层的价值->份额换算残差以计价货币找零形式保留，不会进入或吞噬资产。
"""

from __future__ import annotations

import bisect
import threading
from decimal import Decimal

from .errors import InsufficientShares, InvalidAmount, UnknownAsset
from .models import LedgerEvent, PositionReconciliation

ZERO = Decimal(0)


class Ledger:
    """线程安全的内存账本。

    除实时余额外，每笔成功的存/取都会在**仅追加事件流**中留痕
    （带全局自增序号），用于历史份额回放与净变化对账。失败操作不留痕。
    """

    def __init__(self, share_scale: int = 18) -> None:
        self._balances: dict[str, dict[str, Decimal]] = {}
        self._dust: dict[str, Decimal] = {}
        # (asset, user) -> 该用户在该资产上的事件（按 (ts, seq) 有序，仅追加）
        self._events: dict[tuple[str, str], list[LedgerEvent]] = {}
        self._seq = 0
        self._lock = threading.RLock()
        self.share_scale = share_scale

    def register_asset(self, asset: str) -> None:
        """注册资产账本（幂等）。"""
        with self._lock:
            self._balances.setdefault(asset, {})
            self._dust.setdefault(asset, ZERO)

    def is_registered(self, asset: str) -> bool:
        with self._lock:
            return asset in self._balances

    def deposit(self, asset: str, user: str, shares: Decimal, ts: float = 0.0) -> Decimal:
        """存入份额，返回操作后该用户余额。"""
        shares = self._check_share_amount(shares)
        with self._lock:
            if asset not in self._balances:
                raise UnknownAsset(asset)
            balances = self._balances[asset]
            new_balance = balances.get(user, ZERO) + shares
            balances[user] = new_balance
            self._append_event("deposit", asset, user, shares, ts, new_balance)
            return new_balance

    def withdraw(self, asset: str, user: str, shares: Decimal, ts: float = 0.0) -> Decimal:
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
            self._append_event("withdraw", asset, user, -shares, ts, new_balance)
            return new_balance

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

    # ---- 历史回放（只读） ----

    def balance_at(
        self, asset: str, user: str, ts: float
    ) -> tuple[Decimal, bool]:
        """回放用户在 ts 时点（含 ts 当时发生的变动）的份额余额。

        返回 (shares, had_position)：
        - had_position=False 表示 ts 之前没有任何份额变动记录（当时尚无持仓）；
        - had_position=True 但 shares 可能为 0（曾持仓后提光），二者可区分。
        回放只读当前事件流的一致性快照，不改动任何余额。
        """
        with self._lock:
            events = list(self._events.get((asset, user), ()))
        if not events or events[0].ts > ts:
            return ZERO, False
        i = bisect.bisect_right(events, ts, key=lambda e: e.ts) - 1
        return events[i].balance_after, True

    def events_between(
        self, asset: str, user: str, after_ts: float, until_ts: float | None
    ) -> tuple[LedgerEvent, ...]:
        """返回 (after_ts, until_ts] 区间内该用户的份额事件（含两端边界规则）。

        同一时点多笔事件按全局序号确定顺序；until_ts 为 None 表示到最新。
        """
        with self._lock:
            events = list(self._events.get((asset, user), ()))
        lo = bisect.bisect_right(events, after_ts, key=lambda e: e.ts)
        if until_ts is None:
            hi = len(events)
        else:
            hi = bisect.bisect_right(events, until_ts, key=lambda e: e.ts)
        return tuple(events[lo:hi])

    def reconcile(
        self,
        asset: str,
        user: str,
        historical_ts: float,
        current_ts: float | None = None,
    ) -> PositionReconciliation:
        """对账：当前份额应等于历史时点份额 + 其后全部份额变动的净变化。

        current_ts 缺省按事件流中该用户最新事件时间（无事件则用历史时点）。
        """
        with self._lock:
            events_all = list(self._events.get((asset, user), ()))
            current_shares = (
                self._balances[asset].get(user, ZERO)
                if asset in self._balances
                else ZERO
            )
        hist_shares, had = self.balance_at(asset, user, historical_ts)
        if not had:
            hist_shares = ZERO
        after = self.events_between(asset, user, historical_ts, current_ts)
        net_delta = sum((e.shares_delta for e in after), ZERO)
        if current_ts is None:
            current_ts = events_all[-1].ts if events_all else historical_ts
        return PositionReconciliation(
            user=user,
            asset=asset,
            historical_ts=historical_ts,
            current_ts=current_ts,
            historical_shares=hist_shares,
            current_shares=current_shares,
            net_delta=net_delta,
            matches=current_shares == hist_shares + net_delta,
            events=after,
        )

    # ---- 内部 ----

    def _append_event(
        self,
        kind: str,
        asset: str,
        user: str,
        delta: Decimal,
        ts: float,
        balance_after: Decimal,
    ) -> None:
        """追加一笔事件（调用方持锁）。"""
        self._seq += 1
        event = LedgerEvent(
            seq=self._seq,
            ts=ts,
            kind=kind,
            user=user,
            asset=asset,
            shares_delta=delta,
            balance_after=balance_after,
        )
        series = self._events.setdefault((asset, user), [])
        # 同一调用栈内 ts 非降序写入；为稳妥起见按 (ts, seq) 维持有序
        bisect.insort(series, event, key=lambda e: (e.ts, e.seq))

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
