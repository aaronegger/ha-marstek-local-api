#!/usr/bin/env python3
"""Regression checks for compatibility scaling factors.

This script validates that known 10x scaling regressions stay fixed:
- HW 3.0 battery temperature should not be divided by 10
- HW 3.0 battery capacity should not be multiplied by 10
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_compatibility_module():
    """Load compatibility.py directly without importing Home Assistant package init."""
    module_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "marstek_local_api"
        / "compatibility.py"
    )
    spec = importlib.util.spec_from_file_location("compatibility", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_close(actual: float, expected: float, label: str) -> None:
    """Assert with a compact numeric tolerance message."""
    if abs(actual - expected) > 1e-9:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def main() -> None:
    """Run scaling regression assertions."""
    compatibility = _load_compatibility_module()
    matrix = compatibility.CompatibilityMatrix

    # HW 3.0 regression checks: these were off by 10x.
    hw3 = matrix("VenusE 3.0", 139)
    _assert_close(hw3.scale_value(15.0, "bat_temp"), 15.0, "HW3 bat_temp")
    _assert_close(hw3.scale_value(5020.0, "bat_capacity"), 5020.0, "HW3 bat_capacity")
    _assert_close(
        hw3.scale_value(5020.0, "bat_capacity") / 1000.0,
        5.02,
        "HW3 remaining_capacity_kwh",
    )

    # Guard one existing non-HW3 behavior so we do not break unrelated scaling.
    hw2_old = matrix("VenusE", 100)
    _assert_close(hw2_old.scale_value(100.0, "bat_power"), 10.0, "HW2 legacy bat_power")

    print("PASS: compatibility scaling regression checks")


if __name__ == "__main__":
    main()
