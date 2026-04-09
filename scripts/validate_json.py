#!/usr/bin/env python3
"""Validate story/framework/storyboard JSON files for xyz-video-skill."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


VALID_STORY_BEATS = {"开端", "发展", "高潮", "结尾"}
VALID_STORY_TIMES = {"day", "dusk", "night", "dawn"}
VALID_ROLE_TYPES = {"protagonist", "antagonist", "supporting", "extra"}
VALID_ENVIRONMENT_TYPES = {"indoor", "outdoor", "natural", "urban", "fantasy"}
VALID_CONTINUITY_MODES = {"strict", "scene_end", "free"}
VALID_DISTANCE_TO_TARGET = {"getting_closer", "getting_farther", "holding_position"}
VALID_SHOT_TYPES = {"visible_subject", "offscreen_reaction", "transition_reveal", "free_atmosphere"}
VALID_VIDEO_REFERENCE_USAGES = {
    "first_frame",
    "reference_character",
    "reference_prop",
    "reference_composition",
    "reference_style",
    "reference_color",
    "reference_target_state",
    "reference_stage",
    "reference_motion",
}
SUBJECT_CONSTRAINT_LIST_KEYS = {
    "required_visible_subjects",
    "optional_visible_subjects",
    "offscreen_subjects",
    "continuity_subjects",
    "forbidden_visible_subjects",
    "semantic_rules",
}
SUBJECT_CONSTRAINT_STRING_OR_LIST_KEYS = {
    "pose_contract",
}
SUBJECT_CONSTRAINT_DICT_KEYS = {
    "gaze_contract",
}


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if is_non_empty_string(item)]


class ValidationReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    @property
    def ok(self) -> bool:
        return not self.errors


def is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def require_non_empty_string(report: ValidationReport, obj: dict[str, Any], key: str, path: str) -> None:
    if not is_non_empty_string(obj.get(key)):
        report.error(path, f'missing or empty "{key}"')


def require_positive_number(report: ValidationReport, obj: dict[str, Any], key: str, path: str) -> None:
    value = obj.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        report.error(path, f'"{key}" must be a positive number')


def require_list(report: ValidationReport, obj: dict[str, Any], key: str, path: str) -> list[Any]:
    value = obj.get(key)
    if not isinstance(value, list):
        report.error(path, f'"{key}" must be a list')
        return []
    if not value:
        report.error(path, f'"{key}" must not be empty')
    return value


def require_dict(report: ValidationReport, obj: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    value = obj.get(key)
    if not isinstance(value, dict):
        report.error(path, f'"{key}" must be an object')
        return {}
    if not value:
        report.error(path, f'"{key}" must not be empty')
    return value


def validate_story(data: dict[str, Any], report: ValidationReport) -> None:
    root = "story"
    for key in ["title", "synopsis", "source_interpretation", "narrative", "visual_tone"]:
        require_non_empty_string(report, data, key, root)

    beats = require_list(report, data, "story_beats", root)
    durations = data.get("suggested_duration_per_beat")
    if not isinstance(durations, list) or not durations:
        report.error(root, '"suggested_duration_per_beat" must be a non-empty list')
    elif beats and len(durations) != len(beats):
        report.warn(root, '"suggested_duration_per_beat" length does not match "story_beats"')

    for idx, beat in enumerate(beats):
        path = f"{root}.story_beats[{idx}]"
        if not isinstance(beat, dict):
            report.error(path, "must be an object")
            continue
        for key in ["beat", "description", "emotion", "key_visual", "narrative_range"]:
            require_non_empty_string(report, beat, key, path)
        beat_name = beat.get("beat")
        if is_non_empty_string(beat_name) and beat_name not in VALID_STORY_BEATS:
            report.warn(path, f'unexpected beat value "{beat_name}"')


def validate_framework(data: dict[str, Any], report: ValidationReport) -> None:
    root = "framework"
    for key in ["title", "synopsis", "narrative", "visual_style_anchor"]:
        require_non_empty_string(report, data, key, root)
    require_positive_number(report, data, "total_duration", root)

    story_time = data.get("story_time")
    if not is_non_empty_string(story_time):
        report.error(root, 'missing or empty "story_time"')
    elif story_time not in VALID_STORY_TIMES:
        report.error(root, f'"story_time" must be one of {sorted(VALID_STORY_TIMES)}')

    characters = require_list(report, data, "suggested_characters", root)
    locations = require_list(report, data, "suggested_locations", root)
    scenes = require_list(report, data, "scenes", root)

    character_ids: set[str] = set()
    location_names: set[str] = set()

    for idx, character in enumerate(characters):
        path = f"{root}.suggested_characters[{idx}]"
        if not isinstance(character, dict):
            report.error(path, "must be an object")
            continue
        for key in [
            "id",
            "name",
            "role_type",
            "personality",
            "appearance",
            "default_clothing",
            "ref_description",
        ]:
            require_non_empty_string(report, character, key, path)
        key_features = require_list(report, character, "key_features", path)
        if key_features and not all(is_non_empty_string(item) for item in key_features):
            report.error(path, '"key_features" must contain only non-empty strings')
        role_type = character.get("role_type")
        if is_non_empty_string(role_type) and role_type not in VALID_ROLE_TYPES:
            report.warn(path, f'unexpected role_type "{role_type}"')
        char_id = character.get("id")
        if is_non_empty_string(char_id):
            if char_id in character_ids:
                report.error(path, f'duplicate character id "{char_id}"')
            character_ids.add(char_id)

    for idx, location in enumerate(locations):
        path = f"{root}.suggested_locations[{idx}]"
        if not isinstance(location, dict):
            report.error(path, "must be an object")
            continue
        for key in ["name", "description", "environment_type"]:
            require_non_empty_string(report, location, key, path)
        env_type = location.get("environment_type")
        if is_non_empty_string(env_type) and env_type not in VALID_ENVIRONMENT_TYPES:
            report.warn(path, f'unexpected environment_type "{env_type}"')
        name = location.get("name")
        if is_non_empty_string(name):
            location_names.add(name)

    for idx, scene in enumerate(scenes):
        path = f"{root}.scenes[{idx}]"
        if not isinstance(scene, dict):
            report.error(path, "must be an object")
            continue
        for key in [
            "name",
            "location",
            "narrative_segment",
            "summary",
            "visual_description",
            "emotion_arc",
        ]:
            require_non_empty_string(report, scene, key, path)
        require_positive_number(report, scene, "duration", path)
        char_list = require_list(report, scene, "characters_in_scene", path)
        for item in char_list:
            if not is_non_empty_string(item):
                report.error(path, '"characters_in_scene" must contain only non-empty strings')
            elif character_ids and item not in character_ids:
                report.error(path, f'references unknown character id "{item}"')
        location = scene.get("location")
        if is_non_empty_string(location) and location_names and location not in location_names:
            report.error(path, f'references unknown location "{location}"')


def validate_storyboard_current(data: dict[str, Any], report: ValidationReport) -> None:
    root = "storyboard"
    for key in ["title", "narrative", "style_anchor", "character_ref_dir"]:
        require_non_empty_string(report, data, key, root)
    require_positive_number(report, data, "total_duration", root)

    characters = require_dict(report, data, "characters", root)
    prop_refs = data.get("prop_refs", {})
    scenes = require_list(report, data, "scenes", root)

    character_ids = set(characters.keys())
    prop_ids = set(prop_refs.keys()) if isinstance(prop_refs, dict) else set()
    for char_id, character in characters.items():
        path = f"{root}.characters.{char_id}"
        if not isinstance(character, dict):
            report.error(path, "must be an object")
            continue
        for key in ["ref_image", "ref_description", "appearance"]:
            require_non_empty_string(report, character, key, path)

    if prop_refs is not None:
        if not isinstance(prop_refs, dict):
            report.error(root, '"prop_refs" must be an object when provided')
            prop_refs = {}
            prop_ids = set()
        for prop_id, prop_info in prop_refs.items():
            path = f"{root}.prop_refs.{prop_id}"
            if not isinstance(prop_info, dict):
                report.error(path, "must be an object")
                continue
            for key in ["ref_description", "appearance"]:
                require_non_empty_string(report, prop_info, key, path)

    seen_shot_ids: set[int] = set()
    for idx, scene in enumerate(scenes):
        path = f"{root}.scenes[{idx}]"
        if not isinstance(scene, dict):
            report.error(path, "must be an object")
            continue
        for key in [
            "name",
            "location",
            "narrative_segment",
            "summary",
            "visual_description",
            "emotion_arc",
        ]:
            require_non_empty_string(report, scene, key, path)
        require_positive_number(report, scene, "duration", path)
        chars = require_list(report, scene, "characters_in_scene", path)
        for item in chars:
            if not is_non_empty_string(item):
                report.error(path, '"characters_in_scene" must contain only non-empty strings')
            elif character_ids and item not in character_ids:
                report.error(path, f'references unknown character id "{item}"')
        shots = require_list(report, scene, "shots", path)
        for shot_index, shot in enumerate(shots):
            shot_path = f"{path}.shots[{shot_index}]"
            if not isinstance(shot, dict):
                report.error(shot_path, "must be an object")
                continue
            shot_id = shot.get("id")
            if not isinstance(shot_id, int):
                report.error(shot_path, '"id" must be an integer')
            elif shot_id in seen_shot_ids:
                report.error(shot_path, f'duplicate shot id "{shot_id}"')
            else:
                seen_shot_ids.add(shot_id)
            for key in [
                "narrative_segment",
                "scene_prompt",
                "end_frame_description",
                "action_prompt",
            ]:
                require_non_empty_string(report, shot, key, shot_path)
            shot_duration = shot.get("estimated_duration", shot.get("duration"))
            if not isinstance(shot_duration, (int, float)) or shot_duration <= 0:
                report.error(shot_path, 'requires positive "estimated_duration" or "duration"')
            shot_chars = require_list(report, shot, "characters_in_shot", shot_path)
            for item in shot_chars:
                if not is_non_empty_string(item):
                    report.error(shot_path, '"characters_in_shot" must contain only non-empty strings')
                elif character_ids and item not in character_ids:
                    report.error(shot_path, f'references unknown character id "{item}"')
            shot_props = shot.get("props_in_shot", [])
            if shot_props is not None:
                if not isinstance(shot_props, list):
                    report.error(shot_path, '"props_in_shot" must be a list')
                else:
                    for item in shot_props:
                        if not is_non_empty_string(item):
                            report.error(shot_path, '"props_in_shot" must contain only non-empty strings')
                        elif prop_ids and item not in prop_ids:
                            report.error(shot_path, f'references unknown prop id "{item}"')
            subject_constraints = shot.get("subject_constraints")
            if subject_constraints is not None:
                if not isinstance(subject_constraints, dict):
                    report.error(shot_path, '"subject_constraints" must be an object')
                else:
                    sc_path = f"{shot_path}.subject_constraints"
                    for key, value in subject_constraints.items():
                        if key in SUBJECT_CONSTRAINT_LIST_KEYS:
                            if not isinstance(value, list):
                                report.error(sc_path, f'"{key}" must be a list')
                                continue
                            if not all(is_non_empty_string(item) for item in value):
                                report.error(sc_path, f'"{key}" must contain only non-empty strings')
                            continue
                        if key in SUBJECT_CONSTRAINT_STRING_OR_LIST_KEYS:
                            if isinstance(value, str):
                                if not is_non_empty_string(value):
                                    report.error(sc_path, f'"{key}" must be a non-empty string when provided as string')
                            elif isinstance(value, list):
                                if not all(is_non_empty_string(item) for item in value):
                                    report.error(sc_path, f'"{key}" must contain only non-empty strings')
                            else:
                                report.error(sc_path, f'"{key}" must be a string or a list')
                            continue
                        if key in SUBJECT_CONSTRAINT_DICT_KEYS:
                            if not isinstance(value, dict):
                                report.error(sc_path, f'"{key}" must be an object')
                            continue
                        report.warn(sc_path, f'unknown subject_constraints key "{key}"')
            shot_type = shot.get("shot_type")
            if not is_non_empty_string(shot_type):
                report.error(shot_path, '"shot_type" is required and must be a non-empty string')
            elif str(shot_type).strip() not in VALID_SHOT_TYPES:
                report.error(shot_path, f'"shot_type" must be one of {sorted(VALID_SHOT_TYPES)}')
            else:
                cleaned_shot_type = str(shot_type).strip()
                sc = subject_constraints if isinstance(subject_constraints, dict) else {}
                required_visible = _clean_string_list(sc.get("required_visible_subjects"))
                offscreen_subjects = _clean_string_list(sc.get("offscreen_subjects"))
                continuity_subjects = _clean_string_list(sc.get("continuity_subjects"))
                forbidden_visible = _clean_string_list(sc.get("forbidden_visible_subjects"))

                if cleaned_shot_type == "offscreen_reaction":
                    if not subject_constraints:
                        report.error(
                            shot_path,
                            '"subject_constraints" is required for shot_type="offscreen_reaction"',
                        )
                    if not offscreen_subjects:
                        report.error(
                            shot_path,
                            'shot_type="offscreen_reaction" requires non-empty "subject_constraints.offscreen_subjects"',
                        )
                    if not forbidden_visible:
                        report.error(
                            shot_path,
                            'shot_type="offscreen_reaction" requires non-empty "subject_constraints.forbidden_visible_subjects"',
                        )
                elif cleaned_shot_type == "transition_reveal":
                    if not subject_constraints:
                        report.error(
                            shot_path,
                            '"subject_constraints" is required for shot_type="transition_reveal"',
                        )
                    if not required_visible:
                        report.error(
                            shot_path,
                            'shot_type="transition_reveal" requires non-empty "subject_constraints.required_visible_subjects"',
                        )
                    if not continuity_subjects:
                        report.error(
                            shot_path,
                            'shot_type="transition_reveal" requires non-empty "subject_constraints.continuity_subjects"',
                        )
            cont_mode = shot.get("continuity_mode")
            if cont_mode is not None and cont_mode not in VALID_CONTINUITY_MODES:
                report.error(shot_path, f'"continuity_mode" must be one of {sorted(VALID_CONTINUITY_MODES)}')
            motion_control = shot.get("motion_control")
            if motion_control is not None:
                if not isinstance(motion_control, dict):
                    report.error(shot_path, '"motion_control" must be an object')
                else:
                    mc_path = f"{shot_path}.motion_control"
                    for key in [
                        "subject_facing",
                        "camera_relation",
                        "movement_direction",
                        "screen_trajectory",
                        "target",
                        "distance_to_target",
                    ]:
                        require_non_empty_string(report, motion_control, key, mc_path)
                    phase_beats = require_list(report, motion_control, "phase_beats", mc_path)
                    if phase_beats and not all(is_non_empty_string(item) for item in phase_beats):
                        report.error(mc_path, '"phase_beats" must contain only non-empty strings')
                    distance = motion_control.get("distance_to_target")
                    if is_non_empty_string(distance) and distance not in VALID_DISTANCE_TO_TARGET:
                        report.error(mc_path, f'"distance_to_target" must be one of {sorted(VALID_DISTANCE_TO_TARGET)}')
                    movement = str(motion_control.get("movement_direction", "")).strip()
                    if movement in {"upstairs", "toward_target", "forward"} and distance == "getting_farther":
                        report.error(mc_path, 'movement_direction conflicts with "distance_to_target=getting_farther"')
                    if movement in {"away_from_target", "backward"} and distance == "getting_closer":
                        report.error(mc_path, 'movement_direction conflicts with "distance_to_target=getting_closer"')
            keyframes = shot.get("keyframes")
            if keyframes is not None:
                if not isinstance(keyframes, list):
                    report.error(shot_path, '"keyframes" must be a list')
                else:
                    prev_timestamp: float | None = None
                    for kf_idx, kf in enumerate(keyframes):
                        kf_path = f"{shot_path}.keyframes[{kf_idx}]"
                        if not isinstance(kf, dict):
                            report.error(kf_path, "must be an object")
                            continue
                        timestamp = kf.get("timestamp")
                        if not isinstance(timestamp, (int, float)):
                            report.error(kf_path, '"timestamp" must be a number')
                        else:
                            if timestamp <= 0 or timestamp >= shot_duration:
                                report.error(
                                    kf_path,
                                    f'"timestamp" must be between 0 and shot duration ({shot_duration})',
                                )
                            if prev_timestamp is not None and timestamp <= prev_timestamp:
                                report.error(kf_path, '"timestamp" must be strictly increasing')
                            prev_timestamp = float(timestamp)
                        if not is_non_empty_string(kf.get("description")):
                            report.error(kf_path, '"description" must be a non-empty string')
                    if cont_mode == "free" and keyframes:
                        report.warn(shot_path, '"keyframes" are ignored when continuity_mode is "free"')
            video_references = shot.get("video_references")
            if video_references is not None:
                if not isinstance(video_references, list):
                    report.error(shot_path, '"video_references" must be a list')
                else:
                    for ref_idx, ref in enumerate(video_references):
                        ref_path = f"{shot_path}.video_references[{ref_idx}]"
                        if not isinstance(ref, dict):
                            report.error(ref_path, "must be an object")
                            continue
                        usage = ref.get("usage")
                        if not is_non_empty_string(usage):
                            report.error(ref_path, '"usage" must be a non-empty string')
                        elif str(usage).strip() not in VALID_VIDEO_REFERENCE_USAGES:
                            report.error(
                                ref_path,
                                f'"usage" must be one of {sorted(VALID_VIDEO_REFERENCE_USAGES)}',
                            )
                        for key in ("path", "source", "source_type", "source_id", "subject", "stage", "description"):
                            value = ref.get(key)
                            if value is not None and not isinstance(value, str):
                                report.error(ref_path, f'"{key}" must be a string when provided')
                        source_type = str(ref.get("source_type", "")).strip()
                        source_id = str(ref.get("source_id", "")).strip()
                        if source_type == "character" and source_id and character_ids and source_id not in character_ids:
                            report.error(ref_path, f'references unknown character id "{source_id}"')
                        if source_type == "prop" and source_id and prop_ids and source_id not in prop_ids:
                            report.error(ref_path, f'references unknown prop id "{source_id}"')
                        enabled = ref.get("enabled")
                        if enabled is not None and not isinstance(enabled, bool):
                            report.error(ref_path, '"enabled" must be a boolean when provided')
                        timestamp = ref.get("timestamp")
                        if timestamp is not None and not isinstance(timestamp, (int, float)):
                            report.error(ref_path, '"timestamp" must be a number when provided')
                        if not any(is_non_empty_string(ref.get(key)) for key in ("path", "source", "source_type")):
                            report.warn(ref_path, 'should declare at least one of "path", "source", or "source_type"')


def validate_storyboard_legacy(data: dict[str, Any], report: ValidationReport) -> None:
    root = "storyboard"
    report.warn(root, 'legacy flat "shots" format detected; migrate to "scenes > shots" when possible')
    require_positive_number(report, data, "total_duration", root)
    shots = require_list(report, data, "shots", root)
    seen_ids: set[int] = set()
    for idx, shot in enumerate(shots):
        path = f"{root}.shots[{idx}]"
        if not isinstance(shot, dict):
            report.error(path, "must be an object")
            continue
        shot_id = shot.get("id")
        if not isinstance(shot_id, int):
            report.error(path, '"id" must be an integer')
        elif shot_id in seen_ids:
            report.error(path, f'duplicate shot id "{shot_id}"')
        else:
            seen_ids.add(shot_id)
        duration = shot.get("estimated_duration", shot.get("duration"))
        if not isinstance(duration, (int, float)) or duration <= 0:
            report.error(path, 'requires positive "estimated_duration" or "duration"')
        if not (
            is_non_empty_string(shot.get("visual_description"))
            or is_non_empty_string(shot.get("image_prompt"))
        ):
            report.error(path, 'requires "visual_description" or "image_prompt"')


def validate_storyboard(data: dict[str, Any], report: ValidationReport) -> None:
    if isinstance(data.get("scenes"), list):
        validate_storyboard_current(data, report)
    elif isinstance(data.get("shots"), list):
        validate_storyboard_legacy(data, report)
    else:
        report.error("storyboard", 'must contain either "scenes" or "shots"')


def detect_kind(file_path: Path, data: dict[str, Any]) -> str | None:
    name = file_path.name.lower()
    if name == "story.json":
        return "story"
    if name == "framework.json":
        return "framework"
    if name == "storyboard.json":
        return "storyboard"
    if "story_beats" in data:
        return "story"
    if "suggested_characters" in data and "suggested_locations" in data:
        return "framework"
    if "scenes" in data or "shots" in data:
        return "storyboard"
    return None


def validate_file(file_path: Path, kind: str | None = None) -> ValidationReport:
    report = ValidationReport()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.error(str(file_path), "file not found")
        return report
    except json.JSONDecodeError as err:
        report.error(str(file_path), f"invalid JSON: {err}")
        return report

    if not isinstance(data, dict):
        report.error(str(file_path), "root must be a JSON object")
        return report

    resolved_kind = kind or detect_kind(file_path, data)
    if resolved_kind == "story":
        validate_story(data, report)
    elif resolved_kind == "framework":
        validate_framework(data, report)
    elif resolved_kind == "storyboard":
        validate_storyboard(data, report)
    else:
        report.error(str(file_path), "unable to detect JSON kind; pass --kind story|framework|storyboard")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate xyz-video-skill JSON inputs")
    parser.add_argument("files", nargs="+", help="JSON files to validate")
    parser.add_argument("--kind", choices=["story", "framework", "storyboard"], help="Force JSON kind")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON report")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    results: list[dict[str, Any]] = []
    exit_code = 0

    for raw_path in args.files:
        file_path = Path(raw_path).expanduser().resolve()
        report = validate_file(file_path, args.kind)
        if not report.ok:
            exit_code = 1
        results.append(
            {
                "file": str(file_path),
                "ok": report.ok,
                "errors": report.errors,
                "warnings": report.warnings,
            }
        )

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "OK" if result["ok"] else "FAIL"
            print(f"[{status}] {result['file']}")
            for warning in result["warnings"]:
                print(f"  WARN  {warning}")
            for error in result["errors"]:
                print(f"  ERROR {error}")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
