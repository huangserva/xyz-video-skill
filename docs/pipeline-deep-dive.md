# XYZ Video Skill -- 流水线深度推演

本文档从参数传递层级完整推演整个视频生成流水线，覆盖每一步的输入/输出、字段流向、API 调用和决策逻辑。

---

## 全局架构

```
用户输入主题
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 1: 故事创作          LLM 思考 → story.json             │
│  步骤 2: 剧本框架+角色设计  LLM 思考 → framework.json         │
│  步骤 3: 角色参考图         ad_assets.py --mode character_refs │
│  步骤 4: 分镜脚本          LLM 思考 → storyboard.json        │
│  ── Storyboard 规范化 ──   run_pipeline.py normalize         │
│  步骤 5: 素材生成          ad_assets.py --mode assets         │
│  步骤 5.5: 导演审图（可选） LLM 视觉裁定                      │
│  步骤 6: 品牌化（可选）     ad_brand.py                       │
│  步骤 7: 编辑决策+合成     ad_compose.py                      │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
  pipeline_result.json + 多平台 .mp4
```

**编排器 `run_pipeline.py`** 负责串联步骤 3-7，阶段顺序固定为：

```
validate → refs → [normalize] → assets → brand → compose
```

可用 `--from`/`--to` 截取窗口，或 `--skip_*` 跳过单个阶段。

---

## 步骤 1: 故事创作 → `story.json`

### 执行者

宿主 LLM（对话模式，非脚本）

### 输入

用户口头描述的主题/想法

### 输出 `story.json`

```json
{
  "title": "string",
  "synopsis": "string (30字以内)",
  "source_interpretation": "string",
  "narrative": "string (200-400字，完整连贯叙事，后续所有步骤的单一真相源)",
  "story_beats": [
    {
      "beat": "开端|发展|高潮|结尾",
      "description": "string (50-80字，可视化场景描述)",
      "emotion": "string",
      "key_visual": "string (一句话核心画面)",
      "narrative_range": "string (对应 narrative 中的起止句)"
    }
  ],
  "visual_tone": "string (色调/氛围/风格，30-50字)",
  "suggested_duration_per_beat": [8, 10, 12, 10]
}
```

### 关键约束

- `narrative` 是整个视频的故事主线，必须满足：因果链、角色动机、感官细节、环境渐变、连续动作线
- `story_beats` 是 `narrative` 的结构化切分，`narrative_range` 标注对应 narrative 原文
- 所有 beats 的 `narrative_range` 合起来必须完整覆盖 `narrative`

### 下游消费

- `validate_json.py` 校验结构完整性
- `framework.json` 的 `narrative` 字段从此复制或润色
- 后续所有步骤的场景切分、分镜拆解都从 `narrative` 派生

---

## 步骤 2: 剧本框架 + 角色设计 → `framework.json`

### 执行者

宿主 LLM

### 输入

`story.json`

### 输出 `framework.json`

```json
{
  "title": "string",
  "synopsis": "string (50字以内)",
  "narrative": "string (从 story.json 复制或润色)",
  "visual_style_anchor": "string (80-120字，全局视觉风格锚点)",
  "total_duration": 60,
  "story_time": "day|dusk|night|dawn",
  "suggested_characters": [
    {
      "id": "string (英文唯一ID)",
      "name": "string",
      "role_type": "protagonist|antagonist|supporting|extra",
      "personality": "string (20字以内)",
      "appearance": "string (80-150字，极其详细的外貌描述)",
      "default_clothing": "string (50-80字)",
      "key_features": ["string", "string", "string"],
      "ref_description": "string (给图片模型的说明，可用英文)"
    }
  ],
  "suggested_locations": [
    {
      "name": "string",
      "description": "string (100字)",
      "environment_type": "indoor|outdoor|natural|urban|fantasy"
    }
  ],
  "scenes": [
    {
      "name": "string",
      "location": "string (引用 suggested_locations.name)",
      "narrative_segment": "string (引用 narrative 原文)",
      "summary": "string (30字以内)",
      "visual_description": "string (50-100字)",
      "characters_in_scene": ["character_id"],
      "emotion_arc": "string",
      "duration": 20
    }
  ]
}
```

### 关键决策

| 决策点 | 规则 |
|---|---|
| 场景切分 | 光线质变 / 天气变化 / 地点变化 / 时间跨度 → 新场景 |
| 角色外貌 | 必须写到"看完描述就能画出来"，包含形体、颜色方案、材质纹理、3个识别标记 |
| `narrative_segment` | 直接引用 narrative 原文，所有 scenes 合起来完整覆盖 narrative |

### 下游消费

- `ad_assets.py --mode character_refs` 读取 `suggested_characters` 生成参考图
- `storyboard.json` 的 `characters`/`scenes` 从此继承
- `visual_style_anchor` 贯穿所有图片/视频 prompt 的风格锚定

### 校验规则 (`validate_json.py`)

- 必填字段：`title`, `synopsis`, `narrative`, `visual_style_anchor`, `total_duration`, `story_time`
- 角色 ID 唯一性检查
- 场景 `characters_in_scene` 交叉引用角色 ID
- 场景 `location` 交叉引用地点名

---

## 步骤 3: 角色参考图生成

### 执行者

`ad_assets.py --mode character_refs`

### CLI 参数

```bash
python3 ad_assets.py \
  --mode character_refs \
  --framework {output_dir}/framework.json \
  --output_dir {output_dir}/character_refs \
  [--character_id blue_mecha]    # 可选，只重新生成单个角色
  [--image_width 1024]
  [--image_height 1024]
  [--no_api]                     # 生成占位图
  [--verbose]
```

### 内部流程

```
framework.json
  │
  ├─ 读取 suggested_characters[]
  ├─ 读取 visual_style_anchor → 推断 medium lock (illustrated/3D/photorealistic)
  │
  ▼ 对每个角色:
  │
  ├─ 1. 净化 appearance/clothing 文本 (去掉动作短语/武器/场景元素)
  ├─ 2. 构建英文 ref prompt:
  │     "Single-character cinematic reference portrait of {name}."
  │     + medium lock line
  │     + style anchor
  │     + appearance + clothing + key_features
  │     + 约束 (neutral background, no props, full-body, no text)
  │
  ├─ 3. API 调用:
  │     Provider: apimart (Gemini 3.1 Flash Image)
  │     Endpoint: POST /chat/completions
  │     Payload: { model, messages: [{role: "user", content: prompt}] }
  │     → 返回 base64 PNG → 保存为 ref_{character_id}.png
  │
  │     失败 → fallback: POST /images/generations
  │     再失败 → 生成灰色占位图
  │
  └─ 4. 输出: character_refs.json
```

