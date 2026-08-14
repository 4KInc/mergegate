"""The buyer's pinned grader.

This bundle is hashed into the task contract before the provider is allowed to
submit, and it is injected into the workspace *after* the provider's diff is
applied. Whatever the provider does to the test tree, these are the bytes that
run.

The base repository ships an add() that returns 0 for negative operands, so a
provider has to actually fix the function — passing this is not free.
"""

from src.calc import add


def test_adds_positives():
    assert add(2, 2) == 4


def test_adds_negatives():
    assert add(-1, -1) == -2


def test_adds_mixed_signs():
    assert add(-5, 3) == -2
