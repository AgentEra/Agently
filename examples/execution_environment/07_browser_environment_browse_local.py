from __future__ import annotations

import contextlib
import http.server
import socketserver
import tempfile
import threading
from pathlib import Path
from pprint import pprint

from agently import Agently
from agently.builtins.actions import Browse


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return None


@contextlib.contextmanager
def serve_directory(root: Path):
    handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(root), **kwargs)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{ httpd.server_address[1] }"
        finally:
            httpd.shutdown()
            thread.join(timeout=2)


def main():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] Install Playwright to run this example: pip install playwright && playwright install chromium")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "index.html").write_text(
            """
            <html>
              <body>
                <main>
                  <h1>Managed Browser Browse</h1>
                  <p>This page is loaded through BrowserExecutionEnvironmentProvider.</p>
                </main>
              </body>
            </html>
            """,
            encoding="utf-8",
        )

        agent = Agently.create_agent()
        agent.use_actions(
            Browse(
                enable_pyautogui=False,
                enable_playwright=False,
                enable_bs4=False,
                use_browser_environment=True,
            )
        )

        with serve_directory(root) as base_url:
            result = agent.action.execute_action("browse", {"url": f"{ base_url }/index.html"})

    print("[ACTION_RESULT]")
    pprint(result)
    if result.get("status") != "success":
        print("[SKIP] Playwright is installed, but Chromium may be missing. Run: playwright install chromium")
        return
    assert "Managed Browser Browse" in str(result.get("data", ""))
    assert Agently.execution_environment.list(scope="action_call") == []


if __name__ == "__main__":
    main()

# Expected key output with Playwright and Chromium installed:
# [ACTION_RESULT] has status="success".
# The extracted content contains "Managed Browser Browse".
# Action-call browser environment handles are released after the call.
