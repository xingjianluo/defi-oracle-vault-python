def test_skeleton_imports():
    import defi_oracle_vault  # noqa: F401

    assert defi_oracle_vault.VaultService is not None
