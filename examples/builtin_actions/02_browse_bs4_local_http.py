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
        import bs4  # noqa: F401
    except ImportError:
        print("[SKIP] Install beautifulsoup4 to run this example: pip install beautifulsoup4")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "index.html").write_text(
            """
            <html>
              <body>
                <nav>Home Docs Pricing</nav>
                <main>
                  <article>
                    <h1>Agently Browse Example</h1>
                    <p>Browse extracts the primary content from a page.</p>
                    <p>This example runs against a local HTTP server.</p>
                  </article>
                </main>
              </body>
            </html>
            """,
            encoding="utf-8",
        )

        browse = Browse(
            enable_pyautogui=False,
            enable_playwright=False,
            enable_bs4=True,
            fallback_order=("bs4",),
        )
        agent = Agently.create_agent()
        agent.use_actions(browse)

        with serve_directory(root) as base_url:
            result = agent.action.execute_action("browse", {"url": f"{ base_url }/index.html"})

    print("[ACTION_RESULT]")
    pprint(result)
    assert result.get("status") == "success"
    assert "Agently Browse Example" in str(result.get("data", ""))
    assert "Home Docs Pricing" not in str(result.get("data", ""))


if __name__ == "__main__":
    main()

# Expected key output:
# [ACTION_RESULT] has status="success".
# The extracted content contains "Agently Browse Example".
# Navigation text such as "Home Docs Pricing" is not included in the readable body.
