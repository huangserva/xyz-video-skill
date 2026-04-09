#!/usr/bin/env python3
"""Summarize per-shot quality_audit.json files into one JSON report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_audit(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def summarize_shot(shot_dir: Path) -> dict[str, Any]:
    audit_path = shot_dir / "quality_audit.json"
    entries = load_audit(audit_path)
    attempts = []
    final_action = None
    redundant_attempts: list[int] = []
    for entry in entries:
        quality = entry.get("quality", {})
        attempts.append(
            {
                "attempt": entry.get("attempt"),
                "action": entry.get("action"),
                "trigger": quality.get("trigger"),
                "trim_to": quality.get("trim_to"),
                "profile": quality.get("profile"),
                "suggested_trim_time": quality.get("analysis", {}).get("suggested_trim_time"),
                "exemption_reason": quality.get("analysis", {}).get("exemption_reason"),
            }
        )
        final_action = entry.get("action")

    if len(entries) > 1 and final_action == "passed":
        redundant_attempts = [entry.get("attempt") for entry in entries[1:]]

    return {
        "shot": shot_dir.name,
        "attempt_count": len(entries),
        "final_action": final_action,
        "attempts": attempts,
        "redundant_attempts": redundant_attempts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize audit results")
    parser.add_argument("--audit-root", required=True, help="Directory containing shot_*/quality_audit.json")
    parser.add_argument("--output", required=True, help="Path to output JSON summary")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    audit_root = Path(args.audit_root)
    output_path = Path(args.output)
    shots = []
    for shot_dir in sorted(audit_root.glob("shot_*")):
        audit_path = shot_dir / "quality_audit.json"
        if audit_path.exists():
            shots.append(summarize_shot(shot_dir))

    summary = {"audit_root": str(audit_root), "shots": shots}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
