#!/opt/homebrew/bin/python3.14
"""素材生成模块 — 图片/视频/BGM 生成。

宿主 LLM 生成 storyboard.json 后，调用本脚本生成素材：
    python3 ad_assets.py --storyboard storyboard.json --output_dir ./output/assets
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import shutil
import json
import logging
import os
import mimetypes
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np
from PIL import Image, ImageDraw

from utils import get_api_credentials, get_model_config, load_external_api_config, setup_logging, timestamp_id, write_json, write_text
from content_filter import ContentFilter, VideoPromptBuilder

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent
DETECTOR_VERSION = "quality_profiles_v5"
REVIEW_MODES = {"metrics_only", "hybrid_judge", "director_review"}
SHOT_TYPES = {"visible_subject", "offscreen_reaction", "transition_reveal", "free_atmosphere"}
VIDEO_REFERENCE_USAGE_PRIORITY = {
    "first_frame": 0,
    "reference_character": 1,
    "reference_prop": 2,
    "reference_composition": 3,
    "reference_style": 4,
    "reference_color": 5,
    "reference_target_state": 6,
    "reference_stage": 7,
    "reference_motion": 8,
    "reference": 99,
}
VIDEO_REFERENCE_USAGE_ALIASES = {
    "first_frame": "first_frame",
    "last_frame": "reference_target_state",
    "keyframe": "reference_stage",
    "reference_character": "reference_character",
    "reference_prop": "reference_prop",
    "reference_composition": "reference_composition",
    "reference_style": "reference_style",
    "reference_color": "reference_color",
    "reference_target_state": "reference_target_state",
    "reference_stage": "reference_stage",
    "reference_motion": "reference_motion",
}

QUALITY_PROFILES: dict[str, dict[str, float | int]] = {
    "static": {
        "threshold_base": 250,
        "threshold_multiplier": 6,
        "required_stable": 6,
        "consecutive_bad": 8,
        "grace_window_frames": 8,
        "confirm_bad_frames": 6,
        "trim_backoff_frames": 2,
        "min_amplitude_base": 80,
        "min_amplitude_multiplier": 4,
        "reversal_count": 8,
        "spike_factor": 2.0,
        "spike_abs_min": 120,
    },
    "medium_motion": {
        "threshold_base": 350,
        "threshold_multiplier": 10,
        "required_stable": 6,
        "consecutive_bad": 14,
        "grace_window_frames": 12,
        "confirm_bad_frames": 8,
        "trim_backoff_frames": 3,
        "min_amplitude_base": 120,
        "min_amplitude_multiplier": 5,
        "reversal_count": 10,
        "spike_factor": 3.0,
        "spike_abs_min": 180,
    },
    "heavy_motion": {
        "threshold_base": 500,
        "threshold_multiplier": 14,
        "required_stable": 8,
        "consecutive_bad": 20,
        "grace_window_frames": 18,
        "confirm_bad_frames": 10,
        "trim_backoff_frames": 4,
        "min_amplitude_base": 180,
        "min_amplitude_multiplier": 6,
        "reversal_count": 12,
        "spike_factor": 3.5,
        "spike_abs_min": 250,
    },
}

# ── Gemini 图片生成 system prompt ────────────────────────────────

GEMINI_IMAGE_SYSTEM_PROMPT = (
    "You are a cinematic image generator for a professional video production pipeline. "
    "Your task is to generate a single cinematic frame that will be used as "
    "a keyframe for AI video generation (Seedance I2V).\n\n"
    "CRITICAL RULES:\n"
    "1. STYLE LOCK: The style_anchor provided in the prompt defines the EXACT visual style — "
    "color grading, rendering quality, art direction, and aesthetic. You MUST match this style "
    "precisely in EVERY frame, whether it is a wide shot, close-up, or detail shot. "
    "NEVER switch between illustrated/animated and photorealistic styles within the same project. "
    "If the style anchor says 'painterly quality', ALL frames must have that painterly quality.\n"
    "2. CHARACTER FIDELITY: When reference images are provided, the generated character MUST be "
    "visually identical — same face, proportions, colors, textures, and distinguishing features.\n"
    "3. VISUAL INFERENCE: Infer logically necessary details that the prompt may not explicitly state. "
    "Examples: rain → wet surfaces, reflective puddles, damp hair and clothes; "
    "holding an umbrella in rain; battle scene → debris, dust, scorch marks; "
    "cold weather → visible breath vapor, reddened cheeks.\n"
    "4. NO TEXT: Never include any text, labels, watermarks, or annotations in the image.\n"
    "5. CINEMATIC COMPOSITION: Frame the shot as a professional cinematographer would — "
    "follow the rule of thirds, use depth of field, and create visual depth."
)


class AssetGenerator:
    """素材生成器。"""

    def __init__(
        self,
        storyboard: dict[str, Any],
        output_root: Path,
        image_width: int = 1024,
        image_height: int = 1024,
        parallel: int = 4,
        use_api: bool = True,
        review_mode: str | None = None,
        video_only: bool = False,
    ):
        self.storyboard = storyboard
        self.output_root = output_root
        self.image_width = image_width
        self.image_height = image_height
        self.use_api = use_api
        self.video_only = video_only  # 调试模式：跳过图片生成
        self.sem = asyncio.Semaphore(parallel)

        self.image_dir = output_root / "images"
        self.voice_dir = output_root / "voiceovers"
        self.bgm_dir = output_root / "bgm"
        self.video_dir = output_root / "videos"

        for d in [self.image_dir, self.voice_dir, self.bgm_dir, self.video_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self.cfg = load_external_api_config()
        video_cfg = get_model_config("video")
        configured_review_mode = str(video_cfg.get("review_mode", "metrics_only")).strip() or "metrics_only"
        self.review_mode = review_mode or configured_review_mode
        if self.review_mode not in REVIEW_MODES:
            logger.warning(f"未知 review_mode={self.review_mode}，回退到 metrics_only")
            self.review_mode = "metrics_only"
        self.director_prompts = self._load_director_prompts()

    def _video_fallback_chain(self) -> list[str]:
        video_cfg = get_model_config("video")
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            provider = str(video_cfg.get("provider", "byteplus")).strip()
            return [provider] if provider else ["byteplus"]
        return list(video_cfg.get("fallback_chain", ["byteplus"]))

    def _video_provider_config(self, provider: str) -> dict[str, Any]:
        video_cfg = get_model_config("video")
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            selected = str(video_cfg.get("provider", "")).strip()
            return video_cfg if provider == selected else {}
        return dict(video_cfg.get(provider, {}))

    def _any_video_provider_supports_stage_references(self) -> bool:
        return any(
            int(self._video_provider_config(provider).get("max_reference_images", 2)) > 2
            for provider in self._video_fallback_chain()
        )

    def _max_video_reference_images(self) -> int:
        max_refs = 2
        for provider in self._video_fallback_chain():
            pcfg = self._video_provider_config(provider)
            if not pcfg:
                continue
            max_refs = max(max_refs, int(pcfg.get("max_reference_images", 2)))
        return max_refs

    @staticmethod
    def _path_to_data_url(path: Path) -> str:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _build_evolink_reference_prompt(prompt: str, refs: list[dict[str, Any]]) -> str:
        return VideoPromptBuilder.compose_video_generation_prompt(prompt, refs, mention_prefix="@Image")

    @staticmethod
    def _normalize_video_reference_usage(value: Any) -> str:
        cleaned = str(value or "").strip()
        return VIDEO_REFERENCE_USAGE_ALIASES.get(cleaned, cleaned or "reference")

    @staticmethod
    def _video_reference_priority(ref: dict[str, Any]) -> tuple[int, int]:
        usage = AssetGenerator._normalize_video_reference_usage(ref.get("usage"))
        source_type = str(ref.get("source_type", "")).strip()
        bonus = 0
        if usage == "reference_composition" and source_type == "scene":
            bonus = -1
        return VIDEO_REFERENCE_USAGE_PRIORITY.get(usage, 99), bonus

    @staticmethod
    def _quality_profile_for_camera(camera_movement: str) -> str:
        movement = camera_movement.strip().lower()
        if not movement or movement == "static":
            return "static"
        if any(token in movement for token in ["orbital", "crane", "whip", "rapid", "fast", "handheld", "tracking"]):
            return "heavy_motion"
        return "medium_motion"

    def _review_config(self) -> dict[str, Any]:
        video_cfg = get_model_config("video")
        review_cfg = dict(video_cfg.get("review", {}).get(self.review_mode, {}))
        strict_modes = {"hybrid_judge", "director_review"}
        review_cfg.setdefault("metrics_profile", "strict" if self.review_mode in strict_modes else "relaxed")
        review_cfg.setdefault("export_risk_bundle", self.review_mode == "hybrid_judge")
        return review_cfg

    def _load_director_prompts(self) -> dict[int, dict[str, Any]]:
        raw = self.storyboard.get("director_prompts")
        if isinstance(raw, dict):
            shots = raw.get("shots", raw)
            if isinstance(shots, dict):
                loaded: dict[int, dict[str, Any]] = {}
                for key, value in shots.items():
                    try:
                        shot_id = int(key)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        loaded[shot_id] = value
                if loaded:
                    logger.info(f"已加载内联 director_prompts: {len(loaded)} shots")
                    return loaded

        path_value = str(self.storyboard.get("director_prompts_file", "")).strip()
        if not path_value:
            return {}

        candidates = [
            Path(path_value).expanduser(),
            (SCRIPT_DIR.parent / path_value).expanduser(),
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                shots = data.get("shots", data)
                if not isinstance(shots, dict):
                    continue
                loaded: dict[int, dict[str, Any]] = {}
                for key, value in shots.items():
                    try:
                        shot_id = int(key)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        loaded[shot_id] = value
                if loaded:
                    logger.info(f"已加载 director_prompts 文件: {candidate}")
                    return loaded
            except Exception as e:
                logger.warning(f"读取 director_prompts 失败 ({candidate}): {e}")
        logger.warning(f"director_prompts_file 未找到或不可读: {path_value}")
        return {}

    def _director_prompt_entry(self, shot_id: int) -> dict[str, Any]:
        entry = self.director_prompts.get(shot_id, {})
        return entry if isinstance(entry, dict) else {}

    @staticmethod
    def _shot_type(shot: dict[str, Any]) -> str:
        shot_type = str(shot.get("shot_type", "")).strip()
        if shot_type in SHOT_TYPES:
            return shot_type
        if str(shot.get("continuity_mode", "")).strip() == "free":
            return "free_atmosphere"
        return "visible_subject"

    @staticmethod
    def _spec_active_for_shot(spec: dict[str, Any], shot_id: int) -> bool:
        applies_from = spec.get("applies_from_shot")
        applies_to = spec.get("applies_to_shot")
        try:
            if applies_from is not None and shot_id < int(applies_from):
                return False
            if applies_to is not None and shot_id > int(applies_to):
                return False
        except (TypeError, ValueError):
            return True
        return True

    @classmethod
    def _resolve_scene_props_for_shot(cls, props: Any, shot_id: int) -> list[str]:
        resolved: list[str] = []
        if not isinstance(props, list):
            return resolved
        for item in props:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    resolved.append(text)
            elif isinstance(item, dict):
                if not cls._spec_active_for_shot(item, shot_id):
                    continue
                text = str(item.get("name") or item.get("text") or "").strip()
                if text:
                    resolved.append(text)
        return resolved

    @classmethod
    def _resolve_scene_continuity_for_shot(cls, scene_continuity: Any, shot_id: int) -> dict[str, Any]:
        if not isinstance(scene_continuity, dict):
            return {}

        resolved: dict[str, Any] = {}
        stable_facts = scene_continuity.get("stable_facts", {})
        if isinstance(stable_facts, dict):
            resolved_facts: dict[str, list[str]] = {}
            for key, value in stable_facts.items():
                items: list[str] = []
                if isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, str):
                            text = entry.strip()
                            if text:
                                items.append(text)
                        elif isinstance(entry, dict):
                            if not cls._spec_active_for_shot(entry, shot_id):
                                continue
                            text = str(entry.get("text") or entry.get("description") or "").strip()
                            if text:
                                items.append(text)
                if items:
                    resolved_facts[key] = items
            if resolved_facts:
                resolved["stable_facts"] = resolved_facts

        entity_registry = scene_continuity.get("entity_registry", {})
        if isinstance(entity_registry, dict):
            resolved_registry: dict[str, Any] = {}
            for entity_id, config in entity_registry.items():
                if not isinstance(config, dict):
                    continue
                if not cls._spec_active_for_shot(config, shot_id):
                    continue
                cleaned = dict(config)
                cleaned.pop("applies_from_shot", None)
                cleaned.pop("applies_to_shot", None)
                resolved_registry[entity_id] = cleaned
            if resolved_registry:
                resolved["entity_registry"] = resolved_registry

        carry_forward = scene_continuity.get("carry_forward_subjects", [])
        if isinstance(carry_forward, list):
            cleaned = [str(item).strip() for item in carry_forward if str(item).strip()]
            if cleaned:
                resolved["carry_forward_subjects"] = cleaned

        return resolved

    @staticmethod
    def _director_plan(shot: dict[str, Any]) -> dict[str, Any]:
        plan = shot.get("director_plan", {})
        return plan if isinstance(plan, dict) else {}

    @classmethod
    def _director_nodes(cls, shot: dict[str, Any]) -> list[dict[str, Any]]:
        plan = cls._director_plan(shot)
        nodes = plan.get("nodes", [])
        if not isinstance(nodes, list):
            return []
        return [node for node in nodes if isinstance(node, dict)]

    @staticmethod
    def _director_node_text(node: dict[str, Any], *, include_delta: bool = False) -> str:
        parts: list[str] = []
        story_function = str(node.get("story_function", "")).strip()
        visual_focus = str(node.get("visual_focus", "")).strip()
        must_show = node.get("must_show", [])
        must_not_show = node.get("must_not_show", [])
        delta_from_previous = str(node.get("delta_from_previous", "")).strip()

        if story_function:
            parts.append(story_function)
        if visual_focus:
            parts.append(f"视觉重心：{visual_focus}")
        if isinstance(must_show, list):
            cleaned_show = [str(item).strip() for item in must_show if str(item).strip()]
            if cleaned_show:
                parts.append(f"必须出现：{', '.join(cleaned_show)}")
        if isinstance(must_not_show, list):
            cleaned_not_show = [str(item).strip() for item in must_not_show if str(item).strip()]
            if cleaned_not_show:
                parts.append(f"不能提前出现：{', '.join(cleaned_not_show)}")
        if include_delta and delta_from_previous:
            parts.append(f"相对上一阶段主变化：{delta_from_previous}")
        return "；".join(parts).strip()

    @classmethod
    def _director_action_text(cls, nodes: list[dict[str, Any]]) -> str:
        transitions: list[str] = []
        for idx, node in enumerate(nodes[1:], start=2):
            delta = str(node.get("delta_from_previous", "")).strip()
            story_function = str(node.get("story_function", "")).strip()
            if delta:
                transitions.append(f"阶段{idx - 1}到阶段{idx}：{delta}")
            elif story_function:
                transitions.append(f"阶段{idx - 1}到阶段{idx}：推进到{story_function}")
        return "；".join(transitions).strip()

    @classmethod
    def _resolve_shot_contract(cls, shot: dict[str, Any]) -> dict[str, Any]:
        director_plan = cls._director_plan(shot)
        nodes = cls._director_nodes(shot)
        estimated_duration = max(int(shot.get("estimated_duration", 10) or 10), 1)

        legacy_scene = str(shot.get("scene_prompt") or shot.get("image_prompt", "")).strip()
        legacy_action = str(shot.get("action_prompt", "")).strip()
        legacy_end = str(shot.get("end_frame_description", "")).strip()
        legacy_keyframes = shot.get("keyframes", [])
        if not isinstance(legacy_keyframes, list):
            legacy_keyframes = []

        first_node = nodes[0] if nodes else None
        last_node = nodes[-1] if nodes else None
        middle_nodes = nodes[1:-1] if len(nodes) > 2 else []

        scene_prompt = cls._director_node_text(first_node) if first_node else legacy_scene
        end_frame_description = cls._director_node_text(last_node) if last_node else legacy_end
        action_prompt = cls._director_action_text(nodes) if nodes else legacy_action

        keyframes: list[dict[str, Any]]
        if middle_nodes:
            denominator = max(len(nodes) - 1, 1)
            keyframes = []
            for idx, node in enumerate(middle_nodes, start=1):
                timestamp = round((estimated_duration * idx) / denominator, 2)
                keyframes.append(
                    {
                        "timestamp": timestamp,
                        "description": cls._director_node_text(node),
                    }
                )
        else:
            keyframes = [item for item in legacy_keyframes if isinstance(item, dict)]

        return {
            "director_plan": director_plan,
            "nodes": nodes,
            "first_node": first_node,
            "last_node": last_node,
            "middle_nodes": middle_nodes,
            "scene_prompt": scene_prompt or legacy_scene,
            "action_prompt": action_prompt or legacy_action,
            "end_frame_description": end_frame_description or legacy_end,
            "keyframes": keyframes,
        }

    async def run(self) -> dict[str, Any]:
        """执行素材生成。"""
        # ── 支持 scenes > shots 结构，向下兼容 flat shots ──
        scenes = self.storyboard.get("scenes", [])
        if not scenes:
            flat_shots = self.storyboard.get("shots", [])
            if not flat_shots:
                raise ValueError("storyboard 缺少 scenes 或 shots")
            # 向下兼容：flat shots → 包进一个默认 scene
            scenes = [{"id": "default", "name": "default", "shots": flat_shots}]

        # 收集所有 shots 用于校验和计数
        all_shots = []
        for scene in scenes:
            for shot in scene.get("shots", []):
                all_shots.append(shot)

        # 校验：所有 shot 必须有 end_frame_description（包括最后一个）
        for shot in all_shots:
            sid = shot.get("id", "?")
            resolved_contract = self._resolve_shot_contract(shot)
            efd = str(resolved_contract.get("end_frame_description", "")).strip()
            if not efd:
                raise ValueError(
                    f"shot_{sid} 缺少 end_frame_description。"
                    "所有镜头（包括最后一个）都必须有 end_frame_description，"
                    "否则首尾帧衔接会断裂。请修改 storyboard.json 后重试。"
                )

        # ── 全局 prompt 提取：一次性为所有 shot 生成首帧/尾帧/动作 prompt ──
        extracted_prompts = await self._extract_all_shot_prompts(all_shots)

        # 串行处理：按 scene > shots 顺序
        shot_results = []
        pending_reviews: list[dict[str, Any]] = []
        previous_video_path: Path | None = None
        shot_index = 0
        pause_pipeline = False

        allow_style_reset_between_scenes = bool(
            self.storyboard.get("allow_style_reset_between_scenes", False)
        )

        for scene in scenes:
            if pause_pipeline:
                break
            scene_resets_visual_continuity = bool(
                scene.get("reset_visual_continuity", allow_style_reset_between_scenes)
            )
            carry_style_reference_from_previous_scene = (
                previous_video_path is not None and not scene_resets_visual_continuity
            )

            # 提取 scene 层级环境上下文（同场景所有镜头共享）
            scene_context = {
                "environment_description": scene.get("environment_description", ""),
                "lighting": scene.get("lighting", ""),
                "weather": scene.get("weather", ""),
                "props": scene.get("props", []),
                "scene_continuity_raw": scene.get("scene_continuity", {}),
            }

            # ── 生成场景图（纯环境，无角色）——同场景所有镜头共享 ──
            scene_id = scene.get("id", "default")
            scene_image_path = await self._generate_scene_image(scene)
            if scene_image_path:
                scene_context["scene_image"] = scene_image_path
                logger.info(f"场景 {scene_id}: 场景图已生成 → {scene_image_path}")

            scene_shots = scene.get("shots", [])
            for i, shot in enumerate(scene_shots):
                if pause_pipeline:
                    break
                shot_index += 1
                is_last_in_scene = (i == len(scene_shots) - 1)
                continuity_mode = shot.get("continuity_mode", "scene_end")
                shot_id = int(shot.get("id", 0))
                scene_context["scene_continuity"] = self._resolve_scene_continuity_for_shot(
                    scene_context.get("scene_continuity_raw", {}),
                    shot_id,
                )
                scene_context["active_props"] = self._resolve_scene_props_for_shot(
                    scene.get("props", []),
                    shot_id,
                )
                logger.info(f"处理镜头 {shot_index}/{len(all_shots)} (scene_last={is_last_in_scene}, continuity={continuity_mode})")

                # chain_from_previous: 从上一 shot 的实际视频提取尾帧作为首帧
                chain = shot.get("chain_from_previous", False)
                style_reference_frame: Path | None = None
                if chain and previous_video_path:
                    # 从视频提取实际最后一帧（比生成的尾帧图更连贯）
                    extracted_frame_path = self.image_dir / f"shot_{shot.get('id', 0):03d}_chained.png"
                    first_frame = self._extract_video_last_frame(previous_video_path, extracted_frame_path)
                    if first_frame:
                        logger.info(f"shot_{shot.get('id')}: chain_from_previous=true，从上一视频提取实际尾帧")
                    else:
                        logger.warning(f"shot_{shot.get('id')}: 视频尾帧提取失败，fallback 到独立生成")
                        first_frame = None
                else:
                    first_frame = None
                    if i == 0 and carry_style_reference_from_previous_scene and previous_video_path:
                        style_ref_path = self.image_dir / f"shot_{shot.get('id', 0):03d}_style_ref.png"
                        style_reference_frame = self._extract_video_last_frame(previous_video_path, style_ref_path)
                        if style_reference_frame:
                            logger.info(
                                f"shot_{shot.get('id')}: 跨 scene 保留风格连续性，"
                                "使用上一镜头尾帧作为 style reference"
                            )
                        else:
                            logger.warning(
                                f"shot_{shot.get('id')}: style reference 提取失败，fallback 到独立生成"
                            )

                result = await self._generate_shot(
                    shot, provided_first_frame=first_frame,
                    scene_context=scene_context, extracted_prompts=extracted_prompts,
                    is_last_in_scene=is_last_in_scene,
                    continuity_mode=continuity_mode,
                    style_reference_frame=style_reference_frame,
                )
                image_review = result.get("image_review")
                if isinstance(image_review, dict) and image_review.get("status") == "pending_judgment":
                    pending_reviews.append(
                        {
                            "shot_id": shot.get("id"),
                            "review_type": "image",
                            **image_review,
                        }
                    )
                    shot_results.append(result)
                    previous_video_path = None
                    pause_pipeline = True
                    logger.info(
                        f"shot_{shot.get('id')}: 图片阶段等待视觉判断，暂停后续素材生成"
                    )
                    continue
                reference_review = result.get("reference_review")
                if isinstance(reference_review, dict) and reference_review.get("status") == "pending_judgment":
                    pending_reviews.append(
                        {
                            "shot_id": shot.get("id"),
                            "review_type": "reference",
                            **reference_review,
                        }
                    )
                    shot_results.append(result)
                    previous_video_path = None
                    pause_pipeline = True
                    logger.info(
                        f"shot_{shot.get('id')}: 参考图阶段等待结构判断，暂停后续素材生成"
                    )
                    continue

                # 视频质量检测 + 自动重试/裁剪 + 审查追踪
                max_retries = 2
                shot_id_str = f"shot_{shot.get('id', 0):03d}"
                audit_dir = self.video_dir.parent / "audit" / shot_id_str
                audit_dir.mkdir(parents=True, exist_ok=True)
                audit_log: list[dict[str, Any]] = []

                for attempt in range(max_retries + 1):
                    if "video" not in result:
                        break

                    vid_path = Path(result["video"]["path"])

                    # 保存原始视频副本（不被裁剪覆盖）
                    raw_copy = audit_dir / f"raw_attempt_{attempt + 1}.mp4"
                    import shutil
                    shutil.copy2(vid_path, raw_copy)
                    logger.info(f"{shot_id_str}: 原始视频已保存 → {raw_copy}")

                    quality = self._scan_video_quality(
                        vid_path,
                        audit_dir=audit_dir,
                        attempt=attempt + 1,
                        camera_movement=str(shot.get("camera_movement", "")),
                        expected_character_count=len(shot.get("characters_in_shot", [])),
                        expected_subject_facing=str((shot.get("motion_control") or {}).get("subject_facing", "")),
                        review_mode=self.review_mode,
                    )

                    audit_entry = {
                        "attempt": attempt + 1,
                        "source_video": str(raw_copy),
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
                            "risk_segments": quality.get("risk_segments", []),
                        },
                        "detector_version": DETECTOR_VERSION,
                        "review_mode": self.review_mode,
                    }

                    if self.review_mode == "hybrid_judge":
                        self._export_risk_bundle(
                            video_path=vid_path,
                            audit_dir=audit_dir,
                            attempt=attempt + 1,
                            quality=quality,
                            shot=shot,
                        )

                        # 读取 vision_judge 结果并应用决策
                        bundle_dir = audit_dir / f"vision_bundle_attempt_{attempt + 1}"
                        judge_result_path = bundle_dir / "vision_judge_result.json"
                        if judge_result_path.exists():
                            audit_entry["status"] = "judged"
                            with open(judge_result_path, encoding="utf-8") as f:
                                judge_result = json.load(f)
                            overall_action = judge_result.get("overall_action", "keep")
                            audit_entry["action"] = overall_action
                            audit_entry["vision_judge_result"] = judge_result

                            if overall_action == "regenerate" and attempt < max_retries:
                                audit_entry["status"] = "applied"
                                audit_log.append(audit_entry)
                                logger.warning(f"shot_{shot.get('id')}: vision judge 建议重新生成")
                                result = await self._generate_shot(
                                    shot, provided_first_frame=first_frame,
                                    scene_context=scene_context, extracted_prompts=extracted_prompts,
                                    is_last_in_scene=is_last_in_scene,
                                    continuity_mode=continuity_mode,
                                    style_reference_frame=style_reference_frame,
                                )
                                continue
                            elif overall_action == "cut_segment":
                                audit_entry["status"] = "finalized"
                                audit_entry["action"] = "cut_segment"
                                # 执行片段裁剪
                                segments_to_cut = [
                                    {"start": seg["start"], "end": seg["end"]}
                                    for seg in judge_result.get("segments", [])
                                    if seg.get("action") == "cut_segment"
                                ]
                                if segments_to_cut:
                                    quality["cut_segments"] = segments_to_cut
                                    audit_entry["quality"]["cut_segments"] = segments_to_cut
                                    logger.info(f"shot_{shot.get('id')}: 将裁剪 {len(segments_to_cut)} 个片段")
                                audit_log.append(audit_entry)
                                break
                            else:
                                audit_entry["status"] = "finalized"
                                audit_entry["action"] = "keep"
                                audit_log.append(audit_entry)
                                break
                        else:
                            audit_entry["status"] = "pending_judgment"
                            audit_entry["action"] = "vision_judge_pending"
                            audit_log.append(audit_entry)
                            logger.info(f"shot_{shot.get('id')}: 等待 LLM 视觉判断，vision_bundle 已导出到 {bundle_dir}")
                            break

                    if quality["needs_regeneration"] and attempt < max_retries:
                        audit_entry["action"] = "regenerate"
                        audit_log.append(audit_entry)
                        logger.warning(
                            f"shot_{shot.get('id')}: 质量不合格，重新生成 "
                            f"(尝试 {attempt + 2}/{max_retries + 1})"
                        )
                        result = await self._generate_shot(
                            shot, provided_first_frame=first_frame,
                            scene_context=scene_context, extracted_prompts=extracted_prompts,
                            is_last_in_scene=is_last_in_scene,
                            continuity_mode=continuity_mode,
                            style_reference_frame=style_reference_frame,
                        )
                        continue
                    elif quality["needs_regeneration"]:
                        audit_entry["action"] = "keep_best_effort"
                        logger.error(
                            f"shot_{shot.get('id')}: 重试 {max_retries} 次仍不合格，保留当前结果"
                        )

                    # 裁剪到最后一个稳定点
                    if quality.get("cut_segments"):
                        self._remove_video_segments(vid_path, quality["cut_segments"])
                        edited_copy = audit_dir / f"trimmed_attempt_{attempt + 1}.mp4"
                        shutil.copy2(vid_path, edited_copy)
                        audit_entry["action"] = audit_entry.get("action", "segment_cut")
                        audit_entry["trimmed_video"] = str(edited_copy)
                        logger.info(f"{shot_id_str}: 局部裁剪后视频已保存 → {edited_copy}")
                    elif quality["trim_to"] is not None:
                        self._trim_video_at(vid_path, quality["trim_to"])
                        trimmed_copy = audit_dir / f"trimmed_attempt_{attempt + 1}.mp4"
                        shutil.copy2(vid_path, trimmed_copy)
                        audit_entry["action"] = audit_entry.get("action", "trimmed")
                        audit_entry["trimmed_video"] = str(trimmed_copy)
                        logger.info(f"{shot_id_str}: 裁剪后视频已保存 → {trimmed_copy}")
                    else:
                        audit_entry["action"] = audit_entry.get("action", "passed")

                    audit_log.append(audit_entry)
                    break

                # 保存审查日志
                if audit_log:
                    audit_json = audit_dir / "quality_audit.json"
                    with open(audit_json, "w", encoding="utf-8") as f:
                        json.dump(audit_log, f, indent=2, ensure_ascii=False)
                    logger.info(f"{shot_id_str}: 质量审查日志 → {audit_json}")

                shot_results.append(result)

                # 提取尾帧供同场景链式使用
                if "video" in result:
                    previous_video_path = Path(result["video"]["path"])
                else:
                    previous_video_path = None

        images = [r["image"] for r in shot_results]
        videos = [r["video"] for r in shot_results if "video" in r]
        shot_references = [
            {
                "shot_id": int(r["image"]["shot_id"]),
                "references": r.get("video_references", []),
            }
            for r in shot_results
            if "image" in r
        ]
        shot_prompts = [
            r["video_prompt"]
            for r in shot_results
            if isinstance(r.get("video_prompt"), dict)
        ]

        if pending_reviews:
            return {
                "generated_at": timestamp_id(),
                "asset_root": str(self.output_root),
                "images": images,
                "videos": videos,
                "shot_prompts": shot_prompts,
                "shot_references": shot_references,
                "bgm": None,
                "pending_reviews": pending_reviews,
            }

        # BGM
        duration = int(self.storyboard.get("total_duration", 60))
        bgm_style = str(self.storyboard.get("bgm_style", "upbeat"))
        bgm_path, bgm_provider = await self._generate_bgm(bgm_style, duration)

        return {
            "generated_at": timestamp_id(),
            "asset_root": str(self.output_root),
            "images": images,
            "videos": videos,
            "shot_prompts": shot_prompts,
            "shot_references": shot_references,
            "bgm": {"path": str(bgm_path), "provider": bgm_provider, "style": bgm_style},
            "pending_reviews": pending_reviews,
        }

    @staticmethod
    def _extract_video_last_frame(video_path: Path, output_path: Path) -> Path | None:
        """用 ffmpeg 提取视频的最后一帧作为图片。"""
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", str(output_path)],
                check=True, capture_output=True,
            )
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"提取视频尾帧失败: {e}")
        return None

    @staticmethod
    def _scan_video_quality(
        video_path: Path,
        min_keep: float = 3.0,
        audit_dir: Path | None = None,
        attempt: int = 1,
        camera_movement: str = "",
        expected_character_count: int = 0,
        expected_subject_facing: str = "",
        review_mode: str = "metrics_only",
    ) -> dict:
        """全帧扫描视频质量，找到最后一个稳定点。

        策略：从后往前找到第一个"稳定区域"（连续 N 帧 MSE 都在阈值以下），
        裁到那个点。如果裁完剩余不足 min_keep 秒，标记为需要重新生成。

        同时检测奇偶帧交替闪烁（高-低-高-低模式）。

        返回:
            {
                "ok": bool,                    # 整体质量是否合格
                "needs_regeneration": bool,     # 是否需要重新生成（裁完太短）
                "trim_to": float | None,        # 裁剪到的时间点（秒），None 表示不裁
                "duration": float,
                "fps": float,
            }
        """
        profile_name = AssetGenerator._quality_profile_for_camera(camera_movement)
        profile = QUALITY_PROFILES[profile_name]
        result = {
            "ok": True,
            "needs_regeneration": False,
            "trim_to": None,
            "duration": 0,
            "fps": 24,
            "profile": profile_name,
            "trigger": "passed",
            "analysis": {},
            "bad_segments": [],
            "cut_segments": [],
            "risk_segments": [],
        }

        # 获取视频信息
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "json", str(video_path)],
                check=True, capture_output=True, text=True,
            )
            info = json.loads(probe.stdout)
            duration = float(info["format"]["duration"])
            rfr = info["streams"][0]["r_frame_rate"]
            num, den = rfr.split("/")
            fps = float(num) / float(den)
        except (subprocess.CalledProcessError, KeyError, ValueError, ZeroDivisionError) as e:
            logger.warning(f"质量检测: 无法获取视频信息 {e}")
            return result

        result["duration"] = duration
        result["fps"] = fps

        if duration < 2.0:
            return result

        # 提取全视频帧（缩小版加速）
        with tempfile.TemporaryDirectory() as tmp_dir:
            frame_pattern = str(Path(tmp_dir) / "frame_%04d.png")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(video_path),
                     "-vf", "scale=160:-1", "-q:v", "2", frame_pattern],
                    check=True, capture_output=True, timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logger.warning(f"质量检测: 帧提取失败 {e}")
                return result

            frame_files = sorted(Path(tmp_dir).glob("frame_*.png"))
            if len(frame_files) < 6:
                return result

            frames: list[Image.Image] = []
            frame_arrays: list[np.ndarray] = []
            for ff in frame_files:
                try:
                    img = Image.open(ff).convert("RGB")
                    frames.append(img)
                    frame_arrays.append(np.asarray(img, dtype=np.float32))
                except Exception:
                    continue

            if len(frames) < 6:
                return result

            # 计算所有相邻帧 MSE（numpy 向量化）
            diffs: list[float] = []
            for j in range(1, len(frame_arrays)):
                diff = frame_arrays[j] - frame_arrays[j - 1]
                mse = float(np.mean(diff * diff))
                diffs.append(mse)

            if not diffs:
                return result

            # 用前 1/4 帧的中位数作为稳定基准
            stable_count = max(3, len(diffs) // 4)
            sorted_stable = sorted(diffs[:stable_count])
            median_diff = sorted_stable[len(sorted_stable) // 2]

            threshold = max(
                float(profile["threshold_base"]),
                median_diff * float(profile["threshold_multiplier"]),
            )
            result["analysis"].update({
                "median_diff": median_diff,
                "threshold": threshold,
                "stable_sample_count": stable_count,
            })

            # ── 用滑动窗口平滑 MSE，消除闪烁的高低交替 ──
            # 窗口大小 5：每帧的"有效 MSE"= 周围 5 帧的最大值
            # 这能有效消除奇偶帧交替闪烁（高-1-高-1 模式）
            diffs_arr = np.array(diffs)
            n = len(diffs)
            smoothed_arr = diffs_arr.copy()
            for offset in (-2, -1, 1, 2):
                lo = max(0, offset)
                hi = min(0, offset)
                src_start = max(0, -offset)
                src_end = n + min(0, -offset)
                smoothed_arr[lo: n + hi if hi else n] = np.maximum(
                    smoothed_arr[lo: n + hi if hi else n],
                    diffs_arr[src_start:src_end],
                )
            smoothed: list[float] = smoothed_arr.tolist()

            # ── 从后往前找最后一个稳定点 ──
            # "稳定"定义：连续 6 帧平滑后的 MSE 都在阈值以下
            required_stable = int(profile["required_stable"])
            stable_run = 0
            last_stable_idx = len(smoothed)  # 默认：整个视频都稳定
            reverse_first_bad_idx = None

            for j in range(len(smoothed) - 1, -1, -1):
                if smoothed[j] <= threshold:
                    stable_run += 1
                    if stable_run >= required_stable:
                        last_stable_idx = j + stable_run
                        break
                else:
                    reverse_first_bad_idx = j
                    stable_run = 0

            if stable_run < required_stable:
                # 从头找稳定区域的结束点
                last_stable_idx = 0
                for j in range(len(smoothed)):
                    if smoothed[j] <= threshold:
                        last_stable_idx = j + 1
                    else:
                        break

            # ── 正向扫描：找第一个连续异常段 ──
            # 解决中间段出问题但尾部恢复、导致反向扫描漏检的情况
            consecutive_bad = 0
            first_bad_start = None
            confirmed_bad_start = None
            grace_window = int(profile["grace_window_frames"])
            confirm_bad_frames = int(profile["confirm_bad_frames"])
            trim_backoff_frames = int(profile["trim_backoff_frames"])
            for j in range(len(smoothed)):
                if smoothed[j] > threshold:
                    if consecutive_bad == 0:
                        first_bad_start = j
                    consecutive_bad += 1
                else:
                    if consecutive_bad >= int(profile["consecutive_bad"]) and first_bad_start is not None:
                        confirm_start = min(len(smoothed), first_bad_start + grace_window)
                        confirm_slice = smoothed[confirm_start:]
                        confirm_hits = 0
                        for value in confirm_slice:
                            if value > threshold:
                                confirm_hits += 1
                                if confirm_hits >= confirm_bad_frames:
                                    confirmed_bad_start = confirm_start
                                    break
                            else:
                                confirm_hits = 0
                        candidate_idx = max(0, (confirmed_bad_start or first_bad_start) - trim_backoff_frames)
                        forward_keep = (candidate_idx + 1) / fps
                        if forward_keep < (last_stable_idx + 1) / fps:
                            if confirmed_bad_start is not None:
                                last_stable_idx = candidate_idx
                                result["trigger"] = "forward_mse_run"
                                logger.info(
                                    f"质量检测: 正向扫描确认异常段 @{forward_keep:.1f}s "
                                    f"(start={((first_bad_start + 1) / fps):.1f}s, confirm={((confirmed_bad_start + 1) / fps):.1f}s)"
                                )
                        break
                    consecutive_bad = 0
                    first_bad_start = None
            else:
                if consecutive_bad >= int(profile["consecutive_bad"]) and first_bad_start is not None:
                    confirm_start = min(len(smoothed), first_bad_start + grace_window)
                    confirm_slice = smoothed[confirm_start:]
                    confirm_hits = 0
                    for value in confirm_slice:
                        if value > threshold:
                            confirm_hits += 1
                            if confirm_hits >= confirm_bad_frames:
                                confirmed_bad_start = confirm_start
                                break
                        else:
                            confirm_hits = 0
                    candidate_idx = max(0, (confirmed_bad_start or first_bad_start) - trim_backoff_frames)
                    forward_keep = (candidate_idx + 1) / fps
                    if forward_keep < (last_stable_idx + 1) / fps:
                        if confirmed_bad_start is not None:
                            last_stable_idx = candidate_idx
                            result["trigger"] = "forward_mse_run"

            # 计算可保留的时长
            keep_time = (last_stable_idx + 1) / fps  # +1 因为 diffs[j] 对应帧 j+1
            first_over_threshold_idx = next((idx for idx, value in enumerate(smoothed) if value > threshold), None)
            result["analysis"].update({
                "required_stable": required_stable,
                "reverse_first_bad_time": (
                    (reverse_first_bad_idx + 1) / fps if reverse_first_bad_idx is not None else None
                ),
                "forward_bad_start_time": (
                    (first_bad_start + 1) / fps if first_bad_start is not None else None
                ),
                "forward_confirm_time": (
                    (confirmed_bad_start + 1) / fps if confirmed_bad_start is not None else None
                ),
                "first_over_threshold_time": (
                    (first_over_threshold_idx + 1) / fps if first_over_threshold_idx is not None else None
                ),
                "preliminary_keep_time": keep_time,
                "grace_window_frames": grace_window,
                "confirm_bad_frames": confirm_bad_frames,
                "trim_backoff_frames": trim_backoff_frames,
            })

            motion_ramp_exempt = False
            motion_ramp_start = first_bad_start
            if (
                motion_ramp_start is not None
                and motion_ramp_start < len(smoothed) - 8
                and profile_name in {"medium_motion", "heavy_motion"}
            ):
                ramp_segment = smoothed_arr[motion_ramp_start:]
                ramp_deltas = np.diff(ramp_segment)
                up_steps = int(np.sum(ramp_deltas >= 0))
                large_drops = int(np.sum(ramp_deltas < -threshold * 0.2))
                total_steps = max(1, len(ramp_segment) - 1)
                ramp_up_ratio = up_steps / total_steps
                ramp_peak = float(np.max(ramp_segment))
                motion_ramp_exempt = ramp_up_ratio >= 0.6 and large_drops <= 1 and ramp_peak >= threshold * 1.2
                result["analysis"]["motion_ramp"] = {
                    "start_time": (motion_ramp_start + 1) / fps,
                    "up_ratio": ramp_up_ratio,
                    "large_drops": large_drops,
                    "peak_smoothed": ramp_peak,
                    "exempted": motion_ramp_exempt,
                }

            # ── 闪烁检测：奇偶帧交替跳变（高-低-高-低模式） ──
            # 正常运动的 MSE 连续渐变，闪烁模式下相邻帧 MSE 反复反转
            # 从后往前找最后一个无闪烁的稳定点
            min_amplitude = max(
                float(profile["min_amplitude_base"]),
                median_diff * float(profile["min_amplitude_multiplier"]),
            )  # 反转幅度阈值

            # 向量化计算相邻差分方向和振幅
            if n >= 3:
                deltas = np.diff(diffs_arr)  # diffs_arr[j] - diffs_arr[j-1], 长度 n-1
                directions = deltas > 0  # True = 上升
                amplitudes = np.abs(deltas)
                # 反转 = 相邻方向不同且振幅超阈值
                is_reversal = (directions[1:] != directions[:-1]) & (amplitudes[1:] > min_amplitude)
            else:
                is_reversal = np.array([], dtype=bool)

            # 从后往前找闪烁段（保持原逻辑：连续反转达到阈值时裁剪）
            reversals = 0
            reversal_count_threshold = int(profile["reversal_count"])
            for j in range(len(is_reversal) - 1, -1, -1):
                if is_reversal[j]:
                    reversals += 1
                else:
                    if reversals >= reversal_count_threshold:
                        # j+2 对应 diffs 中的索引（is_reversal[j] 对应 diffs[j+2] vs diffs[j+1]）
                        flicker_start_time = (j + 2 + 1) / fps
                        if flicker_start_time < keep_time:
                            keep_time = flicker_start_time
                            result["trigger"] = "flicker"
                            logger.info(
                                f"质量检测: 检测到闪烁 ({reversals} 次反转 @{flicker_start_time:.1f}s)，"
                                f"裁剪到 {keep_time:.1f}s"
                            )
                    reversals = 0

            # 检查开头处的闪烁
            if reversals >= reversal_count_threshold:
                flicker_start_time = 2 / fps
                if flicker_start_time < keep_time:
                    keep_time = flicker_start_time
                    result["trigger"] = "flicker"

            # ── 局部突变检测：单帧或少数帧的面部变形/跳变 ──
            # 用滑动窗口计算局部均值，如果某帧 MSE 超过局部均值的 spike_factor 倍
            # 且绝对值超过 spike_abs_min，标记为 spike
            spike_factor = float(profile["spike_factor"])
            spike_abs_min = float(profile["spike_abs_min"])
            spike_window = 12  # 前后各 12 帧（约 0.5s）计算局部基准
            spike_count = 0
            first_spike_time = None

            # 用 cumsum 做 O(n) 滑动窗口局部均值（排除自身）
            cumsum = np.concatenate(([0.0], np.cumsum(diffs_arr)))
            for j in range(n):
                w_start = max(0, j - spike_window)
                w_end = min(n, j + spike_window + 1)
                window_sum = cumsum[w_end] - cumsum[w_start]
                window_count = w_end - w_start - 1  # 排除自身
                if window_count <= 0:
                    continue
                local_mean = (window_sum - diffs_arr[j]) / window_count

                if diffs_arr[j] > max(spike_abs_min, local_mean * spike_factor):
                    spike_count += 1
                    t = (j + 1) / fps
                    if first_spike_time is None:
                        first_spike_time = t
                    logger.debug(
                        f"质量检测: 局部突变 @{t:.2f}s "
                        f"(MSE={diffs_arr[j]:.0f}, 局部均值={local_mean:.0f}, "
                        f"倍率={diffs_arr[j]/local_mean:.1f}x)"
                    )

            if spike_count >= 2:
                # 多个突变点 → 标记需要裁剪或重新生成
                spike_trim = first_spike_time - 0.1  # 在第一个突变前 0.1s 裁
                if spike_trim > 0 and spike_trim < keep_time:
                    keep_time = spike_trim
                    result["trigger"] = "spike"
                    logger.info(
                        f"质量检测: 检测到 {spike_count} 个局部突变，"
                        f"首个 @{first_spike_time:.1f}s，裁剪到 {keep_time:.1f}s"
                    )
            result["analysis"]["first_spike_time"] = first_spike_time

            semantic_segments = AssetGenerator._semantic_audit_video(
                video_path=video_path,
                fps=fps,
                duration=duration,
                expected_character_count=expected_character_count,
                expected_subject_facing=expected_subject_facing,
            )
            result["bad_segments"] = semantic_segments
            risk_segments: list[dict[str, Any]] = []
            if semantic_segments:
                result["analysis"]["semantic_reasons"] = [segment["reason"] for segment in semantic_segments]
                risk_segments.extend(dict(segment) for segment in semantic_segments)
                tail_segment = next(
                    (segment for segment in semantic_segments if segment["end"] >= duration - 0.3 and segment["start"] >= min_keep),
                    None,
                )
                cut_segment = next(
                    (
                        segment for segment in semantic_segments
                        if segment["end"] < duration - 0.3
                        and duration - (segment["end"] - segment["start"]) >= min_keep
                    ),
                    None,
                )
                if tail_segment is not None:
                    keep_time = min(keep_time, float(tail_segment["start"]))
                    result["trigger"] = str(tail_segment["reason"])
                elif cut_segment is not None:
                    result["cut_segments"] = [cut_segment]
                    result["trigger"] = str(cut_segment["reason"])

            # ── 人脸变形检测：DNN 置信度骤降 ──
            # 用 OpenCV DNN SSD 人脸检测器，追踪人脸置信度变化
            # 如果曾经稳定检测到人脸，后来置信度骤降 → 脸部变形
            try:
                import cv2
                model_dir = Path(__file__).parent.parent / "models"
                proto = model_dir / "deploy.prototxt"
                weights = model_dir / "res10_300x300_ssd_iter_140000.caffemodel"
                if proto.exists() and weights.exists():
                    net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
                    face_confs = []  # (time, confidence)
                    sample_interval = 3  # 每 3 帧检测一次
                    with tempfile.TemporaryDirectory() as face_td:
                        subprocess.run(
                            ["ffmpeg", "-i", str(video_path), "-vf", "scale=480:-1",
                             f"{face_td}/f%05d.png", "-loglevel", "error"],
                            check=True,
                        )
                        face_frames = sorted(Path(face_td).glob("f*.png"))
                        for fi, ff in enumerate(face_frames):
                            if fi % sample_interval != 0:
                                continue
                            img = cv2.imread(str(ff))
                            if img is None:
                                continue
                            blob = cv2.dnn.blobFromImage(
                                img, 1.0, (300, 300), (104.0, 177.0, 123.0)
                            )
                            net.setInput(blob)
                            detections = net.forward()
                            best_conf = 0.0
                            for di in range(detections.shape[2]):
                                c = float(detections[0, 0, di, 2])
                                if c > best_conf:
                                    best_conf = c
                            face_confs.append((fi / fps, best_conf))

                    # 分析置信度变化：找"曾有脸→脸消失"的骤降点
                    if face_confs:
                        # 用滑动窗口找到最后一段"稳定有脸"区间
                        high_threshold = 0.5
                        low_threshold = 0.3
                        window = 3  # 连续 3 个采样点有脸 = 稳定有脸

                        # 找最后一个 conf >= high 的连续段
                        last_good_face_time = None
                        consecutive_high = 0
                        had_stable_face = False
                        for t, c in face_confs:
                            if c >= high_threshold:
                                consecutive_high += 1
                                if consecutive_high >= window:
                                    had_stable_face = True
                                last_good_face_time = t
                            else:
                                consecutive_high = 0

                        if had_stable_face and last_good_face_time is not None:
                            # 检查 last_good_face_time 之后是否还有高置信度人脸
                            # 如果后面脸又回来了，说明只是转身，不是变形
                            face_returned = False
                            for t, c in face_confs:
                                if t > last_good_face_time + 2.0 and c >= low_threshold:
                                    face_returned = True
                                    break

                            remaining = duration - last_good_face_time
                            if remaining > 1.5 and not face_returned:
                                # 脸消失后再也没回来 → 脸部变形
                                face_trim = last_good_face_time + 0.3
                                result["analysis"]["last_good_face_time"] = last_good_face_time
                                if face_trim < keep_time:
                                    result["analysis"]["face_dnn_warning_time"] = face_trim
            except ImportError:
                pass  # cv2 不可用，跳过人脸检测
            except Exception as e:
                logger.debug(f"质量检测: 人脸检测跳过 ({e})")

            top_diff_indices = sorted(range(len(diffs)), key=lambda idx: diffs[idx], reverse=True)[:5]
            result["analysis"]["top_diffs"] = [
                {
                    "time": (idx + 1) / fps,
                    "diff": diffs[idx],
                    "smoothed": smoothed[idx],
                }
                for idx in top_diff_indices
            ]
            strong_trigger = result["trigger"] in {
                "flicker",
                "spike",
                "identity_hallucination",
                "face_orientation_discontinuity",
                "face_identity_drift",
                "head_body_inconsistency",
            }
            suggested_keep_time = keep_time

            if result["trigger"] == "forward_mse_run" and motion_ramp_exempt and not strong_trigger:
                result["analysis"]["suggested_trim_time"] = suggested_keep_time
                result["analysis"]["exemption_reason"] = "continuous_motion_ramp"
                result["trigger"] = "motion_ramp_exempt"
                keep_time = duration

            if result["trigger"] == "forward_mse_run" and profile_name == "heavy_motion" and not strong_trigger:
                result["analysis"]["suggested_trim_time"] = suggested_keep_time
                result["analysis"]["exemption_reason"] = "heavy_motion_warning_only"
                result["trigger"] = "forward_mse_warning"
                keep_time = duration

            if result["trigger"] == "passed" and keep_time < duration - 0.1:
                result["analysis"]["suggested_trim_time"] = suggested_keep_time
                result["analysis"]["exemption_reason"] = "tail_mse_warning_only"
                result["trigger"] = "reverse_mse_tail_warning"
                keep_time = duration

            if suggested_keep_time < duration - 0.1:
                risk_segments.append(
                    {
                        "start": max(0.0, suggested_keep_time - 0.35),
                        "end": min(duration, suggested_keep_time + 0.75),
                        "reason": result["trigger"],
                        "confidence": 0.65,
                    }
                )

            result["risk_segments"] = AssetGenerator._merge_segments(risk_segments)

            result["analysis"]["final_keep_time"] = keep_time

            if review_mode == "hybrid_judge":
                result["analysis"]["review_mode"] = "hybrid_judge"
                result["analysis"]["metrics_action"] = {
                    "trim_to": result["trim_to"],
                    "cut_segments": result["cut_segments"],
                    "trigger": result["trigger"],
                }
                result["trim_to"] = None
                result["cut_segments"] = []
                result["needs_regeneration"] = False
                result["ok"] = True

            if keep_time >= duration - 0.1:
                # 整个视频都稳定，不需要裁剪
                logger.debug(f"质量检测: 视频质量正常，无需处理")
                # 保存首尾帧作为审查参考
                if audit_dir:
                    AssetGenerator._save_audit_frames(frames, fps, audit_dir, attempt, keep_time, duration, diffs, threshold)
                return result

            if result["cut_segments"]:
                result["ok"] = True
                result["trim_to"] = None
                logger.info(f"质量检测: 检测到可局部裁剪片段 {result['cut_segments']}")
            elif keep_time >= min_keep:
                # 可以裁剪保留前面的好内容
                result["trim_to"] = keep_time
                result["ok"] = True
                if result["trigger"] == "passed":
                    result["trigger"] = "reverse_mse_tail"
                logger.info(
                    f"质量检测: 发现异常帧，裁剪到 {keep_time:.1f}s "
                    f"({duration:.1f}s → {keep_time:.1f}s, "
                    f"median={median_diff:.0f}, threshold={threshold:.0f}, profile={profile_name}, trigger={result['trigger']})"
                )
            else:
                # 裁完太短，需要重新生成
                result["needs_regeneration"] = True
                result["ok"] = False
                if result["trigger"] == "passed":
                    result["trigger"] = "reverse_mse_tail"
                logger.warning(
                    f"质量检测: 异常帧过多，稳定内容仅 {keep_time:.1f}s (< {min_keep}s)，需重新生成 "
                    f"(median={median_diff:.0f}, threshold={threshold:.0f}, profile={profile_name}, trigger={result['trigger']})"
                )

            # 保存问题帧截图和稳定帧对比
            if audit_dir:
                AssetGenerator._save_audit_frames(frames, fps, audit_dir, attempt, keep_time, duration, diffs, threshold)

            return result

    @staticmethod
    def _face_crop_duplicate_score(face_a: Any, face_b: Any) -> float:
        try:
            import cv2
            gray_a = cv2.cvtColor(face_a, cv2.COLOR_BGR2GRAY)
            gray_b = cv2.cvtColor(face_b, cv2.COLOR_BGR2GRAY)
            gray_a = cv2.resize(gray_a, (32, 32))
            gray_b = cv2.resize(gray_b, (32, 32))
            diff = cv2.absdiff(gray_a, gray_b)
            mean_diff = float(diff.mean())
            return max(0.0, 1.0 - mean_diff / 255.0)
        except Exception:
            return 0.0

    @staticmethod
    def _face_crop_identity_score(face_a: Any, face_b: Any) -> float:
        try:
            import cv2
            gray_a = cv2.cvtColor(face_a, cv2.COLOR_BGR2GRAY)
            gray_b = cv2.cvtColor(face_b, cv2.COLOR_BGR2GRAY)
            gray_a = cv2.resize(gray_a, (48, 48))
            gray_b = cv2.resize(gray_b, (48, 48))
            diff = cv2.absdiff(gray_a, gray_b)
            gray_score = max(0.0, 1.0 - float(diff.mean()) / 255.0)

            hist_a = cv2.calcHist([cv2.resize(face_a, (64, 64))], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
            hist_b = cv2.calcHist([cv2.resize(face_b, (64, 64))], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
            cv2.normalize(hist_a, hist_a)
            cv2.normalize(hist_b, hist_b)
            hist_score = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
            hist_score = max(0.0, min(1.0, (hist_score + 1.0) / 2.0))
            return 0.6 * gray_score + 0.4 * hist_score
        except Exception:
            return 0.0

    @staticmethod
    def _box_iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
        ax1, ay1, ax2, ay2 = box_a
        bx1, by1, bx2, by2 = box_b
        inter_x1 = max(ax1, bx1)
        inter_y1 = max(ay1, by1)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)
        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            return 0.0
        inter_area = float((inter_x2 - inter_x1) * (inter_y2 - inter_y1))
        area_a = float(max(1, (ax2 - ax1) * (ay2 - ay1)))
        area_b = float(max(1, (bx2 - bx1) * (by2 - by1)))
        return inter_area / (area_a + area_b - inter_area)

    @staticmethod
    def _dedupe_boxes(
        boxes: list[tuple[int, int, int, int, float]],
        iou_threshold: float = 0.45,
    ) -> list[tuple[int, int, int, int, float]]:
        if not boxes:
            return []
        ordered = sorted(boxes, key=lambda item: (item[4], (item[2] - item[0]) * (item[3] - item[1])), reverse=True)
        kept: list[tuple[int, int, int, int, float]] = []
        for candidate in ordered:
            candidate_box = candidate[:4]
            if any(AssetGenerator._box_iou(candidate_box, existing[:4]) >= iou_threshold for existing in kept):
                continue
            kept.append(candidate)
        return kept

    @staticmethod
    def _merge_segments(segments: list[dict[str, Any]], gap_tolerance: float = 0.35) -> list[dict[str, Any]]:
        if not segments:
            return []
        segments = sorted(segments, key=lambda item: (item["reason"], item["start"]))
        merged: list[dict[str, Any]] = [dict(segments[0])]
        for segment in segments[1:]:
            current = merged[-1]
            if (
                segment["reason"] == current["reason"]
                and float(segment["start"]) <= float(current["end"]) + gap_tolerance
            ):
                current["end"] = max(float(current["end"]), float(segment["end"]))
                current["confidence"] = max(float(current.get("confidence", 0.0)), float(segment.get("confidence", 0.0)))
            else:
                merged.append(dict(segment))
        return merged

    @staticmethod
    def _semantic_audit_video(
        video_path: Path,
        fps: float,
        duration: float,
        expected_character_count: int = 0,
        expected_subject_facing: str = "",
    ) -> list[dict[str, Any]]:
        try:
            import cv2
        except ImportError:
            return []

        model_dir = Path(__file__).parent.parent / "models"
        proto = model_dir / "deploy.prototxt"
        weights = model_dir / "res10_300x300_ssd_iter_140000.caffemodel"
        if not (proto.exists() and weights.exists()):
            return []

        try:
            net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
            frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        except Exception:
            return []

        sample_interval = max(3, int(round(fps / 8)))
        stable_expected_facing = str(expected_subject_facing).strip().lower()
        frontal_expected = stable_expected_facing in {"toward_camera", "front", "front_of_subject"}
        left_expected = stable_expected_facing == "left_profile"
        right_expected = stable_expected_facing == "right_profile"
        orientation_states: list[tuple[float, str, int]] = []
        drift_samples: list[tuple[float, float, tuple[float, float, float, float] | None, str]] = []
        bad_segments: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory() as face_td:
            try:
                subprocess.run(
                    ["ffmpeg", "-i", str(video_path), "-vf", "scale=480:-1", f"{face_td}/f%05d.png", "-loglevel", "error"],
                    check=True,
                )
            except Exception:
                return []

            person_hog = cv2.HOGDescriptor()
            person_hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            anchor_face = None
            anchor_box = None

            face_frames = sorted(Path(face_td).glob("f*.png"))
            for fi, ff in enumerate(face_frames):
                if fi % sample_interval != 0:
                    continue
                img = cv2.imread(str(ff))
                if img is None:
                    continue
                h, w = img.shape[:2]
                blob = cv2.dnn.blobFromImage(img, 1.0, (300, 300), (104.0, 177.0, 123.0))
                net.setInput(blob)
                detections = net.forward()
                faces: list[tuple[int, int, int, int, float]] = []
                for di in range(detections.shape[2]):
                    conf = float(detections[0, 0, di, 2])
                    if conf < 0.5:
                        continue
                    box = detections[0, 0, di, 3:7] * [w, h, w, h]
                    x1, y1, x2, y2 = [int(v) for v in box]
                    x1 = max(0, min(x1, w - 1))
                    x2 = max(0, min(x2, w))
                    y1 = max(0, min(y1, h - 1))
                    y2 = max(0, min(y2, h))
                    if x2 - x1 < 20 or y2 - y1 < 20:
                        continue
                    faces.append((x1, y1, x2, y2, conf))
                face_candidates = list(faces)

                sample_time = fi / fps
                person_boxes, weights = person_hog.detectMultiScale(img, winStride=(4, 4), padding=(16, 16), scale=1.02)
                # 过滤低权重的误报
                person_boxes = [box for box, w in zip(person_boxes, weights) if w > 0.15]
                person_box = None
                if len(person_boxes) > 0:
                    px, py, pw, ph = max(person_boxes, key=lambda box: box[2] * box[3])
                    person_box = (float(px), float(py), float(px + pw), float(py + ph))

                # 检测重复人体（基于 HOG）
                # 只要检测到多个相似人体，就标记为风险，交给视觉层判断
                if len(person_boxes) >= 2:
                    logger.debug(f"[{sample_time:.2f}s] HOG detected {len(person_boxes)} persons (expected {expected_character_count})")
                    duplicate_person_score = 0.0
                    for idx in range(len(person_boxes)):
                        px1, py1, pw1, ph1 = person_boxes[idx]
                        crop_a = img[py1:py1+ph1, px1:px1+pw1]
                        for jdx in range(idx + 1, len(person_boxes)):
                            px2, py2, pw2, ph2 = person_boxes[jdx]
                            crop_b = img[py2:py2+ph2, px2:px2+pw2]
                            if crop_a.size > 0 and crop_b.size > 0:
                                score = AssetGenerator._face_crop_identity_score(crop_a, crop_b)
                                duplicate_person_score = max(duplicate_person_score, score)
                                logger.debug(f"[{sample_time:.2f}s] Person {idx} vs {jdx}: similarity={score:.3f}")
                    if duplicate_person_score >= 0.70:
                        bad_segments.append({
                            "start": sample_time,
                            "end": min(duration, sample_time + sample_interval / fps),
                            "reason": "identity_hallucination",
                            "confidence": duplicate_person_score,
                        })
                        logger.debug(f"[{sample_time:.2f}s] Duplicate person detected: score={duplicate_person_score:.3f}")

                if expected_character_count > 0 and len(faces) > expected_character_count:
                    duplicate_score = 0.0
                    for idx in range(len(faces)):
                        x1, y1, x2, y2, _ = faces[idx]
                        crop_a = img[y1:y2, x1:x2]
                        for jdx in range(idx + 1, len(faces)):
                            xx1, yy1, xx2, yy2, _ = faces[jdx]
                            crop_b = img[yy1:yy2, xx1:xx2]
                            duplicate_score = max(duplicate_score, AssetGenerator._face_crop_duplicate_score(crop_a, crop_b))
                    if duplicate_score >= 0.88:
                        bad_segments.append(
                            {
                                "start": sample_time,
                                "end": min(duration, sample_time + sample_interval / fps),
                                "reason": "identity_hallucination",
                                "confidence": duplicate_score,
                            }
                        )

                if faces:
                    x1, y1, x2, y2, conf = max(faces, key=lambda item: (item[4], (item[2] - item[0]) * (item[3] - item[1])))
                    primary_crop = img[y1:y2, x1:x2]
                    if primary_crop.size > 0:
                        if anchor_face is None and (x2 - x1) >= 36 and (y2 - y1) >= 36:
                            anchor_face = primary_crop.copy()
                            anchor_box = (float(x1), float(y1), float(x2), float(y2))
                        if anchor_face is not None:
                            identity_score = AssetGenerator._face_crop_identity_score(anchor_face, primary_crop)
                            drift_samples.append((sample_time, identity_score, person_box, "face"))

                if frontal_expected or left_expected or right_expected:
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    frontal_hits = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                    left_hits = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                    flipped = cv2.flip(gray, 1)
                    right_hits = profile.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                    logger.debug(f"[{sample_time:.2f}s] Haar: frontal={len(frontal_hits)}, left={len(left_hits)}, right={len(right_hits)}")
                    for x, y, ww, hh in frontal_hits:
                        face_candidates.append((int(x), int(y), int(x + ww), int(y + hh), 0.55))
                    for x, y, ww, hh in left_hits:
                        face_candidates.append((int(x), int(y), int(x + ww), int(y + hh), 0.5))
                    for x, y, ww, hh in right_hits:
                        x1 = int(w - (x + ww))
                        x2 = int(w - x)
                        face_candidates.append((x1, int(y), x2, int(y + hh), 0.5))
                    logger.debug(f"[{sample_time:.2f}s] face_candidates before dedupe: {len(face_candidates)}")

                    observed = "unknown"
                    if len(frontal_hits) > 0:
                        observed = "frontal"
                    elif len(left_hits) > 0 and len(right_hits) == 0:
                        observed = "left"
                    elif len(right_hits) > 0 and len(left_hits) == 0:
                        observed = "right"
                    orientation_states.append((sample_time, observed, len(faces)))

                deduped_candidates = AssetGenerator._dedupe_boxes(face_candidates)
                # 注释掉基于候选框的重复检测，因为 Haar cascade 误报太多
                # 只保留 HOG 人体检测和原有的人脸重复检测

        if drift_samples:
            low_run = 0
            run_start = None
            recent_boxes: list[tuple[float, float, float, float] | None] = []
            last_identity = None
            for sample_time, identity_score, person_box, _ in drift_samples:
                recent_boxes.append(person_box)
                recent_boxes = recent_boxes[-3:]
                if last_identity is not None and identity_score < 0.58 and last_identity < 0.7:
                    if low_run == 0:
                        run_start = sample_time
                    low_run += 1
                else:
                    if low_run >= 2 and run_start is not None:
                        bad_segments.append(
                            {
                                "start": max(0.0, run_start - sample_interval / fps),
                                "end": sample_time,
                                "reason": "face_identity_drift",
                                "confidence": 0.9 - identity_score * 0.2,
                            }
                        )
                    low_run = 0
                    run_start = None
                last_identity = identity_score

        if orientation_states and (frontal_expected or left_expected or right_expected):
            dominant = "frontal" if frontal_expected else "left" if left_expected else "right"
            mismatch_run = 0
            run_start = None
            last_state = None
            flip_count = 0
            for sample_time, observed, face_count in orientation_states:
                if face_count == 0 or observed == "unknown":
                    continue
                if observed != dominant:
                    if mismatch_run == 0:
                        run_start = sample_time
                        flip_count = 0
                    if last_state and observed != last_state:
                        flip_count += 1
                    mismatch_run += 1
                else:
                    if mismatch_run >= 2 and flip_count >= 1 and run_start is not None:
                        bad_segments.append(
                            {
                                "start": max(0.0, run_start - sample_interval / fps),
                                "end": sample_time,
                                "reason": "face_orientation_discontinuity",
                                "confidence": 0.85,
                            }
                        )
                    mismatch_run = 0
                    run_start = None
                    flip_count = 0
                last_state = observed
            if mismatch_run >= 2 and flip_count >= 1 and run_start is not None:
                bad_segments.append(
                    {
                        "start": max(0.0, run_start - sample_interval / fps),
                        "end": min(duration, run_start + mismatch_run * sample_interval / fps),
                        "reason": "face_orientation_discontinuity",
                        "confidence": 0.85,
                    }
                )

        if orientation_states and drift_samples:
            orientation_map = {round(sample_time, 2): observed for sample_time, observed, face_count in orientation_states if face_count > 0}
            jump_run = 0
            run_start = None
            last_orientation = None
            for sample_time, identity_score, person_box, _ in drift_samples:
                observed = orientation_map.get(round(sample_time, 2), "unknown")
                if observed == "unknown":
                    continue
                if last_orientation and observed != last_orientation and identity_score < 0.62:
                    if jump_run == 0:
                        run_start = sample_time
                    jump_run += 1
                else:
                    if jump_run >= 2 and run_start is not None:
                        bad_segments.append(
                            {
                                "start": max(0.0, run_start - sample_interval / fps),
                                "end": sample_time,
                                "reason": "head_body_inconsistency",
                                "confidence": 0.82,
                            }
                        )
                    jump_run = 0
                    run_start = None
                last_orientation = observed

        return AssetGenerator._merge_segments(bad_segments)

    @staticmethod
    def _save_audit_frames(
        frames: list,
        fps: float,
        audit_dir: Path,
        attempt: int,
        keep_time: float,
        duration: float,
        diffs: list[float],
        threshold: float,
    ) -> None:
        """保存质量检测的关键帧截图，供人工审查。"""
        try:
            prefix = f"attempt_{attempt}"

            # 保存首帧
            if frames:
                frames[0].save(audit_dir / f"{prefix}_first_frame.png")

            # 保存最后一个好帧（裁剪点）
            trim_frame_idx = min(int(keep_time * fps), len(frames) - 1)
            if trim_frame_idx > 0 and trim_frame_idx < len(frames):
                frames[trim_frame_idx].save(audit_dir / f"{prefix}_trim_point_{keep_time:.1f}s.png")

            # 保存裁剪点后的第一个坏帧
            bad_frame_idx = min(trim_frame_idx + 1, len(frames) - 1)
            if bad_frame_idx < len(frames) and bad_frame_idx != trim_frame_idx:
                frames[bad_frame_idx].save(audit_dir / f"{prefix}_first_bad_frame_{bad_frame_idx / fps:.1f}s.png")

            # 保存尾帧
            if len(frames) > 1:
                frames[-1].save(audit_dir / f"{prefix}_last_frame.png")

            # 保存 MSE 最高的帧（最严重的问题帧）
            if diffs:
                worst_idx = max(range(len(diffs)), key=lambda i: diffs[i])
                worst_time = (worst_idx + 1) / fps
                if worst_idx + 1 < len(frames):
                    frames[worst_idx + 1].save(
                        audit_dir / f"{prefix}_worst_frame_{worst_time:.1f}s_mse{diffs[worst_idx]:.0f}.png"
                    )

            logger.info(f"质量审查: 关键帧截图已保存 → {audit_dir}/{prefix}_*.png")
        except Exception as e:
            logger.debug(f"质量审查: 截图保存失败 ({e})")

    @staticmethod
    def _trim_video_at(video_path: Path, end_time: float, min_remaining: float = 3.0) -> Path:
        """裁剪视频到指定时长。

        Args:
            video_path: 视频路径
            end_time: 保留到的时间点（秒）
            min_remaining: 裁后最短时长

        Returns:
            裁剪后的路径（原地替换）
        """
        if end_time < min_remaining:
            logger.warning(f"裁剪: 目标时长 {end_time:.1f}s 太短，跳过")
            return video_path

        trimmed_path = video_path.with_suffix(".trimmed.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path),
                 "-t", str(end_time),
                 "-c", "copy", str(trimmed_path)],
                check=True, capture_output=True, timeout=30,
            )
            trimmed_path.replace(video_path)
            logger.info(f"裁剪完成: {video_path} → {end_time:.1f}s")
            return video_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"裁剪失败: {e}")
            trimmed_path.unlink(missing_ok=True)
            return video_path

    @staticmethod
    def _remove_video_segments(video_path: Path, segments: list[dict[str, Any]], min_remaining: float = 3.0) -> Path:
        if len(segments) != 1:
            logger.warning("局部裁剪: 当前仅支持单个问题片段")
            return video_path
        segment = segments[0]
        start_time = float(segment["start"])
        end_time = float(segment["end"])
        if end_time <= start_time:
            return video_path

        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(probe.stdout.strip())
        except Exception:
            return video_path

        remaining = duration - (end_time - start_time)
        if remaining < min_remaining:
            logger.warning("局部裁剪: 剩余时长过短，跳过")
            return video_path

        edited_path = video_path.with_suffix(".edited.mp4")
        filter_complex = (
            f"[0:v]trim=start=0:end={start_time},setpts=PTS-STARTPTS[v0];"
            f"[0:v]trim=start={end_time}:end={duration},setpts=PTS-STARTPTS[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[v]"
        )
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-filter_complex",
                    filter_complex,
                    "-map",
                    "[v]",
                    str(edited_path),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            edited_path.replace(video_path)
            logger.info(f"局部裁剪完成: 删除片段 {start_time:.1f}s-{end_time:.1f}s")
            return video_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"局部裁剪失败: {e}")
            edited_path.unlink(missing_ok=True)
            return video_path

    @staticmethod
    def _extract_segment_frames(
        video_path: Path,
        output_dir: Path,
        segment: dict[str, Any],
        max_frames: int = 5,
    ) -> list[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start + 1.0))
        if end <= start:
            end = start + 1.0
        frame_times = []
        if max_frames <= 1:
            frame_times = [start]
        else:
            step = (end - start) / max_frames
            frame_times = [start + idx * step for idx in range(max_frames)]
        extracted: list[str] = []
        for idx, ts in enumerate(frame_times, start=1):
            frame_path = output_dir / f"frame_{idx:02d}_{ts:.2f}s.png"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(frame_path)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                if frame_path.exists():
                    extracted.append(str(frame_path))
            except Exception:
                frame_path.unlink(missing_ok=True)
        return extracted

    def _export_risk_bundle(
        self,
        video_path: Path,
        audit_dir: Path,
        attempt: int,
        quality: dict[str, Any],
        shot: dict[str, Any],
    ) -> None:
        review_cfg = self._review_config()
        if not bool(review_cfg.get("export_risk_bundle", False)):
            return
        max_frames = int(get_model_config("vision_judge").get("max_frames_per_segment", 5))
        bundle_dir = audit_dir / f"vision_bundle_attempt_{attempt}"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        segments = quality.get("risk_segments", [])
        segment_payload = []
        for idx, segment in enumerate(segments, start=1):
            segment_dir = bundle_dir / f"segment_{idx:02d}"
            frames = self._extract_segment_frames(video_path, segment_dir, segment, max_frames=max_frames)
            segment_payload.append(
                {
                    "segment": segment,
                    "frames": frames,
                }
            )
        prompt_payload = {
            "shot_id": shot.get("id"),
            "review_mode": self.review_mode,
            "camera_movement": shot.get("camera_movement", ""),
            "motion_control": shot.get("motion_control", {}),
            "characters_in_shot": shot.get("characters_in_shot", []),
            "risk_segments": segment_payload,
            "judge_questions": [
                "Does the character identity drift or deform in this segment?",
                "Are there duplicate or hallucinated repeated characters?",
                "Does the head orientation contradict the body motion or continuity?",
                "Should this segment be kept, cut, or should the whole shot be regenerated?",
            ],
        }
        write_json(bundle_dir / "vision_judge_request.json", prompt_payload)

        # 自动调用 vision_judge（默认不使用外部 API）
        use_external_api = bool(get_model_config("vision_judge").get("use_external_api", False))
        judge_result_path = bundle_dir / "vision_judge_result.json"
        judge_script = Path(__file__).parent / "vision_judge.py"
        cmd = [
            sys.executable,
            str(judge_script),
            "--request", str(bundle_dir / "vision_judge_request.json"),
            "--output", str(judge_result_path),
        ]
        if use_external_api:
            cmd.append("--use-api")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            if use_external_api:
                logger.info(f"Vision judge completed: {judge_result_path}")
            else:
                logger.info(f"Vision bundle exported, waiting for manual judgment: {bundle_dir}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Vision judge failed: {e.stderr}")
        except Exception as e:
            logger.warning(f"Vision judge error: {e}")

    def _get_character_appearances(self, characters_in_shot: list[str]) -> list[tuple[str, str]]:
        """从 storyboard.characters 读取角色外貌（单一真相源）。"""
        characters_cfg = self.storyboard.get("characters", {})
        appearances = []
        for char_id in characters_in_shot:
            char = characters_cfg.get(char_id, {})
            appearance = char.get("appearance", "")
            if appearance:
                appearances.append((char_id, appearance))
        return appearances

    def _get_prop_appearances(self, props_in_shot: list[str]) -> list[tuple[str, str]]:
        """从 storyboard.prop_refs 读取关键道具描述。"""
        prop_cfg = self.storyboard.get("prop_refs", {})
        appearances = []
        if not isinstance(prop_cfg, dict):
            return appearances
        for prop_id in props_in_shot:
            prop = prop_cfg.get(prop_id, {})
            if not isinstance(prop, dict):
                continue
            appearance = str(prop.get("appearance") or prop.get("ref_description") or "").strip()
            if appearance:
                appearances.append((prop_id, appearance))
        return appearances

    async def _generate_scene_image(self, scene: dict[str, Any]) -> Path | None:
        """生成纯环境场景图（无角色），同场景所有镜头共享。"""
        scene_id = scene.get("id", "default")
        env_desc = scene.get("environment_description", "")
        if not env_desc:
            return None

        style_anchor = str(self.storyboard.get("style_anchor", ""))
        lighting = scene.get("lighting", "")
        weather = scene.get("weather", "")
        prompt_parts = []
        if style_anchor:
            prompt_parts.append(style_anchor)
        prompt_parts.append("")
        prompt_parts.append("Generate a PURE BACKGROUND/ENVIRONMENT scene image.")
        prompt_parts.append("⚠️ CRITICAL: DO NOT draw any characters, people, or figures. This is ONLY the environment/background.")
        prompt_parts.append("")
        prompt_parts.append(f"ENVIRONMENT: {env_desc}")
        if lighting:
            prompt_parts.append(f"LIGHTING: {lighting}")
        if weather:
            prompt_parts.append(f"WEATHER/ATMOSPHERE: {weather}")
        prompt_parts.append("")
        prompt_parts.append("REQUIREMENTS:")
        prompt_parts.append("1. NO characters, people, animals, or figures — ONLY environment")
        prompt_parts.append("2. Do NOT include handheld or shot-specific props that should only appear when later shots call for them")
        prompt_parts.append("3. Rich environmental details with cinematic lighting")
        prompt_parts.append("4. Leave space where characters would naturally be positioned")
        prompt_parts.append("5. High quality background suitable as reference for subsequent shot generation")

        prompt = "\n".join(prompt_parts)
        out = self.image_dir / f"scene_{scene_id}.png"

        logger.info(f"场景 {scene_id}: 生成纯环境场景图...")
        async with self.sem:
            path, provider = await self._generate_image(prompt, shot_id=0, output_path=out)
        if path and path.exists() and path.stat().st_size > 1000:
            return path
        logger.warning(f"场景 {scene_id}: 场景图生成失败，分镜将不使用场景参考图")
        return None

    def _collect_character_ref_bindings(self, characters_in_shot: list[str]) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        characters_cfg = self.storyboard.get("characters", {})
        ref_dir = str(self.storyboard.get("character_ref_dir", "")).strip()
        for char_id in characters_in_shot:
            char_info = characters_cfg.get(char_id, {})
            ref_image = str(char_info.get("ref_image", "")).strip()
            ref_path_value = str(char_info.get("ref_path", "")).strip()
            resolved_path: Path | None = None
            if ref_path_value:
                resolved_path = Path(ref_path_value).expanduser().resolve()
            elif ref_image and ref_dir:
                resolved_path = (Path(ref_dir) / ref_image).expanduser().resolve()
            bindings.append(
                {
                    "character_id": char_id,
                    "ref_image": ref_image,
                    "ref_path": str(resolved_path) if resolved_path else None,
                    "exists": bool(resolved_path and resolved_path.exists()),
                }
            )
        return bindings

    def _collect_prop_ref_bindings(self, props_in_shot: list[str]) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        prop_cfg = self.storyboard.get("prop_refs", {})
        if not isinstance(prop_cfg, dict):
            return bindings
        for prop_id in props_in_shot:
            prop = prop_cfg.get(prop_id, {})
            if not isinstance(prop, dict):
                continue
            ref_image = str(prop.get("ref_image", "")).strip()
            ref_path_value = str(prop.get("ref_path", "")).strip()
            resolved_path: Path | None = None
            if ref_path_value:
                resolved_path = Path(ref_path_value).expanduser().resolve()
            bindings.append(
                {
                    "prop_id": prop_id,
                    "ref_image": ref_image,
                    "ref_path": str(resolved_path) if resolved_path else None,
                    "exists": bool(resolved_path and resolved_path.exists()),
                }
            )
        return bindings

    @staticmethod
    def _append_video_reference(
        refs: list[dict[str, Any]],
        *,
        path: Path | None,
        usage: str,
        source_type: str,
        source: str,
        subject: str = "",
        stage: str = "",
        description: str = "",
        timestamp: float | None = None,
        generated: bool = False,
    ) -> None:
        if not path or not path.exists():
            return
        entry: dict[str, Any] = {
            "path": path,
            "usage": AssetGenerator._normalize_video_reference_usage(usage),
            "source_type": source_type,
            "source": source,
            "media_type": "image",
            "generated": generated,
        }
        if subject:
            entry["subject"] = subject
        if stage:
            entry["stage"] = stage
        if description:
            entry["description"] = description
        if isinstance(timestamp, (int, float)):
            entry["timestamp"] = float(timestamp)
        refs.append(entry)

    def _resolve_explicit_video_reference(
        self,
        ref_cfg: dict[str, Any],
        *,
        first_frame_path: Path,
        scene_image: Path | None,
        style_reference_frame: Path | None,
        end_frame_path: Path | None,
        character_ref_bindings: list[dict[str, Any]],
        prop_ref_bindings: list[dict[str, Any]],
        keyframe_results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        path_value = str(ref_cfg.get("path", "")).strip()
        resolved_path: Path | None = None
        source_type = str(ref_cfg.get("source_type", "")).strip()
        source_id = str(ref_cfg.get("source_id", "")).strip()
        compact_source = str(ref_cfg.get("source", "")).strip()

        if path_value:
            resolved_path = Path(path_value).expanduser().resolve()
            source_type = source_type or "file"
            source_id = source_id or resolved_path.name
        else:
            token = compact_source or source_type
            if compact_source and ":" in compact_source:
                source_type, source_id = compact_source.split(":", 1)
                source_type = source_type.strip()
                source_id = source_id.strip()

            character_map = {
                str(item.get("character_id", "")).strip(): Path(str(item["ref_path"]))
                for item in character_ref_bindings
                if item.get("exists") and item.get("ref_path")
            }
            prop_map = {
                str(item.get("prop_id", "")).strip(): Path(str(item["ref_path"]))
                for item in prop_ref_bindings
                if item.get("exists") and item.get("ref_path")
            }
            stage_map = {
                str(item.get("index")): Path(str(item["path"]))
                for item in keyframe_results
                if item.get("path")
            }

            usage = self._normalize_video_reference_usage(ref_cfg.get("usage"))
            if source_type in {"first_frame", "frame"} and (
                source_id in {"", "first_frame"} or (source_type == "frame" and usage == "first_frame")
            ):
                resolved_path = first_frame_path
                source_type = "frame"
                source_id = "first_frame"
            elif source_type in {"target_state", "last_frame", "frame"} and (
                source_id in {"target_state", "last_frame"} or (source_type == "frame" and usage == "reference_target_state")
            ):
                resolved_path = end_frame_path
                source_type = "frame"
                source_id = "target_state"
            elif source_type in {"scene", "scene_ref"}:
                resolved_path = scene_image
                source_type = "scene"
                source_id = source_id or "scene_ref"
            elif source_type in {"style", "style_ref"}:
                resolved_path = style_reference_frame
                source_type = "style"
                source_id = source_id or "style_ref"
            elif source_type == "character":
                resolved_path = character_map.get(source_id)
            elif source_type == "prop":
                resolved_path = prop_map.get(source_id)
            elif source_type == "stage":
                resolved_path = stage_map.get(source_id)

        if not resolved_path or not resolved_path.exists():
            return None

        usage = self._normalize_video_reference_usage(ref_cfg.get("usage"))
        resolved: dict[str, Any] = {
            "path": resolved_path,
            "usage": usage,
            "source_type": source_type or "file",
            "source": source_id or compact_source or resolved_path.name,
            "media_type": "image",
            "generated": bool(ref_cfg.get("generated", False)),
        }
        for key in ("subject", "stage", "description"):
            value = str(ref_cfg.get(key, "")).strip()
            if value:
                resolved[key] = value
        timestamp = ref_cfg.get("timestamp")
        if isinstance(timestamp, (int, float)):
            resolved["timestamp"] = float(timestamp)
        return resolved

    def _build_video_references(
        self,
        shot: dict[str, Any],
        *,
        first_frame_path: Path,
        scene_image: Path | None,
        style_reference_frame: Path | None,
        end_frame_path: Path | None,
        character_ref_bindings: list[dict[str, Any]],
        prop_ref_bindings: list[dict[str, Any]],
        keyframe_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        explicit_refs = shot.get("video_references")
        if isinstance(explicit_refs, list) and explicit_refs:
            resolved_explicit: list[dict[str, Any]] = []
            for item in explicit_refs:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled") is False:
                    continue
                resolved = self._resolve_explicit_video_reference(
                    item,
                    first_frame_path=first_frame_path,
                    scene_image=scene_image,
                    style_reference_frame=style_reference_frame,
                    end_frame_path=end_frame_path,
                    character_ref_bindings=character_ref_bindings,
                    prop_ref_bindings=prop_ref_bindings,
                    keyframe_results=keyframe_results,
                )
                if resolved:
                    resolved_explicit.append(resolved)
            if resolved_explicit:
                return resolved_explicit

        refs: list[dict[str, Any]] = []
        self._append_video_reference(
            refs,
            path=first_frame_path,
            usage="first_frame",
            source_type="frame",
            source="first_frame",
            generated=True,
        )
        self._append_video_reference(
            refs,
            path=scene_image,
            usage="reference_composition",
            source_type="scene",
            source="scene_ref",
        )
        self._append_video_reference(
            refs,
            path=style_reference_frame,
            usage="reference_style",
            source_type="style",
            source="style_ref",
        )
        for item in character_ref_bindings:
            ref_path = item.get("ref_path")
            char_id = str(item.get("character_id", "")).strip()
            if ref_path and char_id:
                self._append_video_reference(
                    refs,
                    path=Path(str(ref_path)),
                    usage="reference_character",
                    source_type="character",
                    source=char_id,
                    subject=char_id,
                )
        for item in prop_ref_bindings:
            ref_path = item.get("ref_path")
            prop_id = str(item.get("prop_id", "")).strip()
            if ref_path and prop_id:
                self._append_video_reference(
                    refs,
                    path=Path(str(ref_path)),
                    usage="reference_prop",
                    source_type="prop",
                    source=prop_id,
                    subject=prop_id,
                )
        for item in keyframe_results:
            ref_path = item.get("path")
            if not ref_path:
                continue
            self._append_video_reference(
                refs,
                path=Path(str(ref_path)),
                usage="reference_stage",
                source_type="stage",
                source=str(item.get("index", "")),
                stage=str(item.get("stage", "")).strip(),
                description=str(item.get("description", "")).strip(),
                timestamp=item.get("timestamp"),
                generated=True,
            )
        self._append_video_reference(
            refs,
            path=end_frame_path,
            usage="reference_target_state",
            source_type="frame",
            source="target_state",
            generated=True,
        )
        return refs

    def _assess_image_review_risk(
        self,
        shot: dict[str, Any],
        image_provider: str,
        character_ref_bindings: list[dict[str, Any]],
        style_reference_frame: Path | None,
    ) -> dict[str, Any]:
        reasons: list[dict[str, Any]] = []
        subject_constraints = shot.get("subject_constraints", {})
        pose_contract = subject_constraints.get("pose_contract", []) if isinstance(subject_constraints, dict) else []
        shot_delta = shot.get("shot_delta", [])
        scene_continuity = shot.get("_scene_continuity", {})
        if image_provider == "placeholder":
            reasons.append(
                {
                    "reason": "placeholder_image",
                    "severity": "high",
                    "details": "Image generation fell back to placeholder output.",
                }
            )
        missing_refs = [item["character_id"] for item in character_ref_bindings if not item.get("exists")]
        if missing_refs:
            reasons.append(
                {
                    "reason": "missing_character_reference",
                    "severity": "high",
                    "details": f"Missing character refs: {', '.join(missing_refs)}",
                }
            )
        if style_reference_frame and style_reference_frame.exists():
            reasons.append(
                {
                    "reason": "cross_scene_style_continuity",
                    "severity": "medium",
                    "details": "This shot starts a new scene but should preserve the previous scene's visual medium and character rendering.",
                }
            )
        if isinstance(pose_contract, list) and any(str(item).strip() for item in pose_contract):
            reasons.append(
                {
                    "reason": "pose_contract_continuity",
                    "severity": "medium",
                    "details": "This shot declares pose-contract constraints that should be checked for posture and support-state drift.",
                }
            )
        if isinstance(shot_delta, list) and any(str(item).strip() for item in shot_delta):
            reasons.append(
                {
                    "reason": "shot_delta_scope",
                    "severity": "medium",
                    "details": "This shot declares a limited change scope that should be checked against unexpected extra changes.",
                }
            )
        if isinstance(scene_continuity, dict) and scene_continuity:
            stable_facts = scene_continuity.get("stable_facts", {})
            carry_forward_subjects = scene_continuity.get("carry_forward_subjects", [])
            has_stable_facts = isinstance(stable_facts, dict) and any(
                isinstance(value, list) and any(str(item).strip() for item in value)
                for value in stable_facts.values()
            )
            has_carry_forward = isinstance(carry_forward_subjects, list) and any(
                str(item).strip() for item in carry_forward_subjects
            )
            if has_stable_facts or has_carry_forward:
                reasons.append(
                    {
                        "reason": "scene_continuity_facts",
                        "severity": "medium",
                        "details": "This shot inherits scene-level continuity facts that should be checked for spatial, prop, and environment consistency.",
                    }
                )
        return {
            "needs_review": bool(reasons),
            "reasons": reasons,
        }

    @staticmethod
    def _collect_hard_constraint_summary(
        shot: dict[str, Any],
    ) -> dict[str, Any]:
        subject_constraints = shot.get("subject_constraints", {})
        scene_continuity = shot.get("_scene_continuity", {})

        pose_contract = []
        gaze_contract = {}
        if isinstance(subject_constraints, dict):
            pose_value = subject_constraints.get("pose_contract", [])
            if isinstance(pose_value, str):
                pose_contract = [pose_value]
            elif isinstance(pose_value, list):
                pose_contract = [str(item).strip() for item in pose_value if str(item).strip()]
            gaze_value = subject_constraints.get("gaze_contract", {})
            if isinstance(gaze_value, dict):
                gaze_contract = gaze_value

        stable_facts = {}
        entity_registry = {}
        if isinstance(scene_continuity, dict):
            stable_value = scene_continuity.get("stable_facts", {})
            if isinstance(stable_value, dict):
                stable_facts = stable_value
            entity_value = scene_continuity.get("entity_registry", {})
            if isinstance(entity_value, dict):
                entity_registry = entity_value

        shot_delta = shot.get("shot_delta", [])
        if not isinstance(shot_delta, list):
            shot_delta = []
        shot_delta = [str(item).strip() for item in shot_delta if str(item).strip()]

        return {
            "pose_contract": pose_contract,
            "gaze_contract": gaze_contract,
            "stable_facts": stable_facts,
            "entity_registry": entity_registry,
            "shot_delta": shot_delta,
        }

    @staticmethod
    def _needs_reference_validation(
        shot: dict[str, Any],
        hard_constraints: dict[str, Any],
    ) -> bool:
        if any(hard_constraints.get(key) for key in ("pose_contract", "gaze_contract", "stable_facts", "entity_registry")):
            return True
        return bool(hard_constraints.get("shot_delta")) and str(shot.get("continuity_mode", "")).strip() == "strict"

    def _export_reference_review_bundle(
        self,
        shot: dict[str, Any],
        references: list[dict[str, Any]],
        hard_constraints: dict[str, Any],
        video_prompt_text: str | None = None,
    ) -> Path:
        shot_id = int(shot.get("id", 0))
        bundle_dir = self.output_root / "image_audit" / f"shot_{shot_id:03d}" / "reference_bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        resolved_contract = self._resolve_shot_contract(shot)

        copied_refs: list[dict[str, Any]] = []
        for item in references:
            path = item.get("path")
            if not path:
                continue
            src = Path(path)
            if not src.exists():
                continue
            dst = bundle_dir / src.name
            shutil.copy2(src, dst)
            copied = dict(item)
            copied["copied_path"] = str(dst)
            copied_refs.append(copied)

        video_prompt_path: str | None = None
        if video_prompt_text:
            prompt_file = bundle_dir / "video_prompt.txt"
            write_text(prompt_file, video_prompt_text)
            video_prompt_path = str(prompt_file)

        payload = {
            "shot_id": shot_id,
            "review_type": "reference_validation",
            "review_mode": self.review_mode,
            "video_prompt_path": video_prompt_path,
            "references": copied_refs,
            "shot_context": {
                "director_plan": resolved_contract.get("director_plan", {}),
                "scene_prompt": resolved_contract.get("scene_prompt", ""),
                "action_prompt": resolved_contract.get("action_prompt", ""),
                "end_frame_description": resolved_contract.get("end_frame_description", ""),
                "keyframes": resolved_contract.get("keyframes", []),
                "video_references": shot.get("video_references", []),
                "subject_constraints": shot.get("subject_constraints", {}),
                "shot_delta": shot.get("shot_delta", []),
                "scene_continuity": shot.get("_scene_continuity", {}),
            },
            "hard_constraints": hard_constraints,
            "judge_questions": [
                "Do these reference images represent a coherent sequence of narrative state nodes for this shot?",
                "Do they preserve pose_contract, gaze_contract, scene stable facts, and entity uniqueness before video generation?",
                "Should video generation be blocked until the references are regenerated?",
            ],
        }
        write_json(bundle_dir / "reference_review_request.json", payload)
        return bundle_dir

    @staticmethod
    def _serialize_video_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **{k: v for k, v in ref.items() if k != "path"},
                "path": str(ref["path"]),
            }
            for ref in references
        ]

    @staticmethod
    def _compose_prompt_with_reference_mentions(
        prompt: str,
        references: list[dict[str, Any]],
    ) -> str:
        prompt_ready_refs: list[dict[str, Any]] = []
        for idx, ref in enumerate(references, start=1):
            prompt_ref = dict(ref)
            prompt_ref["mention"] = f"@图片{idx}"
            prompt_ready_refs.append(prompt_ref)
        return VideoPromptBuilder.compose_video_generation_prompt(prompt, prompt_ready_refs)

    def _export_video_prompt_artifact(
        self,
        shot_id: int,
        prompt_text: str,
    ) -> Path:
        prompt_path = self.output_root / "prompts" / f"shot_{shot_id:03d}_video_prompt.txt"
        write_text(prompt_path, prompt_text)
        return prompt_path

    def _export_image_review_bundle(
        self,
        shot: dict[str, Any],
        image_path: Path,
        image_provider: str,
        scene_image: Path | None,
        style_reference_frame: Path | None,
        character_ref_bindings: list[dict[str, Any]],
        risk: dict[str, Any],
    ) -> Path:
        shot_id = int(shot.get("id", 0))
        bundle_dir = self.output_root / "image_audit" / f"shot_{shot_id:03d}" / "image_bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        resolved_contract = self._resolve_shot_contract(shot)

        shot_image_copy = bundle_dir / image_path.name
        if image_path.exists():
            shutil.copy2(image_path, shot_image_copy)

        copied_scene_image: str | None = None
        if scene_image and scene_image.exists():
            scene_copy = bundle_dir / f"scene_reference{scene_image.suffix}"
            shutil.copy2(scene_image, scene_copy)
            copied_scene_image = str(scene_copy)

        copied_style_reference: str | None = None
        if style_reference_frame and style_reference_frame.exists():
            style_copy = bundle_dir / f"style_reference{style_reference_frame.suffix}"
            shutil.copy2(style_reference_frame, style_copy)
            copied_style_reference = str(style_copy)

        copied_character_refs: list[dict[str, Any]] = []
        for binding in character_ref_bindings:
            ref_path = binding.get("ref_path")
            copied_entry = dict(binding)
            if ref_path and Path(ref_path).exists():
                src = Path(ref_path)
                dst = bundle_dir / f"character_ref_{binding['character_id']}{src.suffix}"
                shutil.copy2(src, dst)
                copied_entry["copied_path"] = str(dst)
            copied_character_refs.append(copied_entry)

        payload = {
            "shot_id": shot_id,
            "review_type": "image",
            "review_mode": self.review_mode,
            "image_path": str(shot_image_copy),
            "image_provider": image_provider,
            "scene_reference_path": copied_scene_image,
            "style_reference_path": copied_style_reference,
            "character_refs": copied_character_refs,
            "risk_summary": risk,
            "shot_context": {
                "director_plan": resolved_contract.get("director_plan", {}),
                "scene_prompt": resolved_contract.get("scene_prompt", ""),
                "action_prompt": resolved_contract.get("action_prompt", ""),
                "end_frame_description": resolved_contract.get("end_frame_description", ""),
                "camera_movement": shot.get("camera_movement", ""),
                "camera_technical": shot.get("camera_technical", ""),
                "characters_in_shot": shot.get("characters_in_shot", []),
                "shot_type": shot.get("shot_type", ""),
                "consistency_anchors": shot.get("consistency_anchors", {}),
                "motion_control": shot.get("motion_control", {}),
                "subject_constraints": shot.get("subject_constraints", {}),
                "shot_delta": shot.get("shot_delta", []),
                "scene_continuity": shot.get("_scene_continuity", {}),
            },
            "judge_questions": [
                "Does this shot preserve the same visual medium and rendering style as the prior scene when continuity is expected?",
                "Do the main characters still match their reference identity and material treatment?",
                "If scene continuity facts are provided, does this image preserve the required spatial layout, prop states, and environment states?",
                "If pose contracts are provided, do the characters keep the same physical support relationships without implausible posture drift?",
                "Does this image limit itself to the declared shot_delta changes instead of changing unrelated stable facts?",
                "Is there an obvious photorealistic vs illustrated/anime style jump that should block video generation?",
                "Should this image be kept or regenerated before video generation?",
            ],
        }
        write_json(bundle_dir / "image_review_request.json", payload)
        return bundle_dir

    async def _extract_all_shot_prompts(
        self, all_shots: list[dict[str, Any]],
    ) -> dict[int, dict[str, str]]:
        """全局 prompt 提取：一次性为所有 shot 生成首帧/尾帧/动作 prompt。

        将完整 narrative + 所有 shot 的上下文一次性交给 LLM，
        LLM 看到完整故事线 + 前后镜头上下文，提取出的 prompt 天然连贯。

        返回: {shot_id: {"first_frame_prompt": ..., "last_frame_prompt": ..., "video_action_prompt": ...}}
        如果 LLM 调用失败，fallback 到各 shot 的原始字段。
        """
        narrative = str(self.storyboard.get("narrative", ""))

        # 构建 fallback 结果（用原始字段）
        fallback: dict[int, dict[str, str]] = {}
        all_have_director_prompts = bool(all_shots)
        for shot in all_shots:
            sid = int(shot.get("id", 0))
            resolved_contract = self._resolve_shot_contract(shot)
            director_entry = self._director_prompt_entry(sid)
            director_first = str(((director_entry.get("first_frame") or {}).get("prompt") or "")).strip()
            director_last = str(((director_entry.get("last_frame") or {}).get("prompt") or "")).strip()
            director_action = str(director_entry.get("video_action") or "").strip()
            fallback[sid] = {
                "first_frame_prompt": director_first or str(resolved_contract.get("scene_prompt", "")),
                "last_frame_prompt": director_last or str(resolved_contract.get("end_frame_description", "")),
                "video_action_prompt": director_action or str(resolved_contract.get("action_prompt", "")),
            }
            if not (director_first and director_last and director_action):
                all_have_director_prompts = False

        if not all_shots:
            return fallback

        if all_have_director_prompts:
            logger.info("所有 shots 均存在 director_prompts，跳过 LLM 提取")
            return fallback

        # LLM 凭据
        llm_cfg = get_model_config("llm")
        creds = get_api_credentials(llm_cfg.get("provider", "apimart"), self.cfg)
        if not creds.get("api_key"):
            logger.warning("LLM API 无凭据，fallback 到原始 prompt")
            return fallback

        model = llm_cfg.get("model", "gemini-2.5-flash")
        timeout = llm_cfg.get("timeout", 120)

        style_anchor = str(self.storyboard.get("style_anchor", ""))
        style_medium_lock = VideoPromptBuilder.infer_style_medium_lock(style_anchor)

        # 构建 system prompt（统一中文，不再做中文→英文翻译）
        system_prompt = (
            "你是一个服务于视频生成流水线的视觉提示词提取器。\n\n"
            "你会收到完整 narrative 和所有 shots。你的任务不是翻译，而是把已有中文分镜描述整理成更精确、彼此连贯的中文提示词。\n\n"
            "如果某个 shot 包含 DIRECTOR_PLAN 节点，请把它视为阶段设计的权威来源：首节点对应首帧，中间节点对应关键状态，末节点对应尾帧。\n\n"
            "对每个 shot，输出三个中文结果：\n\n"
            "1. FIRST_FRAME：动作开始前一瞬间的静态画面描述\n"
            "2. LAST_FRAME：动作完成后一瞬间的静态画面描述\n"
            "3. VIDEO_ACTION：连接 FIRST_FRAME 和 LAST_FRAME 的运动过程描述\n\n"
            "规则：\n"
            "- 全部使用中文\n"
            "- 不要重复角色外貌与服装描述（这些由参考图和代码注入）\n"
            "- 保持相邻镜头首尾状态连续\n"
            "- FIRST_FRAME 和 LAST_FRAME 都必须是静态画面，不写运动过程\n"
            "- VIDEO_ACTION 只写运动和变化，不写静态外观\n"
            "- 只输出指定格式，不要附加解释\n\n"
            "输出格式：\n"
            "===SHOT_{id}===\n"
            "FIRST_FRAME: <中文提示词>\n"
            "LAST_FRAME: <中文提示词>\n"
            "VIDEO_ACTION: <中文提示词>\n"
            "(每个 shot 重复一次)"
        )

        # 构建 user message：narrative + all shots
        user_parts = []
        if narrative:
            user_parts.append(f"完整叙事：\n{narrative}")
            user_parts.append("")
        if style_anchor:
            user_parts.append(f"全局风格锚点：\n{style_anchor}")
            user_parts.append("")
        if style_medium_lock.get("lock_line"):
            user_parts.append(f"风格媒介锁定：\n{style_medium_lock['lock_line']}")
            user_parts.append("")

        for i, shot in enumerate(all_shots):
            sid = shot.get("id", i + 1)
            resolved_contract = self._resolve_shot_contract(shot)
            user_parts.append(f"--- SHOT {sid} ---")
            ns = shot.get("narrative_segment", "")
            if ns:
                user_parts.append(f"对应叙事片段：{ns}")
            director_plan = resolved_contract.get("director_plan", {})
            if isinstance(director_plan, dict) and director_plan:
                dramatic_core = str(director_plan.get("dramatic_core", "")).strip()
                if dramatic_core:
                    user_parts.append(f"导演戏核：{dramatic_core}")
                not_this_shot = str(director_plan.get("not_this_shot", "")).strip()
                if not_this_shot:
                    user_parts.append(f"非本镜任务：{not_this_shot}")
                viewer_flow = director_plan.get("viewer_information_flow", [])
                if isinstance(viewer_flow, list):
                    cleaned_flow = [str(item).strip() for item in viewer_flow if str(item).strip()]
                    if cleaned_flow:
                        user_parts.append(f"观众信息顺序：{' -> '.join(cleaned_flow)}")
                nodes = resolved_contract.get("nodes", [])
                if isinstance(nodes, list) and nodes:
                    for node_index, node in enumerate(nodes, start=1):
                        node_text = self._director_node_text(node, include_delta=True)
                        if node_text:
                            user_parts.append(f"导演节点 {node_index}: {node_text}")
            sp = resolved_contract.get("scene_prompt", "")
            if sp:
                user_parts.append(f"首帧基础描述：{sp}")
            ap = resolved_contract.get("action_prompt", "")
            if ap:
                user_parts.append(f"动作过程：{ap}")
            ef = resolved_contract.get("end_frame_description", "")
            if ef:
                user_parts.append(f"尾帧基础描述：{ef}")
            keyframes = resolved_contract.get("keyframes", [])
            if isinstance(keyframes, list):
                for keyframe in keyframes:
                    if not isinstance(keyframe, dict):
                        continue
                    description = str(keyframe.get("description", "")).strip()
                    timestamp = keyframe.get("timestamp")
                    if description:
                        user_parts.append(f"关键帧@{timestamp}s: {description}")
            dur = shot.get("estimated_duration", 8)
            user_parts.append(f"镜头时长: {dur}s")
            cm = shot.get("camera_movement", "")
            if cm:
                user_parts.append(f"运镜: {cm}")
            user_parts.append("")

        user_msg = "\n".join(user_parts)

        # 调用 LLM
        try:
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 4096,
                "temperature": 0.3,
                "stream": False,
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"全局 prompt 提取失败 ({resp.status}): {body[:200]}，fallback")
                        return fallback
                    data = await resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            if not content:
                logger.warning("全局 prompt 提取：LLM 返回空内容，fallback")
                return fallback

            # 解析输出 — 按 ===SHOT_{id}=== 分块
            result = dict(fallback)  # 以 fallback 为底，逐个覆盖
            import re
            blocks = re.split(r'===SHOT[_\s]*(\d+)===', content)
            # blocks: ['', '1', 'block1_content', '2', 'block2_content', ...]
            i = 1
            while i < len(blocks) - 1:
                try:
                    shot_id = int(blocks[i])
                except (ValueError, IndexError):
                    i += 2
                    continue
                block = blocks[i + 1]

                first_frame = ""
                last_frame = ""
                video_action = ""
                for line in block.split("\n"):
                    line = line.strip()
                    upper = line.upper()
                    if upper.startswith("FIRST_FRAME:"):
                        first_frame = line.split(":", 1)[1].strip()
                    elif upper.startswith("LAST_FRAME:"):
                        last_frame = line.split(":", 1)[1].strip()
                    elif upper.startswith("VIDEO_ACTION:"):
                        video_action = line.split(":", 1)[1].strip()

                if first_frame or last_frame or video_action:
                    extracted = {}
                    if first_frame:
                        extracted["first_frame_prompt"] = first_frame
                    else:
                        extracted["first_frame_prompt"] = fallback.get(shot_id, {}).get("first_frame_prompt", "")
                    if last_frame:
                        extracted["last_frame_prompt"] = last_frame
                    else:
                        extracted["last_frame_prompt"] = fallback.get(shot_id, {}).get("last_frame_prompt", "")
                    if video_action:
                        extracted["video_action_prompt"] = video_action
                    else:
                        extracted["video_action_prompt"] = fallback.get(shot_id, {}).get("video_action_prompt", "")
                    result[shot_id] = extracted
                    logger.info(f"shot_{shot_id}: 全局提取成功")
                    logger.debug(f"  first_frame: {first_frame[:80]}...")
                    logger.debug(f"  last_frame: {last_frame[:80]}...")
                    logger.debug(f"  video_action: {video_action[:80]}...")

                i += 2

            logger.info(f"全局 prompt 提取完成: {len(result)} shots")
            return result

        except Exception as e:
            logger.warning(f"全局 prompt 提取异常: {e}，fallback")
            return fallback

    async def _generate_shot(
        self, shot: dict[str, Any],
        provided_first_frame: Path | None = None,
        scene_context: dict[str, Any] | None = None,
        extracted_prompts: dict[int, dict[str, str]] | None = None,
        is_last_in_scene: bool = True,
        continuity_mode: str = "scene_end",
        style_reference_frame: Path | None = None,
    ) -> dict[str, Any]:
        """生成单镜头素材。

        Args:
            provided_first_frame: 上一镜头的尾帧图路径，直接复用为本镜头首帧。
            scene_context: 场景层级环境上下文（lighting, weather, props, environment_description）。
            extracted_prompts: 全局提取的 prompt dict {shot_id: {first_frame_prompt, last_frame_prompt, video_action_prompt}}。
            is_last_in_scene: 是否为 scene 最后一个 shot。
            continuity_mode: 连续性模式 - "strict"(强约束尾帧) / "scene_end"(仅scene末尾) / "free"(自由运动)。
        """
        if scene_context is None:
            scene_context = {}
        if extracted_prompts is None:
            extracted_prompts = {}

        shot_id = int(shot.get("id", 0))
        resolved_contract = self._resolve_shot_contract(shot)
        director_entry = self._director_prompt_entry(shot_id)
        director_plan = resolved_contract.get("director_plan", {})
        first_node = resolved_contract.get("first_node")
        last_node = resolved_contract.get("last_node")
        middle_nodes = resolved_contract.get("middle_nodes", [])
        # 兼容新旧字段名：scene_prompt（新）/ image_prompt（旧）
        scene_prompt = str(resolved_contract.get("scene_prompt", ""))
        end_frame_desc = str(resolved_contract.get("end_frame_description", ""))
        estimated_duration = int(shot.get("estimated_duration", 10))
        characters_in_shot = shot.get("characters_in_shot", [])
        props_in_shot = shot.get("props_in_shot", [])
        if not isinstance(props_in_shot, list):
            props_in_shot = []
        consistency_anchors = shot.get("consistency_anchors")
        motion_control = shot.get("motion_control")
        subject_constraints = shot.get("subject_constraints")
        shot_delta = shot.get("shot_delta")
        scene_continuity = scene_context.get("scene_continuity", {})
        shot_type = self._shot_type(shot)

        if scene_continuity and shot.get("_scene_continuity") != scene_continuity:
            shot["_scene_continuity"] = scene_continuity

        # 6D 增强字段
        camera_move = str(shot.get("camera_movement", ""))
        camera_tech = str(shot.get("camera_technical", ""))
        atmosphere = str(shot.get("atmosphere_lighting", ""))
        physics = str(shot.get("physics_note", ""))
        raw_action = str(resolved_contract.get("action_prompt", ""))

        # narration: 优先 narration，fallback 到 tts_text → subtitle
        narration = str(shot.get("narration", "") or shot.get("tts_text", "") or shot.get("subtitle", ""))

        style_anchor = str(self.storyboard.get("style_anchor", ""))

        # ── 单一真相源：从 characters 读取外貌 ──
        char_appearances = self._get_character_appearances(characters_in_shot)
        prop_appearances = self._get_prop_appearances(props_in_shot)

        # ── 从全局提取结果获取首帧/尾帧/动作 prompt ──
        shot_prompts = extracted_prompts.get(shot_id, {})
        director_first = str(((director_entry.get("first_frame") or {}).get("prompt") or "")).strip()
        director_last = str(((director_entry.get("last_frame") or {}).get("prompt") or "")).strip()
        director_action = str(director_entry.get("video_action") or "").strip()
        first_frame_text = director_first or shot_prompts.get("first_frame_prompt", "") or scene_prompt
        last_frame_text = director_last or shot_prompts.get("last_frame_prompt", "") or end_frame_desc
        video_action_text = director_action or shot_prompts.get("video_action_prompt", "") or raw_action

        # ── 构建结构化图片 prompt（首帧） ──
        image_prompt = VideoPromptBuilder.build_image_prompt(
            style_anchor=style_anchor,
            character_appearances=char_appearances,
            prop_appearances=prop_appearances,
            scene_description=first_frame_text,  # 全局提取的首帧视觉描述
            motion_control=motion_control,
            camera_technical=camera_tech,
            atmosphere=atmosphere,
            physics=physics,
            consistency_anchors=consistency_anchors,
            action_hint=raw_action,  # 让首帧为接下来的动作做好姿态准备
            scene_environment=scene_context.get("environment_description", ""),
            scene_lighting=scene_context.get("lighting", ""),
            scene_weather=scene_context.get("weather", ""),
            scene_props=scene_context.get("active_props", scene_context.get("props")),
            scene_continuity=scene_continuity,
            subject_constraints=subject_constraints,
            shot_delta=shot_delta,
            shot_type=shot_type,
            director_plan=director_plan,
            node_context=first_node,
        )

        # ── 场景参考图（同场景所有镜头共享的视觉基底）──
        scene_image = scene_context.get("scene_image")
        character_ref_bindings = self._collect_character_ref_bindings(characters_in_shot)
        prop_ref_bindings = self._collect_prop_ref_bindings(props_in_shot)

        # ── 首帧图：优先复用上一镜头尾帧，否则自己生成 ──
        if provided_first_frame and provided_first_frame.exists():
            image_path = provided_first_frame
            image_provider = "chained"
            logger.info(f"shot_{shot_id}: 复用上一镜头尾帧作为首帧")
        else:
            # 检查 video_only 模式
            if self.video_only:
                # 检查图片是否已存在
                expected_image = self.image_dir / f"shot_{shot_id:03d}.png"
                if expected_image.exists():
                    image_path = expected_image
                    image_provider = "existing"
                    logger.info(f"shot_{shot_id}: video_only 模式，使用已存在的图片: {expected_image.name}")
                else:
                    logger.error(f"shot_{shot_id}: video_only 模式但图片不存在: {expected_image}")
                    logger.error(f"  请先生成图片，或不使用 --video-only 参数")
                    raise SystemExit("video_only 模式下图片必须已存在")
            else:
                # 正常流程，生成图片
                async with self.sem:
                    image_path, image_provider = await self._generate_image(
                        image_prompt, shot_id,
                        characters_in_shot=characters_in_shot,
                        props_in_shot=props_in_shot,
                        scene_image=scene_image,
                        style_reference_image=style_reference_frame,
                    )

        image_review_risk = self._assess_image_review_risk(
            shot=shot,
            image_provider=image_provider,
            character_ref_bindings=character_ref_bindings,
            style_reference_frame=style_reference_frame,
        )

        if self.review_mode == "hybrid_judge" and image_review_risk.get("needs_review"):
            bundle_dir = self._export_image_review_bundle(
                shot=shot,
                image_path=image_path,
                image_provider=image_provider,
                scene_image=scene_image,
                style_reference_frame=style_reference_frame,
                character_ref_bindings=character_ref_bindings,
                risk=image_review_risk,
            )
            judge_result_path = bundle_dir / "image_judge_result.json"
            result = {
                "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                "image_review": {
                    "status": "pending_judgment",
                    "bundle_dir": str(bundle_dir),
                    "risk_summary": image_review_risk,
                },
            }
            if judge_result_path.exists():
                with open(judge_result_path, encoding="utf-8") as f:
                    judge_result = json.load(f)
                overall_action = str(judge_result.get("overall_action", "keep")).strip() or "keep"
                result["image_review"] = {
                    "status": "judged",
                    "bundle_dir": str(bundle_dir),
                    "risk_summary": image_review_risk,
                    "judge_result": judge_result,
                }
                if overall_action != "keep":
                    result["image_review"]["status"] = "pending_judgment"
                else:
                    logger.info(f"shot_{shot_id}: 图片视觉判断通过，继续生成视频")
            else:
                logger.info(f"shot_{shot_id}: 图片风险 bundle 已导出，等待视觉判断 → {bundle_dir}")

            if result["image_review"]["status"] == "pending_judgment":
                return result

        # ── 导演审图模式（director_review）──
        if self.review_mode == "director_review":
            review_cfg = self._review_config()
            max_retries = review_cfg.get("max_retries", 3)
            retry_delay = review_cfg.get("retry_delay", 2)

            # 审图重试循环
            for attempt in range(max_retries):
                # 导出审图所需的上下文文件，等待母模型审图
                audit_dir = self.output_root / "image_audit" / f"shot_{shot_id}"
                audit_dir.mkdir(parents=True, exist_ok=True)

                # 写入审图上下文
                review_context = {
                    "shot_id": shot_id,
                    "shot": shot,
                    "director_entry": director_entry,
                    "scene_context": {
                        "scene_id": shot.get("scene_id", ""),
                        "is_first_in_scene": shot.get("is_first_in_scene", False),
                        "is_last_in_scene": shot.get("is_last_in_scene", False),
                    },
                    "image_path": str(image_path),
                    "image_provider": image_provider,
                    "attempt": attempt + 1,
                    "max_attempts": max_retries,
                }
                context_path = audit_dir / "review_context.json"
                write_json(context_path, review_context)

                # 复制图片到审图目录
                audit_image_path = audit_dir / "generated_image.png"
                if image_path.exists():
                    shutil.copy(image_path, audit_image_path)

                # 检查是否有母模型的审图结果
                judge_result_path = audit_dir / "director_judge_result.json"

                # 如果是重试，先删除旧的审图结果
                if attempt > 0 and judge_result_path.exists():
                    judge_result_path.unlink()

                result = {
                    "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                    "image_review": {
                        "status": "pending_judgment",
                        "audit_dir": str(audit_dir),
                        "context_path": str(context_path),
                        "attempt": attempt + 1,
                        "max_attempts": max_retries,
                    },
                }

                # 等待母模型审图
                if judge_result_path.exists():
                    with open(judge_result_path, encoding="utf-8") as f:
                        judge_result = json.load(f)
                    overall_action = str(judge_result.get("overall_action", "keep")).strip().lower() or "keep"

                    if overall_action == "keep":
                        result["image_review"]["status"] = "approved"
                        result["image_review"]["judge_result"] = judge_result
                        logger.info(f"shot_{shot_id}: 导演审图通过（尝试 {attempt + 1}/{max_retries}）")
                        break  # 通过，退出重试循环
                    else:
                        reason = judge_result.get("reason", "")
                        adjustment_prompt = judge_result.get("adjustment_prompt", "")
                        logger.warning(f"shot_{shot_id}: 导演审图不通过（尝试 {attempt + 1}/{max_retries}）")
                        logger.warning(f"  原因: {reason}")

                        if attempt < max_retries - 1 and adjustment_prompt:
                            # 使用调整建议重新生成
                            logger.info(f"  使用调整建议重新生成: {adjustment_prompt}")
                            revised_prompt = f"{image_prompt}\n\n调整要求: {adjustment_prompt}"
                            async with self.sem:
                                image_path, image_provider = await self._generate_image(
                                    revised_prompt, shot_id,
                                    characters_in_shot=characters_in_shot,
                                    props_in_shot=props_in_shot,
                                    scene_image=scene_image,
                                    style_reference_image=style_reference_frame,
                                )
                            await asyncio.sleep(retry_delay)
                            continue  # 继续重试循环
                        else:
                            # 达到最大重试次数或无调整建议
                            result["image_review"]["status"] = "regenerate"
                            result["image_review"]["judge_result"] = judge_result
                            if attempt >= max_retries - 1:
                                logger.warning(f"shot_{shot_id}: 达到最大重试次数，返回 regenerate")
                            else:
                                logger.warning(f"shot_{shot_id}: 无调整建议，返回 regenerate")
                            return result
                else:
                    # 没有审图结果，等待母模型审图
                    logger.info(f"shot_{shot_id}: 等待母模型审图 → {audit_dir}")
                    result["image_review"]["status"] = "pending_judgment"
                    return result

        keyframe_results: list[dict[str, Any]] = []
        if shot_type == "offscreen_reaction":
            logger.info(f"shot_{shot_id}: shot_type=offscreen_reaction，禁用中间实体 reveal keyframes")
        should_generate_end_frame = (
            continuity_mode == "strict"
            or (continuity_mode == "scene_end" and is_last_in_scene)
        )
        if shot_type != "offscreen_reaction" and continuity_mode != "free" and self._any_video_provider_supports_stage_references():
            director_keyframes = director_entry.get("keyframes", [])
            if not isinstance(director_keyframes, list):
                director_keyframes = []
            keyframes = director_keyframes or resolved_contract.get("keyframes", [])
            max_refs = self._max_video_reference_images()
            available_slots = max_refs - 1  # first_frame
            if should_generate_end_frame:
                available_slots -= 1  # last_frame
            available_slots = max(0, available_slots)
            if len(keyframes) > available_slots:
                logger.info(
                    f"shot_{shot_id}: keyframes {len(keyframes)} 超出参考图限制，"
                    f"仅保留前 {available_slots} 个"
                )
            for idx, keyframe in enumerate(keyframes[:available_slots]):
                if not isinstance(keyframe, dict):
                    continue
                keyframe_description = str(
                    keyframe.get("prompt")
                    or keyframe.get("description", "")
                ).strip()
                if not keyframe_description:
                    continue
                keyframe_timestamp = float(keyframe.get("timestamp", 0))
                node_context = middle_nodes[idx] if idx < len(middle_nodes) else None
                keyframe_prompt = VideoPromptBuilder.build_image_prompt(
                    style_anchor=style_anchor,
                    character_appearances=char_appearances,
                    prop_appearances=prop_appearances,
                    scene_description=keyframe_description,
                    motion_control=motion_control,
                    camera_technical=camera_tech,
                    atmosphere=atmosphere,
                    physics=physics,
                    consistency_anchors=consistency_anchors,
                    scene_environment=scene_context.get("environment_description", ""),
                    scene_lighting=scene_context.get("lighting", ""),
                    scene_weather=scene_context.get("weather", ""),
                    scene_props=scene_context.get("active_props", scene_context.get("props")),
                    scene_continuity=scene_continuity,
                    subject_constraints=subject_constraints,
                    shot_delta=shot_delta,
                    shot_type=shot_type,
                    director_plan=director_plan,
                    node_context=node_context,
                )
                timestamp_slug = str(keyframe_timestamp).replace(".", "_")
                keyframe_out = self.image_dir / f"shot_{shot_id:03d}_keyframe_{idx + 1}_{timestamp_slug}s.png"

                # 检查 video_only 模式
                if self.video_only and keyframe_out.exists():
                    keyframe_path = keyframe_out
                    keyframe_provider = "existing"
                    logger.info(f"shot_{shot_id}: video_only 模式，使用已存在的 keyframe: {keyframe_out.name}")
                else:
                    async with self.sem:
                        keyframe_path, keyframe_provider = await self._generate_image(
                            keyframe_prompt,
                            shot_id,
                            output_path=keyframe_out,
                            characters_in_shot=characters_in_shot,
                            props_in_shot=props_in_shot,
                            scene_image=scene_image,
                            style_reference_image=style_reference_frame,
                        )
                keyframe_results.append(
                    {
                        "index": idx + 1,
                        "timestamp": keyframe_timestamp,
                        "stage": str(keyframe.get("stage") or keyframe.get("goal", "")).strip(),
                        "description": keyframe_description,
                        "path": str(keyframe_path),
                        "provider": keyframe_provider,
                    }
                )
            if keyframe_results:
                logger.info(f"shot_{shot_id}: 已生成 {len(keyframe_results)} 张中间关键帧参考图")

        # ── 尾帧图（根据 continuity_mode 决定是否生成） ──
        # strict: 强制生成尾帧图（LLM 判断为关键镜头）
        # scene_end: 仅 scene 末尾 shot 生成（默认行为）
        # free: 不生成尾帧图，Seedance 自由运动
        end_frame_path: Path | None = None
        end_frame_provider = ""
        if should_generate_end_frame and last_frame_text:
            end_prompt = VideoPromptBuilder.build_image_prompt(
                style_anchor=style_anchor,
                character_appearances=char_appearances,
                prop_appearances=prop_appearances,
                scene_description=last_frame_text,  # 全局提取的尾帧视觉描述
                motion_control=motion_control,
                camera_technical=camera_tech,
                atmosphere=atmosphere,
                physics=physics,
                consistency_anchors=consistency_anchors,
                scene_environment=scene_context.get("environment_description", ""),
                scene_lighting=scene_context.get("lighting", ""),
                scene_weather=scene_context.get("weather", ""),
                scene_props=scene_context.get("active_props", scene_context.get("props")),
                scene_continuity=scene_continuity,
                subject_constraints=subject_constraints,
                shot_delta=shot_delta,
                shot_type=shot_type,
                director_plan=director_plan,
                node_context=last_node,
            )
            end_out = self.image_dir / f"shot_{shot_id:03d}_end.png"

            # 检查 video_only 模式
            if self.video_only and end_out.exists():
                end_frame_path = end_out
                end_frame_provider = "existing"
                logger.info(f"shot_{shot_id}: video_only 模式，使用已存在的尾帧: {end_out.name}")
            else:
                async with self.sem:
                    end_frame_path, end_frame_provider = await self._generate_image(
                        end_prompt, shot_id, output_path=end_out,
                        characters_in_shot=characters_in_shot,
                        props_in_shot=props_in_shot,
                        scene_image=scene_image,
                        style_reference_image=style_reference_frame,
                    )
            if continuity_mode == "strict":
                logger.info(f"shot_{shot_id}: continuity_mode=strict，生成尾帧图作为终点锚")
            else:
                logger.info(f"shot_{shot_id}: scene 末尾，生成尾帧图作为终点锚")
        elif continuity_mode == "free":
            logger.info(f"shot_{shot_id}: continuity_mode=free，跳过尾帧图生成，Seedance 自由运动")
        elif not is_last_in_scene:
            logger.info(f"shot_{shot_id}: 非 scene 末尾（continuity_mode=scene_end），跳过尾帧图生成")

        # ── 构建结构化视频 prompt（使用全局提取的 video_action_prompt） ──
        video_prompt = VideoPromptBuilder.build_video_prompt(
            style_anchor=style_anchor,
            character_appearances=char_appearances,
            action_description=video_action_text,
            shot_intent=str(shot.get("narrative_segment", "")).strip(),
            opening_state=first_frame_text,
            target_outcome=last_frame_text,
            time_beats=shot.get("time_beats", []),
            motion_control=motion_control,
            camera_movement=camera_move,
            consistency_anchors=consistency_anchors,
            narration=narration,
            scene_environment=scene_context.get("environment_description", ""),
            scene_continuity=scene_continuity,
            subject_constraints=subject_constraints,
            shot_delta=shot_delta,
            shot_type=shot_type,
            director_plan=director_plan,
        )
        hard_constraints = self._collect_hard_constraint_summary(shot)
        video_references = self._build_video_references(
            shot,
            first_frame_path=image_path,
            scene_image=scene_image,
            style_reference_frame=style_reference_frame,
            end_frame_path=end_frame_path,
            character_ref_bindings=character_ref_bindings,
            prop_ref_bindings=prop_ref_bindings,
            keyframe_results=keyframe_results,
        )
        final_video_prompt = self._compose_prompt_with_reference_mentions(video_prompt, video_references)
        video_prompt_path = self._export_video_prompt_artifact(shot_id, final_video_prompt)
        serialized_video_references = self._serialize_video_references(video_references)
        if self._needs_reference_validation(shot, hard_constraints):
            bundle_dir = self._export_reference_review_bundle(
                shot=shot,
                references=video_references,
                hard_constraints=hard_constraints,
                video_prompt_text=final_video_prompt,
            )
            judge_result_path = bundle_dir / "reference_judge_result.json"
            if judge_result_path.exists():
                with open(judge_result_path, encoding="utf-8") as f:
                    judge_result = json.load(f)
                overall_action = str(judge_result.get("overall_action", "keep")).strip() or "keep"
                if overall_action != "keep":
                    return {
                        "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                        "video_prompt": {"shot_id": shot_id, "path": str(video_prompt_path)},
                        "video_references": serialized_video_references,
                        "keyframes": keyframe_results,
                        "end_frame": (
                            {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}
                            if end_frame_path and end_frame_path.exists() else None
                        ),
                        "reference_review": {
                            "status": "pending_judgment",
                            "bundle_dir": str(bundle_dir),
                            "judge_result": judge_result,
                            "hard_constraints": hard_constraints,
                        },
                    }
                logger.info(f"shot_{shot_id}: 参考图前置验证通过，允许继续生成视频")
            else:
                logger.info(f"shot_{shot_id}: 参考图前置验证待判断 → {bundle_dir}")
                return {
                    "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                    "video_prompt": {"shot_id": shot_id, "path": str(video_prompt_path)},
                    "video_references": serialized_video_references,
                    "keyframes": keyframe_results,
                    "end_frame": (
                        {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}
                        if end_frame_path and end_frame_path.exists() else None
                    ),
                    "reference_review": {
                        "status": "pending_judgment",
                        "bundle_dir": str(bundle_dir),
                        "hard_constraints": hard_constraints,
                    },
                }

        # ── Seedance I2V：首帧 + 尾帧 + prompt → 视频片段 ──
        result = {
            "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
            "video_prompt": {"shot_id": shot_id, "path": str(video_prompt_path)},
            "video_references": serialized_video_references,
        }
        if keyframe_results:
            result["keyframes"] = keyframe_results

        if self.use_api and image_path.exists():
            video_path, video_provider = await self._generate_video(
                image_path, video_prompt, shot_id,
                estimated_duration=estimated_duration,
                last_frame_path=end_frame_path,
                video_references=video_references,
            )
            if video_path:
                result["video"] = {
                    "shot_id": shot_id,
                    "path": str(video_path),
                    "provider": video_provider,
                }

        # 返回尾帧路径，供下一镜头复用
        if end_frame_path and end_frame_path.exists():
            result["end_frame"] = {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}

        return result

    # ── 视频生成（I2V） ────────────────────────────────────────────────

    async def _generate_video(
        self, image_path: Path, prompt: str, shot_id: int,
        estimated_duration: int = 10,
        last_frame_path: Path | None = None,
        video_references: list[dict[str, Any]] | None = None,
    ) -> tuple[Path | None, str]:
        """图生视频：dispatch + fallback_chain 模式。"""
        out = self.video_dir / f"shot_{shot_id:03d}.mp4"
        video_cfg = get_model_config("video")

        # ── Backward compat: 旧扁平格式 → 新嵌套格式 ──
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            old_provider = video_cfg["provider"]
            fallback_chain = [old_provider]
            max_retries = 1
            retry_delay = 5
        else:
            fallback_chain = video_cfg.get("fallback_chain", ["byteplus"])
            max_retries = video_cfg.get("max_retries", 2)
            retry_delay = video_cfg.get("retry_delay", 5)

        dispatch = {
            "volcengine_seedance2": lambda img, p, o, dur, lf, refs: self._video_seedance(
                "volcengine_seedance2", img, p, o, duration=dur, last_frame_path=lf, video_references=refs
            ),
            "byteplus": lambda img, p, o, dur, lf, refs: self._video_seedance(
                "byteplus", img, p, o, duration=dur, last_frame_path=lf, video_references=refs
            ),
        }

        for provider in fallback_chain:
            fn = dispatch.get(provider)
            if not fn:
                logger.warning(f"shot_{shot_id}: 未知视频 provider '{provider}'，跳过")
                continue

            # 读取 per-provider 配置（兼容旧格式）
            if "provider" in video_cfg and "fallback_chain" not in video_cfg:
                pcfg = video_cfg
            else:
                pcfg = video_cfg.get(provider, {})

            min_dur = pcfg.get("min_duration", 5)
            max_dur = pcfg.get("max_duration", 12)
            clamped = max(min_dur, min(max_dur, estimated_duration))
            logger.info(f"shot_{shot_id}: estimated_duration={estimated_duration}s → clamped={clamped}s (provider={provider})")

            provider_references = list(video_references or [])
            if not provider_references:
                provider_references = [{"path": image_path, "usage": "first_frame", "source_type": "frame"}]
                if last_frame_path and last_frame_path.exists():
                    provider_references.append(
                        {
                            "path": last_frame_path,
                            "usage": "reference_target_state",
                            "source_type": "frame",
                        }
                    )

            max_refs = int(pcfg.get("max_reference_images", 2))
            if max_refs > 0 and len(provider_references) > max_refs:
                ranked = sorted(
                    enumerate(provider_references),
                    key=lambda pair: (self._video_reference_priority(pair[1]), pair[0]),
                )
                keep_indices = {idx for idx, _ in ranked[:max_refs]}
                provider_references = [
                    ref for idx, ref in enumerate(provider_references) if idx in keep_indices
                ]
                kept_usages = [self._normalize_video_reference_usage(ref.get("usage")) for ref in provider_references]
                logger.info(
                    f"shot_{shot_id}: {provider} 参考图超限，按用途优先级保留 {len(provider_references)} 张"
                    f" ({', '.join(kept_usages)})"
                )

            for attempt in range(max_retries):
                if await fn(image_path, prompt, out, clamped, last_frame_path, provider_references):
                    return out, provider
                if attempt < max_retries - 1:
                    logger.warning(f"shot_{shot_id}: {provider} 第 {attempt+1} 次失败，{retry_delay}s 后重试")
                    await asyncio.sleep(retry_delay)
            logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        logger.warning(f"视频生成失败 shot_{shot_id}，将使用静态图 fallback")
        return None, "none"

    async def _video_seedance(
        self,
        provider_name: str,
        image_path: Path,
        prompt: str,
        output: Path,
        duration: int = 10,
        last_frame_path: Path | None = None,
        video_references: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Ark Seedance I2V（图生视频），支持 per-provider 配置。"""
        video_cfg = get_model_config("video")
        if provider_name in video_cfg and isinstance(video_cfg[provider_name], dict):
            pcfg = video_cfg[provider_name]
        else:
            pcfg = video_cfg

        api_provider = str(pcfg.get("provider", "byteplus")).strip() or "byteplus"
        creds = get_api_credentials(api_provider, self.cfg)
        if not creds.get("api_key"):
            logger.warning(f"{api_provider} api_key 未配置，跳过 Seedance")
            return False

        try:
            api_base = creds["api_base"]
            api_key = creds["api_key"]

            raw_model = pcfg.get("model", "seedance-1-5-pro-251215")
            batch_mode = raw_model.endswith("-batch")
            model = raw_model.removesuffix("-batch") if batch_mode else raw_model
            if batch_mode:
                logger.info(f"Seedance batch mode: {raw_model} → {model}")

            refs = list(video_references or [])
            if not refs:
                refs = [{"path": image_path, "usage": "first_frame", "source_type": "frame"}]
                if last_frame_path and last_frame_path.exists():
                    refs.append(
                        {
                            "path": last_frame_path,
                            "usage": "reference_target_state",
                            "source_type": "frame",
                        }
                    )

            final_prompt = prompt

            content: list[dict[str, Any]] = []
            ordered_refs: list[dict[str, Any]] = []
            for ref in refs:
                ref_path = ref.get("path")
                if not isinstance(ref_path, Path) or not ref_path.exists():
                    continue
                media_type = str(ref.get("media_type", "image")).strip() or "image"
                if media_type != "image":
                    logger.info(f"Seedance: 当前执行层仅发送图片参考，跳过 {media_type} 参考素材")
                    continue
                mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                data_url = f"data:{mime};base64,{img_b64}"
                item: dict[str, Any] = {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                    "role": "reference_image",
                }
                content.append(item)
                ordered_refs.append(ref)
            if ordered_refs:
                prompt_ready_refs: list[dict[str, Any]] = []
                for idx, ref in enumerate(ordered_refs, start=1):
                    prompt_ref = dict(ref)
                    prompt_ref["mention"] = f"@图片{idx}"
                    prompt_ready_refs.append(prompt_ref)
                final_prompt = VideoPromptBuilder.compose_video_generation_prompt(final_prompt, prompt_ready_refs)
            content.insert(0, {"type": "text", "text": final_prompt})
            usages = [self._normalize_video_reference_usage(ref.get("usage")) for ref in ordered_refs]
            logger.info(f"Seedance: 使用 {len(usages)} 张参考图 ({', '.join(usages)})")

            payload = {
                "model": model,
                "content": content,
                "duration": duration,
                "ratio": pcfg.get("ratio", "16:9"),
                "resolution": pcfg.get("resolution", "720p"),
                "generate_audio": pcfg.get("generate_audio", True),
                "watermark": pcfg.get("watermark", False),
            }
            if batch_mode:
                payload["service_tier"] = "flex"
                payload["execution_expires_after"] = 86400

            submit_timeout = pcfg.get("submit_timeout", 30)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=submit_timeout)) as session:
                async with session.post(
                    f"{api_base}/contents/generations/tasks",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"Seedance 提交失败 ({resp.status}): {body[:200]}")
                        return False
                    data = await resp.json()
                    task_id = data.get("id")
                    if not task_id:
                        logger.warning(f"Seedance 返回无 task_id: {data}")
                        return False
                    mode_tag = " [batch]" if batch_mode else ""
                    logger.info(f"Seedance{mode_tag} 任务已提交: {task_id}")

            poll_timeout = pcfg.get("poll_timeout", 15)
            poll_interval = pcfg.get("poll_interval", 5)
            default_poll_max = 720 if batch_mode else 60
            poll_max = pcfg.get("poll_max_attempts", default_poll_max)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=poll_timeout)) as session:
                for attempt in range(poll_max):
                    await asyncio.sleep(poll_interval)
                    async with session.get(
                        f"{api_base}/contents/generations/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        result = await resp.json()
                        status = result.get("status", "")

                        if status == "succeeded":
                            video_url = (result.get("content") or {}).get("video_url")
                            if video_url:
                                logger.info(f"Seedance 生成成功，下载视频...")
                                # 用独立 session 下载视频，避免 poll_timeout 过短导致下载超时
                                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as dl_session:
                                    return await self._download(
                                        dl_session, video_url, output
                                    )
                            logger.warning("Seedance 成功但无 video_url")
                            return False

                        if status in ("failed", "cancelled"):
                            error = result.get("error")
                            logger.warning(f"Seedance 任务 {status}: {error}")
                            return False

                        # running / pending — 继续等
                        if attempt % 6 == 0:
                            elapsed = attempt * poll_interval
                            logger.info(f"Seedance{mode_tag} 生成中... ({elapsed}s)")

            logger.warning(f"Seedance 超时（{poll_max * poll_interval}s）")
            return False

        except Exception as e:
            logger.warning(f"Seedance 异常: {e}")
            return False

    async def _generate_image(
        self,
        prompt: str,
        shot_id: int,
        output_path: Path | None = None,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> tuple[Path, str]:
        """生成图片。同一模型重试最多 3 次，不切换到其他风格不同的模型。"""
        out = output_path or (self.image_dir / f"shot_{shot_id:03d}.png")
        img_cfg = get_model_config("image")
        max_retries = img_cfg.get("max_retries", 3)
        retry_delay = img_cfg.get("retry_delay", 5)
        fallback_chain = img_cfg.get("fallback_chain", ["apimart", "volcengine", "fal"])

        if self.use_api:
            dispatch = {
                "volcengine": lambda p, o: self._image_volcengine(
                    p,
                    o,
                    characters_in_shot=characters_in_shot or [],
                    props_in_shot=props_in_shot or [],
                    scene_image=scene_image,
                    style_reference_image=style_reference_image,
                ),
                "apimart": lambda p, o: self._image_apimart(
                    p,
                    o,
                    characters_in_shot=characters_in_shot or [],
                    props_in_shot=props_in_shot or [],
                    scene_image=scene_image,
                    style_reference_image=style_reference_image,
                ),
                "fal": lambda p, o: self._image_flux(p, o),
            }
            for provider in fallback_chain:
                fn = dispatch.get(provider)
                if not fn:
                    continue
                # 同一 provider 重试多次
                for attempt in range(max_retries):
                    if await fn(prompt, out):
                        return out, provider
                    if attempt < max_retries - 1:
                        logger.warning(f"shot_{shot_id}: {provider} 第 {attempt+1} 次失败，{retry_delay}s 后重试")
                        await asyncio.sleep(retry_delay)
                logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        self._placeholder_image(out, shot_id, prompt)
        return out, "placeholder"

    async def _image_volcengine(
        self,
        prompt: str,
        output: Path,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> bool:
        """Volcengine Seedream 图像生成（支持角色参考图）。"""
        img_cfg = get_model_config("image").get("volcengine", {})
        creds = get_api_credentials("volcengine", self.cfg)
        if not creds.get("api_key"):
            return False

        # 构建参考图 image_urls（角色参考图 + 道具参考图 + 场景参考图）
        image_urls: list[str] = []
        characters_cfg = self.storyboard.get("characters", {})
        ref_dir = self.storyboard.get("character_ref_dir", "")
        for char_id in (characters_in_shot or []):
            char_info = characters_cfg.get(char_id, {})
            ref_file = char_info.get("ref_image", "")
            if ref_file and ref_dir:
                ref_path = Path(ref_dir) / ref_file
                if ref_path.exists():
                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                    image_urls.append(f"data:{mime};base64,{img_b64}")

        prop_cfg = self.storyboard.get("prop_refs", {})
        if isinstance(prop_cfg, dict):
            for prop_id in (props_in_shot or []):
                prop_info = prop_cfg.get(prop_id, {})
                if not isinstance(prop_info, dict):
                    continue
                ref_path_value = str(prop_info.get("ref_path", "")).strip()
                ref_path = Path(ref_path_value).expanduser() if ref_path_value else None
                if ref_path and ref_path.exists():
                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                    image_urls.append(f"data:{mime};base64,{img_b64}")

        if scene_image and scene_image.exists():
            mime = mimetypes.guess_type(str(scene_image))[0] or "image/png"
            img_b64 = base64.b64encode(scene_image.read_bytes()).decode()
            image_urls.append(f"data:{mime};base64,{img_b64}")

        if style_reference_image and style_reference_image.exists():
            mime = mimetypes.guess_type(str(style_reference_image))[0] or "image/png"
            img_b64 = base64.b64encode(style_reference_image.read_bytes()).decode()
            image_urls.append(f"data:{mime};base64,{img_b64}")

        try:
            timeout = img_cfg.get("timeout", 120)
            payload = {
                "model": img_cfg.get("model", "doubao-seedream-4-0-250828"),
                "prompt": prompt,
                "size": img_cfg.get("size", "2K"),
                "response_format": img_cfg.get("response_format", "url"),
                "watermark": img_cfg.get("watermark", True),
            }
            if image_urls:
                payload["image_urls"] = image_urls

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/images/generations",
                    headers={"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    url = data.get("data", [{}])[0].get("url")
                    if url:
                        return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"volcengine 失败: {e}")
        return False

    async def _image_apimart(
        self,
        prompt: str,
        output: Path,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> bool:
        """ApiMart 图像生成 — Gemini 模型走 chat/completions，其他走 images/generations。"""
        img_cfg = get_model_config("image").get("apimart", {})
        creds = get_api_credentials("apimart", self.cfg)
        if not creds.get("api_key"):
            return False

        model = img_cfg.get("model", "gemini-3.1-flash-image-preview")
        is_gemini = "gemini" in model.lower()

        try:
            timeout = img_cfg.get("timeout", 180)
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                if is_gemini:
                    # 构建多模态 content：场景参考图 + 角色参考图 + 道具参考图 + 结构化 prompt
                    user_content: list[dict] | str = []
                    characters_cfg = self.storyboard.get("characters", {})
                    prop_cfg = self.storyboard.get("prop_refs", {})
                    chars_to_use = characters_in_shot or []
                    ref_dir = self.storyboard.get("character_ref_dir", "")

                    has_refs = False
                    if (chars_to_use and ref_dir) or scene_image or style_reference_image:
                        user_content = []

                        # 场景参考图（纯环境，同场景共享的视觉基底）
                        if scene_image and scene_image.exists():
                            mime = mimetypes.guess_type(str(scene_image))[0] or "image/png"
                            img_b64 = base64.b64encode(scene_image.read_bytes()).decode()
                            user_content.append({"type": "text", "text": "场景参考图——这是当前镜头的环境与背景基底。请把人物放入这张场景中，保持建筑、地形、光线、天气和空间关系一致。"})
                            user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                            has_refs = True

                        if style_reference_image and style_reference_image.exists():
                            mime = mimetypes.guess_type(str(style_reference_image))[0] or "image/png"
                            img_b64 = base64.b64encode(style_reference_image.read_bytes()).decode()
                            user_content.append({"type": "text", "text": "风格连续性参考图——保持同一种人物绘制媒介、面部身份、材质处理和整体视觉媒介，但不要直接复制它的构图和背景。"})
                            user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                            has_refs = True

                        # 角色参考图
                        if chars_to_use and ref_dir:
                            ref_base = Path(ref_dir)
                            for char_id in chars_to_use:
                                char_info = characters_cfg.get(char_id, {})
                                ref_file = char_info.get("ref_image", "")
                                ref_desc = char_info.get("ref_description", char_info.get("appearance", ""))
                                ref_path = ref_base / ref_file if ref_file else None

                                if ref_path and ref_path.exists():
                                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                                    user_content.append({"type": "text", "text": f"角色参考图——{char_id}: {ref_desc}"})
                                    user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                                    has_refs = True

                        if isinstance(prop_cfg, dict):
                            for prop_id in (props_in_shot or []):
                                prop_info = prop_cfg.get(prop_id, {})
                                if not isinstance(prop_info, dict):
                                    continue
                                ref_path_value = str(prop_info.get("ref_path", "")).strip()
                                ref_desc = str(prop_info.get("ref_description", "") or prop_info.get("appearance", "")).strip()
                                ref_path = Path(ref_path_value).expanduser() if ref_path_value else None
                                if ref_path and ref_path.exists():
                                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                                    user_content.append({"type": "text", "text": f"道具参考图——{prop_id}: {ref_desc}"})
                                    user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                                    has_refs = True

                        instruction = (
                            "现在生成一张新的电影感镜头图片。"
                        )
                        if scene_image and scene_image.exists():
                            instruction += (
                                "使用场景参考图作为环境基底，保持同样的建筑、地形、山道、光线与天气，把人物放入这张环境中。"
                            )
                        if style_reference_image and style_reference_image.exists():
                            instruction += (
                                "使用风格连续性参考图来保持同一种人物媒介、面部身份、材质处理和整体视觉感受；这张图只用于风格连续，不用于复制原构图和原背景。"
                            )
                        instruction += (
                            "关键要求："
                            "1）人物身份必须与各自参考图一致；"
                            "2）关键道具应与道具参考图一致；"
                            "3）环境与场景参考图保持连续；"
                            "4）不要出现任何文字、标签或注释；"
                            "5）基于下面的提示词生成新的镜头构图。\\n\\n"
                            f"{prompt}"
                        )
                        user_content.append({"type": "text", "text": instruction})

                        if not has_refs:
                            user_content = f"生成一张图片：{prompt}"
                    else:
                        user_content = f"生成一张图片：{prompt}"

                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": GEMINI_IMAGE_SYSTEM_PROMPT},
                            {"role": "user", "content": user_content}
                        ],
                        "max_tokens": 4096,
                        "stream": False,
                    }
                    async with session.post(
                        f"{creds['api_base']}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.warning(f"apimart gemini 返回 {resp.status}: {body[:200]}")
                            return False
                        data = await resp.json()
                        # 从 choices[0].message.content 中提取图片
                        choices = data.get("choices", [])
                        if not choices:
                            return False
                        msg = choices[0].get("message", {})
                        content = msg.get("content", "")

                        # content 可能是 str 或 list（multimodal）
                        if isinstance(content, str):
                            # Gemini 返回 markdown 格式: ![image](data:image/jpeg;base64,...)
                            import re
                            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)', content)
                            if m:
                                b64_str = m.group(1).replace('\n', '').replace(' ', '')
                                output.write_bytes(base64.b64decode(b64_str))
                                return True
                            # 也可能直接返回 URL
                            m_url = re.search(r'https?://\S+', content)
                            if m_url:
                                return await self._download(session, m_url.group(0), output)
                            logger.warning(f"apimart gemini 返回内容无图片: {content[:200]}")
                            return False
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict):
                                    if part.get("type") == "image_url":
                                        url_or_b64 = part.get("image_url", {}).get("url", "")
                                        if url_or_b64.startswith("data:"):
                                            b64_str = url_or_b64.split(",", 1)[1]
                                            output.write_bytes(base64.b64decode(b64_str))
                                            return True
                                        elif url_or_b64.startswith("http"):
                                            return await self._download(session, url_or_b64, output)
                            return False
                        return False
                else:
                    # 非 Gemini 模型走标准 images/generations 接口
                    poll_interval = img_cfg.get("poll_interval", 5)
                    poll_max = img_cfg.get("poll_max_attempts", 30)
                    async with session.post(
                        f"{creds['api_base']}/images/generations",
                        headers=headers,
                        json={"model": model, "prompt": prompt, "size": f"{self.image_width}x{self.image_height}", "n": 1},
                    ) as resp:
                        if resp.status != 200:
                            return False
                        data = await resp.json()
                        first = data.get("data", [{}])[0]

                        # 异步任务模式
                        if task_id := first.get("task_id"):
                            for _ in range(poll_max):
                                await asyncio.sleep(poll_interval)
                                async with session.get(f"{creds['api_base']}/tasks/{task_id}", headers=headers) as poll:
                                    if poll.status == 200:
                                        task_data = await poll.json()
                                        if task_data.get("data", {}).get("status") == "completed":
                                            url_val = task_data.get("data", {}).get("result", {}).get("images", [{}])[0].get("url")
                                            if isinstance(url_val, list):
                                                url_val = url_val[0] if url_val else None
                                            if url_val:
                                                return await self._download(session, url_val, output)
                            return False

                        # 同步模式
                        url = first.get("url")
                        if isinstance(url, list):
                            url = url[0] if url else None
                        if url:
                            return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"apimart 失败: {e}")
        return False

    # ─── Video generation (Seedance I2V) ───────────────────────────────

    # NOTE: 第二组 _generate_video / _video_seedance 已废弃，
    # 使用上方的版本（支持 estimated_duration 动态映射）。

    # ─── Image generation (Flux via fal.ai) ──────────────────────────

    async def _image_flux(self, prompt: str, output: Path) -> bool:
        """Fal.ai Flux 图像生成。"""
        img_cfg = get_model_config("image").get("fal", {})
        creds = get_api_credentials("fal", self.cfg)
        if not creds.get("api_key"):
            return False

        timeout = img_cfg.get("timeout", 180)
        poll_interval = img_cfg.get("poll_interval", 1)
        poll_max = img_cfg.get("poll_max_attempts", 90)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/fal-ai/flux/schnell",
                    headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                    json={
                        "prompt": prompt,
                        "image_size": {"width": self.image_width, "height": self.image_height},
                        "num_inference_steps": img_cfg.get("num_inference_steps", 4),
                        "num_images": img_cfg.get("num_images", 1),
                    },
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()

                    # 直接返回
                    if data.get("images"):
                        url = data["images"][0].get("url")
                        if url:
                            return await self._download(session, url, output)

                    # 轮询模式
                    if request_id := data.get("request_id"):
                        for _ in range(poll_max):
                            await asyncio.sleep(poll_interval)
                            async with session.get(f"{creds['api_base']}/fal-ai/flux/schnell/requests/{request_id}", headers={"Authorization": f"Key {creds['api_key']}"}) as poll:
                                if poll.status == 200:
                                    result = await poll.json()
                                    if result.get("images"):
                                        url = result["images"][0].get("url")
                                        if url:
                                            return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"flux 失败: {e}")
        return False

    async def _generate_tts(self, text: str, shot_id: int) -> tuple[Path, str]:
        """生成语音：Edge TTS → 静音。"""
        output = self.voice_dir / f"shot_{shot_id:03d}.wav"
        text = text.strip() or "请关注"

        if self.use_api and await self._tts_edge(text, output):
            return output, "edge-tts"

        self._silence_wav(output, 2)
        return output, "silence"

    async def _tts_edge(self, text: str, output: Path) -> bool:
        """Edge TTS 生成语音。"""
        try:
            import edge_tts

            tts_cfg = get_model_config("tts")
            voice = os.getenv("EDGE_TTS_VOICE", tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural"))
            await edge_tts.Communicate(text=text, voice=voice).save(str(output))
            return output.exists() and output.stat().st_size > 0
        except Exception as e:
            logger.warning(f"edge-tts 失败: {e}")
            return False

    async def _generate_bgm(self, style: str, duration: int) -> tuple[Path, str]:
        """生成 BGM：MiniMax → 静音。"""
        output = self.bgm_dir / "bgm.wav"

        if self.use_api and await self._bgm_minimax(style, duration, output):
            return output, "minimax"

        self._silence_wav(output, duration)
        return output, "silence"

    async def _bgm_minimax(self, style: str, duration: int, output: Path) -> bool:
        """MiniMax BGM 生成（via fal.ai）。"""
        bgm_cfg = get_model_config("bgm")
        creds = get_api_credentials(bgm_cfg.get("provider", "fal"), self.cfg)
        if not creds.get("api_key"):
            return False

        timeout = bgm_cfg.get("timeout", 240)
        poll_interval = bgm_cfg.get("poll_interval", 1)
        poll_max = bgm_cfg.get("poll_max_attempts", 120)
        endpoint = bgm_cfg.get("endpoint", "/fal-ai/minimax-music/v2")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}{endpoint}",
                    headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                    json={"prompt": f"{style}, instrumental, no vocals", "lyrics_prompt": "[Instrumental]", "duration": duration},
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()

                    # 直接返回
                    if audio := data.get("audio", {}).get("url"):
                        return await self._download(session, audio, output)

                    # 轮询
                    poll_endpoint = endpoint.replace("/v2", "")
                    if request_id := data.get("request_id"):
                        for _ in range(poll_max):
                            await asyncio.sleep(poll_interval)
                            async with session.get(f"{creds['api_base']}{poll_endpoint}/requests/{request_id}", headers={"Authorization": f"Key {creds['api_key']}"}) as poll:
                                if poll.status == 200:
                                    result = await poll.json()
                                    if audio := result.get("audio", {}).get("url"):
                                        return await self._download(session, audio, output)
        except Exception as e:
            logger.warning(f"minimax 失败: {e}")
        return False

    async def _download(self, session: aiohttp.ClientSession, url: str, output: Path) -> bool:
        """下载文件。"""
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    output.write_bytes(await resp.read())
                    return output.exists() and output.stat().st_size > 0
        except Exception as e:
            logger.warning(f"下载失败: {e}")
        return False

    def _placeholder_image(self, output: Path, shot_id: int, prompt: str) -> None:
        """生成占位图。"""
        img = Image.new("RGB", (self.image_width, self.image_height), (21, 27, 38))
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 16, self.image_width - 16, self.image_height - 16), outline=(255, 180, 0), width=4)
        draw.multiline_text((40, 50), f"SHOT {shot_id}\n{prompt[:120]}", fill=(240, 240, 240))
        img.save(output)

    def _silence_wav(self, path: Path, seconds: int) -> None:
        """生成静音 WAV。"""
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(b"\x00\x00" * (16000 * seconds))


