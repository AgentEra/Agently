from bs4 import BeautifulSoup

from agently.builtins.actions import Browse


def test_browse_extract_prefers_main_content_and_ignores_sidebar():
    html = """
    <html>
      <body>
        <aside class="VPSidebar sidebar">
          <h2>Sidebar Title</h2>
          <p>Sidebar paragraph should be ignored.</p>
        </aside>
        <main>
          <article class="vp-doc">
            <h1>Agently TriggerFlow</h1>
            <p>TriggerFlow is event-driven orchestration.</p>
            <h2>Use Cases</h2>
            <p>Multi-step tool workflows.</p>
          </article>
        </main>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    content = Browse._extract_text_from_soup(soup)

    assert "# Agently TriggerFlow" in content
    assert "## Use Cases" in content
    assert "TriggerFlow is event-driven orchestration." in content
    assert "Sidebar Title" not in content
    assert "Sidebar paragraph should be ignored." not in content


def test_browse_extract_filters_nav_and_footer_noise():
    html = """
    <html>
      <body>
        <header>
          <h1>Site Header</h1>
        </header>
        <nav class="navbar">
          <p>Home Docs API</p>
        </nav>
        <div class="content">
          <h1>Main Doc</h1>
          <p>Core content line.</p>
        </div>
        <footer>
          <p>Copyright footer.</p>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    content = Browse._extract_text_from_soup(soup)

    assert "# Main Doc" in content
    assert "Core content line." in content
    assert "Site Header" not in content
    assert "Home Docs API" not in content
    assert "Copyright footer." not in content


def test_browse_extract_falls_back_to_blacklist_strategy_when_whitelist_is_too_thin():
    html = """
    <html>
      <body>
        <header>
          <p>Top nav</p>
        </header>
        <nav class="navbar">
          <p>Docs Blog Pricing</p>
        </nav>
        <div class="hero">
          <p>Hi</p>
        </div>
        <section class="generic-wrapper">
          <h1>Release Notes</h1>
          <p>This release adds reliable browser fallback behavior for content extraction.</p>
          <p>It also keeps noisy navigation blocks out of the final text output.</p>
        </section>
        <footer>
          <p>Footer links</p>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    content = Browse._extract_text_from_soup(soup, min_length=80)

    assert "# Release Notes" in content
    assert "reliable browser fallback behavior" in content
    assert "Docs Blog Pricing" not in content
    assert "Footer links" not in content


def test_browse_extract_falls_back_to_raw_body_when_text_is_still_too_short():
    html = """
    <html>
      <body>
        <div class="gallery">
          <img src="/cover.png" alt="" />
        </div>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    content = Browse._extract_text_from_soup(soup, min_length=80)

    assert "<body>" in content
    assert "<img" in content