### 输出 `character_refs/character_refs.json`

```json
{
  "character_ref_dir": "/path/to/character_refs",
  "characters": {
    "character_id": {
      "ref_image": "ref_character_id.png",
      "path": "/absolute/path/to/ref_character_id.png",
      "provider": "apimart",
      "ref_description": "string (从 framework 复制)"
    }
  }
}
```

### Provider 配置 (providers.yaml → models.image)

```yaml
image:
  fallback_chain: [apimart, fal]
  apimart:
    model: "gemini-3.1-flash-image-preview"
    timeout: 180
  fal:
    num_inference_steps: 4
    timeout: 180
```

### API 凭据解析优先级 (utils.get_api_credentials)

1. 环境变量: `APIMART_API_KEY`, `FAL_KEY`, `VOLCENGINE_API_KEY` 等
2. 配置文件: `config/api_keys.yaml` (或 `$AD_GENERATOR_CONFIG`)
3. `providers.yaml` 中的 `api_base` 作为 fallback

---

## 步骤 4: 分镜脚本 → `storyboard.json`

### 执行者

宿主 LLM（依据 `SKILL.md` + `templates/storyboard_prompt.md` 中的规则）

### 输入

- `framework.json`
- `character_refs/` 中的参考图（人工确认后）
- 参考文档: `reference/video_generation_strategy.md`, `reference/cross_shot_continuity.md`

### 输出 `storyboard.json` 核心结构

```json
{
  "title": "string",
  "total_duration": 70,
  "character_ref_dir": "{output_dir}/character_refs",
  "bgm_style": "string",
  "narrative": "string (从 framework 复制)",
  "style_anchor": "string (从 framework.visual_style_anchor 复制)",
  "characters": {
    "character_id": {
      "ref_image": "ref_{id}.png",
      "ref_description": "string",
      "appearance": "string",
      "key_features": ["string"],
      "weapon": "string (可选)"
    }
  },
  "prop_refs": {
    "prop_id": {
      "ref_description": "string",
      "appearance": "string",
      "ref_image": "string (可选)"
    }
  },
  "scenes": [
    {
      "id": "scene_1",
      "name": "string",
      "location": "string",
      "narrative_segment": "string (引用 narrative 原文)",
      "lighting": "string (中文，色温/方向/质感)",
      "weather": "string (中文)",
      "props": ["string"],
      "environment_description": "string (中文，80-150字)",
      "scene_continuity": { ... },
      "shots": [ ... ]
    }
  ]
}
```

### Shot 完整字段清单

```json
{
  "id": 1,
  "characters_in_shot": ["character_id"],
  "props_in_shot": ["prop_id"],
  "narrative_segment": "string (从 scene.narrative_segment 进一步切分)",

  // ── 导演设计层 (复杂 shot 必填) ──
  "director_plan": {
    "dramatic_core": "string (这镜只完成什么)",
    "not_this_shot": "string (这镜明确不完成什么)",
    "viewer_information_flow": ["先知道什么", "再知道什么", "落在哪里"],
    "camera_intent": {
      "shot_purpose": "观察|揭示|压迫|跟随|等待",
      "framing_base": "string",
      "camera_contract": "string"
    },
    "stable_subjects": ["string"],
    "changing_subjects": ["string"],
    "invariants": ["string"],
    "allowed_progressions": ["string"],
    "nodes": [
      {
        "id": "n1",
        "story_function": "string",
        "visual_focus": "string",
        "must_show": ["string"],
        "must_not_show": ["string"],
        "delta_from_previous": "string"
      }
    ]
  },

  // ── 故事三段式 ──
  "scene_prompt": "string (中文，起始画面状态)",
  "action_prompt": "string (中文，运动过程)",
  "end_frame_description": "string (中文，结束画面状态)",

  // ── 镜头参数 ──
  "camera_movement": "string",
  "camera_technical": "string (焦距+光圈)",
  "speed_baseline": "1.0x",
  "estimated_duration": 8,

  // ── 叙事 ──
  "narration": "string",
  "tts_text": "string",
  "subtitle": "string",

  // ── 生成策略字段 ──
  "shot_type": "visible_subject|offscreen_reaction|transition_reveal|free_atmosphere",
  "chain_from_previous": false,
  "continuity_mode": "strict|scene_end|free",
  "reference_strategy": "single_anchor|anchor_with_end|anchor_with_keyframes|anchor_keyframes_end",

  // ── 时间节拍 ──
  "time_beats": ["0-2s: ...", "2-4s: ...", "4-6s: ..."],

  // ── 用途驱动参考素材 ──
  "video_references": [
    {
      "source_type": "frame|character|prop|scene|style|stage|file",
      "source_id": "string",
      "usage": "first_frame|reference_character|reference_prop|reference_composition|reference_style|reference_stage|reference_target_state|reference_motion",
      "subject": "string (可选)",
      "stage": "string (可选)",
      "description": "string (可选)"
    }
  ],

  // ── 关键帧 (兼容字段，映射为 reference_stage) ──
  "keyframes": [
    { "timestamp": 3.0, "description": "string", "stage": "string", "goal": "string" }
  ],

  // ── 结构化约束 ──
  "subject_constraints": {
    "required_visible_subjects": ["character_id"],
    "optional_visible_subjects": ["character_id"],
    "offscreen_subjects": [],
    "continuity_subjects": ["character_id"],
    "forbidden_visible_subjects": [],
    "semantic_rules": ["string"],
    "pose_contract": ["string"],
    "gaze_contract": {
      "character_id": {
        "primary_target": "string",
        "target_zone": "string"
      }
    }
  },
  "shot_delta": ["本镜头只允许发生的变化"],
  "motion_control": {
    "subject_facing": "away_from_camera|toward_camera|left_profile|...",
    "camera_relation": "rear_three_quarter|front_of_subject|side_follow_left|...",
    "movement_direction": "upstairs|toward_target|static|...",
    "screen_trajectory": "lower_right_to_upper_left|left_to_right|...",
    "target": "string",
    "distance_to_target": "getting_closer|getting_farther|holding_position",
    "phase_beats": ["string", "string", "string"]
  },

  // ── 衔接 ──
  "transition_in": { "type": "cross-dissolve", "duration": 0.5 },
  "consistency_anchors": {
    "characters": [{ "id": "string", "must_show": ["string"], "expression": "string" }],
    "environment": ["string"]
  }
}
```

### `scene_continuity` 结构

