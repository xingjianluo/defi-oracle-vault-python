"""本地演示装配：一组示例资产与一个共享 Vault。

运行：``uv run fastapi dev main.py`` 或 ``uv run uvicorn main:app``。
可用 ``VAULT_STATE`` 模块级对象直接喂入本地价格（见 tests/test_service.py）。
"""

from decimal import Decimal

from .models import AssetConfig, SourceConfig, VaultConfig
from .vault import Vault

ASSETS = {
    "BTC": AssetConfig(
        symbol="BTC",
        sources=(SourceConfig("binance"), SourceConfig("coinbase"), SourceConfig("kraken")),
        min_valid_sources=2,
        max_price_age_seconds=60.0,
        outlier_deviation=Decimal("0.05"),
        volatility_threshold=Decimal("0.10"),
        observation_window_seconds=300.0,
        unavailable_grace_seconds=120.0,
        recovery_required_valid_observations=3,
    ),
    "ETH": AssetConfig(
        symbol="ETH",
        sources=(SourceConfig("binance"), SourceConfig("coinbase")),
        min_valid_sources=2,
        max_price_age_seconds=60.0,
        outlier_deviation=Decimal("0.05"),
        volatility_threshold=Decimal("0.10"),
        observation_window_seconds=300.0,
        unavailable_grace_seconds=120.0,
        recovery_required_valid_observations=2,
    ),
}


def build_demo_vault(clock=None) -> Vault:
    cfg = VaultConfig(
        assets=ASSETS,
        recovery_operators=frozenset({"risk_admin"}),
    )
    return Vault(cfg, clock=clock)
