"""Require an explicit execution boundary for every collected test."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        groups = [mark.name for mark in item.iter_markers() if mark.name in {"unit", "subprocess"}]
        if len(groups) != 1:
            raise pytest.UsageError(
                f"{item.nodeid}: mark each test with exactly one of 'unit' or 'subprocess'"
            )
