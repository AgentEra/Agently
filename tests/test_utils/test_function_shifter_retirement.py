from __future__ import annotations

from pathlib import Path


def test_production_paths_do_not_depend_on_function_shifter() -> None:
    package_root = Path(__file__).resolve().parents[2] / "agently"
    compatibility_files = {
        package_root / "utils" / "FunctionShifter.py",
        package_root / "utils" / "__init__.py",
    }

    offenders = [
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*.py")
        if path not in compatibility_files and "FunctionShifter" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
