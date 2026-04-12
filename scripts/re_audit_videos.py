#!/usr/bin/env python3
"""Offline re-audit raw videos from an existing audit directory."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from ad_assets import AssetGenerator
from video_quality import trim_video_at, scan_video_quality, remove_video_segments


def copy_trimmed(source_video: Path, output_video: Path, trim_to: float) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_video, output_video)
    trim_video_at(output_video, trim_to)


def collect_attempts(source_audit_dir: Path) -> list[tuple[int, Path]]:
    attempts: list[tuple[int, Path]] = []
    for path in sorted(source_audit_dir.glob("raw_attempt_*.mp4")):
        suffix = path.stem.replace("raw_attempt_", "")
        try:
            attempts.append((int(suffix), path))
        except ValueError:
            continue
    return attempts


def re_audit_shot(
    source_audit_dir: Path,
    output_root: Path,
    camera_movement: str,
    expected_character_count: int,
    expected_subject_facing: str,
    detector_version: str,
    review_mode: str,
) -> None:
    shot_dir = output_root / source_audit_dir.name
    shot_dir.mkdir(parents=True, exist_ok=True)
    for existing in shot_dir.iterdir():
        if existing.is_file():
            existing.unlink()
    audit_log: list[dict[str, Any]] = []
    generator = AssetGenerator(storyboard={"characters": {}}, output_root=output_root, use_api=False, review_mode=review_mode)

    for attempt, source_video in collect_attempts(source_audit_dir):
        raw_copy = shot_dir / f"raw_attempt_{attempt}.mp4"
        shutil.copy2(source_video, raw_copy)
        quality = scan_video_quality(
            source_video,
            audit_dir=shot_dir,
            attempt=attempt,
            camera_movement=camera_movement,
            expected_character_count=expected_character_count,
            expected_subject_facing=expected_subject_facing,
            review_mode=review_mode,
        )
        entry: dict[str, Any] = {
            "attempt": attempt,
            "source_video": str(source_video),
            "raw_video": str(raw_copy),
            "status": "audited",
            "quality": {
                "ok": quality["ok"],
                "needs_regeneration": quality["needs_regeneration"],
                "trim_to": quality["trim_to"],
                "duration": quality["duration"],
                "profile": quality.get("profile"),
                "trigger": quality.get("trigger"),
                "analysis": quality.get("analysis", {}),
                "bad_segments": quality.get("bad_segments", []),
                "cut_segments": quality.get("cut_segments", []),
            },
            "detector_version": detector_version,
            "review_mode": review_mode,
        }
        if review_mode == "hybrid_judge":
            generator._export_risk_bundle(
                video_path=source_video,
                audit_dir=shot_dir,
                attempt=attempt,
                quality=quality,
                shot={
                    "id": source_audit_dir.name.replace("shot_", ""),
                    "camera_movement": camera_movement,
                    "motion_control": {"subject_facing": expected_subject_facing} if expected_subject_facing else {},
                    "characters_in_shot": ["_"] * expected_character_count,
                },
            )

            # 读取 vision_judge 结果
            bundle_dir = shot_dir / f"vision_bundle_attempt_{attempt}"
            judge_result_path = bundle_dir / "vision_judge_result.json"
            if judge_result_path.exists():
                entry["status"] = "judged"
                with open(judge_result_path, encoding="utf-8") as f:
                    judge_result = json.load(f)
                overall_action = judge_result.get("overall_action", "keep")
                entry["action"] = overall_action
                entry["vision_judge_result"] = judge_result

                if overall_action in ("cut_segment", "keep"):
                    entry["status"] = "finalized"
                    # 执行片段裁剪
                    if overall_action == "cut_segment":
                        segments_to_cut = [
                            {"start": seg["start"], "end": seg["end"]}
                            for seg in judge_result.get("segments", [])
                            if seg.get("action") == "cut_segment"
                        ]
                        if segments_to_cut:
                            entry["quality"]["cut_segments"] = segments_to_cut
                elif overall_action == "regenerate":
                    entry["status"] = "applied"
            else:
                entry["status"] = "pending_judgment"
                entry["action"] = "vision_judge_pending"

            audit_log.append(entry)
            continue
        if quality.get("cut_segments"):
            trimmed_copy = shot_dir / f"trimmed_attempt_{attempt}.mp4"
            copy_trimmed(source_video, trimmed_copy, duration := quality["duration"])
            remove_video_segments(trimmed_copy, quality["cut_segments"])
            entry["action"] = "segment_cut"
            entry["trimmed_video"] = str(trimmed_copy)
        elif quality["trim_to"] is not None:
            trimmed_copy = shot_dir / f"trimmed_attempt_{attempt}.mp4"
            copy_trimmed(source_video, trimmed_copy, quality["trim_to"])
            entry["action"] = "trimmed"
            entry["trimmed_video"] = str(trimmed_copy)
        else:
            trimmed_copy = shot_dir / f"trimmed_attempt_{attempt}.mp4"
            if trimmed_copy.exists():
                trimmed_copy.unlink()
            entry["action"] = "passed"
        audit_log.append(entry)

    audit_json = shot_dir / "quality_audit.json"
    with open(audit_json, "w", encoding="utf-8") as handle:
        json.dump(audit_log, handle, indent=2, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline re-audit raw videos from an existing audit directory")
    parser.add_argument("--source-audit-root", required=True, help="Existing audit root containing shot_*/raw_attempt_N.mp4")
    parser.add_argument("--output-root", required=True, help="Output directory for re-audit results")
    parser.add_argument("--detector-version", default="audit2_profiles_v2", help="Version label written to quality_audit.json")
    parser.add_argument("--review_mode", choices=["metrics_only", "hybrid_judge"], default="metrics_only", help="Review mode for offline audit")
    parser.add_argument(
        "--camera",
        action="append",
        default=[],
        help="Override camera movement per shot, format: shot_001=slow push-in with slight rotation",
    )
    parser.add_argument(
        "--chars",
        action="append",
        default=[],
        help="Expected character count per shot, format: shot_001=2",
    )
    parser.add_argument(
        "--facing",
        action="append",
        default=[],
        help="Expected subject facing per shot, format: shot_001=toward_camera",
    )
    parser.add_argument("shots", nargs="+", help="Shot directory names, e.g. shot_001 shot_004")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source_root = Path(args.source_audit_root)
    output_root = Path(args.output_root)
    camera_map: dict[str, str] = {}
    chars_map: dict[str, int] = {}
    facing_map: dict[str, str] = {}
    for item in args.camera:
        if "=" not in item:
            raise SystemExit(f"Invalid --camera value: {item}")
        shot, value = item.split("=", 1)
        camera_map[shot.strip()] = value.strip()
    for item in args.chars:
        if "=" not in item:
            raise SystemExit(f"Invalid --chars value: {item}")
        shot, value = item.split("=", 1)
        chars_map[shot.strip()] = int(value.strip())
    for item in args.facing:
        if "=" not in item:
            raise SystemExit(f"Invalid --facing value: {item}")
        shot, value = item.split("=", 1)
        facing_map[shot.strip()] = value.strip()

    for shot in args.shots:
        source_audit_dir = source_root / shot
        if not source_audit_dir.exists():
            raise SystemExit(f"Missing source audit dir: {source_audit_dir}")
        re_audit_shot(
            source_audit_dir=source_audit_dir,
            output_root=output_root,
            camera_movement=camera_map.get(shot, ""),
            expected_character_count=chars_map.get(shot, 0),
            expected_subject_facing=facing_map.get(shot, ""),
            detector_version=args.detector_version,
            review_mode=args.review_mode,
        )


if __name__ == "__main__":
    main()
