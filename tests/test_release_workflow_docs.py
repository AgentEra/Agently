from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflows_require_foundation_example_effect_gate():
    english = (ROOT / "docs/en/development/release-workflows.md").read_text(encoding="utf-8")
    chinese = (ROOT / "docs/cn/development/release-workflows.md").read_text(encoding="utf-8")

    for text in (english, chinese):
        assert "Foundation Example Effect Gate" in text
        assert "examples/" in text
        assert "DeepSeek" in text
        assert "online model" in text or "线上模型" in text
        assert "pyright" in text
        assert "pytest" in text
        assert "default `pytest`" in text or "默认 `pytest`" in text
        assert "fail closed" in text or "fails closed" in text
        assert "Foundation example effect checks" in text
        assert "Pinned Developer Usage Example Gate" in text or "锁定开发者用法 Example Gate" in text
        assert "ask the maintainer" in text or "请示维护者" in text
        assert "recommended usage" in text or "推荐用法" in text
        assert "all-allowed test capability policy" in text or "全开的测试 capability" in text


def test_4_1_4_5_release_notes_are_linked_from_public_indexes():
    english_notes = ROOT / "docs/en/development/release-notes-4.1.4.5.md"
    chinese_notes = ROOT / "docs/cn/development/release-notes-4.1.4.5.md"

    assert english_notes.exists()
    assert chinese_notes.exists()
    for path in (
        ROOT / "README.md",
        ROOT / "README_CN.md",
        ROOT / "docs/en/index.md",
        ROOT / "docs/cn/index.md",
        ROOT / "docs/en/development/README.md",
        ROOT / "docs/cn/development/README.md",
    ):
        assert "release-notes-4.1.4.5.md" in path.read_text(encoding="utf-8")