```json
{
  "stable_facts": {
    "spatial_layout": ["稳定空间关系"],
    "prop_states": ["稳定道具状态"],
    "environment_states": ["稳定环境状态"],
    "character_states": ["稳定角色状态"]
  },
  "entity_registry": {
    "prop_id": {
      "count": 1,
      "holder": "character_id.right_hand",
      "persistent_state": "string"
    }
  },
  "carry_forward_subjects": ["character_id", "prop_id"]
}
```

### 核心设计决策流

```
对每个 shot:
  │
  ├─ 1. 是否需要强导演模式?
  │     条件: transition_reveal / 强动作 / 情绪转折 / chain_from_previous / keyframes >= 2
  │     → 是: 先写 director_plan，再投影到字段
  │     → 否: 直接轻量写 scene_prompt + action_prompt + end_frame_description
  │
  ├─ 2. 参考图策略判断 (4 维度)
  │     ├─ 动作复杂度 (low/medium/high)
  │     ├─ 状态变化幅度
  │     ├─ 起止姿态约束强度
  │     └─ 与前后镜头的衔接依赖
  │     → 决定需要哪些 video_references 用途
  │
  ├─ 3. shot_type 分类
  │     ├─ visible_subject:    主体在画面中
  │     ├─ offscreen_reaction: 反应方在画面中，动作方不在
  │     ├─ transition_reveal:  从无到有揭示主体
  │     └─ free_atmosphere:    纯氛围空镜
  │
  ├─ 4. continuity_mode 选择
  │     ├─ strict:    强制生成尾帧 (情绪转折/状态大变化)
  │     ├─ scene_end: 仅场景末尾生成尾帧 (默认)
  │     └─ free:      不生成尾帧约束 (氛围空镜)
  │
  └─ 5. 分镜拆分 3x3 法则
        ├─ 建立 (慢 0.7x): 2-3 镜
        ├─ 冲突 (快 1.0-1.2x): 2-3 镜
        └─ 结局 (慢 0.7x): 1-2 镜
        每镜 5-12 秒，每镜只做一个动作
```

---

## Storyboard 规范化 (run_pipeline.py 内部)

### 触发时机

在 `refs` 阶段完成后、`assets` 阶段开始前自动执行。

### 三步规范化

#### 1. 绑定角色参考图

从 `character_refs.json` 读取每个角色的 `ref_image` 和 `path`，写入 storyboard 的 `characters` 字典。

#### 2. 回填 `shot_type` (对 legacy storyboard)

对缺少 `shot_type` 的 shot，按以下规则推断：

```
continuity_mode == "free"
  → free_atmosphere

subject_constraints.offscreen_subjects 非空
  → offscreen_reaction

subject_constraints.continuity_subjects + required_visible_subjects 都非空
  → transition_reveal

其他
  → visible_subject (默认)
```

#### 3. 规范化 / 推断 `video_references`

**已有 `video_references`:** 规范化 usage 别名：

| 原始 usage | 规范化 usage |
|---|---|
| `last_frame` | `reference_target_state` |
| `keyframe` | `reference_stage` |
| 其他已知值 | 保持不变 |

**缺少 `video_references`:** 自动推断：

```
默认参考列表:
  ├─ first_frame (frame)
  ├─ reference_composition (scene)
  ├─ reference_character × N (从 characters_in_shot)
  ├─ reference_prop × N (从 props_in_shot)
  ├─ reference_stage × N (从 keyframes)
  └─ reference_target_state (如果 continuity_mode==strict 或 scene_end+最后一镜)
```

### 输出

- `_normalized/storyboard.json` — 规范化后的 storyboard，后续所有阶段使用此文件
- `_normalized/ref_binding_report.json` — 角色参考图绑定报告
- `_normalized/storyboard_migration_report.json`:

```json
{
  "storyboard": "原始路径",
  "normalized_storyboard": "规范化路径",
  "summary": {
    "shot_count": 8,
    "shot_type_backfilled_count": 3,
    "video_references_backfilled_count": 5,
    "video_references_normalized_count": 2
  },
  "shot_migrations": [
    {
      "shot_id": 1,
      "scene_id": "scene_1",
      "original_shot_type": null,
      "final_shot_type": "visible_subject",
      "subject_constraints_present": true,
      "original_video_references_present": false,
      "notes": ["backfilled shot_type: visible_subject (fallback default)"]
    }
  ]
}
```

---

## 步骤 5: 素材生成

### 执行者

`ad_assets.py --mode assets`

### CLI 参数

```bash
python3 ad_assets.py \
  --mode assets \
  --storyboard {output_dir}/_normalized/storyboard.json \
  --output_dir {output_dir}/assets \
  --parallel 4 \
  --image_width 1024 \
  --image_height 1024 \
  [--review_mode metrics_only|hybrid_judge|director_review] \
  [--video_only]    # 跳过图片生成，直接用已有图片 \
  [--no_api]        # 生成占位素材 \
  [--verbose]
```

### 全局初始化

```
storyboard.json
  │
  ├─ 读取 characters{} → 角色外貌(单一真相源) + ref_image 路径
  ├─ 读取 prop_refs{} → 道具外貌 + ref_image 路径
  ├─ 读取 style_anchor → 风格锚点
  ├─ 读取 narrative → 完整叙事
  ├─ 读取 director_prompts (inline 或 director_prompts_file)
  │
  ├─ providers.yaml → 加载 video/image/bgm/llm/vision_judge 配置
  └─ api_keys.yaml / 环境变量 → 加载 API 凭据
```

### 全局 Prompt 提取

在逐镜生成之前，先做全局 prompt 提取：

```
将完整 narrative + 所有 shots 发给 LLM
  │
  ├─ Provider: apimart (Gemini 2.5 Flash)
  ├─ Endpoint: POST /chat/completions
  ├─ 目的: 为每个 shot 提取连贯的 first_frame / last_frame / video_action prompt
  │
  ├─ 成功: 使用 LLM 提取的 prompt (保证全局叙事连贯性)
  ├─ 失败: fallback 到 storyboard 原始字段
  └─ 跳过: 如果所有 shot 都有 director_prompts
```

### 逐场景 → 逐镜头生成

