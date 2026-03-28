#!/opt/homebrew/bin/python3.14
"""视频创作数据模型（简化版）。

从 xyz script_models.py 精简而来，去掉数据库依赖，
保留核心数据结构用于 JSON 序列化/反序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional
from datetime import datetime
import uuid


# ==================== 枚举类型 ====================

class ScriptStyle(str, Enum):
    """视觉风格"""
    ANIME = "anime"
    REALISTIC = "realistic"
    CYBERPUNK = "cyberpunk"
    FANTASY = "fantasy"
    THREE_D = "3d"
    CUSTOM = "custom"


class MusicStyle(str, Enum):
    """音乐风格"""
    ORCHESTRAL = "orchestral"
    ELECTRONIC = "electronic"
    ACOUSTIC = "acoustic"
    TENSE = "tense"
    WARM = "warm"
    CUSTOM = "custom"


class TransitionType(str, Enum):
    """转场类型"""
    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"
    WIPE = "wipe"


class StoryTime(str, Enum):
    """故事时间（决定全局光线基调）"""
    DAY = "day"
    DUSK = "dusk"
    NIGHT = "night"
    DAWN = "dawn"


class ContinuityMode(str, Enum):
    """连续性控制模式（由宿主 LLM 在 storyboard 中标注）"""
    STRICT = "strict"        # 强约束：生成尾帧图锚定终点（关键叙事转折、角色状态大变化）
    SCENE_END = "scene_end"  # 默认：仅 scene 末尾 shot 生成尾帧图
    FREE = "free"            # 自由运动：不生成尾帧图，Seedance 自由补全


class ShotStatus(str, Enum):
    """分镜状态"""
    PENDING = "pending"
    AUDIO_READY = "audio_ready"
    VIDEO_READY = "video_ready"
    COMPLETED = "completed"
    FAILED = "failed"


# ==================== 数据模型 ====================

@dataclass
class Character:
    """角色"""
    name: str
    role_type: str = "protagonist"  # protagonist/supporting/extra
    personality: str = ""
    appearance: str = ""            # 详细外貌描述（唯一真相来源）
    default_clothing: str = ""      # 默认服装
    key_features: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Character:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Location:
    """地点（物理空间）"""
    name: str
    description: str = ""           # 详细视觉描述（用于生成背景图）
    environment_type: str = "indoor" # indoor/outdoor/natural/urban/fantasy
    weather: str = "clear"
    atmosphere: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Location:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class DialogueLine:
    """对白"""
    speaker: str                    # 角色名 或 "narrator"
    text: str
    emotion: str = "neutral"
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsistencyAnchors:
    """一致性锚点"""
    characters: list[dict] = field(default_factory=list)  # [{name, must_show, expression}]
    props: list[str] = field(default_factory=list)
    environment: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Keyframe:
    """镜头内关键帧锚点。"""
    timestamp: float
    description: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Keyframe:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class MotionControl:
    """运动结构控制层。"""
    subject_facing: str = ""
    camera_relation: str = ""
    movement_direction: str = ""
    screen_trajectory: str = ""
    target: str = ""
    distance_to_target: str = ""
    phase_beats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionControl:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Shot:
    """分镜"""
    sequence: int
    estimated_duration: int = 8
    visual_description: str = ""        # 静态首帧描述（用于生成图片）
    visual_description_with_details: str = ""  # 注入角色外貌后的完整描述
    action_prompt: str = ""             # 动态动作指令（用于生成视频）
    character_actions: str = ""
    camera: str = "中景"
    dialogue: Optional[dict] = None     # {speaker, text, emotion}
    consistency_anchors: Optional[dict] = None
    subject_constraints: Optional[dict] = None
    shot_type: str = "visible_subject"
    narration: str = ""                # 画外旁白文案（Seedance 音画同轨）
    # PONYO 6D 框架增强字段
    camera_movement: str = ""          # S: 运镜方式（推/拉/摇/跟/环绕/固定）
    camera_technical: str = ""         # S: 镜头参数（"50mm, f/2.8"）
    atmosphere_lighting: str = ""      # A: 光影（"侧光, 5500K, 光比2:1, 自然主义"）
    physics_note: str = ""             # P: 物理细节（布料飘动/尘埃/光影折射等）
    speed_baseline: str = "1.0x"       # E: 动作速度倍率
    transition_out: str = "cut"
    # xyz-video-skill 独有：末帧描述（视觉连续性）
    end_frame_description: str = ""
    end_frame_prompt: str = ""
    # 连续性控制模式（由宿主 LLM 标注）
    continuity_mode: str = "scene_end"  # strict / scene_end / free
    motion_control: Optional[MotionControl] = None
    keyframes: list[Keyframe] = field(default_factory=list)
    # 生成结果
    first_frame_url: Optional[str] = None
    last_frame_url: Optional[str] = None
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    status: str = "pending"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Shot:
        payload = {
            k: v
            for k, v in data.items()
            if k in cls.__dataclass_fields__ and k not in {"keyframes", "motion_control"}
        }
        shot = cls(**payload)
        if isinstance(data.get("motion_control"), dict):
            shot.motion_control = MotionControl.from_dict(data["motion_control"])
        shot.keyframes = [Keyframe.from_dict(item) for item in data.get("keyframes", []) if isinstance(item, dict)]
        return shot


@dataclass
class SceneBlock:
    """场景（叙事单元）"""
    name: str
    sequence: int = 1
    location: str = ""              # 引用地点名
    summary: str = ""
    visual_description: str = ""
    key_moment: str = ""
    emotion_arc: str = ""
    characters_in_scene: list[str] = field(default_factory=list)
    lighting: str = ""
    atmosphere: str = ""
    duration: int = 20
    shots: list[Shot] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["shots"] = [s.to_dict() for s in self.shots]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> SceneBlock:
        shots_data = data.pop("shots", [])
        scene = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        scene.shots = [Shot.from_dict(s) for s in shots_data]
        return scene


@dataclass
class Script:
    """剧本"""
    title: str = "未命名剧本"
    synopsis: str = ""
    total_duration: int = 60
    style: str = "anime"
    music_style: str = "orchestral"
    story_time: str = "day"
    visual_style_anchor: str = ""      # 全局视觉风格锚点（色调/笔触/光影/氛围）
    characters: list[Character] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    scenes: list[SceneBlock] = field(default_factory=list)
    # 元数据
    idea: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["characters"] = [c.to_dict() for c in self.characters]
        d["locations"] = [l.to_dict() for l in self.locations]
        d["scenes"] = [s.to_dict() for s in self.scenes]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Script:
        chars = data.pop("characters", [])
        locs = data.pop("locations", [])
        scenes = data.pop("scenes", [])
        script = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        script.characters = [Character.from_dict(c) for c in chars]
        script.locations = [Location.from_dict(l) for l in locs]
        script.scenes = [SceneBlock.from_dict(s) for s in scenes]
        return script

    @property
    def all_shots(self) -> list[Shot]:
        """获取所有分镜（按场景顺序）"""
        shots = []
        for scene in self.scenes:
            shots.extend(scene.shots)
        return shots

    @property
    def shot_count(self) -> int:
        return sum(len(s.shots) for s in self.scenes)


@dataclass
class Prop:
    """道具"""
    name: str
    description: str = ""
    importance: str = "normal"  # critical/normal
    appears_in_locations: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Prop:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
