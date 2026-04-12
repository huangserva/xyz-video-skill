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
import re
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw

from utils import VIDEO_REFERENCE_USAGE_ALIASES, get_api_credentials, get_model_config, load_external_api_config, setup_logging, timestamp_id, write_json, write_text
from content_filter import ContentFilter, VideoPromptBuilder
from image_gen import ImageGenerator
from reference_builder import ReferenceBuilder
from video_gen import VideoGenerator
from video_quality import (
    DETECTOR_VERSION,
    extract_segment_frames,
    extract_video_last_frame,
    remove_video_segments,
    scan_video_quality,
    trim_video_at,
)

logger = logging.getLogger(__name__)
SCRIPT_DIR = Path(__file__).resolve().parent
REVIEW_MODES = {"metrics_only", "hybrid_judge", "director_review"}
SHOT_TYPES = {"visible_subject", "offscreen_reaction", "transition_reveal", "free_atmosphere"}
REVIEW_PENDING_STATUSES = {"pending_judgment"}
REVIEW_BLOCKING_STATUSES = {"needs_regeneration", "judged_blocked", "regenerate"}


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
        resume: bool = False,
    ):
        self.storyboard = storyboard
        self.output_root = output_root
        self.image_width = image_width
        self.image_height = image_height
        self.use_api = use_api
        self.video_only = video_only  # 调试模式：跳过图片生成
        self.resume = resume
        self.checkpoint_path = output_root / ".checkpoint.json"
        self._session: aiohttp.ClientSession | None = None
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
        self.reference_builder = ReferenceBuilder(
            storyboard=self.storyboard,
            output_root=self.output_root,
            review_mode=self.review_mode,
            normalize_usage=self._normalize_video_reference_usage,
            resolve_shot_contract=self._resolve_shot_contract,
        )
        self.image_generator = ImageGenerator(
            storyboard=self.storyboard,
            image_dir=self.image_dir,
            image_width=self.image_width,
            image_height=self.image_height,
            use_api=self.use_api,
            cfg=self.cfg,
            get_session=self._get_session,
            download=self._download,
        )
        self.video_generator = VideoGenerator(
            video_dir=self.video_dir,
            cfg=self.cfg,
            get_session=self._get_session,
            download=self._download,
            reference_builder=self.reference_builder,
            normalize_usage=self._normalize_video_reference_usage,
        )

    # ── Checkpoint (断点续传) ──────────────────────────────────────

    def _load_checkpoint(self) -> dict[str, Any] | None:
        if not self.resume or not self.checkpoint_path.exists():
            return None
        try:
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            completed = data.get("completed_shot_ids", [])
            logger.info(f"加载 checkpoint: 已完成 {len(completed)} 个 shot ({completed})")
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"checkpoint 文件损坏，忽略: {e}")
            return None

    def _save_checkpoint(
        self,
        completed_shot_ids: list[int],
        shot_results: list[dict[str, Any]],
        previous_video_path: str | None,
    ) -> None:
        data = {
            "completed_shot_ids": completed_shot_ids,
            "shot_results": shot_results,
            "previous_video_path": previous_video_path,
        }
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.checkpoint_path)

    def _clear_checkpoint(self) -> None:
        self.checkpoint_path.unlink(missing_ok=True)

    # ── aiohttp Session 复用 ──────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _normalize_video_reference_usage(value: Any) -> str:
        cleaned = str(value or "").strip()
        return VIDEO_REFERENCE_USAGE_ALIASES.get(cleaned, cleaned or "reference")

    @staticmethod
    def _review_status_from_action(overall_action: Any) -> str:
        action = str(overall_action or "keep").strip().lower() or "keep"
        if action == "keep":
            return "approved"
        if action == "regenerate":
            return "needs_regeneration"
        return "judged_blocked"

    @staticmethod
    def _review_state_bucket(review: dict[str, Any] | None) -> str | None:
        if not isinstance(review, dict):
            return None
        status = str(review.get("status", "")).strip()
        if status in REVIEW_PENDING_STATUSES:
            return "pending"
        if status in REVIEW_BLOCKING_STATUSES:
            return "blocked"
        return None

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

        # ── 断点续传：加载 checkpoint ──
        checkpoint = self._load_checkpoint()
        completed_shot_ids: set[int] = set()
        shot_results: list[dict[str, Any]] = []
        previous_video_path: Path | None = None
        if checkpoint:
            completed_shot_ids = set(checkpoint.get("completed_shot_ids", []))
            shot_results = checkpoint.get("shot_results", [])
            prev_path = checkpoint.get("previous_video_path")
            if prev_path and Path(prev_path).exists():
                previous_video_path = Path(prev_path)

        # 串行处理：按 scene > shots 顺序
        pending_reviews: list[dict[str, Any]] = []
        blocked_reviews: list[dict[str, Any]] = []
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

                # 断点续传：跳过已完成的 shot
                if shot_id in completed_shot_ids:
                    # 恢复 previous_video_path 以维持 chain_from_previous
                    for prev_r in shot_results:
                        if isinstance(prev_r.get("image"), dict) and int(prev_r["image"].get("shot_id", -1)) == shot_id:
                            if "video" in prev_r:
                                previous_video_path = Path(prev_r["video"]["path"])
                            break
                    logger.info(f"shot_{shot_id}: 已在 checkpoint 中，跳过")
                    continue

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
                    first_frame = extract_video_last_frame(previous_video_path, extracted_frame_path)
                    if first_frame:
                        logger.info(f"shot_{shot.get('id')}: chain_from_previous=true，从上一视频提取实际尾帧")
                    else:
                        logger.warning(f"shot_{shot.get('id')}: 视频尾帧提取失败，fallback 到独立生成")
                        first_frame = None
                else:
                    first_frame = None
                    if i == 0 and carry_style_reference_from_previous_scene and previous_video_path:
                        style_ref_path = self.image_dir / f"shot_{shot.get('id', 0):03d}_style_ref.png"
                        style_reference_frame = extract_video_last_frame(previous_video_path, style_ref_path)
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
                image_review_bucket = self._review_state_bucket(image_review)
                if image_review_bucket:
                    review_entry = (
                        pending_reviews if image_review_bucket == "pending" else blocked_reviews
                    )
                    review_entry.append(
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
                        f"shot_{shot.get('id')}: 图片阶段"
                        + ("等待视觉判断" if image_review_bucket == "pending" else "审查未通过")
                        + "，暂停后续素材生成"
                    )
                    continue
                reference_review = result.get("reference_review")
                reference_review_bucket = self._review_state_bucket(reference_review)
                if reference_review_bucket:
                    review_entry = (
                        pending_reviews if reference_review_bucket == "pending" else blocked_reviews
                    )
                    review_entry.append(
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
                        f"shot_{shot.get('id')}: 参考图阶段"
                        + ("等待结构判断" if reference_review_bucket == "pending" else "审查未通过")
                        + "，暂停后续素材生成"
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

                    quality = scan_video_quality(
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
                        remove_video_segments(vid_path, quality["cut_segments"])
                        edited_copy = audit_dir / f"trimmed_attempt_{attempt + 1}.mp4"
                        shutil.copy2(vid_path, edited_copy)
                        audit_entry["action"] = audit_entry.get("action", "segment_cut")
                        audit_entry["trimmed_video"] = str(edited_copy)
                        logger.info(f"{shot_id_str}: 局部裁剪后视频已保存 → {edited_copy}")
                    elif quality["trim_to"] is not None:
                        trim_video_at(vid_path, quality["trim_to"])
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
                completed_shot_ids.add(shot_id)
                self._save_checkpoint(
                    completed_shot_ids=sorted(completed_shot_ids),
                    shot_results=shot_results,
                    previous_video_path=str(previous_video_path) if previous_video_path else None,
                )

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

        if pending_reviews or blocked_reviews:
            # 有待审阅项目时不清 checkpoint（可能需要继续）
            return {
                "generated_at": timestamp_id(),
                "asset_root": str(self.output_root),
                "images": images,
                "videos": videos,
                "shot_prompts": shot_prompts,
                "shot_references": shot_references,
                "bgm": None,
                "pending_reviews": pending_reviews,
                "blocked_reviews": blocked_reviews,
            }

        # BGM
        duration = int(self.storyboard.get("total_duration", 60))
        bgm_style = str(self.storyboard.get("bgm_style", "upbeat"))
        bgm_path, bgm_provider = await self._generate_bgm(bgm_style, duration)

        # 全部完成，清理 checkpoint
        self._clear_checkpoint()

        return {
            "generated_at": timestamp_id(),
            "asset_root": str(self.output_root),
            "images": images,
            "videos": videos,
            "shot_prompts": shot_prompts,
            "shot_references": shot_references,
            "bgm": {"path": str(bgm_path), "provider": bgm_provider, "style": bgm_style},
            "pending_reviews": pending_reviews,
            "blocked_reviews": blocked_reviews,
        }

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
            frames = extract_segment_frames(video_path, segment_dir, segment, max_frames=max_frames)
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
            path, provider = await self.image_generator.generate_image(prompt, shot_id=0, output_path=out)
        if path and path.exists() and path.stat().st_size > 1000:
            return path
        logger.warning(f"场景 {scene_id}: 场景图生成失败，分镜将不使用场景参考图")
        return None

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
            session = await self._get_session()
            async with session.post(
                f"{creds['api_base']}/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
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

        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, KeyError, ValueError) as e:
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
        char_appearances = self.reference_builder.get_character_appearances(characters_in_shot)
        prop_appearances = self.reference_builder.get_prop_appearances(props_in_shot)

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
        character_ref_bindings = self.reference_builder.collect_character_ref_bindings(characters_in_shot)
        prop_ref_bindings = self.reference_builder.collect_prop_ref_bindings(props_in_shot)

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
                    image_path, image_provider = await self.image_generator.generate_image(
                        image_prompt, shot_id,
                        characters_in_shot=characters_in_shot,
                        props_in_shot=props_in_shot,
                        scene_image=scene_image,
                        style_reference_image=style_reference_frame,
                    )

        image_review_risk = self.reference_builder.assess_image_review_risk(
            shot=shot,
            image_provider=image_provider,
            character_ref_bindings=character_ref_bindings,
            style_reference_frame=style_reference_frame,
        )

        if self.review_mode == "hybrid_judge" and image_review_risk.get("needs_review"):
            bundle_dir = self.reference_builder.export_image_review_bundle(
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
                    "status": self._review_status_from_action(overall_action),
                    "bundle_dir": str(bundle_dir),
                    "risk_summary": image_review_risk,
                    "judge_result": judge_result,
                    "action": overall_action,
                }
                if overall_action == "keep":
                    logger.info(f"shot_{shot_id}: 图片视觉判断通过，继续生成视频")
            else:
                logger.info(f"shot_{shot_id}: 图片风险 bundle 已导出，等待视觉判断 → {bundle_dir}")

            if result["image_review"]["status"] in REVIEW_PENDING_STATUSES | REVIEW_BLOCKING_STATUSES:
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
                                image_path, image_provider = await self.image_generator.generate_image(
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
        if (
            shot_type != "offscreen_reaction"
            and continuity_mode != "free"
            and self.video_generator.any_provider_supports_stage_references()
        ):
            director_keyframes = director_entry.get("keyframes", [])
            if not isinstance(director_keyframes, list):
                director_keyframes = []
            keyframes = director_keyframes or resolved_contract.get("keyframes", [])
            max_refs = self.video_generator.max_reference_images()
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
                        keyframe_path, keyframe_provider = await self.image_generator.generate_image(
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
                    end_frame_path, end_frame_provider = await self.image_generator.generate_image(
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
        hard_constraints = self.reference_builder.collect_hard_constraint_summary(shot)
        video_references = self.reference_builder.build_video_references(
            shot,
            first_frame_path=image_path,
            scene_image=scene_image,
            style_reference_frame=style_reference_frame,
            end_frame_path=end_frame_path,
            character_ref_bindings=character_ref_bindings,
            prop_ref_bindings=prop_ref_bindings,
            keyframe_results=keyframe_results,
        )
        serialized_video_references = self.reference_builder.serialize_video_references(video_references)
        pretrim_video_prompt = self.reference_builder.compose_prompt_with_reference_mentions(video_prompt, video_references)
        pretrim_video_prompt_path = self.reference_builder.export_video_prompt_artifact(
            shot_id,
            pretrim_video_prompt,
            variant="pretrim_video_prompt",
        )
        video_prompt_path = pretrim_video_prompt_path
        provider_prompt_variants: list[dict[str, Any]] = []
        fallback_chain = self.video_generator.fallback_chain()
        for idx, provider_name in enumerate(fallback_chain):
            prepared_variant = self.video_generator.prepare_provider_inputs(
                provider_name,
                image_path=image_path,
                prompt=video_prompt,
                last_frame_path=end_frame_path,
                video_references=video_references,
            )
            provider_prompt_path = self.reference_builder.export_video_prompt_artifact(
                shot_id,
                prepared_variant["prompt_text"],
                variant=f"provider_{provider_name}_video_prompt",
            )
            provider_refs_path = self.reference_builder.export_video_reference_artifact(
                shot_id,
                prepared_variant["serialized_references"],
                variant=f"provider_{provider_name}_final_references",
            )
            provider_prompt_variants.append(
                {
                    "provider": provider_name,
                    "prompt_path": str(provider_prompt_path),
                    "references_path": str(provider_refs_path),
                    "references": prepared_variant["serialized_references"],
                    "prompt_text": prepared_variant["prompt_text"],
                }
            )
            if idx == 0:
                video_prompt_path = self.reference_builder.export_video_prompt_artifact(
                    shot_id,
                    prepared_variant["prompt_text"],
                )

        primary_provider_variant = provider_prompt_variants[0] if provider_prompt_variants else None
        public_provider_variants = [
            {
                "provider": item["provider"],
                "prompt_path": item["prompt_path"],
                "references_path": item["references_path"],
            }
            for item in provider_prompt_variants
        ]
        video_prompt_meta: dict[str, Any] = {
            "shot_id": shot_id,
            "path": str(video_prompt_path),
            "pretrim_path": str(pretrim_video_prompt_path),
            "provider_variants": public_provider_variants,
        }
        if primary_provider_variant:
            video_prompt_meta["primary_provider"] = primary_provider_variant["provider"]

        if self.reference_builder.needs_reference_validation(shot, hard_constraints):
            bundle_dir = self.reference_builder.export_reference_review_bundle(
                shot=shot,
                references=(
                    [
                        {
                            **item,
                            "path": Path(str(item["path"])),
                        }
                        for item in primary_provider_variant["references"]
                    ]
                    if primary_provider_variant else video_references
                ),
                hard_constraints=hard_constraints,
                video_prompt_text=(
                    primary_provider_variant["prompt_text"]
                    if primary_provider_variant else pretrim_video_prompt
                ),
                pretrim_video_prompt_text=pretrim_video_prompt,
                provider_prompt_variants=public_provider_variants,
                resolved_video_references=(
                    primary_provider_variant["references"]
                    if primary_provider_variant else serialized_video_references
                ),
                all_resolved_video_references=serialized_video_references,
            )
            judge_result_path = bundle_dir / "reference_judge_result.json"
            if judge_result_path.exists():
                with open(judge_result_path, encoding="utf-8") as f:
                    judge_result = json.load(f)
                overall_action = str(judge_result.get("overall_action", "keep")).strip() or "keep"
                if overall_action != "keep":
                    return {
                        "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                        "video_prompt": video_prompt_meta,
                        "video_references": serialized_video_references,
                        "keyframes": keyframe_results,
                        "end_frame": (
                            {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}
                            if end_frame_path and end_frame_path.exists() else None
                        ),
                        "reference_review": {
                            "status": self._review_status_from_action(overall_action),
                            "bundle_dir": str(bundle_dir),
                            "judge_result": judge_result,
                            "hard_constraints": hard_constraints,
                            "action": overall_action,
                        },
                    }
                logger.info(f"shot_{shot_id}: 参考图前置验证通过，允许继续生成视频")
            else:
                logger.info(f"shot_{shot_id}: 参考图前置验证待判断 → {bundle_dir}")
                return {
                    "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
                    "video_prompt": video_prompt_meta,
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
            "video_prompt": video_prompt_meta,
            "video_references": serialized_video_references,
        }
        if keyframe_results:
            result["keyframes"] = keyframe_results

        if self.use_api and image_path.exists():
            video_path, video_provider = await self.video_generator.generate_video(
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
                result["video_prompt"]["selected_provider"] = video_provider
                for item in public_provider_variants:
                    if item["provider"] == video_provider:
                        result["video_prompt"]["selected_provider_prompt_path"] = item["prompt_path"]
                        result["video_prompt"]["selected_provider_references_path"] = item["references_path"]
                        break

        # 返回尾帧路径，供下一镜头复用
        if end_frame_path and end_frame_path.exists():
            result["end_frame"] = {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}

        return result

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
        except (ImportError, OSError, ValueError) as e:
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
            session = await self._get_session()
            async with session.post(
                f"{creds['api_base']}{endpoint}",
                headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                json={"prompt": f"{style}, instrumental, no vocals", "lyrics_prompt": "[Instrumental]", "duration": duration},
                timeout=aiohttp.ClientTimeout(total=timeout),
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
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
            logger.warning(f"minimax 失败: {e}")
        return False

    async def _download(self, session: aiohttp.ClientSession, url: str, output: Path) -> bool:
        """下载文件。"""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status == 200:
                    output.write_bytes(await resp.read())
                    return output.exists() and output.stat().st_size > 0
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(f"下载失败: {e}")
        return False

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
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

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
            session = await self._get_session()
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
                    timeout=aiohttp.ClientTimeout(total=timeout),
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
                    timeout=aiohttp.ClientTimeout(total=timeout),
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
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
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
    parser.add_argument("--resume", action="store_true", help="从上次中断处继续，跳过已完成的 shot")
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
        try:
            result = await gen.generate(character_id_filter=args.character_id)
        finally:
            await gen.close()

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
            resume=args.resume,
        )
        try:
            assets = await generator.run()
        finally:
            await generator.close()

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
