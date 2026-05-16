import os
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from _shared import create_deepseek_agent, print_action_results, print_response


def find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(host: str, port: int, timeout_seconds: float = 10.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


agent = create_deepseek_agent(
    "Use HTTP MCP actions for exact calculations. Prefer action execution over estimating."
)


if __name__ == "__main__":
    server_script = Path(__file__).with_name("_calculator_mcp_server.py")
    port = find_open_port()
    env = os.environ.copy()
    env["MCP_TRANSPORT"] = "http"
    env["MCP_PORT"] = str(port)
    process = subprocess.Popen([sys.executable, str(server_script)], env=env)
    try:
        wait_for_port("127.0.0.1", port)
        agent.use_mcp(f"http://127.0.0.1:{port}/mcp")

        agent.input(
            "Use the MCP actions to compute (100.25 + 55.5) * 1.08 and return the exact result."
        )
        records = agent.get_action_result()
        print_action_results(records)
        response = agent.get_response()
        print_response(response)
    finally:
        with suppress(Exception):
            process.kill()
            process.wait(timeout=5)

# Expected key output after configuring DeepSeek:
# The local MCP HTTP server starts on 127.0.0.1.
# [ACTION_RECORDS] includes MCP calculator action calls.
# The final computed value for (100.25 + 55.5) * 1.08 is 168.21.