```
对每个 scene:
  │
  ├─ 1. 生成场景环境图 (纯环境，无角色)
  │     用途: 作为 reference_composition 参考
  │
  ├─ 提取 scene_context:
  │   { environment_description, lighting, weather, props, scene_continuity }
  │
  └─ 对每个 shot:
       │
       ├─ 解析 shot contract (优先级: director_plan.nodes > LLM提取 > 原始字段)
       │
       ├─ A. 图片生成 ────────────────────────────────────────
       │   │
       │   ├─ 构建 image prompt (VideoPromptBuilder.build_image_prompt):
       │   │   【1. 这张图要表达什么】 ← node_context / scene_description (已过滤外貌)
       │   │   【2. 谁出现/谁不能出现】← subject_constraints
       │   │   【3. 姿态/视线/构图/景别】← motion_control + pose/gaze_contract
       │   │   【4. 环境变化】← scene lighting/weather/props
       │   │   【附加硬约束】← shot_type 特定规则
       │   │   【5. 其余见参考图】← fallback
       │   │
       │   ├─ 收集参考图绑定:
       │   │   ├─ 场景环境图
       │   │   ├─ 风格参考图 (跨场景传递)
       │   │   ├─ 角色参考图 × N
       │   │   └─ 道具参考图 × N
       │   │
       │   ├─ 如果 chain_from_previous: 提取上一镜视频最后一帧作为首帧
       │   │
       │   ├─ API 调用 (fallback 链):
       │   │   ├─ apimart (Gemini multimodal): POST /chat/completions
       │   │   │   payload: { model, messages: [{ role: "user", content: [text + image_url × N] }] }
       │   │   ├─ volcengine (Seedream): POST /images/generations
       │   │   │   payload: { model, prompt, image_urls: [base64...], size, response_format }
       │   │   ├─ fal (Flux): POST /fal-ai/flux-1/dev/image-to-image
       │   │   │   payload: { prompt, num_inference_steps, num_images }
       │   │   └─ placeholder: 灰色图片
       │   │
       │   └─ 每个 provider: max_retries 次重试 → 下一个 provider
       │
       ├─ A.1 导演审图 (review_mode == director_review)
       │   ├─ 导出 review_context.json + 图片
       │   ├─ 等待 director_judge_result.json (keep / regenerate + adjustment_prompt)
       │   └─ regenerate: 按 adjustment_prompt 修改 prompt 重新生成，最多 max_retries 次
       │
       ├─ A.2 关键帧图片 (如有 director_plan.nodes 中间节点 或 keyframes)
       │   └─ 同样的 build_image_prompt 流程，但带 per-node context
       │
       ├─ A.3 尾帧图片 (根据 continuity_mode)
       │   ├─ strict → 总是生成
       │   ├─ scene_end + 场景最后一镜 → 生成
       │   └─ free → 跳过
       │
       ├─ B. 视频参考素材组织 ──────────────────────────────────
       │   │
       │   ├─ 默认参考顺序:
       │   │   first_frame → scene_ref → style_ref → character_refs → prop_refs
       │   │   → keyframe_stage_refs → target_state (尾帧)
       │   │
       │   ├─ 如果 shot 声明了 video_references[]:
       │   │   按 source_type 解析实际文件路径:
       │   │   - frame/first_frame → 首帧图片
       │   │   - frame/target_state → 尾帧图片
       │   │   - character → character_refs/ref_{id}.png
       │   │   - prop → prop_refs/{id}.png
       │   │   - scene → 场景环境图
       │   │   - style → 风格参考图
       │   │   - stage → 关键帧图片
       │   │   - file → 直接文件路径
       │   │
       │   ├─ 按 usage 优先级排序:
       │   │   first_frame(0) > reference_character(1) > reference_prop(2)
       │   │   > reference_composition(3) > reference_style(4)
       │   │   > reference_stage(5) > reference_target_state(6) > reference(99)
       │   │
       │   └─ 截断到 max_reference_images (Seedance 2.0: 最多9张)
       │
       ├─ C. 视频 Prompt 构建 ─────────────────────────────────
       │   │
       │   ├─ 构建 video prompt (VideoPromptBuilder.build_video_prompt):
       │   │   【全局风格锚点】← style_anchor
       │   │   【媒介锁定】← infer_style_medium_lock() 推断
       │   │   【起始画面】← scene_prompt (已过滤外貌)
       │   │   【目标结果】← end_frame_description
       │   │   【运动结构控制】← motion_control 6个字段
       │   │   【时间节拍】← time_beats
       │   │   【场景基底 + 场景动作】← camera_movement + action_prompt
       │   │   【重要规则】← 角色忠实度规则
       │   │   旁白：← narration
       │   │
       │   ├─ 包装参考素材调用 (compose_video_generation_prompt):
       │   │   【参考素材调用】
       │   │     @图片1 作为首帧。
       │   │     @图片2 参考角色（武松）。
       │   │     @图片3 参考道具（虎骨棒）。
       │   │     @图片4 参考构图。
       │   │   【生成要求】
       │   │     {完整 video prompt}
       │   │
       │   └─ 额外 guardrails:
       │       - 多人强交互 shot: 追加 6 条 interaction_negative_rules
       │       - offscreen_reaction: 追加不可见主体规则
       │       - transition_reveal: 追加连续性规则
       │
       ├─ D. 视频生成 ─────────────────────────────────────────
       │   │
       │   ├─ Provider: volcengine (Seedance 2.0)
       │   ├─ Model: doubao-seedance-2-0-fast-260128
       │   │
       │   ├─ Submit: POST {api_base}/contents/generations/tasks
       │   │   payload: {
       │   │     model: "doubao-seedance-2-0-fast-260128",
       │   │     content: [
       │   │       { type: "text", text: "完整 video prompt" },
       │   │       { type: "reference_image", image_url: { url: "data:image/png;base64,..." } },
       │   │       { type: "reference_image", image_url: { url: "data:image/png;base64,..." } },
       │   │       ...
       │   │     ],
       │   │     duration: 8,          # 夹到 [min_duration, max_duration]
       │   │     ratio: "16:9",
       │   │     resolution: "720p",
       │   │     generate_audio: true,
       │   │     watermark: false
       │   │   }
       │   │   → 返回 task_id
       │   │
       │   ├─ Poll: GET {api_base}/contents/generations/tasks/{task_id}
       │   │   间隔: 5秒, 最多 120 次 (10分钟)
       │   │   → succeeded: 下载视频 URL → 保存为 shot_{id}_video.mp4
       │   │   → failed/cancelled: 重试或 fallback
       │   │
       │   ├─ Fallback: byteplus (Seedance 1.5 Pro) → 无视频 (静态图 fallback)
       │   └─ 重试: max_retries=2, retry_delay=5s
       │
       ├─ E. 视频质量审查 ─────────────────────────────────────
       │   (详见下方"视频质量审查"章节)
       │
       └─ F. BGM 生成 (整个项目一次)
           ├─ Provider: fal (MiniMax Music v2)
           ├─ Endpoint: POST /fal-ai/minimax-music/v2
           ├─ Payload: { prompt: "{bgm_style}, instrumental, no vocals" }
           ├─ Poll: 间隔 1秒, 最多 120 次
           └─ Fallback: 静默 WAV
```

### 输出 `assets/assets.json`

