#!/usr/bin/env python3
"""Offline vision judge for risk bundles."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

import aiohttp

from utils import extract_json, get_api_credentials, get_model_config, load_external_api_config, setup_logging, write_json


VALID_ACTIONS = {"keep", "cut_segment", "trim_tail", "regenerate", "needs_human_review"}
VALID_SEVERITIES = {"low", "medium", "high"}

SYSTEM_PROMPT = """You are a strict visual quality judge for AI-generated video shots.
You will receive storyboard constraints, risk segments, and sampled frames from those segments.
Your job is to decide whether each segment is acceptable or contains one of these issues:
- face_identity_drift
- identity_hallucination
- head_body_inconsistency
- face_orientation_discontinuity
- continuity_break
- no_issue

For overall_action decision rules:
- "keep": all segments are acceptable
- "cut_segment": problematic segments total duration < 50% of video AND remaining video >= 3 seconds
- "regenerate": problematic segments total duration >= 50% of video OR remaining video < 3 seconds

Output ONLY valid JSON. Do not wrap in markdown.
"""


def validate_segment(segment: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(segment.get("start"), (int, float)):
        errors.append('"start" must be a number')
    if not isinstance(segment.get("end"), (int, float)):
        errors.append('"end" must be a number')
    if segment.get("action") not in VALID_ACTIONS:
        errors.append(f'"action" must be one of {sorted(VALID_ACTIONS)}')
    if segment.get("severity") not in VALID_SEVERITIES:
        errors.append(f'"severity" must be one of {sorted(VALID_SEVERITIES)}')
    confidence = segment.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        errors.append('"confidence" must be a number between 0 and 1')
    if not isinstance(segment.get("reason"), str) or not segment.get("reason", "").strip():
        errors.append('"reason" must be a non-empty string')
    issue_type = segment.get("issue_type")
    if not isinstance(issue_type, str) or not issue_type.strip():
        errors.append('"issue_type" must be a non-empty string')
    return errors


def validate_result(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["result must be a JSON object"]
    segments = data.get("segments")
    if not isinstance(segments, list):
        errors.append('"segments" must be a list')
    else:
        for idx, segment in enumerate(segments):
            if not isinstance(segment, dict):
                errors.append(f"segments[{idx}] must be an object")
                continue
            for item in validate_segment(segment):
                errors.append(f"segments[{idx}]: {item}")
    overall_action = data.get("overall_action")
    if overall_action not in VALID_ACTIONS:
        errors.append(f'"overall_action" must be one of {sorted(VALID_ACTIONS)}')
    return errors


def build_user_content(request_data: dict[str, Any]) -> list[dict[str, Any]]:
    text_payload = {
        "shot_id": request_data.get("shot_id"),
        "review_mode": request_data.get("review_mode"),
        "camera_movement": request_data.get("camera_movement"),
        "motion_control": request_data.get("motion_control", {}),
        "characters_in_shot": request_data.get("characters_in_shot", []),
        "judge_questions": request_data.get("judge_questions", []),
        "risk_segments": [
            {"segment": item.get("segment", {}), "frame_count": len(item.get("frames", []))}
            for item in request_data.get("risk_segments", [])
        ],
        "output_schema": {
            "shot_id": "number or string",
            "segments": [
                {
                    "start": "number",
                    "end": "number",
                    "issue_type": "string",
                    "severity": "low|medium|high",
                    "confidence": "0..1",
                    "action": "keep|cut_segment|trim_tail|regenerate|needs_human_review",
                    "reason": "string",
                }
            ],
            "overall_action": "keep|cut_segment|trim_tail|regenerate|needs_human_review",
        },
    }
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(text_payload, ensure_ascii=False, indent=2)}]
    for idx, item in enumerate(request_data.get("risk_segments", []), start=1):
        segment = item.get("segment", {})
        content.append({"type": "text", "text": f"RISK SEGMENT {idx}: {json.dumps(segment, ensure_ascii=False)}"})
        for frame_path in item.get("frames", []):
            path = Path(frame_path)
            if not path.exists():
                continue
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    return content


def fallback_from_request(request_data: dict[str, Any]) -> dict[str, Any]:
    segments = []
    for item in request_data.get("risk_segments", []):
        segment = item.get("segment", {})
        action = "needs_human_review"
        confidence = float(segment.get("confidence", 0.5))
        if confidence >= 0.75 and segment.get("reason") in {"identity_hallucination", "face_identity_drift", "head_body_inconsistency"}:
            action = "cut_segment"
        segments.append(
            {
                "start": float(segment.get("start", 0.0)),
                "end": float(segment.get("end", 0.0)),
                "issue_type": str(segment.get("reason", "unknown")),
                "severity": "medium",
                "confidence": min(0.69, confidence),
                "action": action,
                "reason": "Fallback from metrics risk proposal due to vision judge failure.",
            }
        )
    overall_action = "keep"
    if any(seg["action"] == "cut_segment" for seg in segments):
        overall_action = "cut_segment"
    elif segments:
        overall_action = "needs_human_review"
    return {
        "shot_id": request_data.get("shot_id"),
        "segments": segments,
        "overall_action": overall_action,
        "fallback_used": True,
    }


async def call_model(request_data: dict[str, Any], timeout: int, max_retries: int) -> dict[str, Any]:
    judge_cfg = get_model_config("vision_judge")
    provider = str(judge_cfg.get("provider", "apimart")).strip()
    model = str(judge_cfg.get("model", get_model_config("llm").get("model", ""))).strip()
    creds = get_api_credentials(provider, load_external_api_config())
    if not creds.get("api_key"):
        raise RuntimeError(f"missing API key for provider {provider}")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_content(request_data)},
        ],
        "max_tokens": 2000,
        "stream": False,
    }

    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/chat/completions",
                    headers={"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"vision judge HTTP {resp.status}: {text[:400]}")
                    data = json.loads(text)
                    content = data["choices"][0]["message"]["content"]
                    parsed = extract_json(content)
                    if not isinstance(parsed, dict):
                        raise RuntimeError("vision judge did not return a JSON object")
                    errors = validate_result(parsed)
                    if errors:
                        raise RuntimeError("; ".join(errors))
                    parsed["fallback_used"] = False
                    return parsed
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(1)
    if last_error:
        raise last_error
    raise RuntimeError("vision judge failed without explicit error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run vision judge on a risk bundle")
    parser.add_argument("--request", required=True, help="Path to vision_judge_request.json")
    parser.add_argument("--output", required=True, help="Path to vision_judge_result.json")
    parser.add_argument("--dry_run", action="store_true", help="Only export the constructed prompt payload")
    parser.add_argument("--mock_result", help="Optional JSON file to use instead of calling the model")
    parser.add_argument("--timeout", type=int, default=90, help="Request timeout in seconds")
    parser.add_argument("--max_retries", type=int, default=3, help="Maximum judge retries")
    parser.add_argument("--auto_execute_threshold", type=float, default=0.7, help="Confidence threshold for automatic execution")
    parser.add_argument("--use-api", action="store_true", help="Use external API instead of waiting for manual judgment")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    request_path = Path(args.request).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    request_data = json.loads(request_path.read_text(encoding="utf-8"))

    if args.dry_run:
        payload = {
            "request": request_data,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_content(request_data)},
            ],
        }
        write_json(output_path, payload)
        return

    if args.mock_result:
        result = json.loads(Path(args.mock_result).expanduser().resolve().read_text(encoding="utf-8"))
        errors = validate_result(result)
        if errors:
            raise SystemExit("mock_result invalid: " + "; ".join(errors))
        result["fallback_used"] = False
    elif args.use_api:
        try:
            result = await call_model(request_data, timeout=args.timeout, max_retries=args.max_retries)
        except Exception:
            result = fallback_from_request(request_data)
    else:
        # 不使用 API，等待手动判断
        return

    for segment in result.get("segments", []):
        segment["auto_executable"] = float(segment.get("confidence", 0.0)) >= args.auto_execute_threshold
        if not segment["auto_executable"] and segment.get("action") != "keep":
            segment["action"] = "needs_human_review"

    # 合并相邻的 cut_segment：间隔 < merge_gap 秒时合并为一个
    merge_gap = 0.5
    segments = result.get("segments", [])
    cut_segs = sorted(
        [s for s in segments if s.get("action") == "cut_segment"],
        key=lambda s: float(s.get("start", 0)),
    )
    keep_segs = [s for s in segments if s.get("action") != "cut_segment"]
    merged: list[dict[str, Any]] = []
    for seg in cut_segs:
        if merged and float(seg["start"]) - float(merged[-1]["end"]) < merge_gap:
            prev = merged[-1]
            prev["end"] = max(float(prev["end"]), float(seg["end"]))
            prev["confidence"] = max(float(prev.get("confidence", 0)), float(seg.get("confidence", 0)))
            if seg.get("issue_type") != prev.get("issue_type"):
                prev["issue_type"] = f"{prev['issue_type']}+{seg['issue_type']}"
            prev["reason"] = f"{prev['reason']} | {seg['reason']}"
        else:
            merged.append(dict(seg))
    result["segments"] = keep_segs + merged

    if result.get("overall_action") != "keep":
        executable_actions = [seg.get("action") for seg in result.get("segments", []) if seg.get("auto_executable")]
        if not executable_actions and result.get("overall_action") != "needs_human_review":
            result["overall_action"] = "needs_human_review"

    write_json(output_path, result)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