# ── 角色参考图生成 ──────────────────────────────────────────────

class CharacterRefGenerator:
    """从 framework.json 生成角色参考图。"""

    def __init__(
        self,
        framework: dict[str, Any],
        output_dir: Path,
        use_api: bool = True,
    ):
        self.framework = framework
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.use_api = use_api
        self.cfg = load_external_api_config()

    @staticmethod
    def _sanitize_ref_text(text: str) -> str:
        """去掉容易把角色参考图带偏成剧情插画的状态/场景描述。"""
        if not text:
            return ""
        cleaned = str(text)

        # 删除明显的从句连接，避免把动作状态整段带入角色定妆图
        cleaned = re.sub(r"\b(while|with|as)\b[^.]*", "", cleaned, flags=re.IGNORECASE)

        # 删除高风险剧情/场景词
        forbidden_patterns = [
            r"\bleaning[^,.]*",
            r"\braising[^,.]*",
            r"\bholding[^,.]*umbrella[^,.]*",
            r"\bagainst a cliff wall\b",
            r"\bcliff wall\b",
            r"\bumbrella\b",
            r"\bsword\b",
            r"\bkatana\b",
            r"\bweapon\b",
            r"\bprotective\b",
        ]
        for pattern in forbidden_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
        cleaned = re.sub(r"([,.;]){2,}", r"\1", cleaned)
        return cleaned.strip(" ,.;")

    @staticmethod
    def _build_character_medium_lock(style_anchor: str) -> str:
        """角色参考图使用更硬的媒介锁，避免真人/插画混漂。"""
        anchor = str(style_anchor or "").lower()
        if any(token in anchor for token in ["anime", "插画", "漫画", "painter", "illustrat", "绘画"]):
            return (
                "Use a single consistent illustrated cinematic medium for this character reference. "
                "Do not drift into photorealistic live-action or 3D rendering."
            )
        if any(token in anchor for token in ["3d", "cg", "cgi", "render", "渲染"]):
            return (
                "Use a single consistent stylized 3D cinematic medium for this character reference. "
                "Do not drift into hand-drawn illustration or photorealistic live-action."
            )
        if any(token in anchor for token in ["写意", "写意写实", "电影感", "cinematic", "古风电影", "东方影视"]):
            return (
                "Use a stylized cinematic medium that blends painterly aesthetics with realistic proportions. "
                "The character should look like a frame from a high-end Chinese period drama with subtle artistic stylization — "
                "NOT a raw photograph of a real person, NOT anime, NOT concept art. "
                "Skin texture, fabric weight, and lighting should feel grounded but with visible artistic intent in rendering."
            )
        return (
            "Use photorealistic live-action cinematic character rendering. "
            "All characters in this project must stay in the same real-human visual medium. "
            "Do not render as illustration, anime, concept art sheet, painting, or stylized game art."
        )

    def _build_ref_prompt(self, character: dict[str, Any]) -> str:
        """构建角色参考图 prompt。"""
        name = character.get("name", "Character")
        appearance = self._sanitize_ref_text(character.get("appearance", ""))
        clothing = self._sanitize_ref_text(character.get("default_clothing", ""))
        key_features = character.get("key_features", [])
        features_str = ", ".join(key_features) if key_features else ""
        style_anchor = self.framework.get("visual_style_anchor", "")
        medium_lock = self._build_character_medium_lock(style_anchor)

        prompt_parts = [
            f"Single-character cinematic reference portrait of {name}.",
            medium_lock,
        ]
        if style_anchor:
            prompt_parts.append(f"Project visual style anchor: {style_anchor}.")
        if appearance:
            prompt_parts.append(f"Stable character appearance: {appearance}.")
        if clothing:
            prompt_parts.append(f"Default clothing only: {clothing}.")
        if features_str:
            prompt_parts.append(f"Key identifying features: {features_str}.")

        prompt_parts.extend(
            [
                "Show one character only.",
                "Use a neutral light gray studio background with no environmental storytelling.",
                "Use a natural full-body standing pose facing camera, with a slight body turn allowed.",
                "No props, no weapons, no umbrella, no hat, no scene elements, no text labels, no annotations, no split-sheet layout.",
                "Sharp focus on facial structure, body proportions, fabric texture, and stable identity.",
            ]
        )
        return " ".join(prompt_parts)

    def _character_id(self, character: dict[str, Any]) -> str:
        """从角色信息生成 ID（英文小写下划线）。"""
        cid = character.get("id", "")
        if cid:
            return cid
        # fallback: 从 name 生成
        name = character.get("name", "char")
        return name.lower().replace(" ", "_").replace("-", "_")

    async def generate(self, character_id_filter: str | None = None) -> dict[str, Any]:
        """生成角色参考图。"""
        characters = self.framework.get("suggested_characters", [])
        if not characters:
            raise ValueError("framework.json 缺少 suggested_characters")

        results = {}
        for char in characters:
            cid = self._character_id(char)
            if character_id_filter and cid != character_id_filter:
                continue

            out_path = self.output_dir / f"ref_{cid}.png"
            prompt = self._build_ref_prompt(char)
            logger.info(f"生成角色参考图: {cid}")

            if self.use_api:
                success = await self._generate_ref_image(prompt, out_path)
                provider = "apimart" if success else "placeholder"
            else:
                success = False
                provider = "placeholder"

            if not success:
                # 生成占位图
                img = Image.new("RGB", (1024, 1024), (255, 255, 255))
                draw = ImageDraw.Draw(img)
                draw.rectangle((16, 16, 1008, 1008), outline=(200, 200, 200), width=2)
                draw.multiline_text(
                    (40, 50),
                    f"CHARACTER REF\n{cid}\n{char.get('name', '')}\n\n{prompt[:200]}",
                    fill=(100, 100, 100),
                )
                img.save(out_path)
                provider = "placeholder"

            results[cid] = {
                "ref_image": out_path.name,
                "path": str(out_path),
                "provider": provider,
                "ref_description": char.get("ref_description", ""),
            }
            logger.info(f"角色参考图完成: {cid} → {out_path} ({provider})")

        return {
            "character_ref_dir": str(self.output_dir),
            "characters": results,
        }

    async def _generate_ref_image(self, prompt: str, output: Path) -> bool:
        """调用图片 API 生成参考图。"""
        img_cfg = get_model_config("image")
        # 优先用 apimart（Gemini）
        apimart_cfg = img_cfg.get("apimart", {})
        creds = get_api_credentials("apimart", self.cfg)
        if not creds.get("api_key"):
            return False

        model = apimart_cfg.get("model", "gemini-3.1-flash-image-preview")
        is_gemini = "gemini" in model.lower()
        timeout = apimart_cfg.get("timeout", 180)
        headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                if is_gemini:
                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": f"Generate an image: {prompt}"}],
                        "max_tokens": 4096,
                        "stream": False,
                    }
                    async with session.post(
                        f"{creds['api_base']}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.warning(f"角色参考图生成失败 ({resp.status}): {body[:200]}")
                            return False
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if not choices:
                            return False
                        content = choices[0].get("message", {}).get("content", "")
                        if isinstance(content, str):
                            import re
                            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)', content)
                            if m:
                                b64_str = m.group(1).replace('\n', '').replace(' ', '')
                                output.write_bytes(base64.b64decode(b64_str))
                                return True
                        return False
                else:
                    async with session.post(
                        f"{creds['api_base']}/images/generations",
                        headers=headers,
                        json={"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
                    ) as resp:
                        if resp.status != 200:
                            return False
                        data = await resp.json()
                        url = data.get("data", [{}])[0].get("url")
                        if url:
                            async with session.get(url) as dl:
                                if dl.status == 200:
                                    output.write_bytes(await dl.read())
                                    return True
                        return False
        except Exception as e:
            logger.warning(f"角色参考图 API 异常: {e}")
            return False


# ── CLI 入口 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="素材生成（图片/视频/BGM/角色参考图）")
    parser.add_argument("--mode", choices=["assets", "character_refs"], default="assets",
                        help="运行模式：assets（默认，生成镜头素材）| character_refs（生成角色参考图）")
    parser.add_argument("--storyboard", help="storyboard.json 路径（assets 模式必需）")
    parser.add_argument("--framework", help="framework.json 路径（character_refs 模式必需）")
    parser.add_argument("--output_dir", required=True, help="素材输出目录")
    parser.add_argument("--character_id", help="只生成指定角色的参考图（character_refs 模式可选）")
    parser.add_argument("--image_width", type=int, default=1024, help="图片宽度")
    parser.add_argument("--image_height", type=int, default=1024, help="图片高度")
    parser.add_argument("--parallel", type=int, default=4, help="并行任务数")
    parser.add_argument("--review_mode", choices=sorted(REVIEW_MODES), help="质量审查模式：metrics_only | hybrid_judge")
    parser.add_argument("--video_only", action="store_true", help="调试模式：跳过图片生成，只生成视频（图片必须已存在）")
    parser.add_argument("--no_api", action="store_true", help="禁用外部 API，生成占位素材")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).expanduser().resolve()

    if args.mode == "character_refs":
        # ── 角色参考图模式 ──
        if not args.framework:
            raise SystemExit("--mode character_refs 需要 --framework 参数")
        framework_path = Path(args.framework).expanduser().resolve()
        with open(framework_path, encoding="utf-8") as f:
            framework = json.load(f)

        gen = CharacterRefGenerator(
            framework=framework,
            output_dir=output_dir,
            use_api=not args.no_api,
        )
        result = await gen.generate(character_id_filter=args.character_id)

        result_path = output_dir / "character_refs.json"
        write_json(result_path, result)
        print(json.dumps({"character_refs_json": str(result_path)}, ensure_ascii=False))

    else:
        # ── 素材生成模式（默认） ──
        if not args.storyboard:
            raise SystemExit("--mode assets 需要 --storyboard 参数")
        storyboard_path = Path(args.storyboard).expanduser().resolve()
        with open(storyboard_path, encoding="utf-8") as f:
            storyboard = json.load(f)

        generator = AssetGenerator(
            storyboard=storyboard,
            output_root=output_dir,
            image_width=args.image_width,
            image_height=args.image_height,
            parallel=args.parallel,
            use_api=not args.no_api,
            review_mode=args.review_mode,
            video_only=args.video_only,
        )
        assets = await generator.run()

        assets_path = output_dir / "assets.json"
        write_json(assets_path, assets)
        print(json.dumps({"assets_json": str(assets_path)}, ensure_ascii=False))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