```json
{
  "output_dir": "string",
  "images": {
    "1": { "shot_id": 1, "path": "string", "prompt": "string", "provider": "string" },
    "2": { ... }
  },
  "videos": {
    "1": { "shot_id": 1, "path": "string", "provider": "string", "duration": 8 },
    "2": { ... }
  },
  "shot_prompts": {
    "1": { "image_prompt": "string", "video_prompt": "string" }
  },
  "shot_references": {
    "1": [
      { "usage": "first_frame", "path": "string", "mention": "@图片1" },
      { "usage": "reference_character", "subject": "wusong", "path": "string", "mention": "@图片2" }
    ]
  },
  "bgm": { "path": "string", "provider": "string" },
  "pending_reviews": []
}
```

### Prompt 落盘

- `assets/prompts/shot_{id}_video_prompt.txt` — 每个 shot 的最终视频 prompt
- `assets/image_audit/shot_{id}/reference_bundle/video_prompt.txt` — 参考图审查时的 prompt 副本

---

## 视频质量审查 (步骤 5 内部)

### 阶段 1: 规则粗筛 (`_scan_video_quality`)

```
输入: video.mp4, camera_movement, expected_character_count, expected_subject_facing
  │
  ├─ 1. 选择质量 profile:
  │     camera_movement 含 static/still → static
  │     camera_movement 含 pan/tilt/track/zoom → medium_motion
  │     camera_movement 含 whip/crash/handheld → heavy_motion
  │
  ├─ 2. 帧提取: ffmpeg → 160px 宽缩略图
  │
  ├─ 3. MSE 分析:
  │     ├─ 逐帧 RGB 像素 MSE
  │     ├─ baseline = 前 1/4 帧的中位数 MSE
  │     ├─ threshold = max(threshold_base, median × threshold_multiplier)
  │     ├─ 滑窗平滑 (size=5)
  │     ├─ 反向扫描: 找最后稳定点 (N 帧连续低于阈值)
  │     └─ 正向扫描: 找首个持续异常段
  │
  ├─ 4. 闪烁检测: MSE 方向翻转 ≥ 8 次 → flicker
  │
  ├─ 5. Spike 检测: 局部均值 × spike_factor 且超过 spike_abs_min → 突变
  │
  ├─ 6. 语义审计:
  │     ├─ OpenCV DNN SSD 人脸检测 (res10_300x300_ssd)
  │     │   → 人脸置信度追踪 → 骤降 = face_deformation
  │     │
  │     ├─ HOG 人体检测
  │     │   → 重复身体检测 (相似度 ≥ 0.70) = identity_hallucination
  │     │
  │     ├─ 人脸裁切对比:
  │     │   → 灰度 32×32 absdiff = duplicate_score
  │     │   → 48×48 灰度 + 颜色直方图相关 (0.6/0.4) = identity_score
  │     │   → 连续低 identity_score = face_identity_drift
  │     │
  │     └─ Haar 级联朝向检测 (正面/侧面)
  │         → 朝向翻转 + 低 identity = head_body_inconsistency
  │
  ├─ 7. 风险片段合并: gap_tolerance = 0.35s
  │
  └─ 输出: {
       ok, needs_regeneration, trim_to, duration, fps, profile, trigger,
       analysis: { baseline_mse, threshold, first_anomaly_frame, ... },
       bad_segments, cut_segments, risk_segments
     }
```

### 质量 profile 阈值

| 参数 | static | medium_motion | heavy_motion |
|---|---|---|---|
| threshold_base | 600 | 900 | 1400 |
| threshold_multiplier | 3.0 | 5.0 | 7.0 |
| spike_factor | 4.0 | 5.0 | 6.0 |
| spike_abs_min | 800 | 1200 | 1800 |
| motion_ramp_exempt | false | true | true |

### 阶段 2: LLM 视觉裁定 (review_mode == hybrid_judge)

```
risk_segments 存在时:
  │
  ├─ 导出 vision_bundle:
  │   assets/audit/shot_{id}/vision_bundle_attempt_{N}/
  │     ├─ frame_XXXX.png × N (风险帧图片)
  │     └─ vision_judge_request.json (判断问题 + 参考信息)
  │
  ├─ 流水线暂停，等待裁定
  │
  └─ 裁定结果 vision_judge_result.json:
     {
       "shot_id": "001",
       "segments": [{
         "start": 2.5, "end": 3.0,
         "issue_type": "identity_hallucination",
         "severity": "high",
         "confidence": 0.95,
         "action": "keep|cut_segment|regenerate",
         "reason": "string"
       }],
       "overall_action": "keep|cut_segment|regenerate",
       "fallback_used": false
     }
```

### 审查状态机

```
pending → audited → pending_judgment → judged → applied → finalized
```

### 重试逻辑

- 质量不合格: 最多 3 次尝试 (2 次重试)
- 全部失败: 保留 best-effort 结果
- hybrid_judge 中 vision judge 也可触发 regenerate
- 任何 `pending_judgment` → 整个流水线暂停，`pipeline_result.json` 标记 `status: "pending_judgment"`

---

## 步骤 6: 品牌化 (可选)

### 触发条件

`run_pipeline.py` 中 `should_brand()` 为 true（任何品牌参数非默认值）。

### CLI 参数

```bash
python3 ad_brand.py \
  --assets_manifest {output_dir}/assets/assets.json \
  --storyboard_file {output_dir}/_normalized/storyboard.json \
  --brand_color '#FF6A00' \
  --watermark_text 'Brand Protected' \
  --title_text '品牌广告' \
  --output_dir {output_dir}/brand \
  [--logo_path /path/to/logo.png] \
  [--product_image /path/to/product.png] \
  [--product_shots "3,5"]
```

### 处理流程 (对每张图片)

```
原始图片
  │
  ├─ 1. 顶部标题条: 品牌色半透明矩形 (10% 高, alpha 180) + 白字 title_text
  ├─ 2. 底部字幕条: 黑色半透明矩形 (12% 高, alpha 160) + 品牌色字 subtitle (≤40字)
  ├─ 3. 水印铺满: 对角线重复 watermark_text (alpha 28)
  ├─ 4. 产品图: 左下角 22% 缩放 (仅 product_shots 指定的镜头)
  └─ 5. Logo: 右下角 16% 缩放 (每张图)
  │
  额外生成:
  ├─ intro.png: 品牌色背景 + "品牌开场" (1080×1920)
  └─ outro.png: 品牌色背景 + "立即行动" (1080×1920)
```

### 输出 `brand/brand_manifest.json`

复制 assets manifest 结构，替换图片路径为品牌化版本，添加:

```json
{
  "branding": {
    "generated_at": "string",
    "brand_color": "#FF6A00",
    "logo": "string|null",
    "watermark_text": "string",
    "intro_frame": "brand/intro.png",
    "outro_frame": "brand/outro.png"
  }
}
```

---

## 步骤 7: 编辑决策 + 视频合成

### 执行者

`ad_compose.py`

### CLI 参数

```bash
python3 ad_compose.py \
  --storyboard {output_dir}/_normalized/storyboard.json \
  --assets {output_dir}/brand/brand_manifest.json \   # 或 assets/assets.json
  --output_dir {output_dir}/videos \
  --platform douyin wechat youtube \
  [--edit_judgments /path/to/edit_judgments.json] \
  [--verbose]
```

### Pair-Level 编辑决策 (`build_edit_decisions`)

```
对每对相邻镜头 (prev_shot, next_shot):
  │
  ├─ 信号采集:
  │   ├─ same_scene: _scene_id 是否相同
  │   ├─ chain_from_previous: next_shot 的 chain_from_previous 字段
  │   ├─ visual_overlap_score: prev 尾帧 vs next 首帧 MSE (仅同场景+chain)
  │   │   阈值: OVERLAP_SCORE_THRESHOLD = 0.30
  │   ├─ shared_characters: 交集
  │   ├─ exclusive_characters: 各自独有的角色
  │   └─ 文本 token 检测: reaction tokens ("听","reacts"...) / impact tokens ("猛然","slam"...)
  │
  ├─ Pair 类型分类:
  │   ┌──────────────────────────┬─────────┬──────────────┬──────────────────────┐
  │   │ pair_type                │ 条件     │ confidence   │ 默认转场              │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ same_moment_overlap      │ 同场景   │ 0.82+0.15×  │ straight-cut, 0s     │
  │   │                          │ +chain   │ overlap      │                      │
  │   │                          │ +overlap │              │                      │
  │   │                          │ ≥0.30   │              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ reverse_shot_same_scene  │ 同场景   │ 0.88         │ straight-cut, 0s     │
  │   │                          │ +共享角色│              │                      │
  │   │                          │ +各有独有│              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ reaction_cut             │ 同场景   │ 0.85         │ straight-cut, 0s     │
  │   │                          │ +reaction│              │                      │
  │   │                          │ tokens   │              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ impact_cut               │ 同场景   │ 0.84         │ straight-cut, 0s     │
  │   │                          │ +impact  │              │                      │
  │   │                          │ tokens   │              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ continuous_action        │ 同场景   │ 0.82         │ straight-cut, 0s     │
  │   │ _same_scene              │ (fallback│              │                      │
  │   │                          │ )        │              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ scene_transition_hard    │ 不同场景 │ 0.90         │ straight-cut, 0s     │
  │   │                          │ +impact  │              │                      │
  │   │                          │ tokens   │              │                      │
  │   ├──────────────────────────┼─────────┼──────────────┼──────────────────────┤
  │   │ scene_transition_soft    │ 不同场景 │ 0.90-0.92    │ cross-dissolve, 0.25s│
  │   └──────────────────────────┴─────────┴──────────────┴──────────────────────┘
  │
  ├─ 裁边 (Trim):
  │   ├─ same_moment_overlap: MSE 重叠检测 → trim_out / trim_in
  │   │   (MSE < 1200 → 计算重叠窗口，最小 fallback: trim_out=0.12, trim_in=0.08)
  │   └─ 其他: trim_out=0, trim_in=0
  │
  ├─ 显式转场覆盖 (shot.transition_in):
  │   ├─ same_moment_overlap: 不允许覆盖
  │   ├─ reverse/reaction/impact: 仅 straight-cut
  │   ├─ continuous_action: straight-cut 或 cross-dissolve
  │   └─ scene_transition: 任意类型
  │
  └─ edit_judgments 覆盖 (--edit_judgments):
     输入格式: [{ "from_shot": 2, "to_shot": 3, "transition_type": "flash-white", "transition_duration": 0.12 }]
     仅当 transition_type 在该 pair 的 transition_candidates 中时生效
```

### 编辑决策输出 `edit_decisions.json`

```json
[
  {
    "from_shot": 1, "to_shot": 2,
    "pair_type": "continuous_action_same_scene",
    "confidence": 0.82,
    "signals": {
      "same_scene": true,
      "chain_from_previous": false,
      "visual_overlap_score": 0.0,
      "prev_scene": "scene_1",
      "next_scene": "scene_1",
      "shared_characters": ["wusong"]
    },
    "trim_out": 0.0, "trim_in": 0.0,
    "transition_type": "straight-cut",
    "transition_duration": 0.0,
    "transition_candidates": [
      { "transition_type": "straight-cut", "transition_duration": 0.0, "reason": "default" },
      { "transition_type": "cross-dissolve", "transition_duration": 0.25, "reason": "alternative" }
    ],
    "judgment_applied": false,
    "needs_human_review": false
  }
]
```

### FFmpeg 合成流程

```
对每个平台 (douyin=1080×1920, wechat=1080×1080, youtube=1920×1080):
  │
  ├─ 1. 逐镜头片段生成:
  │     对每个 shot:
  │       ├─ 有视频: ffmpeg -ss {trim_in} -i video.mp4 -t {keep_duration}
  │       │     -vf scale=W:H:force_original_aspect_ratio=increase,crop=W:H
  │       │     -c:v libx264 -pix_fmt yuv420p -c:a aac → seg_{id}.mp4
  │       │
  │       └─ 仅图片: ffmpeg -loop 1 -i image.png -t {duration - trims}
  │             -vf scale=W:H:... -c:v libx264 -pix_fmt yuv420p → seg_{id}.mp4
  │
  ├─ 2. 转场合并:
  │     ├─ 全部 straight-cut: ffmpeg -f concat -safe 0 -c copy → merged.mp4
  │     │
  │     └─ 含软转场: 逐对迭代合并:
  │         ├─ cross-dissolve: xfade filter (transition=fade, duration=D)
  │         │     + 音频 acrossfade (或 anullsrc 填充)
  │         ├─ flash-white: xfade filter (transition=fadewhite, duration=D)
  │         │     + 音频 acrossfade
  │         └─ straight-cut: concat demuxer
  │
  └─ 3. BGM 混合:
       ├─ 有 BGM + 有视频音频: amix (video volume=1, bgm volume=0.2, loop)
       ├─ 有 BGM + 无视频音频: 直接映射 BGM (-shortest)
       └─ 无 BGM: 直接复制 merged
       → {platform}.mp4
```

### 平台配置 (config/platforms.yaml)

