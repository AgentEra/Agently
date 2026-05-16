import json
from pathlib import Path

from agently.compatibility import (
    CURRENT_FRAMEWORK_VERSION,
    CURRENT_RELEASE_TRAIN,
    get_current_release_manifest,
    get_devtools_compatibility_manifest,
    get_skills_compatibility_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "compatibility" / "index.json"
IN_DEVELOPMENT_PATH = ROOT / "compatibility" / "in-development.json"


def test_current_release_manifest_matches_registry_release_file():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    release_path = ROOT / index["release_files"][CURRENT_FRAMEWORK_VERSION]
    release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
    current_manifest = get_current_release_manifest()

    assert index["latest_release"] == CURRENT_FRAMEWORK_VERSION
    assert current_manifest["schema_version"] == release_manifest["schema_version"]
    assert current_manifest["framework"] == release_manifest["framework"]
    assert current_manifest["framework_version"] == release_manifest["framework_version"]
    assert current_manifest["release_train"] == release_manifest["release_train"]
    assert current_manifest["companions"] == release_manifest["companions"]


def test_devtools_and_skills_companion_views_derive_from_current_release_manifest():
    current = get_current_release_manifest()
    devtools = get_devtools_compatibility_manifest()
    skills = get_skills_compatibility_manifest()

    assert devtools["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert devtools["release_train"] == CURRENT_RELEASE_TRAIN
    assert devtools["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]

    assert skills["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert skills["release_train"] == CURRENT_RELEASE_TRAIN
    assert skills["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]
    assert (
        skills["devtools_guidance_protocol"]
        == current["companions"]["skills"]["devtools_guidance_protocol"]
    )


def test_in_development_manifest_is_registered_and_protocol_compatible():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    in_development = json.loads(IN_DEVELOPMENT_PATH.read_text(encoding="utf-8"))
    current = get_current_release_manifest()

    assert index["in_development_file"] == "compatibility/in-development.json"
    assert in_development["framework"] == "agently"
    assert in_development["target_version"] == "4.1.2"
    assert in_development["companions"]["devtools"]["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert in_development["companions"]["devtools"]["event_naming"] == {
        "preferred_event_type": "ObservationEvent",
        "legacy_event_type": "RuntimeEvent",
        "legacy_compatibility": "Agently 4.1.x",
        "replacement_line": "Agently 4.2",
    }
    assert in_development["companions"]["skills"]["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]
    assert (
        in_development["companions"]["skills"]["devtools_guidance_protocol"]
        == current["companions"]["skills"]["devtools_guidance_protocol"]
    )
    assert in_development["companions"]["skills"]["catalog_generation"] == "v2"
    assert in_development["companions"]["skills"]["recommended_bundle"] == "app"
    assert in_development["companions"]["skills"]["legacy_generations"] == [
        {
            "generation": "v1",
            "last_supported_framework_version": "4.1.1",
            "status": "frozen",
        }
    ]
