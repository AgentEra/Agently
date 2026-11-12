import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("disabled", [False, True])
def test_python_support_notice_is_once_per_process_and_configurable(disabled: bool) -> None:
    env = os.environ.copy()
    env.pop("AGENTLY_PYTHON_310_NOTICE", None)
    if disabled:
        env["AGENTLY_PYTHON_310_NOTICE"] = "0"

    result = subprocess.run(
        [
            sys.executable,
            "-Werror",
            "-c",
            "import importlib; import agently; import agently; "
            "importlib.reload(agently); agently.Agently.create_agent(); print('ready')",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert result.stdout == "ready\n"
    notices = [line for line in result.stderr.splitlines() if "AGENTLY_PYTHON_310_NOTICE" in line]
    if sys.version_info[:2] == (3, 10) and not disabled:
        assert len(notices) == 1
        assert "Python 3.10 has reached its official end of life" in notices[0]
        assert "Agently 4.2 and later will require Python 3.11 or newer" in notices[0]
    else:
        assert notices == []


def test_python_support_notice_tolerates_closed_stderr() -> None:
    env = os.environ.copy()
    env.pop("AGENTLY_PYTHON_310_NOTICE", None)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.close(); import agently; print('ready')"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout == "ready\n"
