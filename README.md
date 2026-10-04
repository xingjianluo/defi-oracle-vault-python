# 本地资产库系统（DeFi Oracle Vault · Python 模拟实现）

一套**本地可运行**的资产库：用本地价格簿模拟多来源预言机，用本地份额账本模拟
资产估值与存取，使**价格异常、并发操作与风控熔断**下的资金结论一致且可解释。

- 全部金额使用 `Decimal` 高精度定点运算，不使用浮点；
- 任何操作序列后账本**份额守恒、投入价值守恒**（见 `/audit` 与测试）；
- 每一类失败都有**独立、可区分的错误类型 / HTTP 错误码**；
- 支持**有界历史回看**：按历史时点复算聚合价、回放持仓并与当前对账，
  只读、可重复、不泄漏未来（超留存/无报价/不可用/无持仓四种结论可区分）；
- 聚合判定、历史回放与熔断触发全程**日志留痕**（价格输入 + 判定依据）。

## 快速开始

```bash
uv sync --group dev

# 命令行演示（完整故事线，无需起服务）
uv run python scripts/demo.py

# 启动 HTTP 服务
uv run uvicorn main:app --reload
# 文档: http://127.0.0.1:8000/docs

# 跑测试（单测实时打印价格输入与判定依据）
uv run pytest
```

## 模块结构

```
src/defi_oracle_vault/
├── errors.py    # 全部可区分失败类型（拒绝原因/账本/熔断/历史回看）
├── models.py    # Quote / AssetConfig / AggregationResult / 历史与对账结构等
├── pricing.py   # PriceBook 有界历史价格簿 + PriceAggregator 实时/时点聚合
├── ledger.py    # Ledger 线程安全份额账本（守恒）+ 仅追加事件流与时点回放
├── circuit.py   # RiskMonitor 波动/不可用熔断 + 受控恢复
├── vault.py     # VaultService 门面：估值、存取、找零、对账、历史回放
└── api.py       # FastAPI HTTP 服务（错误码可区分，含历史端点）
scripts/demo.py  # 端到端演示
tests/           # 聚合/账本/熔断/服务/并发/HTTP/历史回看测试（69 个）
```

## 一、多价格源摄入与聚合

每个资产在 `AssetConfig.sources` 中配置多个来源。每次估值对**所有配置来源**的
最新报价依次做四类校验，任何一类拒绝都**不会静默参与聚合**，拒绝原因彼此可区分：

| reason_code      | 触发条件 | 异常类型 |
|------------------|----------|----------|
| `missing`        | 配置了该来源，但价格簿中没有任何报价 | `QuoteMissing` |
| `expired`        | `now - quote.ts > max_age_seconds`（严格大于） | `QuoteExpired`（含 age / max_age） |
| `non_positive`   | 报价 `<= 0` 或非有限数 | `QuoteNonPositive` |
| `outlier`        | 相对**候选中位数**偏离比例 `abs(p-median)/median > outlier_deviation`（严格大于） | `QuoteOutlier`（含 value/median/deviation/threshold） |
| `unconfigured`   | 价格簿收到该资产未配置的来源 | 内部拒绝，仅记录不参与 |

校验顺序固定为 缺失 → 过期 → 非正数 → 离群，一条报价只会归入其中一类，
`AggregationResult.rejected` 完整保留每条拒绝的来源、原因码与明细。

**聚合方式**：通过全部校验的有效报价取**中位数**（偶数个取中间两值的算术平均）。

**不足即不可用，绝不回退**：

- 有效来源数 `< min_valid_sources` → 抛 `InsufficientValidSources`
  （携带 valid/required 与每条拒绝明细），资产被**明确判定为不可用**；
- 从未摄入过任何报价 → `NoPriceObservation`；
- 系统**不会**沿用过期价格、上次价格或任何默认价格。

## 二、估值与份额存取

