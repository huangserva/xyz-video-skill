"""内容过滤和结构化 prompt 构建。

移植自 xyz-video-creator 的 core_principles.py + content_filter.py，
适配 xyz-video-skill 的 CLI 工作流（无数据库，基于 JSON 文件）。

核心功能：
1. 从文本中移除服装/外貌描述（单一真相源原则）
2. 构建结构化的图片 prompt 和视频 prompt
"""

from __future__ import annotations

import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── 服装关键词（来自 oii/core_principles.py） ───────────────────

CLOTHING_KEYWORDS_CHINESE = [
    # 穿戴动词
    "穿着", "穿了", "身穿", "身着", "着装", "衣着", "换上", "披着",
    "戴着", "戴了", "佩戴", "系着",
    # 上装
    "衬衫", "衬衣", "T恤", "卫衣", "外套", "夹克", "西装", "西服",
    "毛衣", "针织衫", "风衣", "大衣", "羽绒服", "棉服", "马甲",
    "背心", "polo衫", "短袖", "长袖", "连帽衫",
    # 下装
    "裤子", "牛仔裤", "西裤", "休闲裤", "短裤", "裙子", "长裙", "短裙",
    "半身裙", "连衣裙", "百褶裙",
    # 鞋
    "鞋子", "皮鞋", "运动鞋", "高跟鞋", "靴子", "凉鞋", "拖鞋", "球鞋",
    # 配饰
    "帽子", "眼镜", "墨镜", "太阳镜", "围巾", "领带", "领结", "手表",
    "项链", "耳环", "手链", "戒指", "手套", "腰带", "皮带", "背包",
    # 制服/特殊服装
    "校服", "制服", "工装", "礼服", "婚纱", "睡衣",
]

CLOTHING_KEYWORDS_ENGLISH = [
    "wearing", "dressed in", "clothed in",
    "shirt", "t-shirt", "blouse", "sweater", "hoodie", "jacket", "coat",
    "suit", "blazer", "vest", "dress", "skirt", "pants", "trousers",
    "jeans", "shorts", "shoes", "boots", "sneakers", "heels",
    "hat", "cap", "glasses", "sunglasses", "scarf", "tie", "watch",
]

CLOTHING_KEYWORDS = CLOTHING_KEYWORDS_CHINESE + CLOTHING_KEYWORDS_ENGLISH

# ── 服装描述正则模式 ──────────────────────────────────────────

CLOTHING_PATTERNS = [
    # 中文：排除常见动词/介词边界词（在/地/得/把/被/让/向/从/对/给），防止吃掉后续动作
    r'穿着[^，。、；\s在地得把被让向从对给]{1,12}[，、；\s]?',
    r'身穿[^，。、；\s在地得把被让向从对给]{1,12}[，、；\s]?',
    r'身着[^，。、；\s在地得把被让向从对给]{1,12}[，、；\s]?',
    r'戴着[^，。、；\s在地得把被让向从对给]{1,10}[，、；\s]?',
    r'佩戴[^，。、；\s在地得把被让向从对给]{1,10}[，、；\s]?',
    r'系着[^，。、；\s在地得把被让向从对给]{1,8}[，、；\s]?',
    r'披着[^，。、；\s在地得把被让向从对给]{1,10}[，、；\s]?',
    r'换上了?[^，。、；\s在地得把被让向从对给]{1,10}[，、；\s]?',
    # 英文：只匹配到逗号/句号/分号，不贪婪
    r'wearing [^,.;\n]{1,20}[,;.]',
    r'dressed in [^,.;\n]{1,20}[,;.]',
    # "in a suit" 只匹配服装词本身，不吃后面内容
    r'in a (?:shirt|t-shirt|blouse|sweater|hoodie|jacket|coat|suit|blazer|vest|dress|skirt|pants|trousers|jeans|shorts|uniform|gown|robe|outfit|costume)',
]


# ── ContentFilter ────────────────────────────────────────────

