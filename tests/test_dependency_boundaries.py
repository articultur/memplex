"""Import-order and facade compatibility contracts for split storage modules."""

from __future__ import annotations

import itertools
import subprocess
import sys

import pytest

_MIGRATION_MODULES = (
    "memplex.storage.migrations.catalogue_checks",
    "memplex.storage.migrations.ledger_state",
    "memplex.storage.migrations.acl_verification",
    "memplex.storage.migrations.runner",
)


@pytest.mark.parametrize("order", tuple(itertools.permutations(_MIGRATION_MODULES)))
def test_migration_split_modules_support_every_fresh_import_order(order: tuple[str, ...]) -> None:
    """Late facade imports must not make any internal module order-dependent."""
    script = "\n".join(
        [
            "import importlib",
            *(f"importlib.import_module({name!r})" for name in order),
            "from memplex.storage.migrations import runner",
            "from memplex.storage.migrations import acl_verification, catalogue_checks, ledger_state",
            "assert runner._matches_post_core is catalogue_checks._matches_post_core",
            "assert runner._read_ledger_if_present is ledger_state._read_ledger_if_present",
            "assert runner._verify_acl_contracts is acl_verification._verify_acl_contracts",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "order",
    (
        ("memplex.storage.pool", "memplex.storage.postgres_resources"),
        ("memplex.storage.postgres_resources", "memplex.storage.pool"),
    ),
)
def test_pool_resource_facade_supports_both_fresh_import_orders(order: tuple[str, str]) -> None:
    """Legacy pool imports remain identical to the split resource implementation."""
    script = "\n".join(
        [
            "import importlib",
            *(f"importlib.import_module({name!r})" for name in order),
            "from memplex.storage import pool, postgres_resources",
            "assert pool.PostgresStorageResources is postgres_resources.PostgresStorageResources",
            "assert pool.PostgresSyncStorageResources is postgres_resources.PostgresSyncStorageResources",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