约定 **1 份额 = 1 单位资产**，每份额净值 NAV 即聚合中位数价格。

- `deposit(user, asset, shares)`：按份额存入；
- `withdraw(user, asset, shares)`：按份额提取，透支抛 `InsufficientShares`；
- `deposit_value(user, asset, value)`：按计价货币价值存入，
  - `shares = floor(value / NAV)`（向下取整到 `share_scale` 位）；
  - **找零** `change = value − shares × NAV ≥ 0`：
    按 `cash_scale` 位向下截断后记入该用户的**现金余额**（可继续使用或
    `withdraw_cash` 提走）；不足一位现金精度的残差记入资产级 `cash_dust`。
  - 因此恒等式成立：`投入价值 = shares×NAV + 用户现金 + cash_dust`，
    **精度舍入不会凭空产生或吞掉资产**；
  - 折算后不足一个最小份额单位的投入直接拒绝（`InvalidAmount`），不产生任何记账。

份额本身为定点数（默认 18 位小数），超过精度的份额输入直接拒绝。
`audit(asset)` 给出守恒对账：`total_shares == Σ用户份额 + share_dust`。

### 并发

账本与服务层均使用锁（`RLock`）保证原子性，多线程并发存取下：

- 不会透支、不会丢失更新；
- 失败的操作（份额不足/熔断/不可用）不产生任何副作用；
- 总量恒等。见 `tests/test_ledger.py`、`tests/test_concurrency.py`。

## 三、风控熔断与受控恢复

`RiskMonitor` 为每个资产维护滚动价格样本窗口（长度 `volatility_window_seconds`）
与熔断状态机 `CLOSED / OPEN`。

### 触发条件（满足任一即 OPEN，`TriggerEvidence` 完整保留依据）

1. **波动熔断 `volatility`**：窗口内
   `(max(price) − min(price)) / min(price) > volatility_threshold`（严格大于，
   至少 2 个样本）。证据含波动值、阈值、样本数、窗口区间。
2. **长期不可用 `prolonged_unavailable`**：价格持续不可用的时长
   `> unavailable_grace_seconds`（严格大于）。证据含持续时长与宽限。

### 生效语义

- OPEN 时**暂停新增敞口**：`deposit` / `deposit_value` 抛 `CircuitOpen`
  （携带触发依据）；
- OPEN **不阻止提取**，但提取仍要求当前价格有效——价格不可用时抛
  `AssetUnavailable`（绝不按陈旧价结算），与 `CircuitOpen`、
  `InsufficientShares` 均可区分；
- 价格重新可用**不会**自动 CLOSED，必须人工恢复。

### 受控恢复 `recover_circuit(operator, asset)`（无静默自动恢复）

按顺序校验，任一不满足即拒绝：

1. **鉴权**：`operator` 必须在服务初始化的授权名单内，否则
   `UnauthorizedRecovery`（HTTP 403）；
2. **当前价格有效**：能通过完整聚合校验，否则
   `RecoveryConditionsNotMet`（原因：价格仍不可用）；
3. **窗口已平复**：把当前价临时纳入最近一个观察窗口后，波动
   `<= volatility_threshold`，否则 `RecoveryConditionsNotMet`（原因：波动仍超阈值）。

条件不满足时错误中**逐条列出**未满足原因；恢复尝试不会污染价格样本序列。
恢复成功后状态转 CLOSED，但历史触发证据仍可通过 `risk.history(asset)` 审计。

## 四、历史回看（只读、可重复、不泄漏未来）

在不改动现有实时聚合、估值、熔断语义的前提下，新增一层"回看过去"能力：
实时接口的行为、错误类型与日志全部保持兼容。

### 4.1 报价历史留存策略（有界，不会无限占内存）

`PriceBook` 对每个 `(资产, 来源)` 保留一段**有界**报价历史，采用
**时间窗 + 条数双上限，任一触发即淘汰**（均为构造参数，可按来源全局配置）：

