#!/opt/homebrew/bin/python3.14
"""视频合成模块 — FFmpeg 拼接 + BGM 混合。

宿主 LLM 生成 storyboard.json，ad_assets.py 生成素材后，调用本脚本合成视频：
    python3 ad_compose.py --storyboard storyboard.json --assets assets.json --output_dir ./output/videos
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from utils import config_dir, read_yaml, setup_logging, write_json

logger = logging.getLogger(__name__)
OVERLAP_SCORE_THRESHOLD = 0.30


def _flatten_shots(storyboard: dict[str, Any]) -> list[dict[str, Any]]:
    """支持 scenes > shots 嵌套格式，向下兼容 flat shots。"""
    scenes = storyboard.get("scenes", [])
    if scenes:
        shots: list[dict[str, Any]] = []
        for scene in scenes:
            scene_id = scene.get("id")
            for shot in scene.get("shots", []):
                if isinstance(shot, dict):
                    enriched = dict(shot)
                    enriched["_scene_id"] = scene_id
                    shots.append(enriched)
        return shots
    return list(storyboard.get("shots", []))


def _normalize_transition_type(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if value in {"", "straight-cut", "straight_cut", "hard-cut", "hard_cut", "cut"}:
        return "straight-cut"
    if value in {"cross-dissolve", "cross_dissolve", "dissolve"}:
        return "cross-dissolve"
    if value in {"flash-white", "flash_white", "white-flash", "white_flash"}:
        return "flash-white"
    return value or "straight-cut"


def _clean_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _shot_characters(shot: dict[str, Any]) -> set[str]:
    value = shot.get("characters_in_shot", [])
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _has_any_token(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


def _pair_default_strategy(pair_type: str) -> tuple[str, float]:
    mapping = {
        "same_moment_overlap": ("straight-cut", 0.0),
        "continuous_action_same_scene": ("straight-cut", 0.0),
        "reverse_shot_same_scene": ("straight-cut", 0.0),
        "reaction_cut": ("straight-cut", 0.0),
        "impact_cut": ("straight-cut", 0.0),
        "scene_transition_soft": ("cross-dissolve", 0.25),
        "scene_transition_hard": ("straight-cut", 0.0),
        "scene_transition": ("cross-dissolve", 0.25),
    }
    return mapping.get(pair_type, ("straight-cut", 0.0))


def _pair_transition_candidates(pair_type: str) -> list[dict[str, Any]]:
    mapping: dict[str, list[tuple[str, float, str]]] = {
        "same_moment_overlap": [
            ("straight-cut", 0.0, "Same-moment overlap should usually cut directly after trimming duplicate motion."),
        ],
        "continuous_action_same_scene": [
            ("straight-cut", 0.0, "Continuous action in the same scene usually plays best as a direct cut."),
            ("cross-dissolve", 0.12, "Use only if the cut still feels visually abrupt after trimming."),
        ],
        "reverse_shot_same_scene": [
            ("straight-cut", 0.0, "Reverse shots usually need a crisp conversational or action eyeline cut."),
        ],
        "reaction_cut": [
            ("straight-cut", 0.0, "Reaction cuts are usually strongest as direct cuts."),
            ("cross-dissolve", 0.10, "Use sparingly if mood is softer than impact-driven."),
        ],
        "impact_cut": [
            ("straight-cut", 0.10, "Impact beats usually benefit from a hard cut with minimal delay."),
            ("flash-white", 0.12, "Use for stylized impact emphasis before or during a violent beat."),
        ],
        "scene_transition_soft": [
            ("cross-dissolve", 0.25, "Soft scene transition for reflective or gradual narrative flow."),
            ("straight-cut", 0.0, "Use if the narrative shift is clear enough without smoothing."),
        ],
        "scene_transition_hard": [
            ("straight-cut", 0.10, "Hard scene transition for forceful narrative shift."),
            ("flash-white", 0.12, "Use when the new scene should hit like a shock or impact reveal."),
        ],
        "scene_transition": [
            ("cross-dissolve", 0.25, "Default soft scene transition."),
            ("straight-cut", 0.0, "Use if the narrative intent is sharper than reflective."),
        ],
    }
    candidates = mapping.get(pair_type, [("straight-cut", 0.0, "Default direct cut.")])
    return [
        {
            "transition_type": transition_type,
            "transition_duration": duration,
            "reason": reason,
        }
        for transition_type, duration, reason in candidates
    ]


def _load_edit_judgments(path: Path | None) -> dict[tuple[int, int], dict[str, Any]]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("judgments", [])
    result: dict[tuple[int, int], dict[str, Any]] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item["from_shot"]), int(item["to_shot"]))
        except (KeyError, TypeError, ValueError):
            continue
        result[key] = item
    return result


def _explicit_transition_allowed_for_pair(pair_type: str, transition_type: str) -> bool:
    """只有当显式转场不违背语义时，才允许覆盖 pair 策略。"""
    if pair_type == "same_moment_overlap":
        return False
    if pair_type in {"reverse_shot_same_scene", "reaction_cut", "impact_cut"}:
        return transition_type == "straight-cut"
    if pair_type == "continuous_action_same_scene":
        return transition_type in {"straight-cut", "cross-dissolve"}
    if pair_type in {"scene_transition_soft", "scene_transition", "scene_transition_hard"}:
        return True
    return True


def _classify_pair_type(
    prev_shot: dict[str, Any],
    next_shot: dict[str, Any],
    same_scene: bool,
    chain: bool,
    visual_overlap_score: float,
) -> tuple[str, float]:
    prev_chars = _shot_characters(prev_shot)
    next_chars = _shot_characters(next_shot)
    shared_chars = prev_chars & next_chars
    prev_only = prev_chars - next_chars
    next_only = next_chars - prev_chars

    prev_text = " ".join(
        [
            _clean_text(prev_shot.get("narrative_segment")),
            _clean_text(prev_shot.get("action_prompt")),
            _clean_text(prev_shot.get("scene_prompt")),
        ]
    )
    next_text = " ".join(
        [
            _clean_text(next_shot.get("narrative_segment")),
            _clean_text(next_shot.get("action_prompt")),
            _clean_text(next_shot.get("scene_prompt")),
        ]
    )
    pair_text = f"{prev_text} {next_text}"

    reaction_tokens = ["听", "看向", "注视", "侧耳", "reaction", "reacts", "listens", "looks at", "turns to"]
    impact_tokens = ["猛然", "突然", "瞬间", "扑", "撞", "砸", "attack", "impact", "slam", "pounce", "lunge", "collision"]

    if same_scene and chain and visual_overlap_score >= OVERLAP_SCORE_THRESHOLD:
        confidence = 0.82 + (0.15 * visual_overlap_score)
        return "same_moment_overlap", confidence

    if same_scene:
        if shared_chars and prev_only and next_only:
            return "reverse_shot_same_scene", 0.88
        if _has_any_token(next_text, reaction_tokens):
            return "reaction_cut", 0.85
        if _has_any_token(pair_text, impact_tokens):
            return "impact_cut", 0.84
        return "continuous_action_same_scene", 0.82

    if _has_any_token(pair_text, ["与此同时", "随后", "然后", "afterward", "meanwhile", "later"]):
        return "scene_transition_soft", 0.9
    if _has_any_token(pair_text, impact_tokens):
        return "scene_transition_hard", 0.9
    return "scene_transition_soft", 0.92


def _extract_boundary_frames(
    video_path: Path,
    start_time: float,
    duration: float,
    fps: int = 8,
) -> list[tuple[float, Path]]:
    """从视频边界抽取少量缩略帧，用于重叠相似度估计。"""
    if duration <= 0:
        return []
    with tempfile.TemporaryDirectory(prefix="edit_frames_") as tmp:
        tmp_dir = Path(tmp)
        pattern = tmp_dir / "frame_%03d.png"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{max(0.0, start_time):.3f}",
            "-i", str(video_path),
            "-t", f"{duration:.3f}",
            "-vf", f"fps={fps},scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2",
            str(pattern),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            return []

        frames: list[tuple[float, Path]] = []
        for idx, frame_path in enumerate(sorted(tmp_dir.glob("frame_*.png"))):
            persisted = video_path.parent / f".{video_path.stem}_editprobe_{idx:03d}.png"
            shutil.copy2(frame_path, persisted)
            timestamp = start_time + (idx / fps)
            frames.append((timestamp, persisted))
        return frames


def _frame_mse(frame_a: Path, frame_b: Path) -> float:
    with Image.open(frame_a) as img_a, Image.open(frame_b) as img_b:
        a = np.asarray(img_a.convert("RGB"), dtype=np.float32)
        b = np.asarray(img_b.convert("RGB"), dtype=np.float32)
    if a.shape != b.shape or a.size == 0:
        return 1e9
    diff = a - b
    return float(np.mean(diff * diff))


def _estimate_overlap_window(prev_video: Path, next_video: Path) -> tuple[float, float, float]:
    """估计前后镜头的重叠区，返回 trim_out, trim_in, overlap_score。"""
    prev_duration = _get_duration(prev_video)
    next_duration = _get_duration(next_video)
    window = min(0.75, prev_duration, next_duration)
    if window <= 0.08:
        return 0.0, 0.0, 0.0

    prev_frames = _extract_boundary_frames(prev_video, max(0.0, prev_duration - window), window)
    next_frames = _extract_boundary_frames(next_video, 0.0, window)
    if not prev_frames or not next_frames:
        return 0.0, 0.0, 0.0

    best: tuple[float, float, float] | None = None
    for prev_time, prev_frame in prev_frames:
        for next_time, next_frame in next_frames:
            mse = _frame_mse(prev_frame, next_frame)
            if best is None or mse < best[0]:
                best = (mse, prev_time, next_time)

    for _, frame_path in prev_frames + next_frames:
        frame_path.unlink(missing_ok=True)

    if best is None:
        return 0.0, 0.0, 0.0

    mse, prev_time, next_time = best
    if mse > 1200:
        return 0.0, 0.0, max(0.0, 1.0 - (mse / 5000.0))

    trim_out = max(0.0, round(prev_duration - prev_time, 3))
    trim_in = max(0.0, round(next_time, 3))
    overlap_score = max(0.0, min(1.0, 1.0 - (mse / 1200.0)))
    trim_out = min(trim_out, window)
    trim_in = min(trim_in, window)
    return trim_out, trim_in, overlap_score


def build_edit_decisions(
    storyboard: dict[str, Any],
    assets: dict[str, Any],
    edit_judgments: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """基于相邻 shot 关系生成编辑决策。"""
    shots = _flatten_shots(storyboard)
    videos = {int(vid["shot_id"]): Path(vid["path"]) for vid in assets.get("videos", []) if "shot_id" in vid and "path" in vid}
    decisions: list[dict[str, Any]] = []

    for prev_shot, next_shot in zip(shots, shots[1:]):
        prev_id = int(prev_shot["id"])
        next_id = int(next_shot["id"])
        same_scene = prev_shot.get("_scene_id") == next_shot.get("_scene_id")
        chain = bool(next_shot.get("chain_from_previous", False))
        visual_overlap_score = 0.0
        trim_out = 0.0
        trim_in = 0.0

        if same_scene and chain and prev_id in videos and next_id in videos:
            trim_out, trim_in, visual_overlap_score = _estimate_overlap_window(videos[prev_id], videos[next_id])

        pair_type, confidence = _classify_pair_type(
            prev_shot=prev_shot,
            next_shot=next_shot,
            same_scene=same_scene,
            chain=chain,
            visual_overlap_score=visual_overlap_score,
        )

        if pair_type == "same_moment_overlap" and trim_out <= 0.01 and trim_in <= 0.01:
            trim_out = 0.12
            trim_in = 0.08
        elif pair_type != "same_moment_overlap":
            trim_out = 0.0
            trim_in = 0.0

        transition_type, transition_duration = _pair_default_strategy(pair_type)
        transition_candidates = _pair_transition_candidates(pair_type)

        explicit_transition = next_shot.get("transition_in")
        if isinstance(explicit_transition, dict):
            explicit_type = _normalize_transition_type(explicit_transition.get("type"))
            explicit_duration = float(explicit_transition.get("duration", transition_duration) or transition_duration)
            if _explicit_transition_allowed_for_pair(pair_type, explicit_type):
                transition_type = explicit_type
                transition_duration = explicit_duration
        elif isinstance(explicit_transition, str) and explicit_transition.strip():
            explicit_type = _normalize_transition_type(explicit_transition)
            if _explicit_transition_allowed_for_pair(pair_type, explicit_type):
                transition_type = explicit_type

        judgment = (edit_judgments or {}).get((prev_id, next_id))
        if judgment:
            chosen_type = _normalize_transition_type(judgment.get("transition_type", transition_type))
            allowed_types = {item["transition_type"] for item in transition_candidates}
            if chosen_type in allowed_types:
                transition_type = chosen_type
                transition_duration = float(judgment.get("transition_duration", transition_duration) or transition_duration)

        decision = {
            "from_shot": prev_id,
            "to_shot": next_id,
            "pair_type": pair_type,
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "signals": {
                "same_scene": same_scene,
                "chain_from_previous": chain,
                "visual_overlap_score": round(visual_overlap_score, 3),
                "prev_scene": prev_shot.get("_scene_id"),
                "next_scene": next_shot.get("_scene_id"),
                "shared_characters": sorted(_shot_characters(prev_shot) & _shot_characters(next_shot)),
            },
            "trim_out": round(trim_out, 3),
            "trim_in": round(trim_in, 3),
            "transition_type": transition_type,
            "transition_duration": round(transition_duration, 3),
            "transition_candidates": transition_candidates,
            "judgment_applied": bool(judgment),
            "needs_human_review": confidence < 0.7,
        }
        decisions.append(decision)

    return decisions


async def compose_all(
    storyboard: dict[str, Any],
    assets: dict[str, Any],
    output_dir: Path,
    platforms: list[str],
    edit_judgments: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """按平台列表合成视频。"""
    platforms_cfg = read_yaml(config_dir() / "platforms.yaml")
    videos = {}
    edit_decisions = build_edit_decisions(storyboard, assets, edit_judgments=edit_judgments)

    for platform_key, spec in platforms_cfg.get("platforms", {}).items():
        if platform_key not in platforms:
            continue
        video_path = await _compose_video(
            storyboard=storyboard,
            assets=assets,
            edit_decisions=edit_decisions,
            output_dir=output_dir,
            platform=platform_key,
            width=spec["width"],
            height=spec["height"],
        )
        if video_path:
            videos[platform_key] = str(video_path)
            logger.info(f"合成完成: {platform_key} → {video_path}")

    return videos, edit_decisions


async def _compose_video(
    storyboard: dict[str, Any],
    assets: dict[str, Any],
    edit_decisions: list[dict[str, Any]],
    output_dir: Path,
    platform: str,
    width: int,
    height: int,
) -> Path | None:
    """使用 FFmpeg 合成视频。优先使用 Seedance 视频片段，fallback 到图片。"""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg 未安装，跳过合成")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    segment_shot_ids: list[int] = []  # 与 segments 一一对应的 shot_id

    images = {img["shot_id"]: img for img in assets.get("images", [])}
    videos = {vid["shot_id"]: vid for vid in assets.get("videos", [])}
    shots = _flatten_shots(storyboard)
    decisions_by_from = {int(item["from_shot"]): item for item in edit_decisions}
    decisions_by_to = {int(item["to_shot"]): item for item in edit_decisions}

    for shot in shots:
        shot_id = shot["id"]
        seg_path = output_dir / f"seg_{shot_id}.mp4"
        duration = shot.get("estimated_duration", shot.get("duration", 5))
        trim_in = float(decisions_by_to.get(int(shot_id), {}).get("trim_in", 0.0) or 0.0)
        trim_out = float(decisions_by_from.get(int(shot_id), {}).get("trim_out", 0.0) or 0.0)

        # 优先使用 Seedance 生成的视频片段
        vid = videos.get(shot_id)
        if vid:
            vid_path = Path(vid["path"])
            if vid_path.exists():
                source_duration = _get_duration(vid_path)
                keep_duration = max(0.2, source_duration - trim_in - trim_out)
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{trim_in:.3f}",
                    "-i", str(vid_path),
                    "-t", f"{keep_duration:.3f}",
                    "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(seg_path),
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    segments.append(seg_path)
                    segment_shot_ids.append(int(shot_id))
                    logger.info(f"片段 {shot_id}: 使用 Seedance 视频")
                    continue
                except subprocess.CalledProcessError as e:
                    logger.warning(f"片段 {shot_id} 视频缩放失败，fallback 到图片: {e}")

        # Fallback：图片 → 静态视频片段
        img = images.get(shot_id)
        if not img:
            continue

        img_path = Path(img["path"])
        if not img_path.exists():
            continue

        still_duration = max(0.2, float(duration) - trim_in - trim_out)
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(img_path),
            "-t", f"{still_duration:.3f}",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(seg_path),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            segments.append(seg_path)
            segment_shot_ids.append(int(shot_id))
            logger.info(f"片段 {shot_id}: fallback 图片模式")
        except subprocess.CalledProcessError as e:
            logger.warning(f"片段 {shot_id} 合成失败: {e}")

    if not segments:
        return None

    # 拼接片段（支持转场效果）——按实际生成的 segment 顺序匹配转场
    merged = output_dir / f"{platform}_merged.mp4"
    transitions: list[dict[str, Any] | None] = [None]  # 第一个 segment 无转场
    for sid in segment_shot_ids[1:]:
        decision = decisions_by_to.get(sid, {})
        transitions.append(
            {
                "type": decision.get("transition_type", "straight-cut"),
                "duration": decision.get("transition_duration", 0.0),
            }
        )

    merged = _merge_segments_with_transitions(segments, transitions, merged)
    if not merged:
        return None

    # 混合 BGM
    final = output_dir / f"{platform}.mp4"
    bgm_path = Path(assets.get("bgm", {}).get("path", ""))

    if bgm_path.exists():
        # 检查 merged 视频是否有音轨
        has_audio = _has_audio_stream(merged)

        if has_audio:
            # 视频有音轨：混合视频音轨 + BGM
            cmd = [
                "ffmpeg", "-y", "-i", str(merged), "-stream_loop", "-1", "-i", str(bgm_path),
                "-filter_complex", "[0:a]volume=1[a0];[1:a]volume=0.2[a1];[a0][a1]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(final),
            ]
        else:
            # 视频无音轨：直接叠加 BGM
            cmd = [
                "ffmpeg", "-y", "-i", str(merged), "-stream_loop", "-1", "-i", str(bgm_path),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                "-shortest", str(final),
            ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return final
        except subprocess.CalledProcessError:
            return merged
    else:
        shutil.copy(merged, final)
        return final


def _merge_segments_with_transitions(
    segments: list[Path],
    transitions: list[dict | str | None],
    output: Path,
) -> Path | None:
    """按转场类型拼接视频片段。

    transitions[i] 对应第 i 个 shot 的 transition_in（即从 segment[i-1] 到 segment[i] 的过渡）。
    transitions[0] 通常为 None（第一个片段无前序）。

    支持的转场类型：
    - None / "straight-cut"：硬切（简单拼接）
    - "cross-dissolve"：交叉溶解（xfade filter）
    - "flash-white"：白闪（fadeout→白→fadein）

    transition 可以是字符串或 dict: {"type": "cross-dissolve", "duration": 0.5}
    """
    if not segments:
        return None
    if len(segments) == 1:
        shutil.copy(segments[0], output)
        return output

    # 规范化 transitions 列表，使其与 segments 等长
    norm: list[tuple[str, float]] = []
    for i in range(len(segments)):
        t = transitions[i] if i < len(transitions) else None
        if t is None:
            norm.append(("straight-cut", 0.0))
        elif isinstance(t, str):
            t_norm = _normalize_transition_type(t)
            norm.append((t_norm, 0.3 if t_norm != "straight-cut" else 0.0))
        elif isinstance(t, dict):
            t_norm = _normalize_transition_type(t.get("type", "straight-cut"))
            norm.append((t_norm, t.get("duration", 0.3)))
        else:
            norm.append(("straight-cut", 0.0))

    # 检查是否全部为硬切 → 走快速 concat 路径
    all_cuts = all(t == "straight-cut" for t, _ in norm)
    if all_cuts:
        return _simple_concat(segments, output)

    # 逐对拼接（使用 xfade filter chain）
    # ffmpeg xfade 只能两两处理，所以我们迭代地合并
    current = segments[0]
    tmp_dir = output.parent / "_transition_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, len(segments)):
        t_type, t_dur = norm[i]
        next_seg = segments[i]
        tmp_out = tmp_dir / f"merged_{i}.mp4"

        if t_type == "straight-cut" or t_dur <= 0:
            # 硬切：简单拼接两段
            tmp_out = _simple_concat([current, next_seg], tmp_out)
            if not tmp_out:
                logger.warning(f"硬切拼接失败 (段 {i})，尝试跳过")
                continue
        elif t_type == "cross-dissolve":
            tmp_out = _apply_xfade(current, next_seg, tmp_out, "fade", t_dur)
            if not tmp_out:
                logger.warning(f"交叉溶解失败 (段 {i})，fallback 硬切")
                tmp_out = tmp_dir / f"merged_{i}.mp4"
                tmp_out = _simple_concat([current, next_seg], tmp_out)
                if not tmp_out:
                    continue
        elif t_type == "flash-white":
            tmp_out = _apply_flash_white(current, next_seg, tmp_out, t_dur)
            if not tmp_out:
                logger.warning(f"白闪失败 (段 {i})，fallback 硬切")
                tmp_out = tmp_dir / f"merged_{i}.mp4"
                tmp_out = _simple_concat([current, next_seg], tmp_out)
                if not tmp_out:
                    continue
        else:
            logger.warning(f"未知转场类型 '{t_type}'，使用硬切")
            tmp_out = _simple_concat([current, next_seg], tmp_out)
            if not tmp_out:
                continue

        current = tmp_out

    # 复制最终结果到输出路径
    if current and current.exists():
        shutil.copy(current, output)
        # 清理临时文件
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return output

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _simple_concat(segments: list[Path], output: Path) -> Path | None:
    """简单拼接（硬切），使用 ffmpeg concat demuxer。"""
    filelist = output.parent / f"{output.stem}_list.txt"
    filelist.write_text(
        "\n".join(f"file '{s.resolve()}'" for s in segments),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(filelist), "-c", "copy", str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        filelist.unlink(missing_ok=True)
        return output
    except subprocess.CalledProcessError as e:
        logger.warning(f"concat 拼接失败: {e}")
        filelist.unlink(missing_ok=True)
        return None


def _get_duration(video: Path) -> float:
    """用 ffprobe 获取视频时长（秒）。"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 5.0  # 合理默认值


