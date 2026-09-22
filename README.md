# 本地资产库系统（DeFi Oracle Vault · Python 模拟实现）

一套**本地可运行**的资产库：用本地价格簿模拟多来源预言机，用本地份额账本模拟
资产估值与存取，使**价格异常、并发操作与风控熔断**下的资金结论一致且可解释。

- 全部金额使用 `Decimal` 高精度定点运算，不使用浮点；
- 任何操作序列后账本**份额守恒、投入价值守恒**（见 `/audit` 与测试）；
- 每一类失败都有**独立、可区分的错误类型 / HTTP 错误码**；
- 聚合判定与熔断触发全程**日志留痕**（价格输入 + 判定依据）。

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
├── errors.py    # 全部可区分失败类型（拒绝原因/账本/熔断）
├── models.py    # Quote / AssetConfig / AggregationResult / 熔断状态等
├── pricing.py   # PriceBook 本地价格簿 + PriceAggregator 多源聚合
├── ledger.py    # Ledger 线程安全份额账本（守恒）
├── circuit.py   # RiskMonitor 波动/不可用熔断 + 受控恢复
├── vault.py     # VaultService 门面：估值、存取、找零、对账
└── api.py       # FastAPI HTTP 服务（错误码可区分）
scripts/demo.py  # 端到端演示
tests/           # 聚合/账本/熔断/服务/并发/HTTP 测试（40 个）
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

## 四、失败语义汇总

| 场景 | 异常 | HTTP |
|------|------|------|
| 资产未注册 | `UnknownAsset` | 404 |
| 无任何报价 / 有效来源不足 / 过期导致不可用 | `NoPriceObservation`、`InsufficientValidSources`、`AssetUnavailable` | 503 |
| 熔断中新增存款 | `CircuitOpen` | 409 |
| 份额/现金不足 | `InsufficientShares` | 409 |
| 金额非法（非正/超精度/非有限/折算为0） | `InvalidAmount` | 422 |
| 越权恢复 | `UnauthorizedRecovery` | 403 |
| 恢复条件不满足 | `RecoveryConditionsNotMet` | 409 |
| 单条报价拒绝（可在 503 的 `rejected` 中查看） | `QuoteMissing/QuoteExpired/QuoteNonPositive/QuoteOutlier` | — |

## 五、配置项（AssetConfig）

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

服务级参数（`VaultService` 构造）：`share_scale=18`（份额小数位）、
`cash_scale=8`（找零现金小数位）、`operators`（授权恢复操作者集合）。

所有阈值比较均为**严格大于**，即恰好等于阈值不会触发，边界行为有单测覆盖。

## 六、HTTP 端点

| 方法/路径 | 说明 |
|-----------|------|
| `POST /assets/{asset}/configure` | 配置来源与风控参数 |
| `POST /assets/{asset}/quotes` | 摄入一条报价 `{source,value,ts?}` |
| `GET  /assets/{asset}/price?ts=` | 聚合价 + used/rejected 判定依据 |
| `GET  /assets/{asset}/circuit` | 熔断状态、触发证据、历史 |
| `POST /assets/{asset}/recover` | 授权恢复 `{operator,ts?}` |
| `POST /assets/{asset}/deposit` | 按份额存入 |
| `POST /assets/{asset}/deposit-value` | 按价值存入（返回现金余额） |
| `POST /assets/{asset}/withdraw` | 按份额提取 |
| `POST /assets/{asset}/cash/withdraw` | 提走找零现金 |
| `GET  /users/{user}/positions/{asset}` | 持仓估值（份额/NAV/市值） |
| `GET  /assets/{asset}/audit` | 守恒对账 |
| `GET  /health` | 健康检查 |

错误响应体：`{"error_code": 异常类名, "message": 可读说明, ...附加上下文}`。