| 参数 | 默认 | 含义 |
|------|------|------|
| `quote_retention_seconds` | 3600 秒 | 时间窗：以该资产**最晚摄入报价**的时间为锚，只保留 `ts >= 锚 - retention`（边界保留，严格早于才淘汰） |
| `max_quotes_per_source` | 1000 条 | 每个来源最多保留的报价条数，超出只留最新 N 条 |

- 两者任一设为 `None` 表示该维度不限；同时为 `None` 才是无限留存（仅建议测试用）。
- 时间锚点随摄入单调前进；锚点一旦推进，该资产**所有来源**的越界报价都会被
  立即裁剪，因此乱序迟到、且落在窗外的老报价不会堆积，内存占用有明确上界
  （约为 `来源数 × max_quotes_per_source` 条 + 每来源少量元数据）。
- 同一 `(来源, ts)` 重复摄入按**覆盖**处理（幂等），不额外占条数。
- 账本侧的份额变动为**仅追加事件流**（每笔成功存取一条，带全局自增序号），
  用于份额回放与对账；内存实现下其增长与存取笔数线性相关，生产部署应据此
  控制留存或外置持久化。

### 4.2 三种"查不到"是三种可分辨的结论

历史时点查询 `historical_price(asset, ts)` 的失败彼此严格可区分：

| 结论 | 异常 | HTTP | 含义 |
|------|------|------|------|
| 入参非法 | `InvalidHistoryQuery` | 422 | `ts` 为 `NaN`/`±inf` 等非有限数，不执行任何判定 |
| 已超留存范围 | `HistoryOutOfRetention` | 410 (Gone) | `ts` 早于该资产留存下界，数据可能已被淘汰，**无法复现结论** |
| 当时确无报价 | `NoPriceObservation` | 503 | 留存可覆盖该时点，但那一刻**确实还没有任何报价**（无未来信息可用） |
| 当时不可用 | `InsufficientValidSources` | 503 | 当时有报价但有效来源不足，按实时同一口径明确判不可用 |

**留存下界（horizon）的确定规则**（因此"恰好落在边界"一定能复现）：

- 仅时间窗裁剪、且来源序列**未顶到条数上限**时：下界为
  `watermark - retention`。窗内若有更早报价必然还活着，所以从该边界起
  "当时没有报价"是可确信的结论；边界本身保留、可回答。
- 一旦序列顶到了条数上限：更早的报价可能是被条数挤掉的，无法与"从未存在"
  区分，下界收紧为该来源**最早保留报价**的时刻。
- 资产级下界取各来源下界的最小值；从未摄入过的资产直接 `NoPriceObservation`。

### 4.3 历史聚合价：只用当时已存在的报价

`VaultService.historical_price(asset, ts)` 返回 `HistoricalAggregationResult`
（`price / used_sources / rejected / queried_ts / oldest_retained_ts`）：

- **只用 `ts' <= ts` 的报价**（查询时点恰好等于某条报价时刻时该报价生效），
  绝不拿更晚的报价倒推，绝不泄漏未来；
- 拒绝判定顺序、过期年龄、非正数、离群中位数口径与实时 `price_of`
  **完全一致**，`rejected` 逐条给出来源、原因码与明细；
- 有效来源不足即明确不可用，**不许用更早或更晚的价格顶替**；
- **纯只读**：不调用风控、不改变熔断状态与波动观察样本；同一时点反复查询，
  在该时点已"封口"（写入水位已越过它）后结论永远一致。

> 并发下的准确表述：若查询 `ts` 时写入水位尚未越过 `ts`，迟到但 `ts' <= ts`
> 的报价属于合法历史事实，补录后结论随之补全；一旦水位越过 `ts`，之后再写
> 多少 `> ts` 的新报价，`ts` 的回放结论都不再改变。见
> `tests/test_history_concurrency.py`。

### 4.4 历史持仓回放与对账