def _has_audio_stream(video: Path) -> bool:
    """检查视频文件是否包含音频流。"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0", str(video),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def _apply_xfade(
    seg_a: Path, seg_b: Path, output: Path,
    transition: str, duration: float,
) -> Path | None:
    """使用 ffmpeg xfade filter 实现两段视频间的过渡效果，同时保留音频。"""
    return _apply_xfade_impl(seg_a, seg_b, output, transition, duration)


def _apply_flash_white(
    seg_a: Path, seg_b: Path, output: Path, duration: float,
) -> Path | None:
    """白闪转场：使用 xfade 的 fadewhite 过渡效果，同时保留音频。"""
    return _apply_xfade_impl(seg_a, seg_b, output, "fadewhite", duration)


def _apply_xfade_impl(
    seg_a: Path, seg_b: Path, output: Path,
    transition: str, duration: float,
) -> Path | None:
    """xfade 统一实现：视频用 xfade，音频用 acrossfade（有音轨时）或静音填充。"""
    dur_a = _get_duration(seg_a)
    offset = max(0, dur_a - duration)

    has_audio_a = _has_audio_stream(seg_a)
    has_audio_b = _has_audio_stream(seg_b)

    if has_audio_a and has_audio_b:
        # 两段都有音频：视频 xfade + 音频 acrossfade
        filter_complex = (
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
            f"[0:a][1:a]acrossfade=d={duration}:c1=tri:c2=tri[a]"
        )
        map_args = ["-map", "[v]", "-map", "[a]"]
    elif has_audio_a or has_audio_b:
        # 只有一段有音频：给无音频的那段加静音，再 acrossfade
        dur_b = _get_duration(seg_b)
        silent_dur = dur_a if not has_audio_a else dur_b
        a_input = "0:a" if has_audio_a else "silent"
        b_input = "1:a" if has_audio_b else "silent"
        filter_complex = (
            f"anullsrc=r=44100:cl=stereo[silent];"
            f"[silent]atrim=0:{silent_dur}[silent];"
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
            f"[{a_input}][{b_input}]acrossfade=d={duration}:c1=tri:c2=tri[a]"
        )
        map_args = ["-map", "[v]", "-map", "[a]"]
    else:
        # 都没有音频：只做视频 xfade
        filter_complex = (
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v]"
        )
        map_args = ["-map", "[v]"]

    cmd = [
        "ffmpeg", "-y", "-i", str(seg_a), "-i", str(seg_b),
        "-filter_complex", filter_complex,
        *map_args, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output
    except subprocess.CalledProcessError as e:
        logger.warning(f"xfade ({transition}) 失败: {e}")
        return None


# ── CLI 入口 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="视频合成（FFmpeg 拼接 + BGM）")
    parser.add_argument("--storyboard", required=True, help="storyboard.json 路径")
    parser.add_argument("--assets", required=True, help="assets.json 路径")
    parser.add_argument("--output_dir", required=True, help="视频输出目录")
    parser.add_argument("--edit_judgments", help="可选的母基模转场裁定 JSON 路径")
    parser.add_argument("--platform", nargs="*", default=["youtube"], help="目标平台（youtube douyin wechat）")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    with open(Path(args.storyboard).expanduser().resolve(), encoding="utf-8") as f:
        storyboard = json.load(f)
    with open(Path(args.assets).expanduser().resolve(), encoding="utf-8") as f:
        assets = json.load(f)
    edit_judgments = _load_edit_judgments(Path(args.edit_judgments).expanduser().resolve()) if args.edit_judgments else {}

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    videos, edit_decisions = await compose_all(
        storyboard=storyboard,
        assets=assets,
        output_dir=output_dir,
        platforms=args.platform,
        edit_judgments=edit_judgments,
    )

    write_json(output_dir / "edit_decisions.json", edit_decisions)
    result = {
        "videos": videos,
        "output_dir": str(output_dir),
        "edit_decisions_path": str(output_dir / "edit_decisions.json"),
    }
    write_json(output_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
