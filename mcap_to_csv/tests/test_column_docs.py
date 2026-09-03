"""
The documented columns must match the ones actually written.

Three places describe these columns: transect.OUTPUT_COLUMNS writes them,
COLUMNS.md documents them, and UTC's README embeds a copy of that table for
people working from the app rather than the library.

Copies drift. This project has already lost a release to two copies of one rule
disagreeing -- the health report naming a depth source the extractor had stopped
using. So the README's copy is generated, and this is the check that it was
regenerated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from column_docs import COLUMNS_MD, UTC_README, current, render  # noqa: E402

from ccr_m2c.transect import OUTPUT_COLUMNS  # noqa: E402

REGEN = "python tools/column_docs.py --write"


def _documented(text: str) -> list[str]:
    """Column names, in order, from a markdown table."""
    return [m.group(1) for m in re.finditer(r"^\|\s*`([A-Za-z_0-9]+)`\s*\|", text, re.M)]


def test_columns_md_documents_every_column_in_order():
    body = COLUMNS_MD.read_text(encoding="utf-8").split("## Rules")[0]
    assert _documented(body) == OUTPUT_COLUMNS, (
        "COLUMNS.md no longer matches transect.OUTPUT_COLUMNS")


@pytest.mark.skipif(not UTC_README.is_file(), reason="UTC is not in this checkout")
def test_the_utc_readme_table_is_current():
    embedded = current()
    assert embedded is not None, (
        f"the generated block is missing from {UTC_README.name}; run {REGEN}")
    assert embedded == render(), (
        f"{UTC_README.name} is out of date with COLUMNS.md; run {REGEN}")


@pytest.mark.skipif(not UTC_README.is_file(), reason="UTC is not in this checkout")
def test_the_utc_readme_lists_every_column():
    assert _documented(current()) == OUTPUT_COLUMNS


def test_rendering_is_stable():
    """Generating twice must not produce a different file, or the check above
    would fail for no reason."""
    assert render() == render()