- `historical_position(user, asset, ts)` → `HistoricalPosition`：回放该用户
  在 `ts` 的份额余额，并按当时聚合价算市值（`shares / price / value /
  used_sources / rejected`）。纯只读，不动当前账本与现金。
  - 用户在 `ts` 之前没有任何份额变动 → `NoPositionAtTime`（HTTP 404），
    与"资产当时不可用"（503）严格区分，不会硬凑一个数字；
  - 曾持仓后提光：份额为 0 但仍属"有持仓记录"，可正常回放；
  - 资产当时无报价 / 来源不足 → 价格侧异常原样上抛。
- `reconcile_position(user, asset, historical_ts, current_ts=None)` →
  `PositionReconciliation`：与当前持仓对账，恒等式为
  `current_shares == historical_shares + net_delta`，其中 `net_delta` 为
  `(historical_ts, current_ts]` 区间内该用户全部存取的净变化，`events`
  逐笔列出（含序号、时间、方向、变动额、操作后余额），`matches=False`
  即对账不平。同一时点多笔事件按全局序号确定顺序，结论可复现。

### 4.5 并发与性能

- 价格簿、账本、服务层均用锁保护；历史查询在**持锁期间只做一次一致性快照
  拷贝**，随后的校验、离群、中位数计算全部在锁外完成，因此长时间的历史
  判定不会阻塞并发写入，正常实时存取也不会被历史查询拖慢。
- 历史回放全程日志留痕（`evaluate_at` / `historical_position` /
  `reconcile_position`，含喂入价格与 used/rejected 判定依据）。

## 五、失败语义汇总

| 场景 | 异常 | HTTP |
|------|------|------|
| 资产未注册 | `UnknownAsset` | 404 |
| 无任何报价 / 有效来源不足 / 过期导致不可用 | `NoPriceObservation`、`InsufficientValidSources`、`AssetUnavailable` | 503 |
| 历史时点已超留存范围 | `HistoryOutOfRetention` | 410 |
| 历史查询时间点非法（NaN/inf） | `InvalidHistoryQuery` | 422 |
| 用户在该历史时点尚无持仓 | `NoPositionAtTime` | 404 |
| 熔断中新增存款 | `CircuitOpen` | 409 |
| 份额/现金不足 | `InsufficientShares` | 409 |
| 金额非法（非正/超精度/非有限/折算为0） | `InvalidAmount` | 422 |
| 越权恢复 | `UnauthorizedRecovery` | 403 |
| 恢复条件不满足 | `RecoveryConditionsNotMet` | 409 |
| 单条报价拒绝（可在 503 的 `rejected` 中查看） | `QuoteMissing/QuoteExpired/QuoteNonPositive/QuoteOutlier` | — |

## 六、配置项（AssetConfig 与服务级参数）

| 字段 | 默认 | 含义 |
|------|------|------|
| `asset` | — | 资产标识 |
| `sources` | — | 允许参与聚合的来源元组（至少 1 个） |
| `min_valid_sources` | 1 | 判定可用所需的最少有效来源数 |
| `max_age_seconds` | 60 | 报价最大允许年龄，超过即 `expired` |
| `outlier_deviation` | 0.05 | 离群阈值：相对中位数的偏离比例 |
| `volatility_window_seconds` | 300 | 熔断波动观察窗口长度（秒） |
| `volatility_threshold` | 0.10 | 窗口内 `(max-min)/min` 熔断阈值 |
| `unavailable_grace_seconds` | 120 | 价格持续不可用多久后熔断（秒） |

服务级参数（`VaultService` 构造）：

| 参数 | 默认 | 含义 |
|------|------|------|
| `share_scale` | 18 | 份额小数位 |
| `cash_scale` | 8 | 找零现金小数位 |
| `operators` | 空 | 授权恢复操作者集合 |
| `quote_retention_seconds` | 3600 | 报价历史时间窗（秒），`None` 表示不限时间 |
| `max_quotes_per_source` | 1000 | 每来源报价条数上限，`None` 表示不限条数 |