| 平台 key | 标签 | 分辨率 | 比例 | 最大时长 |
|---|---|---|---|---|
| `douyin` | 抖音/快手 | 1080×1920 | 9:16 | 60s |
| `wechat` | 微信朋友圈/微博 | 1080×1080 | 1:1 | 30s |
| `youtube` | YouTube/B站 | 1920×1080 | 16:9 | 120s |

### 最终输出

```
videos/
  ├─ seg_1.mp4, seg_2.mp4, ...          # 逐镜头片段
  ├─ youtube_merged.mp4                  # 合并后 (BGM 前)
  ├─ youtube.mp4                         # 最终成片
  ├─ douyin.mp4
  ├─ wechat.mp4
  ├─ edit_decisions.json                 # 编辑决策
  └─ result.json                         # 合成结果
```

`result.json`:
```json
{
  "videos": {
    "youtube": "/path/to/youtube.mp4",
    "douyin": "/path/to/douyin.mp4",
    "wechat": "/path/to/wechat.mp4"
  },
  "output_dir": "/path/to/videos",
  "edit_decisions_path": "/path/to/edit_decisions.json"
}
```

---

## 编排器完整参数传递

### `run_pipeline.py` CLI 参数

| 参数 | 必填 | 默认值 | 传递目标 |
|---|---|---|---|
| `--story` | 否 | None | validate 阶段 |
| `--framework` | 否 | None | validate + refs 阶段 |
| `--storyboard` | **是** | -- | validate + normalize + assets + compose |
| `--output_dir` | 否 | 自动时间戳 | 所有阶段 |
| `--platform` | 否 | `["youtube"]` | compose 阶段 |
| `--assets_manifest` | 否 | None | 跳过 assets，直接用已有 |
| `--brand_manifest` | 否 | None | 跳过 brand，直接用已有 |
| `--logo_path` | 否 | None | brand 阶段 |
| `--brand_color` | 否 | `#FF6A00` | brand 阶段 |
| `--product_image` | 否 | None | brand 阶段 |
| `--product_shots` | 否 | `""` | brand 阶段 |
| `--watermark_text` | 否 | `"Brand Protected"` | brand 阶段 |
| `--title_text` | 否 | `"品牌广告"` | brand 阶段 |
| `--from` | 否 | None | 阶段窗口起点 |
| `--to` | 否 | None | 阶段窗口终点 |
| `--skip_validate` | 否 | false | 跳过校验 |
| `--skip_refs` | 否 | false | 跳过参考图 |
| `--skip_assets` | 否 | false | 跳过素材 |
| `--skip_brand` | 否 | false | 跳过品牌化 |
| `--skip_compose` | 否 | false | 跳过合成 |
| `--no_api` | 否 | false | refs + assets 阶段 |
| `--parallel` | 否 | 4 | assets 阶段 |
| `--review_mode` | 否 | 配置文件 | assets 阶段 |
| `--video_only` | 否 | false | assets 阶段 |
| `--image_width` | 否 | 1024 | assets 阶段 |
| `--image_height` | 否 | 1024 | assets 阶段 |
| `--verbose` | 否 | false | 所有阶段 |

### 各阶段实际 subprocess 调用

```bash
# refs 阶段
python3 ad_assets.py \
  --mode character_refs \
  --framework /path/to/framework.json \
  --output_dir /path/to/output/character_refs \
  [--no_api] [--verbose]

# assets 阶段 (使用规范化后的 storyboard)
python3 ad_assets.py \
  --mode assets \
  --storyboard /path/to/output/_normalized/storyboard.json \
  --output_dir /path/to/output/assets \
  --parallel 4 \
  --image_width 1024 \
  --image_height 1024 \
  [--review_mode metrics_only] \
  [--video_only] [--no_api] [--verbose]

# brand 阶段 (仅当 should_brand() == true)
python3 ad_brand.py \
  --assets_manifest /path/to/output/assets/assets.json \
  --storyboard_file /path/to/output/_normalized/storyboard.json \
  --brand_color '#FF6A00' \
  --watermark_text 'Brand Protected' \
  --title_text '品牌广告' \
  --output_dir /path/to/output/brand \
  [--logo_path ...] [--product_image ...] [--product_shots ...] [--verbose]

# compose 阶段
python3 ad_compose.py \
  --storyboard /path/to/output/_normalized/storyboard.json \
  --assets /path/to/output/brand/brand_manifest.json \
  --output_dir /path/to/output/videos \
  --platform douyin wechat youtube \
  [--edit_judgments ...] [--verbose]
```

### `pipeline_result.json` 完整结构

```json
{
  "output_dir": "/path/to/output",
  "inputs": {
    "story": "/path/to/story.json",
    "framework": "/path/to/framework.json",
    "storyboard": "/path/to/storyboard.json",
    "assets_manifest": null,
    "brand_manifest": null
  },
  "stages": {
    "character_refs": {
      "output_dir": "/path/to/output/character_refs",
      "manifest": "/path/to/output/character_refs/character_refs.json"
    },
    "assets": {
      "output_dir": "/path/to/output/assets",
      "manifest": "/path/to/output/assets/assets.json",
      "pending_reviews": []
    },
    "brand": {
      "output_dir": "/path/to/output/brand",
      "manifest": "/path/to/output/brand/brand_manifest.json"
    },
    "compose": {
      "output_dir": "/path/to/output/videos",
      "result": "/path/to/output/videos/result.json",
      "assets_used": "/path/to/output/brand/brand_manifest.json"
    }
  },
  "normalized_storyboard": "/path/to/output/_normalized/storyboard.json",
  "storyboard_migration": {
    "report_path": "/path/to/output/_normalized/storyboard_migration_report.json",
    "summary": {
      "shot_count": 8,
      "shot_type_backfilled_count": 0,
      "video_references_backfilled_count": 0,
      "video_references_normalized_count": 0
    }
  },
  "selected_stages": {
    "from": "validate",
    "to": "compose",
    "effective": ["validate", "refs", "assets", "brand", "compose"]
  },
  "status": "completed"
}
```

---

## Provider 配置总览

### providers.yaml 完整结构

