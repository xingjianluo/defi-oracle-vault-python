"""本地启动入口: uv run uvicorn main:app --reload"""

from defi_oracle_vault.api import create_app
from defi_oracle_vault.vault import VaultService

service = VaultService()
app = create_app(service)