所有阈值比较均为**严格大于**，即恰好等于阈值不会触发，边界行为有单测覆盖。

## 七、HTTP 端点

| 方法/路径 | 说明 |
|-----------|------|
| `POST /assets/{asset}/configure` | 配置来源与风控参数 |
| `POST /assets/{asset}/quotes` | 摄入一条报价 `{source,value,ts?}` |
| `GET  /assets/{asset}/price?ts=` | 实时聚合价 + used/rejected 判定依据 |
| `GET  /assets/{asset}/history/price?ts=` | **历史时点**聚合价（只读，410/503/422 可区分） |
| `GET  /users/{user}/positions/{asset}/history?ts=` | **历史持仓回放**：份额/当时价/市值/判定依据 |
| `GET  /users/{user}/positions/{asset}/reconcile?historical_ts=&current_ts=` | **历史-当前对账**：净变化逐笔 + matches |
| `GET  /assets/{asset}/circuit` | 熔断状态、触发证据、历史 |
| `POST /assets/{asset}/recover` | 授权恢复 `{operator,ts?}` |
| `POST /assets/{asset}/deposit` | 按份额存入 |
| `POST /assets/{asset}/deposit-value` | 按价值存入（返回现金余额） |
| `POST /assets/{asset}/withdraw` | 按份额提取 |
| `POST /assets/{asset}/cash/withdraw` | 提走找零现金 |
| `GET  /users/{user}/positions/{asset}` | 当前持仓估值（份额/NAV/市值） |
| `GET  /assets/{asset}/audit` | 守恒对账 |
| `GET  /health` | 健康检查 |

错误响应体：`{"error_code": 异常类名, "message": 可读说明, ...附加上下文}`。
`HistoryOutOfRetention` 额外带 `queried_ts` 与留存下界 `horizon_ts`。

## 八、本地验证

```bash
uv sync --group dev

# 全量单测（日志实时打印喂入价格与 evaluate/evaluate_at 判定依据）
uv run pytest                                  # 69 个
uv run pytest tests/test_history_pricing.py -q     # 留存边界/不泄漏未来/三种不可用
uv run pytest tests/test_history_position.py -q    # 只读回放/无持仓区分/历史对账
uv run pytest tests/test_history_concurrency.py -q # 并发下已封口历史稳定、账本守恒
uv run pytest tests/test_history_api.py -q         # 历史端点与 410/404/503/422 错误码

# 带实时日志观察一次历史回放（可见 ingest 与 evaluate_at 行）
uv run pytest tests/test_history_pricing.py::test_future_quotes_never_leak_into_past -o log_cli=true -s

# 命令行 / HTTP 验证
uv run python scripts/demo.py
uv run uvicorn main:app --reload
# GET  /assets/BTC/history/price?ts=100
# GET  /users/alice/positions/BTC/history?ts=100
# GET  /users/alice/positions/BTC/reconcile?historical_ts=100
```

代码内最小用法：

```python
from decimal import Decimal
from defi_oracle_vault import VaultService, AssetConfig

svc = VaultService(quote_retention_seconds=3600, max_quotes_per_source=1000)
svc.configure(AssetConfig(asset="BTC", sources=("s1", "s2"), min_valid_sources=2))
svc.ingest_quote("BTC", "s1", Decimal("100"), 0)
svc.ingest_quote("BTC", "s2", Decimal("101"), 0)
svc.deposit("alice", "BTC", Decimal("10"), now=0)

svc.historical_price("BTC", 0)          # 当时中位数 100.5 + used/rejected
svc.historical_position("alice", "BTC", 0)   # 10 份 * 100.5 = 1005
svc.reconcile_position("alice", "BTC", 0)    # 当前份额 == 历史份额 + 净变化
```