class ContentFilter:
    """内容过滤器 — 强制单一真相源原则。"""

    @staticmethod
    def remove_clothing_descriptions(text: str) -> str:
        """从文本中移除服装/外貌描述。

        例如：
        输入："小明穿着绿色卫衣，好奇地环顾四周"
        输出："小明好奇地环顾四周"
        """
        if not text:
            return text

        result = text
        for pattern in CLOTHING_PATTERNS:
            result = re.sub(pattern, '', result, flags=re.IGNORECASE)

        # 清理残余标点和空格
        result = re.sub(r'[，、]{2,}', '，', result)
        result = re.sub(r'[,]{2,}', ',', result)
        result = re.sub(r'\s+', ' ', result)
        result = re.sub(r'^[，、,\s]+', '', result)
        result = re.sub(r'[，、,\s]+$', '', result)
        result = re.sub(r'，+', '，', result)
        result = re.sub(r'。+', '。', result)

        return result.strip()

    @staticmethod
    def contains_clothing_description(text: str) -> tuple[bool, list[str]]:
        """检查文本是否包含服装描述。

        对英文关键词使用单词边界匹配，避免 "shattered" 误匹配 "hat" 等误报。
        中文关键词仍用子串匹配（中文无单词边界）。
        """
        if not text:
            return False, []
        found = []
        text_lower = text.lower()
        for keyword in CLOTHING_KEYWORDS:
            kw_lower = keyword.lower()
            if any('\u4e00' <= c <= '\u9fff' for c in keyword):
                # 中文：子串匹配
                if kw_lower in text_lower:
                    found.append(keyword)
            else:
                # 英文：单词边界匹配
                if re.search(r'\b' + re.escape(kw_lower) + r'\b', text_lower):
                    found.append(keyword)
        return len(found) > 0, found


# ── VideoPromptBuilder（适配 xyz-video-skill） ──────────────────

