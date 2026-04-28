"""Shared pytest fixtures for EvalGuard."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Empty project directory."""
    return tmp_path