```yaml
providers:
  volcengine:
    api_base: "https://ark.cn-beijing.volces.com/api/v3"
  apimart:
    api_base: "https://api.apimart.ai/v1"
  fal:
    api_base: "https://queue.fal.run"
  byteplus:
    api_base: "https://ark.ap-southeast.bytepluses.com/api/v3"
  evolink:
    api_base: "https://api.evolink.ai"
  openrouter:
    api_base: "https://openrouter.ai/api/v1"

models:
  video:
    fallback_chain: [volcengine_seedance2]
    max_retries: 2
    retry_delay: 5
    review_mode: metrics_only
    volcengine_seedance2:
      provider: volcengine
      model: "doubao-seedance-2-0-fast-260128"
      supports_multi_reference: true
      max_reference_images: 9
      ratio: "16:9"
      resolution: "720p"
      generate_audio: true
      watermark: false
      min_duration: 4
      max_duration: 15
      poll_interval: 5
      poll_max_attempts: 120

  image:
    fallback_chain: [apimart, fal]
    apimart:
      model: "gemini-3.1-flash-image-preview"
      timeout: 180
    fal:
      num_inference_steps: 4
      timeout: 180

  bgm:
    provider: fal
    endpoint: "/fal-ai/minimax-music/v2"
    timeout: 240

  llm:
    provider: apimart
    model: "gemini-2.5-flash"
    timeout: 120

  vision_judge:
    provider: apimart
    model: "gemini-2.5-flash"
    enabled: false
```

### API 调用清单

| 功能 | Provider | Endpoint | Model | 用途 |
|---|---|---|---|---|
| 角色参考图 | apimart | `/chat/completions` | gemini-3.1-flash-image | 生成角色设计参考图 |
| 首帧/关键帧/尾帧 | apimart → fal | `/chat/completions` | gemini-3.1-flash-image | 生成镜头静态图片 |
| 视频生成 | volcengine | `/contents/generations/tasks` | seedance-2-0-fast | 图生视频 (I2V) |
| 视频 fallback | byteplus | `/contents/generations/tasks` | seedance-1-5-pro | 备用视频生成 |
| Prompt 提取 | apimart | `/chat/completions` | gemini-2.5-flash | 全局首尾帧 prompt 提取 |
| BGM | fal | `/fal-ai/minimax-music/v2` | minimax-music | 背景音乐生成 |
| 视觉判断 | apimart | `/chat/completions` | gemini-2.5-flash | 视频质量视觉裁定 (可选) |

### 环境变量

| 变量 | 对应 Provider |
|---|---|
| `VOLCENGINE_API_KEY` | 火山引擎 |
| `APIMART_API_KEY` | APIMart |
| `FAL_KEY` | fal.ai |
| `BYTEPLUS_API_KEY` | BytePlus |
| `EVOLINK_API_KEY` | Evolink |
| `OPENROUTER_API_KEY` | OpenRouter |
| `VIDEO_OUTPUT_ROOT` | 输出根目录 |

---

## 参考文档

### `reference/video_quality_standard.md`

10 维质量检查标准，用于视频质量审查裁定:

1. 角色一致性 (重复角色/身份漂移)
2. 身体结构 (解剖/比例/多余肢体)
3. 运动连续性 (突变/物理违规)
4. 画面质量 (闪烁/撕裂/模糊)
5. 场景一致性 (背景/光线/空间)
6. 细节一致性 (服装/道具)
7. 镜头语言 (构图/裁切)
8. 时间稳定性 (尾部崩坏/中段退化)
9. 叙事语义 (匹配分镜意图)
10. 跨镜连续性 (朝向/服装/状态衔接)

裁定动作: `keep` / `cut_segment` / `trim_tail` / `regenerate` / `needs_human_review`

### `reference/video_generation_strategy.md`

用途驱动参考协议，定义了:
- 9 种参考用途类型
- 4 个语义决策维度 (动作复杂度/状态变化/起止约束/衔接依赖)
- 4 种组装模式 (轻量 shot / 收束 shot / 中间过程重要 shot / 强动作 shot)
- 参考图截断策略 (按 usage 优先级，而非时间顺序)

### `reference/cross_shot_continuity.md`

场景级约束系统，通过 `scene_continuity` 字段解决独立生成镜头之间的视觉连续性:
- `stable_character_states`: 角色姿态/物理状态/服装/伤势
- `stable_spatial_layout`: 固定空间关系
- `stable_props`: 持久道具状态
- `continuity_rules`: 显式连续性规则

---

## 完整数据流向图

```
story.json ─────────────────────┐
                                │
framework.json ──────────┐      │
  │                      │      │
  │ suggested_characters │      │
  ▼                      │      │
ad_assets.py             │      │
  --mode character_refs  │      │
  │                      │      │
  ▼                      │      │
character_refs.json      │      │
  │                      │      │
  │   ┌──────────────────┘      │
  │   │                         │
  ▼   ▼                         │
storyboard.json ◄───────────────┘
  │    (由 LLM 产出，引用 narrative + characters + scenes)
  │
  ▼
run_pipeline.py ── normalize_storyboard()
  │
  ├─ 绑定 character_refs → characters{}
  ├─ 回填 shot_type (legacy)
  ├─ 规范化 video_references usage
  ├─ 推断缺失 video_references
  │
  ▼
_normalized/storyboard.json ─────────────────────────────────────┐
  │                                                              │
  ▼                                                              │
ad_assets.py --mode assets                                       │
  │                                                              │
  ├─ 全局 prompt 提取 (LLM)                                      │
  │                                                              │
  ├─ 对每个 scene:                                                │
  │   ├─ 生成场景环境图                                           │
  │   └─ 对每个 shot:                                             │
  │       ├─ build_image_prompt() → 图片 API → first_frame.png    │
  │       ├─ [关键帧图片 × N]                                     │
  │       ├─ [尾帧图片] (根据 continuity_mode)                    │
  │       ├─ 组织 video_references → @图片N 调用                   │
  │       ├─ build_video_prompt() → Seedance API → video.mp4      │
  │       ├─ 质量审查 (粗筛 + 可选 LLM 裁定)                      │
  │       └─ [导演审图] (可选)                                     │
  │                                                              │
  ├─ BGM 生成                                                    │
  │                                                              │
  ▼                                                              │
assets.json ─────────────────────┐                               │
  │                              │                               │
  ▼                              │                               │
ad_brand.py (可选)               │                               │
  │                              │                               │
  ▼                              │                               │
brand_manifest.json ─────────────┘                               │
  │                                                              │
  ▼                                                              │
ad_compose.py ◄──────────────────────────────────────────────────┘
  │
  ├─ build_edit_decisions()
  │   ├─ 信号采集 (scene边界 + chain + overlap + characters + text tokens)
  │   ├─ pair 类型分类
  │   ├─ trim 计算
  │   └─ 转场选择 (默认 + 覆盖)
  │
  ├─ 对每个平台:
  │   ├─ 逐镜头 segment (ffmpeg scale+crop)
  │   ├─ 转场合并 (concat / xfade)
  │   └─ BGM 混合 (amix)
  │
  ▼
videos/
  ├─ youtube.mp4
  ├─ douyin.mp4
  ├─ wechat.mp4
  ├─ edit_decisions.json
  └─ result.json

pipeline_result.json ← 汇总所有阶段输入输出
```