class VideoPromptBuilder:
    """结构化 prompt 构建器。

    与 oii 的区别：
    - 无对白/旁白逻辑（xyz-video-skill 用 narration 字段由 Seedance 音画同轨处理）
    - 角色信息从 storyboard.characters dict 读取
    - 支持 consistency_anchors
    """

    @staticmethod
    def infer_style_medium_lock(style_anchor: str) -> dict[str, str]:
        """从 style_anchor 推断媒介锁，避免写实/插画媒介混用。"""
        anchor = (style_anchor or "").strip()
        lower = anchor.lower()

        illustrated_tokens = [
            "ink", "brush", "painterly", "painted", "illustrated", "illustration",
            "anime", "animation", "manga", "comic", "cel-shaded", "stylized",
            "watercolor", "oil painting", "concept art",
        ]
        photoreal_tokens = [
            "photoreal", "photo-real", "live-action", "live action", "realistic",
            "cinematic realism", "natural skin", "lens-based", "film still",
        ]
        three_d_tokens = [
            "3d", "cg", "cgi", "rendered", "unreal", "octane", "game cinematic",
        ]

        if any(token in lower for token in illustrated_tokens):
            return {
                "medium": "illustrated",
                "lock_line": (
                    "风格媒介锁定：插画 / 绘画感电影画面。"
                    "所有镜头都必须保持同一种插画媒介。"
                    "不要漂移成写实真人影像。"
                ),
            }
        if any(token in lower for token in three_d_tokens):
            return {
                "medium": "three_dimensional",
                "lock_line": (
                    "风格媒介锁定：风格化 3D / CG 电影画面。"
                    "所有镜头都必须保持同一种 3D 渲染媒介。"
                    "不要漂移成手绘插画或真人写实影像。"
                ),
            }
        if any(token in lower for token in photoreal_tokens):
            return {
                "medium": "photorealistic",
                "lock_line": (
                    "风格媒介锁定：写实真人电影画面。"
                    "所有镜头都必须保持同一种写实媒介。"
                    "不要漂移成插画、动漫、漫画或绘画渲染。"
                ),
            }
        return {
            "medium": "unspecified",
            "lock_line": (
                "风格媒介锁定：整个项目只能选择一种视觉媒介，并在所有镜头中保持完全一致。"
                "不要在写实、插画、动漫、漫画或 3D 渲染风格之间来回切换。"
            ),
        }

    @staticmethod
    def _reference_usage_line(ref: dict[str, Any], mention: str) -> str:
        usage = str(ref.get("usage", "")).strip() or "reference"
        subject = str(ref.get("subject", "")).strip()
        stage = str(ref.get("stage", "")).strip()
        description = str(ref.get("description", "")).strip()

        suffix = f"（{subject}）" if subject else ""
        mapping = {
            "first_frame": f"{mention} 作为首帧。",
            "reference_character": f"{mention} 参考角色{suffix}。",
            "reference_prop": f"{mention} 参考道具{suffix}。",
            "reference_composition": f"{mention} 参考构图。",
            "reference_style": f"{mention} 参考风格。",
            "reference_color": f"{mention} 参考色调。",
            "reference_target_state": f"{mention} 参考目标状态。",
            "reference_stage": (
                f"{mention} 参考动作阶段：{stage}。"
                if stage else
                f"{mention} 参考中间阶段。"
            ),
            "reference_motion": f"{mention} 参考镜头语言。",
        }
        if usage in mapping:
            return mapping[usage]
        if description:
            return f"{mention} 作为参考：{description}"
        return f"{mention} 作为参考。"

    @staticmethod
    def build_reference_callouts(
        references: list[dict[str, Any]] | None,
        *,
        mention_prefix: str = "@图片",
    ) -> list[str]:
        if not isinstance(references, list):
            return []
        lines: list[str] = []
        for idx, ref in enumerate(references, start=1):
            if not isinstance(ref, dict):
                continue
            mention = str(ref.get("mention", "")).strip() or f"{mention_prefix}{idx}"
            lines.append(VideoPromptBuilder._reference_usage_line(ref, mention))
        return lines

    @staticmethod
    def compose_video_generation_prompt(
        base_prompt: str,
        references: list[dict[str, Any]] | None,
        *,
        mention_prefix: str = "@图片",
    ) -> str:
        callouts = VideoPromptBuilder.build_reference_callouts(references, mention_prefix=mention_prefix)
        if not callouts:
            return base_prompt
        parts = ["【参考素材调用】", *callouts, "", "【生成要求】", base_prompt]
        return "\n".join(parts)

    @staticmethod
    def _append_subject_constraints(
        parts: list[str],
        subject_constraints: dict[str, Any] | None,
        *,
        for_model: bool = False,
    ) -> None:
        """把 shot 级主体语义约束写入 prompt。"""
        if not isinstance(subject_constraints, dict) or not subject_constraints:
            return

        if for_model:
            mapping = [
                ("required_visible_subjects", "必须出镜的主体"),
                ("optional_visible_subjects", "可选出镜的主体"),
                ("continuity_subjects", "需要保持连续性的主体"),
            ]
        else:
            mapping = [
                ("required_visible_subjects", "必须出镜的主体"),
                ("optional_visible_subjects", "可选出镜的主体"),
                ("offscreen_subjects", "必须保持画外的主体"),
                ("continuity_subjects", "需要保持连续性的主体"),
                ("forbidden_visible_subjects", "禁止出镜的主体"),
            ]
        lines: list[str] = []
        for key, label in mapping:
            value = subject_constraints.get(key, [])
            if isinstance(value, list):
                cleaned = [str(item).strip() for item in value if str(item).strip()]
                if cleaned:
                    lines.append(f"  {label}: {', '.join(cleaned)}")

        semantic_rules = subject_constraints.get("semantic_rules", [])
        if isinstance(semantic_rules, list):
            cleaned_rules = [str(item).strip() for item in semantic_rules if str(item).strip()]
            for rule in cleaned_rules:
                if for_model and re.search(r"(不要|禁止|不得|不可|不应)", rule):
                    continue
                lines.append(f"  规则: {rule}")

        pose_contract = subject_constraints.get("pose_contract", [])
        if isinstance(pose_contract, str):
            pose_contract = [pose_contract]
        if isinstance(pose_contract, list):
            cleaned_pose_rules = [str(item).strip() for item in pose_contract if str(item).strip()]
            for rule in cleaned_pose_rules:
                if for_model and re.search(r"(不要|禁止|不得|不可|不应)", rule):
                    continue
                lines.append(f"  姿态合同: {rule}")

        gaze_contract = subject_constraints.get("gaze_contract", {})
        if isinstance(gaze_contract, dict):
            for subject_id, config in gaze_contract.items():
                if not isinstance(config, dict):
                    continue
                primary_target = str(config.get("primary_target", "")).strip()
                target_zone = str(config.get("target_zone", "")).strip()
                gaze_parts = []
                if primary_target:
                    gaze_parts.append(f"主要视线目标={primary_target}")
                if target_zone:
                    gaze_parts.append(f"目标区域={target_zone}")
                if gaze_parts:
                    label = str(subject_id).strip() or "主体"
                    lines.append(f"  视线合同[{label}]: {'；'.join(gaze_parts)}")

        if lines:
            parts.append("")
            if for_model:
                parts.append("【镜头主体】")
            else:
                parts.append("⚠️ 主体约束合同——本镜头必须遵守以下主体级限制：")
            parts.extend(lines)

    @staticmethod
    def _append_shot_type_rules(parts: list[str], shot_type: str) -> None:
        """把 shot 类型对应的通用生成策略写入 prompt。"""
        rules = {
            "visible_subject": [
                "所有必须出镜的主体都要清晰且持续地保留在画面内。",
                "不要在镜头中途改变主体身份、物种或数量。",
            ],
            "offscreen_reaction": [
                "这是一个反应镜头。威胁或目标在整个镜头内都必须保持画外。",
                "不要把未出镜实体直接露出来，也不要幻觉出局部身体进入画面。",
                "危险感只能通过视线、姿态、环境反应、声音暗示、风、尘土或光线变化来表达。",
            ],
            "transition_reveal": [
                "这个镜头负责从画外暗示过渡到正式出镜。",
                "如果有新主体出现，必须渐进式显露，并与后续镜头保持身份一致。",
            ],
            "free_atmosphere": [
                "这是一个氛围镜头。优先保证情绪与环境连续性，而不是人物动作。",
            ],
        }
        cleaned = str(shot_type or "").strip()
        if not cleaned:
            return
        selected = rules.get(cleaned, [])
        parts.append("")
        parts.append(f"⚠️ 镜头类型——{cleaned}")
        for rule in selected:
            parts.append(f"  规则: {rule}")

    @staticmethod
    def _append_scene_continuity(
        parts: list[str],
        scene_continuity: dict[str, Any] | None,
    ) -> None:
        """把 scene 级连续性事实写入 prompt。"""
        if not isinstance(scene_continuity, dict) or not scene_continuity:
            return

        stable_facts = scene_continuity.get("stable_facts", {})
        carry_forward_subjects = scene_continuity.get("carry_forward_subjects", [])
        entity_registry = scene_continuity.get("entity_registry", {})
        lines: list[str] = []

        if isinstance(stable_facts, dict):
            label_map = {
                "spatial_layout": "空间布局",
                "prop_states": "道具状态",
                "environment_states": "环境状态",
                "character_states": "角色稳定状态",
            }
            for key, label in label_map.items():
                value = stable_facts.get(key, [])
                if isinstance(value, list):
                    cleaned = [str(item).strip() for item in value if str(item).strip()]
                    for item in cleaned:
                        lines.append(f"  {label}: {item}")

        if isinstance(carry_forward_subjects, list):
            cleaned_subjects = [str(item).strip() for item in carry_forward_subjects if str(item).strip()]
            if cleaned_subjects:
                lines.append(f"  持续继承主体: {', '.join(cleaned_subjects)}")

        if isinstance(entity_registry, dict):
            for entity_id, config in entity_registry.items():
                if not isinstance(config, dict):
                    continue
                count = config.get("count")
                holder = str(config.get("holder", "")).strip()
                persistent_state = str(config.get("persistent_state", "")).strip()
                entity_parts = []
                if count not in (None, ""):
                    entity_parts.append(f"数量={count}")
                if holder:
                    entity_parts.append(f"持有者={holder}")
                if persistent_state:
                    entity_parts.append(f"持续状态={persistent_state}")
                if entity_parts:
                    lines.append(f"  实体注册[{entity_id}]: {'；'.join(entity_parts)}")

        if lines:
            parts.append("")
            parts.append("⚠️ 场景连续性——以下稳定事实需跨镜头保持一致：")
            parts.extend(lines)

    @staticmethod
    def _append_shot_delta(parts: list[str], shot_delta: list[str] | None) -> None:
        """把 shot 级允许变化范围写入 prompt。"""
        if not isinstance(shot_delta, list):
            return
        cleaned = [str(item).strip() for item in shot_delta if str(item).strip()]
        if not cleaned:
            return
        parts.append("")
        parts.append("⚠️ 本镜头变化边界——只允许发生以下变化：")
        for item in cleaned:
            parts.append(f"  {item}")

    @staticmethod
    def _append_director_plan(
        parts: list[str],
        director_plan: dict[str, Any] | None,
        *,
        node_context: dict[str, Any] | None = None,
    ) -> None:
        """把导演层信息写入 prompt。"""
        if not isinstance(director_plan, dict) or not director_plan:
            return

        lines: list[str] = []
        dramatic_core = str(director_plan.get("dramatic_core", "")).strip()
        not_this_shot = str(director_plan.get("not_this_shot", "")).strip()
        if dramatic_core:
            lines.append(f"  戏核: {dramatic_core}")
        if not_this_shot:
            lines.append(f"  非本镜任务: {not_this_shot}")

        viewer_flow = director_plan.get("viewer_information_flow", [])
        if isinstance(viewer_flow, list):
            cleaned_flow = [str(item).strip() for item in viewer_flow if str(item).strip()]
            if cleaned_flow:
                lines.append(f"  信息顺序: {' -> '.join(cleaned_flow)}")

        if isinstance(node_context, dict) and node_context:
            stage_lines: list[str] = []
            story_function = str(node_context.get("story_function", "")).strip()
            visual_focus = str(node_context.get("visual_focus", "")).strip()
            must_show = node_context.get("must_show", [])
            must_not_show = node_context.get("must_not_show", [])
            delta_from_previous = str(node_context.get("delta_from_previous", "")).strip()
            if story_function:
                stage_lines.append(f"当前阶段任务: {story_function}")
            if visual_focus:
                stage_lines.append(f"当前视觉重心: {visual_focus}")
            if isinstance(must_show, list):
                cleaned_show = [str(item).strip() for item in must_show if str(item).strip()]
                if cleaned_show:
                    stage_lines.append(f"必须出现: {', '.join(cleaned_show)}")
            if isinstance(must_not_show, list):
                cleaned_not_show = [str(item).strip() for item in must_not_show if str(item).strip()]
                if cleaned_not_show:
                    stage_lines.append(f"不能提前出现: {', '.join(cleaned_not_show)}")
            if delta_from_previous:
                stage_lines.append(f"相对上一阶段主变化: {delta_from_previous}")
            if stage_lines:
                lines.extend(f"  {line}" for line in stage_lines)

        if lines:
            parts.append("")
            parts.append("⚠️ 导演阶段合同——本镜头必须遵守以下阶段设计：")
            parts.extend(lines)

    @staticmethod
    def _collect_scene_base_lines(
        scene_environment: str,
        scene_lighting: str,
        scene_weather: str,
        scene_props: list[str] | None,
        *,
        atmosphere: str = "",
        physics: str = "",
    ) -> list[str]:
        lines: list[str] = []
        lighting_text = scene_lighting or atmosphere
        if lighting_text:
            lines.append(f"光线：{lighting_text}")
        weather_text = scene_weather or physics
        if weather_text:
            lines.append(f"天气/物理：{weather_text}")
        if scene_props:
            cleaned_props = [str(item).strip() for item in scene_props if str(item).strip()]
            if cleaned_props:
                lines.append(f"道具：{', '.join(cleaned_props)}")
        return lines

    @staticmethod
    def _collect_image_constraint_lines(
        subject_constraints: dict[str, Any] | None,
        scene_continuity: dict[str, Any] | None,
        shot_delta: list[str] | None,
        shot_type: str,
    ) -> list[str]:
        lines: list[str] = []
        cleaned_shot_type = str(shot_type or "").strip()
        if cleaned_shot_type == "transition_reveal":
            lines.append("画面重点是闯入感与未完成认出的阶段，新主体不能完整露出。")
        elif cleaned_shot_type == "offscreen_reaction":
            lines.append("画面重点是可见主体的反应，不表现画外主体本体。")

        return lines

    @staticmethod
    def _is_high_interaction_video_shot(
        character_appearances: list[tuple[str, str]],
        action_description: str,
        motion_control: dict[str, Any] | None = None,
        camera_movement: str = "",
    ) -> bool:
        """判断是否属于需要加强动作逻辑约束的高交互镜头。"""
        if len(character_appearances) < 2:
            return False

        text_parts = [str(action_description or "").lower(), str(camera_movement or "").lower()]
        if isinstance(motion_control, dict):
            phase_beats = motion_control.get("phase_beats", [])
            if isinstance(phase_beats, list):
                text_parts.extend(str(item).lower() for item in phase_beats if str(item).strip())
            for key in ("target", "movement_direction", "screen_trajectory", "distance_to_target"):
                value = motion_control.get(key, "")
                if str(value).strip():
                    text_parts.append(str(value).lower())

        text_blob = " ".join(text_parts)
        interaction_tokens = [
            "fight", "combat", "battle", "attack", "counter", "dodge", "strike", "hit",
            "pounce", "lunge", "chase", "grapple", "clash", "collision", "tackle",
            "打", "打斗", "交锋", "搏斗", "扑", "扑击", "扑向", "闪避", "反击", "追击", "对打",
        ]
        return any(token in text_blob for token in interaction_tokens)

    @staticmethod
    def _append_interaction_negative_rules(
        parts: list[str],
        character_appearances: list[tuple[str, str]],
        action_description: str,
        motion_control: dict[str, Any] | None = None,
        camera_movement: str = "",
        subject_constraints: dict[str, Any] | None = None,
    ) -> None:
        """为多人强交互镜头补充统一的负向规则模板。"""
        if not VideoPromptBuilder._is_high_interaction_video_shot(
            character_appearances=character_appearances,
            action_description=action_description,
            motion_control=motion_control,
            camera_movement=camera_movement,
        ):
            return

        rules = [
            "整个镜头内的动作必须保持为一条连续的因果链。",
            "除非 storyboard 明确要求，否则不要让任何主体中途脱离交互。",
            "不要把同一次连续冲突拍成两个彼此断开的遭遇。",
            "不要在镜头中途无原因重置距离、战场位置或攻击节奏。",
            "不要让任何主体突然朝别的方向跑开，又在没有可见过渡的情况下重新回到交战。",
            "全程保持攻击方与防守方的方向逻辑一致。",
        ]

        continuity_subjects = []
        if isinstance(subject_constraints, dict):
            value = subject_constraints.get("continuity_subjects", [])
            if isinstance(value, list):
                continuity_subjects = [str(item).strip() for item in value if str(item).strip()]
        if continuity_subjects:
            rules.append(
                "以下连续性绑定主体在整个镜头内都必须保持语义一致："
                + ", ".join(continuity_subjects)
                + "。"
            )

        parts.append("")
        parts.append("【交互连续性负向规则】")
        for idx, rule in enumerate(rules, start=1):
            parts.append(f"{idx}. {rule}")

    @staticmethod
    def build_image_prompt(
        style_anchor: str,
        character_appearances: list[tuple[str, str]],
        scene_description: str,
        prop_appearances: list[tuple[str, str]] | None = None,
        motion_control: dict[str, Any] | None = None,
        camera_technical: str = "",
        atmosphere: str = "",
        physics: str = "",
        consistency_anchors: dict[str, Any] | None = None,
        action_hint: str = "",
        # ── Scene 层级环境参数（同场景所有镜头共享）──
        scene_environment: str = "",
        scene_lighting: str = "",
        scene_weather: str = "",
        scene_props: list[str] | None = None,
        scene_continuity: dict[str, Any] | None = None,
        subject_constraints: dict[str, Any] | None = None,
        shot_delta: list[str] | None = None,
        shot_type: str = "",
        director_plan: dict[str, Any] | None = None,
        node_context: dict[str, Any] | None = None,
    ) -> str:
        """构建图片生成 prompt（给 Gemini 用）。

        Args:
            style_anchor: 全局风格锚点
            character_appearances: [(char_id, appearance_text), ...]
            prop_appearances: [(prop_id, prop_description), ...]
            scene_description: 本镜头特有的动作/构图描述（已过滤外貌）
            motion_control: 结构化运动控制字段
            camera_technical: 焦距+光圈
            atmosphere: 光影参数（向下兼容旧 storyboard，scene_lighting 优先）
            physics: 物理细节（向下兼容旧 storyboard，scene_weather 优先）
            consistency_anchors: 一致性锚点 dict
            action_hint: 动作上下文（首帧需要为接下来的动作做好姿态准备）
            scene_environment: 场景环境描述（来自 scene 层，同场景共享）
            scene_lighting: 场景光线参数（来自 scene 层，同场景共享）
            scene_weather: 天气/粒子效果（来自 scene 层，同场景共享）
            scene_props: 场景道具列表（来自 scene 层，同场景共享）
            scene_continuity: scene 级连续性稳定事实
            subject_constraints: shot 级主体语义约束
            shot_delta: 当前镜头允许发生的变化边界
            shot_type: shot 级生成策略类型
        """
        clean_scene = ContentFilter.remove_clothing_descriptions(scene_description)

        constraint_lines = VideoPromptBuilder._collect_image_constraint_lines(
            subject_constraints=subject_constraints,
            scene_continuity=scene_continuity,
            shot_delta=shot_delta,
            shot_type=shot_type,
        )

        # ── 2. 出镜控制：谁出现 / 谁不能完整出现 ──
        visibility_lines: list[str] = []
        if isinstance(subject_constraints, dict):
            for key, label in [
                ("required_visible_subjects", "必须出镜"),
                ("offscreen_subjects", "必须保持画外"),
                ("forbidden_visible_subjects", "禁止完整出镜"),
            ]:
                value = subject_constraints.get(key, [])
                if isinstance(value, list):
                    cleaned = [str(item).strip() for item in value if str(item).strip()]
                    if cleaned:
                        visibility_lines.append(f"{label}：{', '.join(cleaned)}")

        if isinstance(node_context, dict):
            must_show = node_context.get("must_show", [])
            if isinstance(must_show, list):
                cleaned_show = [str(item).strip() for item in must_show if str(item).strip()]
                if cleaned_show:
                    visibility_lines.append(f"必须看见：{', '.join(cleaned_show)}")
            must_not_show = node_context.get("must_not_show", [])
            if isinstance(must_not_show, list):
                cleaned_not_show = [str(item).strip() for item in must_not_show if str(item).strip()]
                if cleaned_not_show:
                    visibility_lines.append(f"不能出现：{', '.join(cleaned_not_show)}")

        # ── 3. 姿态 / 视线 / 构图 / 景别 ──
        frame_state_lines: list[str] = []
        if motion_control:
            for key, label in [
                ("subject_facing", "主体朝向"),
                ("camera_relation", "镜头关系"),
            ]:
                value = str(motion_control.get(key, "")).strip()
                if value:
                    frame_state_lines.append(f"{label}：{value}")
        if isinstance(subject_constraints, dict):
            pose_contract = subject_constraints.get("pose_contract", [])
            if isinstance(pose_contract, str):
                pose_contract = [pose_contract]
            if isinstance(pose_contract, list):
                for item in pose_contract[:2]:
                    text = str(item).strip()
                    if text:
                        frame_state_lines.append(f"姿态合同：{text}")

            gaze_contract = subject_constraints.get("gaze_contract", {})
            if isinstance(gaze_contract, dict):
                for subject_id, config in gaze_contract.items():
                    if not isinstance(config, dict):
                        continue
                    primary_target = str(config.get("primary_target", "")).strip()
                    target_zone = str(config.get("target_zone", "")).strip()
                    gaze_parts = []
                    if primary_target:
                        gaze_parts.append(f"目标={primary_target}")
                    if target_zone:
                        gaze_parts.append(f"区域={target_zone}")
                    if gaze_parts:
                        frame_state_lines.append(f"视线合同[{subject_id}]：{'；'.join(gaze_parts)}")
        if camera_technical:
            frame_state_lines.append(f"镜头参数：{camera_technical}")

        # ── 4. 环境 delta only ──
        env_parts = VideoPromptBuilder._collect_scene_base_lines(
            scene_environment=scene_environment,
            scene_lighting=scene_lighting,
            scene_weather=scene_weather,
            scene_props=scene_props,
            atmosphere=atmosphere,
            physics=physics,
        )

        # ── 1. 这张图要表达什么 ──
        intent_lines: list[str] = []
        if isinstance(node_context, dict):
            story_function = str(node_context.get("story_function", "")).strip()
            visual_focus = str(node_context.get("visual_focus", "")).strip()
            if story_function:
                intent_lines.append(f"这张图要表达：{story_function}")
            if visual_focus:
                intent_lines.append(f"视觉重心：{visual_focus}")
        if not intent_lines:
            intent_lines.append(f"这张图要表达：{clean_scene}")

        # ── 5. 兜底：其余见参考图 ──
        fallback_lines: list[str] = []
        if character_appearances:
            fallback_lines.append("人物外观见角色参考图。")
        if prop_appearances:
            fallback_lines.append("关键道具外观见道具参考图。")
        if env_parts:
            fallback_lines.append("其余场景与视觉风格见场景参考图。")

        parts: list[str] = []
        parts.append("【1. 这张图要表达什么】")
        parts.extend(intent_lines[:2])

        if visibility_lines:
            parts.append("")
            parts.append("【2. 谁出现 / 谁不能完整出现】")
            parts.extend(visibility_lines)

        if frame_state_lines:
            parts.append("")
            parts.append("【3. 姿态 / 视线 / 构图 / 景别】")
            parts.extend(frame_state_lines)
            if clean_scene:
                parts.append(f"当前画面描述：{clean_scene}")

        if env_parts:
            parts.append("")
            parts.append("【4. 这帧特有的环境变化】")
            parts.extend(env_parts)

        if constraint_lines:
            parts.append("")
            parts.append("【附加硬约束】")
            parts.extend(constraint_lines)

        if fallback_lines:
            parts.append("")
            parts.append("【5. 其余见参考图】")
            parts.extend(fallback_lines)

        return "\n".join(parts)

    @staticmethod
    def build_video_prompt(
        style_anchor: str,
        character_appearances: list[tuple[str, str]],
        action_description: str,
        shot_intent: str = "",
        opening_state: str = "",
        target_outcome: str = "",
        time_beats: list[str] | None = None,
        motion_control: dict[str, Any] | None = None,
        camera_movement: str = "",
        consistency_anchors: dict[str, Any] | None = None,
        narration: str = "",
        scene_environment: str = "",
        scene_continuity: dict[str, Any] | None = None,
        subject_constraints: dict[str, Any] | None = None,
        shot_delta: list[str] | None = None,
        shot_type: str = "",
        director_plan: dict[str, Any] | None = None,
        video_references: list[dict[str, Any]] | None = None,
    ) -> str:
        """构建视频生成 prompt（给 Seedance 用）。

        Args:
            character_appearances: [(char_id, appearance_text), ...]
            action_description: 动作描述（已过滤外貌）
            shot_intent: 这个分镜到底要表达什么 / 完成什么叙事任务
            opening_state: 镜头起始状态（通常来自 scene_prompt）
            target_outcome: 镜头结果状态（通常来自 end_frame_description）
            time_beats: 镜头内部时间节拍（仅保留可执行视觉描述）
            motion_control: 结构化运动控制字段
            camera_movement: 运镜方式
            consistency_anchors: 一致性锚点
            narration: 旁白文本（Seedance 音画同轨）
            scene_environment: 场景环境简述（来自 scene 层）
            scene_continuity: scene 级连续性稳定事实
            subject_constraints: shot 级主体语义约束
            shot_delta: 当前镜头允许发生的变化边界
            shot_type: shot 级生成策略类型
        """
        clean_action = ContentFilter.remove_clothing_descriptions(action_description)

        parts = []

        if style_anchor:
            parts.append(f"【全局风格锚点】{style_anchor}")
        style_lock = VideoPromptBuilder.infer_style_medium_lock(style_anchor)
        if style_lock.get("lock_line"):
            parts.append(f"【媒介锁定】{style_lock['lock_line']}")
            parts.append("")

        state_lines: list[str] = []
        if opening_state.strip():
            state_lines.append(f"【起始画面】{opening_state.strip()}")
        if target_outcome.strip():
            state_lines.append(f"【目标结果】{target_outcome.strip()}")
        if state_lines:
            parts.extend(state_lines)
            parts.append("")

        if motion_control:
            mc_lines = []
            for label, key in [
                ("主体朝向", "subject_facing"),
                ("镜头相对主体", "camera_relation"),
                ("运动方向", "movement_direction"),
                ("画面轨迹", "screen_trajectory"),
                ("目标点", "target"),
                ("与目标距离变化", "distance_to_target"),
            ]:
                value = str(motion_control.get(key, "")).strip()
                if value:
                    mc_lines.append(f"{label}: {value}")
            if mc_lines:
                parts.append("【运动结构控制】")
                parts.extend(mc_lines)
                parts.append("")

        if isinstance(time_beats, list):
            cleaned_time_beats = [str(item).strip() for item in time_beats if str(item).strip()]
            if cleaned_time_beats:
                parts.append("【时间节拍】")
                parts.extend(cleaned_time_beats)
                parts.append("")

        # 动作（核心内容）
        prompt_body = f"{camera_movement}, {clean_action}" if camera_movement and clean_action else (clean_action or camera_movement)
        scene_base_lines = VideoPromptBuilder._collect_scene_base_lines(
            scene_environment=scene_environment,
            scene_lighting="",
            scene_weather="",
            scene_props=None,
        )
        if scene_base_lines:
            parts.append("【场景基底】")
            parts.extend(scene_base_lines)
        parts.append(f"【场景动作】{prompt_body}")

        # 规则
        parts.append("")
        parts.append("【重要规则】")
        parts.append("1. 角色外观必须与上述设定完全一致")
        parts.append("2. 如果画面与角色设定冲突，以角色设定为准")

        result = "\n".join(parts)

        # 拼接旁白（Seedance 音画同轨）
        if narration.strip():
            result = f"{result}\n\n旁白：{narration}"

        return VideoPromptBuilder.compose_video_generation_prompt(result, video_references)
