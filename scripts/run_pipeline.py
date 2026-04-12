#!/usr/bin/env python3
"""Execution orchestrator for xyz-video-skill.

This script does not generate story/framework/storyboard content by itself.
It orchestrates the existing execution stages around already-prepared JSON files:

- validate story/framework/storyboard
- generate character refs
- generate assets
- optionally brand assets
- compose final videos
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from utils import (
    VIDEO_REFERENCE_USAGE_ALIASES,
    create_output_dir,
    default_output_root,
    ensure_dir,
    read_json,
    setup_logging,
    timestamp_id,
    write_json,
)
from validate_json import validate_file


SCRIPT_DIR = Path(__file__).resolve().parent
AD_ASSETS = SCRIPT_DIR / "ad_assets.py"
AD_BRAND = SCRIPT_DIR / "ad_brand.py"
AD_COMPOSE = SCRIPT_DIR / "ad_compose.py"
STAGE_ORDER = ["validate", "refs", "assets", "brand", "compose"]


def run_checked(command: list[str]) -> None:
    subprocess.run(command, check=True)


def infer_legacy_shot_type(shot: dict[str, Any]) -> tuple[str, str]:
    """为旧 storyboard 保守回填 shot_type，避免新协议直接阻断老项目。"""
    continuity_mode = str(shot.get("continuity_mode", "")).strip()
    if continuity_mode == "free":
        return "free_atmosphere", 'continuity_mode="free"'

    subject_constraints = shot.get("subject_constraints")
    if isinstance(subject_constraints, dict):
        offscreen = subject_constraints.get("offscreen_subjects", [])
        continuity = subject_constraints.get("continuity_subjects", [])
        required_visible = subject_constraints.get("required_visible_subjects", [])
        if isinstance(offscreen, list) and any(str(item).strip() for item in offscreen):
            return "offscreen_reaction", 'subject_constraints.offscreen_subjects is non-empty'
        if isinstance(continuity, list) and any(str(item).strip() for item in continuity):
            if isinstance(required_visible, list) and any(str(item).strip() for item in required_visible):
                return "transition_reveal", (
                    "subject_constraints.continuity_subjects and "
                    "subject_constraints.required_visible_subjects are both non-empty"
                )

    return "visible_subject", "fallback default for legacy storyboard"


def normalize_video_reference_usage(value: Any) -> str:
    cleaned = str(value or "").strip()
    return VIDEO_REFERENCE_USAGE_ALIASES.get(cleaned, cleaned or "reference")


def should_generate_target_state_reference(shot: dict[str, Any], *, is_last_in_scene: bool) -> bool:
    continuity_mode = str(shot.get("continuity_mode", "scene_end")).strip() or "scene_end"
    return continuity_mode == "strict" or (continuity_mode == "scene_end" and is_last_in_scene)


def normalize_explicit_video_references(video_references: Any) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    notes: list[str] = []
    if not isinstance(video_references, list):
        return normalized, notes
    for idx, item in enumerate(video_references):
        if not isinstance(item, dict):
            notes.append(f"ignored non-object video_references[{idx}]")
            continue
        entry = dict(item)
        original_usage = entry.get("usage")
        normalized_usage = normalize_video_reference_usage(original_usage)
        if original_usage != normalized_usage:
            notes.append(f'normalized video_references[{idx}].usage from "{original_usage}" to "{normalized_usage}"')
        entry["usage"] = normalized_usage
        if not str(entry.get("source_type", "")).strip():
            if normalized_usage == "first_frame":
                entry["source_type"] = "frame"
                entry.setdefault("source_id", "first_frame")
            elif normalized_usage == "reference_target_state":
                entry["source_type"] = "frame"
                entry.setdefault("source_id", "target_state")
        normalized.append(entry)
    return normalized, notes


def infer_video_references(shot: dict[str, Any], *, is_last_in_scene: bool) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = [
        {"source_type": "frame", "source_id": "first_frame", "usage": "first_frame"},
        {"source_type": "scene", "usage": "reference_composition"},
    ]

    characters_in_shot = shot.get("characters_in_shot", [])
    if isinstance(characters_in_shot, list):
        for char_id in characters_in_shot:
            cleaned = str(char_id).strip()
            if cleaned:
                refs.append(
                    {
                        "source_type": "character",
                        "source_id": cleaned,
                        "usage": "reference_character",
                        "subject": cleaned,
                    }
                )

    props_in_shot = shot.get("props_in_shot", [])
    if isinstance(props_in_shot, list):
        for prop_id in props_in_shot:
            cleaned = str(prop_id).strip()
            if cleaned:
                refs.append(
                    {
                        "source_type": "prop",
                        "source_id": cleaned,
                        "usage": "reference_prop",
                        "subject": cleaned,
                    }
                )

    keyframes = shot.get("keyframes", [])
    if isinstance(keyframes, list):
        for idx, item in enumerate(keyframes, start=1):
            if not isinstance(item, dict):
                continue
            stage = str(item.get("stage") or item.get("goal") or "").strip()
            ref: dict[str, Any] = {
                "source_type": "stage",
                "source_id": str(idx),
                "usage": "reference_stage",
            }
            if stage:
                ref["stage"] = stage
            refs.append(ref)

    if should_generate_target_state_reference(shot, is_last_in_scene=is_last_in_scene):
        refs.append({"source_type": "frame", "source_id": "target_state", "usage": "reference_target_state"})

    return refs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run xyz-video-skill execution pipeline")
    parser.add_argument("--story", help="Optional story.json path (validated only)")
    parser.add_argument("--framework", help="framework.json path")
    parser.add_argument("--storyboard", required=True, help="storyboard.json path")
    parser.add_argument("--output_dir", help="Pipeline output root (default: auto timestamp dir)")
    parser.add_argument("--platform", nargs="*", default=["youtube"], help="Target platforms")
    parser.add_argument("--assets_manifest", help="Use an existing assets manifest when resuming from assets/brand/compose")
    parser.add_argument("--brand_manifest", help="Use an existing branded manifest when resuming from brand/compose")
    parser.add_argument("--logo_path", help="Optional logo path for branding")
    parser.add_argument("--brand_color", default="#FF6A00", help="Brand color for branding stage")
    parser.add_argument("--product_image", help="Optional product image path")
    parser.add_argument("--product_shots", default="", help="Comma-separated shot ids for product placement")
    parser.add_argument("--watermark_text", default="Brand Protected", help="Brand watermark text")
    parser.add_argument("--title_text", default="品牌广告", help="Brand title text")
    parser.add_argument("--from", dest="from_stage", choices=STAGE_ORDER, help="Start pipeline from this stage")
    parser.add_argument("--to", dest="to_stage", choices=STAGE_ORDER, help="Stop pipeline after this stage")
    parser.add_argument("--skip_validate", action="store_true", help="Skip JSON validation")
    parser.add_argument("--skip_refs", action="store_true", help="Skip character refs generation")
    parser.add_argument("--skip_assets", action="store_true", help="Skip assets generation")
    parser.add_argument("--skip_brand", action="store_true", help="Skip branding stage")
    parser.add_argument("--skip_compose", action="store_true", help="Skip video composition")
    parser.add_argument("--no_api", action="store_true", help="Pass through no-api mode to ad_assets")
    parser.add_argument("--parallel", type=int, default=4, help="Parallelism for asset generation")
    parser.add_argument("--review_mode", choices=["metrics_only", "hybrid_judge", "director_review"], help="Quality review mode for asset generation")
    parser.add_argument("--video_only", action="store_true", help="Debug mode: skip image generation, only generate videos (images must exist)")
    parser.add_argument("--image_width", type=int, default=1024, help="Image width for generated assets")
    parser.add_argument("--image_height", type=int, default=1024, help="Image height for generated assets")
    parser.add_argument("--resume", action="store_true", help="Resume asset generation from last checkpoint")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


def stage_enabled(args: argparse.Namespace, stage: str) -> bool:
    if stage not in STAGE_ORDER:
        return False

    if stage == "validate" and args.skip_validate:
        return False
    if stage == "refs" and args.skip_refs:
        return False
    if stage == "assets" and args.skip_assets:
        return False
    if stage == "brand" and args.skip_brand:
        return False
    if stage == "compose" and args.skip_compose:
        return False

    if args.from_stage:
        if STAGE_ORDER.index(stage) < STAGE_ORDER.index(args.from_stage):
            return False
    if args.to_stage:
        if STAGE_ORDER.index(stage) > STAGE_ORDER.index(args.to_stage):
            return False
    return True


def validate_inputs(args: argparse.Namespace) -> None:
    checks: list[tuple[str, Path]] = []
    if args.story:
        checks.append(("story", Path(args.story).expanduser().resolve()))
    if args.framework:
        checks.append(("framework", Path(args.framework).expanduser().resolve()))
    checks.append(("storyboard", Path(args.storyboard).expanduser().resolve()))

    failures = False
    for kind, path in checks:
        report = validate_file(path, kind)
        if report.warnings:
            for warning in report.warnings:
                print(f"WARN  {warning}")
        if report.errors:
            failures = True
            for error in report.errors:
                print(f"ERROR {error}")
    if failures:
        raise SystemExit(1)


def should_brand(args: argparse.Namespace) -> bool:
    return any(
        [
            bool(args.logo_path),
            bool(args.product_image),
            bool(args.product_shots.strip()),
            args.brand_color != "#FF6A00",
            args.watermark_text != "Brand Protected",
            args.title_text != "品牌广告",
        ]
    )


def normalize_storyboard(
    storyboard_path: Path,
    output_dir: Path,
    character_ref_dir: Path | None,
) -> tuple[Path, Path]:
    storyboard = read_json(storyboard_path)
    normalized_dir = ensure_dir(output_dir / "_normalized")
    binding_report: dict[str, Any] = {
        "storyboard": str(storyboard_path),
        "character_ref_dir": str(character_ref_dir) if character_ref_dir else None,
        "characters": {},
        "shot_type_backfilled": [],
        "video_references_backfilled": [],
        "video_references_normalized": [],
    }
    migration_report: dict[str, Any] = {
        "storyboard": str(storyboard_path),
        "normalized_storyboard": None,
        "character_ref_dir": str(character_ref_dir) if character_ref_dir else None,
        "summary": {
            "shot_count": 0,
            "shot_type_backfilled_count": 0,
            "video_references_backfilled_count": 0,
            "video_references_normalized_count": 0,
        },
        "shot_migrations": [],
    }

    if character_ref_dir:
        storyboard["character_ref_dir"] = str(character_ref_dir)
        manifest_path = character_ref_dir / "character_refs.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        manifest_chars = manifest.get("characters", {}) if isinstance(manifest, dict) else {}
        storyboard_chars = storyboard.get("characters", {}) if isinstance(storyboard.get("characters", {}), dict) else {}

        for char_id, char_info in storyboard_chars.items():
            if not isinstance(char_info, dict):
                continue

            report_entry: dict[str, Any] = {
                "storyboard_ref_image_before": char_info.get("ref_image"),
                "manifest_found": False,
            }
            manifest_info = manifest_chars.get(char_id, {})
            if isinstance(manifest_info, dict) and manifest_info:
                report_entry["manifest_found"] = True
                ref_image = manifest_info.get("ref_image")
                if ref_image:
                    char_info["ref_image"] = ref_image
                ref_path = manifest_info.get("path")
                if ref_path:
                    char_info["ref_path"] = ref_path
                for field in ("ref_description", "provider"):
                    value = manifest_info.get(field)
                    if value:
                        char_info[field] = value
                report_entry["storyboard_ref_image_after"] = char_info.get("ref_image")
                report_entry["manifest_ref_image"] = manifest_info.get("ref_image")
                report_entry["manifest_ref_path"] = ref_path
                resolved_path = Path(ref_path).expanduser().resolve() if ref_path else None
                report_entry["resolved_path"] = str(resolved_path) if resolved_path else None
                report_entry["resolved_exists"] = bool(resolved_path and resolved_path.exists())
            else:
                ref_image = str(char_info.get("ref_image", "")).strip()
                resolved_path = (character_ref_dir / ref_image).resolve() if ref_image else None
                report_entry["storyboard_ref_image_after"] = char_info.get("ref_image")
                report_entry["resolved_path"] = str(resolved_path) if resolved_path else None
                report_entry["resolved_exists"] = bool(resolved_path and resolved_path.exists())

            binding_report["characters"][char_id] = report_entry

    scenes = storyboard.get("scenes", [])
    if isinstance(scenes, list):
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            shots = scene.get("shots", [])
            if not isinstance(shots, list):
                continue
            for shot_index, shot in enumerate(shots):
                if not isinstance(shot, dict):
                    continue
                is_last_in_scene = shot_index == len(shots) - 1
                migration_report["summary"]["shot_count"] += 1
                original_shot_type = str(shot.get("shot_type", "")).strip() or None
                shot_id = shot.get("id")
                scene_id = scene.get("id")
                migration_entry: dict[str, Any] = {
                    "shot_id": shot_id,
                    "scene_id": scene_id,
                    "original_shot_type": original_shot_type,
                    "final_shot_type": original_shot_type,
                    "subject_constraints_present": isinstance(shot.get("subject_constraints"), dict),
                    "original_video_references_present": isinstance(shot.get("video_references"), list),
                    "notes": [],
                }
                if original_shot_type:
                    migration_entry["notes"].append("shot_type already present; kept as-is")
                else:
                    inferred, reason = infer_legacy_shot_type(shot)
                    shot["shot_type"] = inferred
                    migration_entry["final_shot_type"] = inferred
                    migration_entry["notes"].append(f"backfilled shot_type because {reason}")
                    binding_report["shot_type_backfilled"].append(
                        {
                            "shot_id": shot_id,
                            "scene_id": scene_id,
                            "inferred_shot_type": inferred,
                            "reason": reason,
                        }
                    )
                    migration_report["summary"]["shot_type_backfilled_count"] += 1

                explicit_video_references = shot.get("video_references")
                if isinstance(explicit_video_references, list) and explicit_video_references:
                    normalized_refs, notes = normalize_explicit_video_references(explicit_video_references)
                    shot["video_references"] = normalized_refs
                    if notes:
                        migration_entry["notes"].extend(notes)
                        binding_report["video_references_normalized"].append(
                            {
                                "shot_id": shot_id,
                                "scene_id": scene_id,
                                "notes": notes,
                            }
                        )
                        migration_report["summary"]["video_references_normalized_count"] += 1
                else:
                    inferred_refs = infer_video_references(shot, is_last_in_scene=is_last_in_scene)
                    shot["video_references"] = inferred_refs
                    migration_entry["notes"].append("backfilled video_references from shot fields and scene context")
                    binding_report["video_references_backfilled"].append(
                        {
                            "shot_id": shot_id,
                            "scene_id": scene_id,
                            "video_references": inferred_refs,
                        }
                    )
                    migration_report["summary"]["video_references_backfilled_count"] += 1

                migration_report["shot_migrations"].append(migration_entry)

    normalized_path = normalized_dir / "storyboard.json"
    migration_report_path = normalized_dir / "storyboard_migration_report.json"
    migration_report["normalized_storyboard"] = str(normalized_path)
    write_json(normalized_path, storyboard)
    write_json(normalized_dir / "ref_binding_report.json", binding_report)
    write_json(migration_report_path, migration_report)
    return normalized_path, migration_report_path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    output_root = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else create_output_dir()
    )
    output_root.mkdir(parents=True, exist_ok=True)

    story_path = Path(args.story).expanduser().resolve() if args.story else None
    framework_path = Path(args.framework).expanduser().resolve() if args.framework else None
    storyboard_path = Path(args.storyboard).expanduser().resolve()
    explicit_assets_manifest = Path(args.assets_manifest).expanduser().resolve() if args.assets_manifest else None
    explicit_brand_manifest = Path(args.brand_manifest).expanduser().resolve() if args.brand_manifest else None

    if args.from_stage and args.to_stage:
        if STAGE_ORDER.index(args.from_stage) > STAGE_ORDER.index(args.to_stage):
            raise SystemExit("--from stage must not come after --to stage")
    if explicit_assets_manifest and explicit_brand_manifest:
        raise SystemExit("pass only one of --assets_manifest or --brand_manifest")

    if stage_enabled(args, "validate"):
        validate_inputs(args)

    if stage_enabled(args, "refs") and framework_path is None:
        raise SystemExit("--framework is required when running refs stage")

    result: dict[str, Any] = {
        "output_dir": str(output_root),
        "inputs": {
            "story": str(story_path) if story_path else None,
            "framework": str(framework_path) if framework_path else None,
            "storyboard": str(storyboard_path),
            "assets_manifest": str(explicit_assets_manifest) if explicit_assets_manifest else None,
            "brand_manifest": str(explicit_brand_manifest) if explicit_brand_manifest else None,
        },
        "stages": {},
    }

    character_ref_dir: Path | None = None
    if stage_enabled(args, "refs") and framework_path:
        character_ref_dir = ensure_dir(output_root / "character_refs")
        command = [
            sys.executable,
            str(AD_ASSETS),
            "--mode",
            "character_refs",
            "--framework",
            str(framework_path),
            "--output_dir",
            str(character_ref_dir),
        ]
        if args.no_api:
            command.append("--no_api")
        if args.verbose:
            command.append("--verbose")
        run_checked(command)
        result["stages"]["character_refs"] = {
            "output_dir": str(character_ref_dir),
            "manifest": str(character_ref_dir / "character_refs.json"),
        }

    normalized_storyboard_path, migration_report_path = normalize_storyboard(storyboard_path, output_root, character_ref_dir)
    result["normalized_storyboard"] = str(normalized_storyboard_path)
    if migration_report_path.exists():
        migration_payload = read_json(migration_report_path)
        result["storyboard_migration"] = {
            "report_path": str(migration_report_path),
            "summary": migration_payload.get("summary", {}),
        }

    assets_manifest_path: Path | None = explicit_assets_manifest
    if stage_enabled(args, "assets"):
        assets_dir = ensure_dir(output_root / "assets")
        command = [
            sys.executable,
            str(AD_ASSETS),
            "--mode",
            "assets",
            "--storyboard",
            str(normalized_storyboard_path),
            "--output_dir",
            str(assets_dir),
            "--parallel",
            str(args.parallel),
            "--image_width",
            str(args.image_width),
            "--image_height",
            str(args.image_height),
        ]
        if args.review_mode:
            command.extend(["--review_mode", args.review_mode])
        if args.video_only:
            command.append("--video_only")
        if args.no_api:
            command.append("--no_api")
        if getattr(args, "resume", False):
            command.append("--resume")
        if args.verbose:
            command.append("--verbose")
        run_checked(command)
        assets_manifest_path = assets_dir / "assets.json"
        assets_payload = read_json(assets_manifest_path) if assets_manifest_path.exists() else {}
        pending_reviews = assets_payload.get("pending_reviews", []) if isinstance(assets_payload, dict) else []
        blocked_reviews = assets_payload.get("blocked_reviews", []) if isinstance(assets_payload, dict) else []
        result["stages"]["assets"] = {
            "output_dir": str(assets_dir),
            "manifest": str(assets_manifest_path),
        }
        if pending_reviews:
            result["stages"]["assets"]["pending_reviews"] = pending_reviews
            result["status"] = "pending_judgment"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if blocked_reviews:
            result["stages"]["assets"]["blocked_reviews"] = blocked_reviews
            if any(str(item.get("status", "")).strip() == "needs_regeneration" for item in blocked_reviews if isinstance(item, dict)):
                result["status"] = "needs_regeneration"
            else:
                result["status"] = "blocked"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
    elif assets_manifest_path is None and (output_root / "assets" / "assets.json").exists():
        assets_manifest_path = output_root / "assets" / "assets.json"

    compose_assets_path = explicit_brand_manifest or assets_manifest_path
    if stage_enabled(args, "brand") and assets_manifest_path and should_brand(args):
        brand_dir = ensure_dir(output_root / "brand")
        command = [
            sys.executable,
            str(AD_BRAND),
            "--assets_manifest",
            str(assets_manifest_path),
            "--storyboard_file",
            str(normalized_storyboard_path),
            "--brand_color",
            args.brand_color,
            "--watermark_text",
            args.watermark_text,
            "--title_text",
            args.title_text,
            "--output_dir",
            str(brand_dir),
        ]
        if args.logo_path:
            command.extend(["--logo_path", str(Path(args.logo_path).expanduser().resolve())])
        if args.product_image:
            command.extend(["--product_image", str(Path(args.product_image).expanduser().resolve())])
        if args.product_shots.strip():
            command.extend(["--product_shots", args.product_shots])
        if args.verbose:
            command.append("--verbose")
        run_checked(command)
        compose_assets_path = brand_dir / "brand_manifest.json"
        result["stages"]["brand"] = {
            "output_dir": str(brand_dir),
            "manifest": str(compose_assets_path),
        }
    elif explicit_brand_manifest is None and (output_root / "brand" / "brand_manifest.json").exists():
        compose_assets_path = output_root / "brand" / "brand_manifest.json"

    if stage_enabled(args, "compose"):
        if compose_assets_path is None:
            raise SystemExit("assets manifest missing; cannot compose without generated or branded assets")
        videos_dir = ensure_dir(output_root / "videos")
        command = [
            sys.executable,
            str(AD_COMPOSE),
            "--storyboard",
            str(normalized_storyboard_path),
            "--assets",
            str(compose_assets_path),
            "--output_dir",
            str(videos_dir),
            "--platform",
            *args.platform,
        ]
        if args.verbose:
            command.append("--verbose")
        run_checked(command)
        result["stages"]["compose"] = {
            "output_dir": str(videos_dir),
            "result": str(videos_dir / "result.json"),
            "assets_used": str(compose_assets_path),
        }

    result["selected_stages"] = {
        "from": args.from_stage or STAGE_ORDER[0],
        "to": args.to_stage or STAGE_ORDER[-1],
        "effective": [stage for stage in STAGE_ORDER if stage_enabled(args, stage)],
    }

    result_path = output_root / "pipeline_result.json"
    write_json(result_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
