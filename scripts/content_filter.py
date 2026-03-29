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
                    "STYLE MEDIUM LOCK: illustrated / painterly cinematic frame. "
                    "All shots must remain in the same illustrated medium. "
                    "Do NOT drift into photorealistic live-action imagery."
                ),
            }
        if any(token in lower for token in three_d_tokens):
            return {
                "medium": "three_dimensional",
                "lock_line": (
                    "STYLE MEDIUM LOCK: stylized 3D / CG cinematic frame. "
                    "All shots must remain in the same 3D-rendered medium. "
                    "Do NOT drift into hand-drawn illustration or live-action photorealism."
                ),
            }
        if any(token in lower for token in photoreal_tokens):
            return {
                "medium": "photorealistic",
                "lock_line": (
                    "STYLE MEDIUM LOCK: photorealistic live-action cinematic frame. "
                    "All shots must remain in the same photoreal medium. "
                    "Do NOT drift into illustration, anime, comic, or painterly rendering."
                ),
            }
        return {
            "medium": "unspecified",
            "lock_line": (
                "STYLE MEDIUM LOCK: choose ONE visual medium for the entire project and keep it identical "
                "across all shots. Do NOT switch between photorealistic, illustrated, anime, comic, or 3D render styles."
            ),
        }

    @staticmethod
    def _append_subject_constraints(parts: list[str], subject_constraints: dict[str, Any] | None) -> None:
        """把 shot 级主体语义约束写入 prompt。"""
        if not isinstance(subject_constraints, dict) or not subject_constraints:
            return

        mapping = [
            ("required_visible_subjects", "Required visible subjects"),
            ("optional_visible_subjects", "Optional visible subjects"),
            ("offscreen_subjects", "Offscreen subjects"),
            ("continuity_subjects", "Continuity-bound subjects"),
            ("forbidden_visible_subjects", "Forbidden visible subjects"),
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
                lines.append(f"  Rule: {rule}")

        if lines:
            parts.append("")
            parts.append("⚠️ SUBJECT CONTRACT — this shot must obey these subject-level constraints:")
            parts.extend(lines)

    @staticmethod
    def _append_shot_type_rules(parts: list[str], shot_type: str) -> None:
        """把 shot 类型对应的通用生成策略写入 prompt。"""
        rules = {
            "visible_subject": [
                "Keep all required visible subjects clearly and continuously in frame.",
                "Do not swap subject identity, species, or count mid-shot.",
            ],
            "offscreen_reaction": [
                "This is a reaction shot. Keep the threat or target OFFSCREEN throughout the shot.",
                "Do not reveal, hallucinate, or partially introduce unseen entities into frame.",
                "Express danger only through gaze, pose, environment, sound implication, wind, dust, or lighting change.",
            ],
            "transition_reveal": [
                "This shot bridges from offscreen implication to onscreen reveal.",
                "If a new entity appears, reveal it gradually and keep identity consistent with later shots.",
            ],
            "free_atmosphere": [
                "This is an atmosphere shot. Prioritize mood and environment continuity over character action.",
            ],
        }
        cleaned = str(shot_type or "").strip()
        if not cleaned:
            return
        selected = rules.get(cleaned, [])
        parts.append("")
        parts.append(f"⚠️ SHOT TYPE — {cleaned}")
        for rule in selected:
            parts.append(f"  Rule: {rule}")

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
            "Keep the action as one continuous causal sequence within this shot.",
            "Do NOT let any subject disengage from the interaction unless the storyboard explicitly requires it.",
            "Do NOT turn one continuous clash into two separate encounters within the same shot.",
            "Do NOT reset distance, battlefield position, or attack cycle mid-shot.",
            "Do NOT send one subject running away in a different direction and then suddenly return to combat without a visible transition.",
            "Preserve attacker-defender directional logic across the full shot.",
        ]

        continuity_subjects = []
        if isinstance(subject_constraints, dict):
            value = subject_constraints.get("continuity_subjects", [])
            if isinstance(value, list):
                continuity_subjects = [str(item).strip() for item in value if str(item).strip()]
        if continuity_subjects:
            rules.append(
                "Keep these continuity-bound subjects semantically consistent throughout the shot: "
                + ", ".join(continuity_subjects)
                + "."
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
        subject_constraints: dict[str, Any] | None = None,
        shot_type: str = "",
    ) -> str:
        """构建图片生成 prompt（给 Gemini 用）。

        Args:
            style_anchor: 全局风格锚点
            character_appearances: [(char_id, appearance_text), ...]
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
            subject_constraints: shot 级主体语义约束
            shot_type: shot 级生成策略类型
        """
        clean_scene = ContentFilter.remove_clothing_descriptions(scene_description)

        parts = []

        # 风格锚点
        if style_anchor:
            parts.append(style_anchor)
        style_lock = VideoPromptBuilder.infer_style_medium_lock(style_anchor)
        if style_lock.get("lock_line"):
            parts.append(style_lock["lock_line"])

        VideoPromptBuilder._append_shot_type_rules(parts, shot_type)
        VideoPromptBuilder._append_subject_constraints(parts, subject_constraints)

        # 角色外观设定（唯一真相来源）
        if character_appearances:
            parts.append("")
            parts.append("⚠️ CHARACTER APPEARANCE — Single Source of Truth, MUST follow strictly:")
            for char_id, appearance in character_appearances:
                parts.append(f"  [{char_id}] {appearance}")

        # 一致性锚点
        if consistency_anchors:
            chars_anchors = consistency_anchors.get("characters", [])
            if chars_anchors:
                anchor_parts = []
                for ca in chars_anchors:
                    cid = ca.get("id", "")
                    must_show = ca.get("must_show", [])
                    expr = ca.get("expression", "")
                    if must_show:
                        anchor_parts.append(f"  [{cid}] MUST SHOW: {', '.join(must_show)}. Expression: {expr}")
                if anchor_parts:
                    parts.append("")
                    parts.append("⚠️ CONSISTENCY ANCHORS — these features MUST be visible:")
                    parts.extend(anchor_parts)

            # 环境锚点（shot 级，向下兼容）
            env_anchors = consistency_anchors.get("environment", [])
            if env_anchors:
                parts.append("")
                parts.append("⚠️ ENVIRONMENT ANCHORS — these elements MUST be present in the scene:")
                parts.append(f"  {', '.join(env_anchors)}")

        # ── 场景环境（scene 层级，同场景所有镜头共享，是视觉基底）──
        env_parts = []
        if scene_environment:
            env_parts.append(f"  Environment: {scene_environment}")
        # scene_lighting 优先，fallback 到旧的 atmosphere 参数
        lighting_text = scene_lighting or atmosphere
        if lighting_text:
            env_parts.append(f"  Lighting: {lighting_text}")
        # scene_weather 优先，fallback 到旧的 physics 参数
        weather_text = scene_weather or physics
        if weather_text:
            env_parts.append(f"  Weather/Physics: {weather_text}")
        if scene_props:
            env_parts.append(f"  Props: {', '.join(scene_props)}")
        if env_parts:
            parts.append("")
            parts.append("⚠️ SCENE ENVIRONMENT — shared by ALL shots in this scene, MUST be consistent:")
            parts.extend(env_parts)

        if motion_control:
            mc_lines = []
            for label, key in [
                ("Subject facing", "subject_facing"),
                ("Camera relation", "camera_relation"),
                ("Movement direction", "movement_direction"),
                ("Screen trajectory", "screen_trajectory"),
                ("Target", "target"),
                ("Distance to target", "distance_to_target"),
            ]:
                value = str(motion_control.get(key, "")).strip()
                if value:
                    mc_lines.append(f"  {label}: {value}")
            phase_beats = motion_control.get("phase_beats", [])
            if isinstance(phase_beats, list) and phase_beats:
                mc_lines.append(f"  Phase beats: {' -> '.join(str(item).strip() for item in phase_beats if str(item).strip())}")
            if mc_lines:
                parts.append("")
                parts.append("⚠️ MOTION CONTROL — MUST preserve these spatial and temporal relations:")
                parts.extend(mc_lines)

        # 本镜头描述（已清洗，不含外貌；只写动作/构图/姿态）
        parts.append("")
        parts.append(f"SHOT: {clean_scene}")

        # 动作上下文（帮助首帧图摆出合适的姿态）
        if action_hint:
            clean_hint = ContentFilter.remove_clothing_descriptions(action_hint)
            parts.append(f"ACTION CONTEXT (the motion that will follow this frame): {clean_hint}")
            parts.append("Pose the characters to naturally lead into this action.")

        # 镜头技术参数
        if camera_technical:
            parts.append(f"TECHNICAL: {camera_technical}")

        # 规则
        parts.append("")
        parts.append("RULES: Character appearance MUST match the settings above exactly. "
                      "Do NOT add or change any clothing, accessories, or body features. "
                      "Do NOT include any text labels or annotations in the image.")

        return "\n".join(parts)

    @staticmethod
    def build_video_prompt(
        style_anchor: str,
        character_appearances: list[tuple[str, str]],
        action_description: str,
        motion_control: dict[str, Any] | None = None,
        camera_movement: str = "",
        consistency_anchors: dict[str, Any] | None = None,
        narration: str = "",
        scene_environment: str = "",
        subject_constraints: dict[str, Any] | None = None,
        shot_type: str = "",
    ) -> str:
        """构建视频生成 prompt（给 Seedance 用）。

        Args:
            character_appearances: [(char_id, appearance_text), ...]
            action_description: 动作描述（已过滤外貌）
            motion_control: 结构化运动控制字段
            camera_movement: 运镜方式
            consistency_anchors: 一致性锚点
            narration: 旁白文本（Seedance 音画同轨）
            scene_environment: 场景环境简述（来自 scene 层）
            subject_constraints: shot 级主体语义约束
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

        if shot_type:
            VideoPromptBuilder._append_shot_type_rules(parts, shot_type)
            parts.append("")
        if subject_constraints:
            VideoPromptBuilder._append_subject_constraints(parts, subject_constraints)
            parts.append("")

        # 角色外观设定
        if character_appearances:
            parts.append("【角色外观设定 - 唯一真相来源】")
            for char_id, appearance in character_appearances:
                # 截断过长描述
                desc = appearance[:300] + "..." if len(appearance) > 300 else appearance
                parts.append(f"【{char_id}】{desc}")
            parts.append("")

        # 一致性锚点
        if consistency_anchors:
            chars_anchors = consistency_anchors.get("characters", [])
            anchor_lines = []
            for ca in chars_anchors:
                cid = ca.get("id", "")
                must_show = ca.get("must_show", [])
                expr = ca.get("expression", "")
                if must_show:
                    anchor_lines.append(f"[{cid}] 必须展示: {', '.join(must_show)}；表情: {expr}")
            # 环境锚点
            env_anchors = consistency_anchors.get("environment", [])
            if env_anchors:
                anchor_lines.append(f"环境要素: {', '.join(env_anchors)}")
            if anchor_lines:
                parts.append("【一致性要素】")
                parts.extend(anchor_lines)
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
            phase_beats = motion_control.get("phase_beats", [])
            if isinstance(phase_beats, list):
                cleaned_beats = [str(item).strip() for item in phase_beats if str(item).strip()]
                if cleaned_beats:
                    mc_lines.append(f"阶段节点: {' -> '.join(cleaned_beats)}")
            if mc_lines:
                parts.append("【运动结构控制】")
                parts.extend(mc_lines)
                parts.append("")

        # 动作（核心内容）
        prompt_body = f"{camera_movement}, {clean_action}" if camera_movement and clean_action else (clean_action or camera_movement)
        if scene_environment:
            parts.append(f"【场景环境】{scene_environment}")
        parts.append(f"【场景动作】{prompt_body}")

        VideoPromptBuilder._append_interaction_negative_rules(
            parts,
            character_appearances=character_appearances,
            action_description=clean_action,
            motion_control=motion_control,
            camera_movement=camera_movement,
            subject_constraints=subject_constraints,
        )

        # 规则
        parts.append("")
        parts.append("【重要规则】")
        parts.append("1. 角色外观必须与上述设定完全一致")
        parts.append("2. 如果画面与角色设定冲突，以角色设定为准")

        result = "\n".join(parts)

        # 拼接旁白（Seedance 音画同轨）
        if narration.strip():
            result = f"{result}\n\n旁白：{narration}"

        return result
