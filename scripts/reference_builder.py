from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from content_filter import VideoPromptBuilder
from utils import write_json, write_text

logger = logging.getLogger(__name__)


class ReferenceBuilder:
    def __init__(
        self,
        *,
        storyboard: dict[str, Any],
        output_root: Path,
        review_mode: str,
        normalize_usage: Callable[[Any], str],
        resolve_shot_contract: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.storyboard = storyboard
        self.output_root = output_root
        self.review_mode = review_mode
        self._normalize_usage = normalize_usage
        self._resolve_shot_contract = resolve_shot_contract

    def get_character_appearances(self, characters_in_shot: list[str]) -> list[tuple[str, str]]:
        characters_cfg = self.storyboard.get("characters", {})
        appearances: list[tuple[str, str]] = []
        for char_id in characters_in_shot:
            char = characters_cfg.get(char_id, {})
            appearance = char.get("appearance", "")
            if appearance:
                appearances.append((char_id, appearance))
        return appearances

    def get_prop_appearances(self, props_in_shot: list[str]) -> list[tuple[str, str]]:
        prop_cfg = self.storyboard.get("prop_refs", {})
        appearances: list[tuple[str, str]] = []
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

    def collect_character_ref_bindings(self, characters_in_shot: list[str]) -> list[dict[str, Any]]:
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

    def collect_prop_ref_bindings(self, props_in_shot: list[str]) -> list[dict[str, Any]]:
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

    def append_video_reference(
        self,
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
            "usage": self._normalize_usage(usage),
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

    def resolve_explicit_video_reference(
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

            usage = self._normalize_usage(ref_cfg.get("usage"))
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

        usage = self._normalize_usage(ref_cfg.get("usage"))
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

    def build_video_references(
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
                resolved = self.resolve_explicit_video_reference(
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
        self.append_video_reference(
            refs,
            path=first_frame_path,
            usage="first_frame",
            source_type="frame",
            source="first_frame",
            generated=True,
        )
        self.append_video_reference(
            refs,
            path=scene_image,
            usage="reference_composition",
            source_type="scene",
            source="scene_ref",
        )
        self.append_video_reference(
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
                self.append_video_reference(
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
                self.append_video_reference(
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
            self.append_video_reference(
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
        self.append_video_reference(
            refs,
            path=end_frame_path,
            usage="reference_target_state",
            source_type="frame",
            source="target_state",
            generated=True,
        )
        return refs

    def assess_image_review_risk(
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
                isinstance(value, list) and any(str(item).strip() for item in value) for value in stable_facts.values()
            )
            has_carry_forward = isinstance(carry_forward_subjects, list) and any(str(item).strip() for item in carry_forward_subjects)
            if has_stable_facts or has_carry_forward:
                reasons.append(
                    {
                        "reason": "scene_continuity_facts",
                        "severity": "medium",
                        "details": "This shot inherits scene-level continuity facts that should be checked for spatial, prop, and environment consistency.",
                    }
                )
        return {"needs_review": bool(reasons), "reasons": reasons}

    @staticmethod
    def collect_hard_constraint_summary(shot: dict[str, Any]) -> dict[str, Any]:
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
    def needs_reference_validation(shot: dict[str, Any], hard_constraints: dict[str, Any]) -> bool:
        if any(hard_constraints.get(key) for key in ("pose_contract", "gaze_contract", "stable_facts", "entity_registry")):
            return True
        return bool(hard_constraints.get("shot_delta")) and str(shot.get("continuity_mode", "")).strip() == "strict"

    def export_reference_review_bundle(
        self,
        shot: dict[str, Any],
        references: list[dict[str, Any]],
        hard_constraints: dict[str, Any],
        video_prompt_text: str | None = None,
        pretrim_video_prompt_text: str | None = None,
        provider_prompt_variants: list[dict[str, Any]] | None = None,
        resolved_video_references: list[dict[str, Any]] | None = None,
        all_resolved_video_references: list[dict[str, Any]] | None = None,
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

        pretrim_video_prompt_path: str | None = None
        if pretrim_video_prompt_text:
            pretrim_prompt_file = bundle_dir / "pretrim_video_prompt.txt"
            write_text(pretrim_prompt_file, pretrim_video_prompt_text)
            pretrim_video_prompt_path = str(pretrim_prompt_file)

        provider_variant_manifest_path: str | None = None
        if provider_prompt_variants:
            provider_variant_manifest = bundle_dir / "provider_prompt_variants.json"
            write_json(provider_variant_manifest, provider_prompt_variants)
            provider_variant_manifest_path = str(provider_variant_manifest)

        serialized_runtime_refs = (
            list(resolved_video_references)
            if isinstance(resolved_video_references, list)
            else self.serialize_video_references(references)
        )
        serialized_all_runtime_refs = (
            list(all_resolved_video_references)
            if isinstance(all_resolved_video_references, list)
            else serialized_runtime_refs
        )

        payload = {
            "shot_id": shot_id,
            "review_type": "reference_validation",
            "review_mode": self.review_mode,
            "video_prompt_path": video_prompt_path,
            "pretrim_video_prompt_path": pretrim_video_prompt_path,
            "provider_prompt_variants_path": provider_variant_manifest_path,
            "references": copied_refs,
            "shot_context": {
                "director_plan": resolved_contract.get("director_plan", {}),
                "scene_prompt": resolved_contract.get("scene_prompt", ""),
                "action_prompt": resolved_contract.get("action_prompt", ""),
                "end_frame_description": resolved_contract.get("end_frame_description", ""),
                "keyframes": resolved_contract.get("keyframes", []),
                "video_references": serialized_runtime_refs,
                "all_resolved_video_references": serialized_all_runtime_refs,
                "storyboard_video_references": shot.get("video_references", []),
                "subject_constraints": shot.get("subject_constraints", {}),
                "shot_delta": shot.get("shot_delta", []),
                "scene_continuity": shot.get("_scene_continuity", {}),
            },
            "hard_constraints": hard_constraints,
            "judge_questions": [
                "Do these references work together according to their declared usage roles such as first frame, character, prop, composition, stage, and target state?",
                "Do they preserve pose_contract, gaze_contract, scene stable facts, entity uniqueness, and other hard constraints before video generation?",
                "Should video generation be blocked until these references are regenerated or re-assigned?",
            ],
        }
        write_json(bundle_dir / "reference_review_request.json", payload)
        return bundle_dir

    @staticmethod
    def serialize_video_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**{k: v for k, v in ref.items() if k != "path"}, "path": str(ref["path"])} for ref in references]

    @staticmethod
    def compose_prompt_with_reference_mentions(prompt: str, references: list[dict[str, Any]]) -> str:
        prompt_ready_refs: list[dict[str, Any]] = []
        for idx, ref in enumerate(references, start=1):
            prompt_ref = dict(ref)
            prompt_ref["mention"] = f"@图片{idx}"
            prompt_ready_refs.append(prompt_ref)
        return VideoPromptBuilder.compose_video_generation_prompt(prompt, prompt_ready_refs)

    def export_video_prompt_artifact(self, shot_id: int, prompt_text: str, *, variant: str = "video_prompt") -> Path:
        prompt_path = self.output_root / "prompts" / f"shot_{shot_id:03d}_{variant}.txt"
        write_text(prompt_path, prompt_text)
        return prompt_path

    def export_video_reference_artifact(self, shot_id: int, references: list[dict[str, Any]], *, variant: str) -> Path:
        refs_path = self.output_root / "prompts" / f"shot_{shot_id:03d}_{variant}.json"
        write_json(refs_path, references)
        return refs_path

    def export_image_review_bundle(
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
