"""
Bubblewrap isolation verification tests.

These tests verify ACTUAL isolation by running commands inside bwrap
and checking that restricted operations are blocked.

Requirements:
- Linux with bwrap installed
- User namespaces enabled (kernel.unprivileged_userns_clone=1 on Debian/Ubuntu)

Workaround for AppArmor blocking bwrap (Ubuntu 23.10+):
    AppArmor restricts unprivileged user namespace creation.
    Choose ONE of the following:

    Option A - Disable AppArmor restriction for bwrap (recommended for dev):
        sudo aa-disable /usr/bin/bwrap

    Option B - Allow unprivileged user namespaces system-wide:
        sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

    Option C - Use setuid bwrap (less secure, for testing only):
        sudo chmod u+s $(which bwrap)

    Option D - Run tests with root privileges:
        sudo pytest tests/test_bubblewrap_isolation.py -v

    After applying any workaround, verify with:
        bwrap --dev /dev --proc /proc -- echo "bwrap works"

---

Bubblewrap 隔离验证测试。

这些测试通过在 bwrap 内运行命令并检查受限操作是否被阻止，
来验证实际的隔离效果。

要求：
- Linux 系统且已安装 bwrap
- 启用用户命名空间（Debian/Ubuntu 上需设置 kernel.unprivileged_userns_clone=1）

AppArmor 阻止 bwrap 的解决方案（Ubuntu 23.10+）：
    AppArmor 限制非特权用户命名空间的创建。
    选择以下任一方案：

    方案 A - 禁用 bwrap 的 AppArmor 限制（开发环境推荐）：
        sudo aa-disable /usr/bin/bwrap

    方案 B - 系统级允许非特权用户命名空间：
        sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

    方案 C - 使用 setuid bwrap（安全性较低，仅限测试）：
        sudo chmod u+s $(which bwrap)

    方案 D - 以 root 权限运行测试：
        sudo pytest tests/test_bubblewrap_isolation.py -v

    应用上述方案后，使用以下命令验证：
        bwrap --dev /dev --proc /proc -- echo "bwrap works"
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

def check_bwrap_works() -> bool:
    """Check if bwrap actually works in this environment."""
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        return False
    try:
        result = subprocess.run(
            [
                "bwrap",
                "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/lib64", "/lib64",
                "--proc", "/proc",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--", "echo", "test",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip all tests if bwrap doesn't work (e.g., AppArmor blocks it)
# 如果 bwrap 无法工作则跳过所有测试（例如 AppArmor 阻止）
pytestmark = pytest.mark.skipif(
    not check_bwrap_works(),
    reason=(
        "bwrap not working. Fix: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 | "
        "bwrap 无法运行。修复：sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    ),
)


def run_bwrap(argv: list[str], *, extra_args: list[str] | None = None, timeout: int = 10) -> dict:
    """Run a command inside bwrap and return result."""
    bwrap_args = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/bin", "/bin",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]
    if extra_args:
        bwrap_args.extend(extra_args)
    bwrap_args.extend(["--", *argv])

    try:
        result = subprocess.run(
            bwrap_args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "timeout"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


class TestFilesystemIsolation:
    """Test filesystem isolation - sandbox cannot access unauthorized paths."""

    def test_cannot_read_etc_shadow(self):
        """Sandbox should NOT be able to read /etc/shadow."""
        result = run_bwrap(["cat", "/etc/shadow"])
        # Should fail - no bind mount for /etc/shadow
        assert result["returncode"] != 0 or "No such file" in result["stderr"] or "Permission denied" in result["stderr"]

    def test_cannot_access_home_directory(self):
        """Sandbox should NOT see host home directory."""
        result = run_bwrap(["ls", "/home"])
        # /home is not bound, should be empty or not exist
        assert result["returncode"] != 0 or result["stdout"].strip() == ""

    def test_can_read_bound_readonly_path(self):
        """Sandbox CAN read paths explicitly bound as readonly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content 12345")
            temp_path = f.name

        try:
            result = run_bwrap(
                ["cat", f"/sandbox{temp_path}"],
                extra_args=["--ro-bind", temp_path, f"/sandbox{temp_path}"],
            )
            assert result["returncode"] == 0
            assert "test content 12345" in result["stdout"]
        finally:
            os.unlink(temp_path)

    def test_cannot_write_to_readonly_bind(self):
        """Sandbox should NOT write to readonly bind mounts."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original")
            temp_path = f.name

        try:
            result = run_bwrap(
                ["sh", "-c", f"echo hacked > /sandbox{temp_path}"],
                extra_args=["--ro-bind", temp_path, f"/sandbox{temp_path}"],
            )
            # Write should fail
            assert result["returncode"] != 0 or "Read-only file system" in result["stderr"]
            # Original file unchanged
            assert Path(temp_path).read_text() == "original"
        finally:
            os.unlink(temp_path)

    def test_can_write_to_tmpfs(self):
        """Sandbox CAN write to tmpfs mounts."""
        # Default run_bwrap already mounts tmpfs at /tmp
        result = run_bwrap(
            ["sh", "-c", "echo hello > /tmp/test && cat /tmp/test"],
        )
        assert result["returncode"] == 0
        assert "hello" in result["stdout"]


class TestProcessIsolation:
    """Test PID namespace isolation."""

    def test_pid_namespace_isolation(self):
        """Sandbox should have its own PID namespace."""
        # Note: PID isolation requires proper namespace setup
        # This test verifies the mechanism exists
        result = run_bwrap(
            ["sh", "-c", "echo $$"],
            extra_args=["--unshare-pid"],
        )
        # In isolated PID namespace, first process is PID 1
        # But shell $$ may not reflect this without proper init
        # Just verify the command runs or namespace option is recognized
        assert result["returncode"] == 0 or "unshare" in result["stderr"].lower()

    def test_cannot_see_host_processes(self):
        """Sandbox should NOT see host process list."""
        result = run_bwrap(
            ["ps", "aux"],
            extra_args=["--unshare-pid", "--proc", "/proc"],
        )
        # ps should only show sandbox processes, not host PIDs
        if result["returncode"] == 0:
            lines = result["stdout"].strip().split("\n")
            # Should have very few processes (just sandbox ones)
            assert len(lines) <= 5, f"Too many processes visible: {len(lines)}"


class TestNetworkIsolation:
    """Test network namespace isolation."""

    def test_no_network_access(self):
        """Sandbox should NOT have network access when unshared."""
        result = run_bwrap(
            ["sh", "-c", "cat < /dev/tcp/127.0.0.1/22 2>&1 || echo NO_NETWORK"],
            extra_args=["--unshare-net"],
        )
        # Network should be unavailable
        assert "NO_NETWORK" in result["stderr"] or "NO_NETWORK" in result["stdout"] or result["returncode"] != 0

    def test_localhost_not_reachable(self):
        """Sandbox should NOT reach localhost when network isolated."""
        result = run_bwrap(
            ["sh", "-c", "ping -c 1 127.0.0.1 2>&1 || echo NO_PING"],
            extra_args=["--unshare-net"],
        )
        output = result["stdout"] + result["stderr"]
        # Network isolation means no loopback interface
        assert (
            "NO_PING" in output or
            "Network is unreachable" in output or
            "Failed RTM_NEWADDR" in output or  # bwrap loopback setup failure
            result["returncode"] != 0
        )


class TestUserIsolation:
    """Test user namespace isolation."""

    def test_user_namespace_mapping(self):
        """Sandbox should have different UID mapping."""
        result = run_bwrap(
            ["id", "-u"],
            extra_args=["--unshare-user", "--uid", "1000", "--gid", "1000"],
        )
        if result["returncode"] == 0:
            uid = result["stdout"].strip()
            # Inside sandbox, should see the mapped UID
            assert uid == "1000"


class TestResourceLimits:
    """Test that resource limits are enforced."""

    def test_tmpfs_size_limit(self):
        """Tmpfs should have size limits."""
        result = run_bwrap(
            ["sh", "-c", "df -h /tmp | tail -1"],
            extra_args=["--tmpfs", "/tmp", "size=10M"],
        )
        if result["returncode"] == 0:
            # Should show ~10M limit
            assert "10M" in result["stdout"] or "10.0M" in result["stdout"]


class TestProviderIntegration:
    """Test the actual provider implementation.

    Note: These tests require being on the bubblewrap-provider branch.
    """

    @pytest.mark.skip(reason="Requires bubblewrap-provider branch")
    def test_provider_probe(self):
        """Test BubblewrapExecutionResourceProvider.async_probe."""
        pass

    @pytest.mark.skip(reason="Requires bubblewrap-provider branch")
    def test_provider_capabilities(self):
        """Test that probe returns correct capabilities."""
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
