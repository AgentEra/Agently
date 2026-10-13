from pathlib import Path
import json
import re
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow_validates_both_mcp_lines_before_publication() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-on-version-change.yml").read_text())
    matrix = workflow["jobs"]["validate"]["strategy"]["matrix"]
    assert matrix["python-version"] == ["3.10", "3.14"]
    assert matrix["fastmcp-version"] == ["3", "4"]
    install = next(step for step in workflow["jobs"]["validate"]["steps"]
                   if step["name"] == "Install release validation tools")
    assert "matrix.fastmcp-version" in install["run"]
    assert workflow["jobs"]["publish"]["needs"] == "validate"


def test_release_workflow_never_publishes_from_pull_requests_or_other_branches() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/publish-on-version-change.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["pull_request"]["branches"] == ["dev", "main"]
    assert workflow["jobs"]["publish"]["if"] == (
        "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert all("POETRY_PYPI_TOKEN_PYPI" not in str(step) for step in workflow["jobs"]["validate"]["steps"])


def test_publish_workflow_installs_optional_typing_dependencies_before_pyright() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/publish-on-version-change.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["validate"]["steps"]
    names = [step["name"] for step in steps]
    assert len(names) == len(set(names))
    install = steps[names.index("Install optional typing dependencies")]
    check = steps[names.index("Type check")]
    assert install["if"] == check["if"]
    assert "playwright==" in install["run"]
    assert "chromadb==" in install["run"]
    assert names.index(install["name"]) < names.index(check["name"])
    assert all("run" in step or "uses" in step for step in steps)


def test_4_1_4_8_change_guide_covers_late_additions_and_has_valid_links() -> None:
    guide = ROOT / "examples/release_pinned_usage/CHANGES_4_1_4_8.md"
    text = guide.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        assert (guide.parent / target.split("#", 1)[0]).is_file(), target
    manifest = json.loads(
        (ROOT / "examples/release_pinned_usage/pinned_usage_manifest.json").read_text(encoding="utf-8")
    )
    covered = {
        path
        for item in manifest["development_line_4_1_4_8_coverage"]
        for path in item["examples"]
    }
    assert {
        "examples/basic/auto_continue.py",
        "examples/agent_auto_orchestration/29_field_long_content_ollama.py",
        "examples/agent_auto_orchestration/29_execution_controls_ollama.py",
        "examples/audio/tts_stt_roundtrip.py",
        "examples/audio/continuous_audio.py",
        "examples/action_runtime/3_8_general_shell_model.py",
        "examples/agent_task/action_result_dependency.py",
        "examples/skills_executor/11_conditional_resource_read.py",
    } <= covered


def test_4_1_4_8_docs_explain_scope_compatibility_and_deferred_work() -> None:
    for language in ("cn", "en"):
        notes = (ROOT / f"docs/{language}/development/release-notes-4.1.4.8.md").read_text(encoding="utf-8")
        for required in ("CHANGES_4_1_4_8.md", "4.1.4.7", "4.1.4.9", "4.2", "ensure_long_output", "auto_continue", "offline"):
            assert required in notes
    for filename in ("README.md", "README_CN.md"):
        introduction = (ROOT / filename).read_text(encoding="utf-8")
        assert "built-in Pattern" not in introduction
        assert "review、verify" not in introduction


def test_publish_workflow_has_an_explicit_failed_release_retry_path():
    workflow = (ROOT / ".github/workflows/publish-on-version-change.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "force_publish:" in workflow
    assert "FORCE_PUBLISH:" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow


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
