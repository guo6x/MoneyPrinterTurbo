"""Offline, deterministic acceptance checks for the 雨夜来信 fixture."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aidrama_studio.domain.script import StructuredScript
from aidrama_studio.domain.shot import ShotPlan
from aidrama_studio.domain.story import StoryBible


FIXTURE_DIR = Path(__file__).parent
TARGET_DURATION = 60.0
EPSILON = 1e-9


def _load_json(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _parse_srt(path: Path) -> list[dict[str, object]]:
    blocks = [block for block in path.read_text(encoding="utf-8").strip().split("\n\n") if block.strip()]
    cues: list[dict[str, object]] = []
    timing = re.compile(
        r"^(?P<start>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}),(?P<ms>\d{3})"
        r"\s+-->\s+"
        r"(?P<end>\d{2}):(?P<end_minute>\d{2}):(?P<end_second>\d{2}),(?P<end_ms>\d{3})$"
    )
    for block in blocks:
        lines = block.splitlines()
        assert len(lines) >= 3
        match = timing.match(lines[1])
        assert match, f"invalid SRT timing: {lines[1]}"

        def _seconds(prefix: str = "") -> float:
            return (
                int(match[f"{prefix}start" if not prefix else "start"]) * 3600
                + int(match[f"{prefix}minute" if prefix else "minute"]) * 60
                + int(match[f"{prefix}second" if prefix else "second"])
                + int(match[f"{prefix}ms" if prefix else "ms"]) / 1000
            )

        # The named groups for the end timestamp are distinct so calculate it
        # explicitly rather than relying on a parser with locale/timezone state.
        end = (
            int(match["end"]) * 3600
            + int(match["end_minute"]) * 60
            + int(match["end_second"])
            + int(match["end_ms"]) / 1000
        )
        cues.append({"index": int(lines[0]), "start": _seconds(), "end": end, "text": "\n".join(lines[2:])})
    return cues


def _assert_contiguous(intervals: list[tuple[float, float]], *, end: float) -> None:
    assert intervals[0][0] == pytest.approx(0.0, abs=EPSILON)
    for previous, current in zip(intervals, intervals[1:]):
        assert current[0] == pytest.approx(previous[1], abs=EPSILON)
    assert intervals[-1][1] == pytest.approx(end, abs=EPSILON)


def test_fixture_is_a_complete_offline_60_second_contract() -> None:
    manifest = _load_json("acceptance_manifest.json")
    for logical_name, filename in manifest["canonical_files"].items():
        canonical_path = FIXTURE_DIR / filename
        assert canonical_path.is_file(), f"missing canonical file {logical_name}: {filename}"
    creative = _load_json("creative_brief.json")
    story_data = _load_json("story_bible.json")
    script_data = _load_json("structured_script.json")
    shot_data = _load_json("shot_plan.json")
    requirements = _load_json("shot_acceptance_requirements.json")
    references = _load_json("reference_requirements.json")
    tts = _load_json("tts_cues.json")
    assembly = _load_json("final_assembly.json")
    profile = _load_json("output_profile.json")

    assert manifest["fixture_id"] == "AIDRAMA_V1_60S_REAL_E2E_ACCEPTANCE_FIXTURE"
    assert manifest["title"] == creative["title"] == story_data["title"] == script_data["title"] == shot_data["title"] == "雨夜来信"
    assert creative["target_duration_seconds"] == manifest["expected_counts"]["target_duration_seconds"] == 60
    assert creative["aspect_ratio"] == "16:9"
    assert creative["native_generation"] == {"width": 1280, "height": 720, "label": "720p"}
    assert creative["delivery"] == {"width": 1920, "height": 1080, "label": "1080p"}
    assert creative["fps"] == 24
    assert creative["deterministic_seed"] == requirements["defaults"]["deterministic_seed_policy"]["base_seed"] == 26082660

    story = StoryBible.model_validate(story_data)
    script = StructuredScript.model_validate(script_data).validate_against(story)
    shot_plan = ShotPlan.model_validate(shot_data)
    shot_plan.validate_against(script, story)
    assert len(story.characters) == 2
    assert {character.name for character in story.characters} == {"林夏", "林父"}
    assert len(story.locations) == 2
    assert {location.name for location in story.locations} == {"雨夜老街", "老屋室内"}
    assert script.total_estimated_duration_seconds == pytest.approx(TARGET_DURATION, abs=EPSILON)
    assert shot_plan.total_duration_seconds == pytest.approx(TARGET_DURATION, abs=EPSILON)
    assert [shot.order for shot in shot_plan.shots] == list(range(1, 13))
    assert [shot.id for shot in shot_plan.shots] == [f"shot_{index:02d}" for index in range(1, 13)]
    assert [shot.duration_seconds for shot in shot_plan.shots] == [5, 5, 4, 6, 5, 6, 5, 6, 4, 5, 5, 4]

    timeline = []
    cursor = 0.0
    for shot in shot_plan.shots:
        timeline.append((cursor, cursor + shot.duration_seconds))
        cursor += shot.duration_seconds
    _assert_contiguous(timeline, end=TARGET_DURATION)

    required_fields = {
        "shot_number", "shot_id", "duration_seconds", "scene", "characters", "framing",
        "camera_motion", "action", "dialogue", "reference_requirements", "generation_brief",
        "continuity_requirements", "deterministic_qc_expectations", "vision_qc_expectations",
        "human_review_acceptance_criteria",
    }
    required_shots = requirements["shots"]
    assert len(required_shots) == 12
    assert {shot["shot_id"] for shot in required_shots} == {shot.id for shot in shot_plan.shots}
    approved_reference_roles = set(requirements["defaults"]["required_reference_roles"])
    all_reference_text = " ".join(" ".join(shot["reference_requirements"]) for shot in required_shots)
    role_labels = {
        "lin_xia": "林夏",
        "lin_father": "林父",
        "rain_old_street": "雨夜老街",
        "old_house_interior": "老屋室内",
    }
    for role in approved_reference_roles:
        assert role_labels[role] in all_reference_text
    for shot in required_shots:
        assert required_fields <= shot.keys()
        assert shot["duration_seconds"] == shot_plan.shots[shot["shot_number"] - 1].duration_seconds
        assert set(shot["characters"]) <= {"林夏", "林父"}
        assert shot["reference_requirements"]
        assert shot["generation_brief"].startswith("1280x720 16:9 24fps")
        assert shot["continuity_requirements"] and shot["deterministic_qc_expectations"]
        assert shot["vision_qc_expectations"] and shot["human_review_acceptance_criteria"]
        assert shot["reference_requirements"]

    subtitle_cues = _parse_srt(FIXTURE_DIR / "subtitle_track.srt")
    assert len(subtitle_cues) == manifest["expected_counts"]["subtitle_cues"] == 7
    subtitle_intervals = [(float(cue["start"]), float(cue["end"])) for cue in subtitle_cues]
    assert all(start < end for start, end in subtitle_intervals)
    assert all(0 <= start < end <= TARGET_DURATION for start, end in subtitle_intervals)
    assert all(left[1] <= right[0] for left, right in zip(subtitle_intervals, subtitle_intervals[1:]))

    assert len(tts["cues"]) == manifest["expected_counts"]["tts_cues"] == 7
    tts_cues = tts["cues"]
    assert [cue["text"] for cue in tts_cues] == [cue["text"] for cue in subtitle_cues]
    shot_by_id = {shot.id: shot for shot in shot_plan.shots}
    tts_intervals = []
    for cue in tts_cues:
        assert cue["speaker_id"] in {"lin_xia", "lin_father"}
        assert cue["voice_profile_id"] in tts["voice_profiles"]
        assert cue["shot_id"] in shot_by_id
        shot = shot_by_id[cue["shot_id"]]
        start = float(cue["start_seconds"])
        end = float(cue["end_seconds"])
        shot_start = sum(item.duration_seconds for item in shot_plan.shots if item.order < shot.order)
        assert shot_start <= start < end <= shot_start + shot.duration_seconds
        tts_intervals.append((start, end))
    assert all(left[1] <= right[0] for left, right in zip(tts_intervals, tts_intervals[1:]))

    assert assembly["expected_order"] == [f"shot_{index:02d}" for index in range(1, 13)]
    assembly_intervals = [(float(item["timeline_start_seconds"]), float(item["timeline_end_seconds"])) for item in assembly["items"]]
    assert [item["shot_id"] for item in assembly["items"]] == assembly["expected_order"]
    assert [item["trimmed_duration_seconds"] for item in assembly["items"]] == [5, 5, 4, 6, 5, 6, 5, 6, 4, 5, 5, 4]
    _assert_contiguous(assembly_intervals, end=TARGET_DURATION)
    assert assembly["expected_duration_seconds"] == TARGET_DURATION

    assert profile["native_generation"] == {"width": 1280, "height": 720, "fps": 24, "aspect_ratio": "16:9"}
    assert profile["delivery"]["width"] == 1920 and profile["delivery"]["height"] == 1080
    assert profile["delivery"]["fps"] == 24 and profile["delivery"]["aspect_ratio"] == "16:9"
    assert profile["video"]["codec"] == "h264" and profile["video"]["encoder"] == "libx264"
    assert profile["audio"]["sample_rate_hz"] == 48000 and profile["audio"]["channels"] == 2

    assert len(references["characters"]) == len(references["locations"]) == 2
    assert set(references["policy"]["allowed_mime_types"]) == {"image/png", "image/jpeg", "image/webp"}
    assert manifest["offline_policy"] == {
        "live_calls": 0,
        "paid_calls": 0,
        "external_api_keys_required": False,
        "provider_calls_allowed": False,
        "network_required_for_validation": False,
    }
    assert len(manifest["e2e_acceptance_assertions"]) == manifest["expected_counts"]["e2e_acceptance_assertions"] == 45

    secret_or_url = re.compile(
        r"(?i)(?:https?://|sk-[a-z0-9]{8,}|(?:api[_-]?key|access[_-]?token|authorization|bearer|client[_-]?secret|password|private[_-]?key|signed[_-]?url)\s*[:=])"
    )
    for filename in manifest["canonical_files"].values():
        text = (FIXTURE_DIR / filename).read_text(encoding="utf-8")
        assert not secret_or_url.search(text), f"secret or URL marker found in {filename}"
