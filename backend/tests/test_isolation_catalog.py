"""AL-269: the TC-ISO-01..10 catalog is complete and every listed test exists."""
import ast
from pathlib import Path

import pytest

from backend.tests.isolation_catalog import CATALOG

pytestmark = pytest.mark.isolation

TESTS_DIR = Path(__file__).parent


def _test_functions(file_name: str) -> set[str]:
    tree = ast.parse((TESTS_DIR / file_name).read_text(encoding="utf-8"))
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


def test_catalog_has_ten_cases_each_with_tests():
    assert list(CATALOG) == [f"TC-ISO-{n:02d}" for n in range(1, 11)]
    for case, spec in CATALOG.items():
        assert spec["title"], case
        assert spec["tests"], f"{case} has no tests"


def test_every_listed_test_exists():
    missing = []
    for case, spec in CATALOG.items():
        for entry in spec["tests"]:
            file_name, function = entry.split("::")
            if not (TESTS_DIR / file_name).exists() or function not in _test_functions(file_name):
                missing.append(f"{case}: {entry}")
    assert not missing, "Catalog lists tests that no longer exist (renamed or deleted):\n  " + "\n  ".join(missing)


def test_listed_tests_are_tagged_when_collected(request):
    """conftest.py tags catalog tests; this test is itself in the isolation set."""
    assert request.node.get_closest_marker("isolation") is not None
