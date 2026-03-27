---
name: xyz-video-skill
description: "视频生成 skill。对话驱动的端到端视频生成流程：输入主题或故事，完成角色设计、剧本框架、分镜脚本、素材生成与视频合成。"
---

# XYZ Video Skill

## 触发条件
用户提到"生成视频"、"广告片"、"短片"、"商业视频"、"产品广告"、"做一个视频"等关键词。

## 工作流程概览

你（宿主 LLM）负责所有思考任务，Python 脚本只负责调用外部 API 生成素材和合成视频。

```
步骤1: 故事创作            → 你与用户对话讨论方向，输出 story.json
步骤2: 剧本框架 + 角色设计  → 你思考，输出 framework.json（从 narrative 切分 scenes + 设计角色）
步骤3: 角色参考图生成        → python3 scripts/ad_assets.py --mode character_refs
步骤4: 分镜脚本            → 你思考，输出 storyboard.json（scenes > shots，从 narrative 派生）
步骤5: 素材生成 + 视频合成   → python3 scripts/ad_assets.py → python3 scripts/ad_compose.py
```

**输出目录约定：** 所有文件输出到同一个目录，如 `~/video-output/20260319_143022/`。

---

## 步骤1: 故事创作

与用户对话讨论创意方向，然后展开为完整故事。

**流程：**
1. 用户给出主题/想法
2. 如果方向不明确，在对话中提出 2-3 个方向建议（自然对话，不写 JSON）
3. 用户确认方向后，输出 `story.json`

**输出 JSON → `{output_dir}/story.json`：**

```json
{
    "title": "故事标题",
    "synopsis": "一句话梗概（30字以内）",
    "source_interpretation": "对用户输入的理解",
    "narrative": "完整连贯叙事（200-400字，见下方规则）",
    "story_beats": [
        {
            "beat": "开端|发展|高潮|结尾",
            "description": "具体发生什么（50-80字，必须是可视化的场景描述）",
            "emotion": "情感基调",
            "key_visual": "最核心的一个画面（一句话）",
            "narrative_range": "对应 narrative 中的起止句（如：'烟雨西湖…' → '…传来慌乱的脚步声'）"
        }
    ],
    "visual_tone": "整体视觉基调建议（色调、氛围、风格，30-50字）",
    "suggested_duration_per_beat": [8, 10, 12, 10]
}
```

### 1.1 narrative（完整连贯叙事）— 最核心的字段

**narrative 是整个视频的故事主线。** 后续步骤2的场景拆分、步骤4的分镜脚本、步骤5的首尾帧/动作提取，都从 narrative 派生。它不是摘要，不是大纲，而是一段**可以直接朗读的、有画面感的连贯叙事**。

**写作规则：**

1. **因果链** — 每个事件必须由前一个事件触发。不是"A发生了，然后B发生了"，而是"因为A发生了，所以B做出了反应，导致C"
   - ❌ "白娘子在桥上。许仙在跑。白娘子递伞。"（三个孤立事件）
   - ✅ "白娘子听到脚步声回头，看到书生在雨中狼狈奔跑，心生怜悯，于是走上前递伞"（因果链）

2. **角色动机** — 每个角色的行为必须有情感/意图驱动
   - ❌ "她递伞给他"（机械动作）
   - ✅ "她看到他无助地望着雨帘，心中一动，将自己的伞递了过去"（有动机）

3. **感官细节** — 写出观众能看到、听到、感受到的东西
   - 声音："慌乱的脚步声"、"雨打伞面"
   - 触觉："指尖在伞柄上相触"
   - 视觉变化："云缝中透出暖光"

4. **环境过渡** — 时间/天气/光线的变化必须是渐变的，不能跳变
   - ❌ "下着大雨。然后阳光灿烂。"（跳变）
   - ✅ "雨渐渐小了，云层裂开一道缝隙，一丝暖光透了出来"（渐变）

5. **连续的动作线** — 角色的位置移动必须有交代
   - ❌ "白娘子在桥上"→ 下一句 "白娘子站在亭子边"（瞬移）
   - ✅ "白娘子将伞交给小青，独自走向亭子"（交代了移动过程）

6. **长度** — 200-400字（中文）。太短没有细节，太长会超出视频表达能力

**示例：**

> 烟雨西湖，白素贞与小青撑伞漫步断桥，享受着雨中的宁静。忽然桥远处传来慌乱的脚步声——一个年轻书生抱着书箱在暴雨中狼狈奔跑，书页从箱中飞散。他踉跄着钻进桥边的石亭，弯腰喘气，脚边散落着被雨水浸湿的书页。白素贞驻足望去，看到他无助地望着亭外的雨帘，心生怜悯。她将伞交给小青，独自走向亭子，站在雨中将伞递给书生。他惊讶抬头，犹豫片刻，伸手接过——两人指尖在伞柄上相触，都微微一怔。雨渐渐停了，云缝中透出暖光。书生感激道别，撑着借来的伞沿桥慢慢走远。走到桥尽头，他忍不住回望——桥的另一端，白素贞仍站在那里，衣袂被晚风轻轻扬起。

### 1.2 story_beats 与 narrative 的关系

**story_beats 是 narrative 的结构化切分**，不是独立创作。每个 beat 的 `narrative_range` 标注它对应 narrative 中的哪段文字。

规则：
- 所有 beats 的 narrative_range 合起来必须覆盖整个 narrative，不能有遗漏
- 每个 beat 的 description 必须与 narrative 对应段落一致，不能添加 narrative 中没有的内容
- beats 之间不能有叙事跳跃——如果 narrative 中有过渡（"雨渐渐停了"），对应的 beat 必须包含这个过渡

**约束：**
- 紧扣原文核心意象，禁止无关发散
- 每个情节点都要能转化为具体画面（有人物、有场景、有动作）
- 必须有明确的 开端 → 发展 → 高潮 → 结尾
- 每个情节点的场景/人物状态/光影必须有明显变化

---

## 步骤2: 剧本框架 + 角色设计

将故事展开为完整剧本框架。**角色设计是本步骤的核心**——角色外貌描述的质量直接决定后续所有视觉一致性。

**输出 JSON → `{output_dir}/framework.json`：**

```json
{
    "title": "剧本标题",
    "synopsis": "故事梗概（50字以内）",
    "narrative": "从 story.json 复制或润色（这是唯一故事主线，必须贯穿始终）",
    "visual_style_anchor": "全局统一视觉风格（色调、质感、光影风格、氛围基调，80-120字）",
    "total_duration": 60,
    "story_time": "day|dusk|night|dawn",
    "suggested_characters": [
        {
            "id": "角色唯一ID（英文，如 blue_mecha, hero_girl）",
            "name": "角色名",
            "role_type": "protagonist|antagonist|supporting|extra",
            "personality": "性格特点（20字以内）",
            "appearance": "详细外貌描述（极其详细！颜色、材质、形状、纹理、特征标记，80-150字。这是角色参考图的生成依据，必须写到看完描述就能画出来的程度）",
            "default_clothing": "默认服装/装甲描述（颜色、款式、配饰、材质细节，50-80字）",
            "key_features": ["识别特征1", "识别特征2", "识别特征3"],
            "ref_description": "传给图片模型的角色说明（英文，包含：这是谁、外观概要、在故事中的角色，用于让图片模型理解参考图）"
        }
    ],
    "suggested_locations": [
        {
            "name": "地点名",
            "description": "地点详细视觉描述（100字左右）",
            "environment_type": "indoor|outdoor|natural|urban|fantasy"
        }
    ],
    "scenes": [
        {
            "name": "场景名",
            "location": "地点名",
            "narrative_segment": "对应 narrative 中的原文段落（直接引用，不改写）",
            "summary": "本场剧情概述（30字以内）",
            "visual_description": "场景视觉描述（50-100字）",
            "characters_in_scene": ["角色ID"],
            "emotion_arc": "情感变化",
            "duration": 20
        }
    ]
}
```

### 2.1 scenes 必须从 narrative 切分

**scenes 不是独立创作，而是 narrative 的分段切割。**

每个 scene 的 `narrative_segment` 字段直接引用 narrative 中的一段原文。所有 scenes 的 narrative_segment 合起来必须完整覆盖 narrative，不能遗漏，不能添加 narrative 中没有的内容。

**场景切分原则：**
- 当以下任一条件发生变化时，切为新场景：地点变化、光线质变、天气变化、时间跨度导致视觉环境显著不同
- 切分点应落在 narrative 的自然段落边界上（因果链的节点处）
- 每个 scene 的 narrative_segment 必须是一段**完整的因果片段**，不能在因果链中间切断

**示例 — 白娘子断桥：**

narrative 的自然切分点：
1. "烟雨西湖...书页从箱中飞散" → 场景1：雨中漫步 + 发现书生（同一视觉环境）
2. "他踉跄着...独自走向亭子" → 场景2：书生避雨 + 白素贞决定上前（同一视觉环境）
3. "站在雨中...都微微一怔" → 场景3：递伞 + 指尖相触（动作高潮）
4. "雨渐渐停了...衣袂被晚风轻轻扬起" → 场景4：雨停离别（视觉环境变化——雨停、暖光）

❌ 错误切分："白素贞驻足望去，看到他无助地望着亭外的雨帘，心生怜悯" 和 "她将伞交给小青，独自走向亭子" 切成两个场景 → 把因果链切断了（怜悯→行动 是一个完整的因果）

### 2.2 角色设计关键规则

**角色外貌描述必须极其详细，因为每张图片都是独立生成的。** 描述要达到"只看文字就能画出完全一样的角色"的精度。

必须包含：
- **形体特征**：身高、体型、比例
- **颜色方案**：主色+辅色+点缀色的精确描述
- **材质纹理**：光滑/粗糙/磨损/反光等质感描述
- **关键识别标记**：至少3个独特视觉锚点（如：胸口徽章、蓝色光眼、肩部排气管）
- **ref_description**（英文）：传给图片模型的简明角色说明，格式为 "This is [角色名] ([角色身份]). [外观概要]. He/She is the [PROTAGONIST/ANTAGONIST/etc]."

**示例：**
- ❌ `"appearance": "一个蓝色的机器人"`
- ✅ `"appearance": "40-foot tall humanoid war machine in faded royal blue and weathered flame-red thick riveted steel armor, deep scratches revealing dark gunmetal underneath, oil stains on chest plate, angular helmet with narrow glowing blue optics, battered silver faceplate with micro-scratches, massive forearms with exposed hydraulic cables and rubber hoses, exhaust pipes on shoulders"`

---

## 步骤3: 角色参考图生成

**这一步是视觉一致性的基石。** 在写分镜之前，必须先为每个角色生成参考图。

```bash
cd /path/to/skills/xyz-video-skill/scripts
python3 ad_assets.py \
    --mode character_refs \
    --framework {output_dir}/framework.json \
    --output_dir {output_dir}/character_refs
```

**脚本会自动：**
1. 读取 framework.json 中的 suggested_characters
2. 为每个角色生成一张"角色设计参考图"（白底，正面+3/4侧面，全身，标注细节）
3. 输出到 `{output_dir}/character_refs/ref_{character_id}.png`

**生成后请检查参考图质量**，如果某个角色不满意，可以重新生成单个角色：
```bash
python3 ad_assets.py --mode character_refs --framework framework.json --output_dir character_refs --character_id blue_mecha
```

确认参考图满意后再进入步骤4。

---

## 步骤4: 分镜脚本（最核心步骤）

将剧本框架拆解为逐镜头的分镜脚本。**分镜质量直接决定最终视频效果。**

**⚠️ 核心原则：分镜是 narrative 的视觉化切割，不是独立创作。**

每个 scene 和 shot 都必须标注 `narrative_segment`，直接引用 narrative 中的对应段落。scene_prompt / action_prompt / end_frame_description 三个字段都从 narrative_segment 派生，不凭空创造内容。

**输出 JSON → `{output_dir}/storyboard.json`：**

```json
{
    "title": "视频标题",
    "total_duration": 70,
    "character_ref_dir": "{output_dir}/character_refs",
    "bgm_style": "BGM 风格描述",
    "narrative": "从 framework.json 复制的完整连贯叙事（唯一故事主线）",
    "style_anchor": "从 framework.json 的 visual_style_anchor 复制（全局渲染风格：色调、质感、渲染方式）",
    "characters": {
        "character_id": {
            "ref_image": "ref_{character_id}.png",
            "ref_description": "从 framework.json 复制的 ref_description",
            "appearance": "从 framework.json 复制的 appearance",
            "key_features": ["特征1", "特征2", "特征3"],
            "weapon": "武器描述（如有）"
        }
    },
    "scenes": [
        {
            "id": "scene_1",
            "name": "场景名称（如：断桥雨中）",
            "location": "地点（如：杭州西湖断桥）",
            "narrative_segment": "对应 narrative 中的原文段落（从 framework.json 的 scene.narrative_segment 复制）",
            "lighting": "光线参数（色温、方向、质感，英文）",
            "weather": "天气/粒子效果（英文）",
            "props": ["道具1", "道具2"],
            "environment_description": "环境视觉描述（英文，80-150字。同场景所有镜头共享的视觉基底）",
            "shots": [
                {
                    "id": 1,
                    "characters_in_shot": ["character_id_1", "character_id_2"],
                    "narrative_segment": "本镜头对应的 narrative 片段（从 scene 的 narrative_segment 中进一步切分）",
                    "scene_prompt": "故事起点 + 起始画面状态（英文，从 narrative_segment 派生。见 5.5 规则）",
                    "end_frame_description": "故事终点 + 结束画面状态（必填！从 narrative_segment 派生。见 5.6 规则）",
                    "action_prompt": "动作描述（从 narrative_segment 派生，只写运动过程）",
                    "camera_movement": "运镜方式",
                    "camera_technical": "焦距+光圈",
                    "speed_baseline": "1.0x",
                    "narration": "",
                    "tts_text": "",
                    "subtitle": "",
                    "estimated_duration": 8,
                    "chain_from_previous": false,
                    "continuity_mode": "scene_end",
                    "motion_control": {
                        "subject_facing": "away_from_camera",
                        "camera_relation": "rear_three_quarter",
                        "movement_direction": "upstairs",
                        "screen_trajectory": "lower_right_to_upper_left",
                        "target": "cave_entrance",
                        "distance_to_target": "getting_closer",
                        "phase_beats": ["at foot of stairs", "ascending halfway", "approaching cave entrance"]
                    },
                    "transition_in": {"type": "cross-dissolve", "duration": 0.5},
                    "consistency_anchors": {
                        "characters": [
                            {"id": "character_id", "must_show": ["特征1", "特征2", "特征3"], "expression": "情绪"}
                        ],
                        "environment": ["环境元素1", "环境元素2"]
                    }
                }
            ]
        }
    ]
}
```

### 4.1 场景（Scene）层级 — 视觉基底共享

**同一个场景下的所有镜头共享完全相同的视觉基底**：光线、天气、道具、环境描述。这是风格一致性的核心机制。

**Scene 字段说明：**

| 字段 | 说明 | 示例 |
|------|------|------|
| `id` | 场景唯一ID | `"scene_1"` |
| `name` | 场景名称 | `"断桥雨中"` |
| `location` | 地点 | `"杭州西湖断桥"` |
| `narrative_segment` | 对应 narrative 中的原文段落 | 从 framework scenes 复制 |
| `lighting` | 光线参数（色温、方向、质感） | `"overcast diffused light, cool blue-grey 6500K"` |
| `weather` | 天气/粒子效果 | `"gentle rain with visible streaks, fog over lake"` |
| `props` | 场景核心道具列表 | `["oil-paper umbrellas", "stone bridge"]` |
| `environment_description` | 环境视觉描述（80-150字） | 地面材质、远景、建筑、水面等完整描述 |

**场景拆分规则：** 场景从 framework.json 的 scenes 继承（已经按 narrative 切分好了）。当以下任一条件变化时，必须拆成新场景：
- 光线发生质变（如从阴雨变为雨霁暖光）
- 天气变化（如雨停）
- 地点变化
- 时间跨度导致视觉环境显著不同

**Shot 的 narrative_segment 切分：** 每个 shot 从所属 scene 的 narrative_segment 中进一步切分出自己负责讲述的那段叙事。所有 shot 的 narrative_segment 合起来必须完整覆盖 scene 的 narrative_segment。

**Shot 在 scene 中只写自己特有的内容：** 动作、姿态、构图、镜头参数。不需要重复环境/光线/天气描述。

### 4.1.1 motion_control（结构控制层，人物运动镜头必填）

`scene_prompt / action_prompt / end_frame_description` 负责讲清楚故事，但它们仍然是自然语言。为了避免“角色朝向错了”“明明是上楼却像下楼”“目标关系不明确”这类系统性问题，人物运动镜头必须额外填写 `motion_control`。

**作用：** 把最容易被模型脑补错的空间关系、运动方向、时间阶段拆成结构化字段，作为 prompt builder 的硬约束输入。

**格式：**

```json
"motion_control": {
  "subject_facing": "away_from_camera",
  "camera_relation": "rear_three_quarter",
  "movement_direction": "upstairs",
  "screen_trajectory": "lower_right_to_upper_left",
  "target": "cave_entrance",
  "distance_to_target": "getting_closer",
  "phase_beats": [
    "at foot of stairs",
    "ascending halfway",
    "approaching cave entrance"
  ]
}
```

**字段说明：**

| 字段 | 作用 | 示例 |
|------|------|------|
| `subject_facing` | 主体身体朝向 | `away_from_camera`, `toward_camera`, `left_profile` |
| `camera_relation` | 镜头相对主体位置 | `rear_three_quarter`, `front_of_subject`, `side_follow_left` |
| `movement_direction` | 主体真实运动方向 | `upstairs`, `downstairs`, `toward_target`, `away_from_target`, `static` |
| `screen_trajectory` | 主体在画面中的轨迹 | `lower_right_to_upper_left`, `left_to_right`, `foreground_to_background` |
| `target` | 当前动作目标点 | `cave_entrance`, `master`, `cliff_edge` |
| `distance_to_target` | 与目标点距离变化 | `getting_closer`, `getting_farther`, `holding_position` |
| `phase_beats` | 时间过程分段 | `["takes first step", "reaches halfway point"]` |

**什么时候必须写：**
- 角色在镜头内发生明确位移
- 上下楼、前进后退、转身、接近目标、远离目标
- 多角色对位、视线交汇、空间调度

**什么时候可以省略：**
- 纯环境空镜
- 几乎静止的情绪特写
- 纯氛围粒子 / 光影镜头

**核心原则：**
- prose 负责“可读性”，`motion_control` 负责“不可误解性”
- 如果 prose 与 `motion_control` 冲突，以 `motion_control` 为准
- `phase_beats` 应该对应动作过程的关键阶段，而不是重复完整句子

### 4.2 分镜拆分原则 — 3x3 法则

先按场景分组（1-3个场景），再在每个场景内按动作拆分镜头。每阶段2-3个镜头：

| 阶段 | 节奏 | 内容 | 镜头数 |
|------|------|------|--------|
| 建立（Phase 1） | 慢 0.7x | 环境建立、角色出场 | 2-3 |
| 冲突（Phase 2） | 快 1.0-1.2x | 核心动作、冲突爆发 | 2-3 |
| 结局（Phase 3） | 慢 0.7x | 情感释放、余韵收尾 | 1-2 |

**硬性约束：**
- **每个镜头只做一个动作** — 一个镜头 = 一个清晰的视觉事件。如果你想写"A攻击B，B反击，然后A找到破绽刺穿B"，这必须拆成3个镜头。
- **每个镜头 5-12 秒** — 根据动作复杂度自由估算 `estimated_duration`：简单静态/对峙 5-6秒，中等动作 7-9秒，复杂变形/战斗 10-12秒。Seedance API 支持 [5, 12] 区间任意整数。
- **首帧到尾帧的动作路径必须物理合理** — 不能出现角色瞬移、位置互换、违反惯性的运动
- 总镜头数 ≈ total_duration ÷ 平均镜头时长（通常 7-10 秒/镜头）

### 4.3 characters_in_shot（必填）

**每个 shot 必须标注该镜头出现的角色列表。** 这决定了生成图片时传哪些角色参考图给图片模型。

规则：
- 只列出画面中**实际可见**的角色
- 如果镜头只有环境/道具没有角色，留空 `[]`
- 如果角色还是非人形态（如：卡车还没变形成机器人），不要列角色ID

### 4.4 单一真相源原则（Single Source of Truth）

**角色外貌只在一个地方定义：`storyboard.characters`（从 framework.json 复制）。分镜中禁止重复描述角色外貌。**

这是一致性的核心原则。代码会自动从 `storyboard.characters` 读取角色外貌，注入到图片和视频 prompt 中。如果你在 `scene_prompt` 里写了外貌，代码的 ContentFilter 会主动过滤掉。

**黄金规则：**
1. **只能引用，不能创造** — `scene_prompt` 只写动作、姿态、场景环境，不写角色外貌/服装
2. **分镜只写动作，不写外观** — ✅ "The robot stands at the intersection" ❌ "The blue-red robot with riveted armor stands..."
3. **角色外观以 `storyboard.characters` 为准** — 代码自动注入，不需要你重复

| 允许写在 scene_prompt | 禁止写在 scene_prompt |
|---------------------|---------------------|
| 角色动作/姿态 | 角色外貌/身高/体型 |
| 场景环境 | 服装/装甲描述 |
| 镜头参数/构图 | 颜色方案/材质纹理 |
| 光影/氛围 | 配饰/武器外观 |

### 4.5 scene_prompt 规则（这个镜头要讲什么 + 开始时的画面状态）

**scene_prompt = 故事上下文 + 视觉起始状态。** 环境/光线/天气不需要写——由 scene 层自动提供。用英文撰写。

**⚠️ 核心原则：scene_prompt 不是孤立的画面描述，它是故事线上的一个锚点。**

代码会用 scene_prompt + action_prompt + end_frame_description 三个字段的完整故事上下文，通过 LLM 提取出精确的首帧生图 prompt。所以 scene_prompt 的职责是**讲清楚这个镜头的故事起点**：

1. **故事上下文** — 这个镜头要讲什么？角色带着什么意图/情感进入这个画面？
2. **视觉起始状态** — 角色的精确位置、姿态、朝向、手持物品、空间关系
3. **构图** — 景别、角度

**三段式故事线：**
| 字段 | 故事作用 | 描述什么 |
|------|----------|----------|
| `scene_prompt` | 故事起点 | 这个镜头要讲什么 + 动作即将开始时的画面状态 |
| `action_prompt` | 故事过程 | 从起点到终点之间发生的运动 |
| `end_frame_description` | 故事终点 | 故事发展到哪了 + 动作完成后的画面状态 |

三个字段构成一条**因果链**。代码会将完整因果链交给 LLM 提取首帧/尾帧生图 prompt，使首尾帧天然带有故事方向，Seedance I2V 能顺着这个方向做运动插值。

**示例 — 递伞场景：**
- ❌ "The woman extends her umbrella toward the scholar"（纯动作，无故事上下文，无起始状态）
- ✅ "Moved by compassion for the rain-soaked scholar, the woman in white has walked over from the bridge to the pavilion edge. She stands holding her cream oil-paper umbrella, looking down at the scholar who sits on the stone step catching his breath, scattered damp pages at his feet. She is about to step forward and offer her umbrella. Medium two-shot at eye level, two meters apart."（有意图、有状态、有空间关系）

**示例 — 战斗场景：**
- ❌ "The robot slashes at the enemy with its blade"（动作正在发生，无故事起点）
- ✅ "Having spotted a weakness in the enemy's defenses, the robot warrior locks onto its target and raises its blade high in attack stance, facing the enemy across the intersection. The moment before the decisive strike. Low angle hero shot."（有意图、有蓄力姿态、有叙事张力）

**禁止：**
- 写角色外貌/服装（单一真相源原则，代码自动注入）
- 用列表/分点格式（必须自然语言流）
- 只写静态画面没有故事上下文（"A woman stands on a bridge" → 缺少为什么站在这里、要做什么）

### 4.5.1 scene_prompt 完整性检查清单

写完每个 scene_prompt 后，心中过一遍以下清单。不需要所有项都写进 prompt，但如果某项在逻辑上应该存在却没有提及，**必须补上**：

| 维度 | 检查项 | 示例 |
|------|--------|------|
| 角色状态 | 姿态/朝向/手持物品/身体状态 | "holding oil-paper umbrella", "wet hair clinging to forehead" |
| 环境交互 | 天气/场景对角色和物体的影响 | 雨→湿发/水面反光/撑伞, 战斗→地面碎裂/烟尘 |
| 光影逻辑 | 光源方向与阴影是否匹配 | 夕阳从右侧→左侧阴影, 火焰→暖色环境光 |
| 道具/载具 | 场景中应该存在的物品 | 油纸伞、书箱、翻倒的椅子、破碎的窗户 |
| 天气/粒子 | 大气效果 | 雨丝、雪花、灰尘、火星、雾气、水面涟漪 |

**核心原则：推导隐含的视觉细节，明确写出来。** 图片模型不会自动推理"下雨所以应该撑伞"——你必须写出来。同理，end_frame_description 也需要过这个检查清单。

### 4.6 end_frame_description 规则（故事发展到哪了 + 结束时的画面状态）

**每个 shot 都必须有 end_frame_description，包括最后一个。** 没有尾帧约束，Seedance 就不知道动作终点在哪里，会产生随机漫游的画面。

**⚠️ end_frame_description 不是孤立的画面描述，它是故事线上的终点锚点。**

代码会用完整故事上下文（scene_prompt + action_prompt + end_frame_description）通过 LLM 提取出精确的尾帧生图 prompt。所以 end_frame_description 的职责是**讲清楚这个镜头的故事终点**：

1. **故事进展** — 动作完成后，故事推进到了什么状态？角色的情感/意图发生了什么变化？
2. **视觉结束状态** — 角色的精确位置、姿态、表情、手持物品
3. **过渡暗示**（如果下一个镜头有变化）— 为下一镜头的视觉过渡埋伏笔

**示例 — 递伞场景：**
- ❌ "Close-up of two hands on the umbrella handle."（纯画面描述，无故事进展）
- ✅ "The scholar has gratefully accepted the umbrella — close-up of their hands meeting on the bamboo handle, her delicate fingers and his ink-stained hand. Both frozen in the unexpected intimacy of the moment. The rain has softened slightly, and through a thin gap in the clouds a faint warm light begins to appear on the umbrella canopy."（有故事进展、有情感、有过渡暗示）

**规则：**
- 描述本镜头动作完成后的**故事状态和画面状态**
- **同样遵守单一真相源**：只写姿态/场景，不写角色外貌（代码自动注入）
- 最后一个 shot 的 end_frame_description 描述最终定格画面
- **首帧→尾帧的运动路径必须单向可插值**：不能方向矛盾、不能景别跳变、位移量必须在 estimated_duration 内物理可完成

### 4.6.1 首帧→尾帧单向运动法则（Seedance 物理限制）

**这是最容易犯错的规则。** Seedance 在首帧和尾帧之间做运动插值，如果两端的视觉状态存在矛盾，模型会被迫在有限时长内强行对齐，产生不自然的"一刷"切换或角色瞬间转身。

**核心法则：一个 shot 内只能有一个运动方向。**

| 维度 | 允许 | 禁止 |
|------|------|------|
| 朝向 | 首帧正面 → 尾帧正面（没转向） | 首帧正面 → 动作中转身 → 尾帧又正面 |
| | 首帧正面 → 尾帧背面（一次转身） | 首帧背面 → 尾帧正面（除非这就是唯一动作） |
| 位移 | 从 A 走到 B（单向） | 从 A 走到 B 再走回 A |
| 景别 | 中景推到近景（单向） | 近景→远景→近景 |
| 姿态 | 站立→坐下（单向） | 站→坐→站 |

**常见错误场景：**

❌ 错误："角色走向亭子（背影），到达后转身面对镜头微笑"
- 首帧：正面 → 动作：转身走（背面）→ 尾帧：又正面 = 方向矛盾
- Seedance 会在 5-8 秒内强行完成 正面→背面→正面，产生生硬的"一刷"

✅ 正确拆法：
- Shot A：角色转身走向亭子（首帧正面 → 尾帧背影到达亭子）
- Shot B：角色在亭子里转身面对镜头（首帧背影 → 尾帧正面微笑）

**自检方法：** 写完每个 shot 后，想象用一条直线连接首帧状态和尾帧状态。如果这条线需要"折返"（方向反转），就必须拆成两个 shot。

**特别注意尾帧约束 shot：** 当 `continuity_mode` 为 `"strict"` 或该 shot 是 scene 末尾时，代码会传尾帧图给 Seedance 强约束终点画面。如果这个 shot 的首帧→尾帧存在方向矛盾，Seedance 会被强制对齐到矛盾的终点，问题最严重。其他 shot 虽然不传尾帧图，但 action_prompt 本身如果描述了往返动作，Seedance 也会产生不自然的运动。

### 4.6.2 chain_from_previous（链式衔接，默认 false）

**默认每个 shot 独立生成首帧。** 只有当你确定相邻两个 shot 满足以下**全部条件**时，才在后一个 shot 设置 `"chain_from_previous": true`：

1. **角色完全相同** — 前后 shot 的 `characters_in_shot` 一致
2. **景别相近** — 不能从特写跳到全景，或反过来
3. **场景/环境连续** — 同一地点、同一光线条件、动作连贯
4. **前一 shot 的 end_frame_description 在视觉上适合作为后一 shot 的开始画面**

当 `chain_from_previous: true` 时，代码会从前一 shot 的实际视频中提取最后一帧作为本 shot 的首帧，不再独立生图。这能保证两个 shot 之间视觉连续。

**链式与转场的配合：**
- `chain_from_previous: true` + `transition_in: null` → 纯链式（连续动作，无转场）
- `chain_from_previous: true` + `transition_in: {"type": "cross-dissolve", "duration": 0.3}` → 链式+溶解兜底（景别微调时）
- `chain_from_previous: false` + `transition_in: {...}` → 独立生成首帧 + 剪辑转场掩盖视觉跳变

**链式生成规则：** 同场景内**默认独立生成首帧**（`chain_from_previous: false`）。只有当相邻 shot 满足上述全部条件时，才显式设置 `chain_from_previous: true`。以下情况**必须断链**：
1. **跨场景** — 地点/环境变化
2. **闪回进入/退出** — 即使叙事上是同一角色的记忆
3. **时间跳跃导致光线质变** — 如雨天→雨停暖光
4. **反打/视角大跳** — 角度差异太大无法链式

**首尾帧生成策略（由 `continuity_mode` 控制）：**

每个 shot 必须标注 `continuity_mode`，由你根据叙事结构判断。代码根据这个字段决定是否生成尾帧图。

| 模式 | 含义 | 代码行为 |
|------|------|---------|
| `"strict"` | 关键镜头，必须精确落到目标画面 | 生成尾帧图，强约束终点 |
| `"scene_end"` | 普通镜头（默认） | 仅当该 shot 是 scene 最后一个时生成尾帧图 |
| `"free"` | 氛围/空镜/过场 | 不生成尾帧图，Seedance 自由运动 |

**你应该标 `"strict"` 的情况（必须有尾帧图锚定终点）：**
1. **情绪转折点** — 角色表情/情感状态发生关键变化的镜头（如从平静到震惊、从犹豫到坚定）
2. **角色状态大变化** — 姿态/位置/持有物品发生重要改变（如拿起关键道具、倒下、起身）
3. **关键动作落点** — 叙事上必须精确到达某个视觉状态的镜头（如递伞完成、门被推开、角色相遇）
4. **scene 最后一个 shot** — 段落收口，确保命中目标画面（这种情况 `"scene_end"` 也会生成，但如果你认为这个收口特别重要，用 `"strict"` 更明确）
5. **下一个 shot 是 `chain_from_previous: true`** — 如果下一个 shot 要从本 shot 视频提取尾帧作为首帧，本 shot 的终点状态就必须准确

**你应该标 `"scene_end"`（默认）的情况：**
- 普通叙事推进，动作方向明确，不需要精确锚定终点
- scene 内中间镜头，动作自然过渡即可

**你应该标 `"free"` 的情况：**
- 纯环境空镜（云、水、风景）
- 氛围渲染段落（慢动作粒子、光影变化）
- 不包含角色的过场镜头

**典型 scene 内标注示例（3 个 shot）：**
- Shot 1（角色抬头看向门口）：`"continuity_mode": "scene_end"` — 普通过渡
- Shot 2（角色快步走向门口，到达门前）：`"continuity_mode": "strict"` — 关键位移落点，下一 shot chain 依赖本帧
- Shot 3（角色推门出去，光线涌入）：`"continuity_mode": "scene_end"` — scene 末尾，默认也会生成尾帧

### 4.6.3 keyframes（中间关键帧，可选）

**仅当 `continuity_mode: "strict"` 且动作复杂时使用。**

如果一个镜头内有多个关键姿态变化，不是简单的首帧 A → 尾帧 B 单向运动，可以标注中间关键帧：

```json
"keyframes": [
  {"timestamp": 3.0, "description": "角色转身面对镜头，表情从平静变为惊讶"},
  {"timestamp": 6.0, "description": "角色举起手中的伞，伞面完全展开"}
]
```

**规则：**
- `timestamp` 必须在 `0` 到 `estimated_duration` 之间，且按时间递增
- `description` 遵守单一真相源：写姿态、位置、动作、场景，不写外貌
- `keyframes` 是 storyboard 的通用字段，用来表达“镜头中间关键状态”
- 当前执行层里，只有支持多参考图的模型（如支持多参考图的 Seedance 2.0 类模型）会真正把 `keyframes` 转成参考图使用
- 其他只支持首尾帧的视频模型会自动忽略 `keyframes`，继续只用首帧 / 尾帧

**何时标注 `keyframes`：**
- 镜头内有明确的阶段性姿态变化（如：蹲下 → 起身 → 转身）
- 需要精确控制中间某个时刻的画面状态
- 动作路径复杂，首尾帧不足以约束

**何时不要标注：**
- 简单的 A → B 运动（走、转头、伸手）
- 连续流畅的动作（跑步、挥手）
- 大部分普通推进镜头

#### 4.6.3.1 参考图分配规则（最多 9 张）

当当前视频模型支持多参考图时，代码按以下顺序分配参考图槽位：

1. **首帧图** — 永远占用第 1 张
2. **中间关键帧 `keyframes`** — 占用中间槽位
3. **尾帧图** — 如果该 shot 根据 `continuity_mode` 需要尾帧约束，则占用最后 1 张

**槽位计算：**
- 如果该 shot 需要尾帧图：`1 张首帧 + 最多 7 张 keyframes + 1 张尾帧 = 最多 9 张`
- 如果该 shot 不需要尾帧图：`1 张首帧 + 最多 8 张 keyframes = 最多 9 张`

**代码行为：**
- 代码会根据当前视频模型的 `max_reference_images` 自动计算可用槽位
- 如果 storyboard 中标注的 `keyframes` 超过可用数量，代码只保留前面的关键帧
- 因此，越重要的中间状态应该越靠前写
- 当前执行层中，多参考图分配主要用于支持多参考图的 Seedance 2.0 类模型
- 其他只支持首尾帧的视频模型会自动忽略 `keyframes`，继续只使用首帧 / 尾帧

**示例：**
- `continuity_mode: "strict"` 且有尾帧 → 最多可写 7 个 `keyframes`
- `continuity_mode: "scene_end"` 且当前不是 scene 最后一个 shot → 无尾帧，最多可写 8 个 `keyframes`
- `continuity_mode: "free"` → 不建议写 `keyframes`，代码会忽略

**示例 — 适合链式：**
- Shot 3: 两人对话中景 → Shot 4: 同一场景两人继续对话，镜头略推近

**示例 — 不适合链式（必须 false）：**
- Shot 1（白素贞+小青）→ Shot 2（许仙独自）：角色不同
- Shot 3（递伞手部特写）→ Shot 4（断桥全景）：景别跳转
- Shot 2（雨天亭下）→ Shot 3（雨中桥上）：场景变化

### 4.6.4 transition_in（剪辑转场）

**每个 shot 必须标注 `transition_in`**，描述从前一个 shot 过渡到本 shot 时使用的剪辑转场效果。第一个 shot 的 transition_in 为 `null`。

**格式：** `{"type": "类型", "duration": 秒数}` 或 `null`

| 转场类型 | 适用场景 | 时长 | FFmpeg 实现 |
|---------|---------|------|------------|
| `"straight-cut"` | 叙事"突然"感（听到脚步声）、反打对望 | 0s | 硬切拼接 |
| `"cross-dissolve"` | 同场景视角切换、景别跳变、空镜→人物 | 0.3-1.0s | xfade fade |
| `"flash-white"` | 闪回进入/退出——模拟记忆闪现 | 0.3s | xfade fadewhite |

**选择逻辑：**
- 景别/视角大跳 + 同场景 → `cross-dissolve`
- 景别/视角大跳 + 跨场景 → `flash-white` 或 `straight-cut`（看叙事意图）
- 连续动作 + 同角色 + `chain_from_previous: true` → `null`（链式无需转场）
- 连续动作但景别微调 → `cross-dissolve` 0.3s（兜底）
- 反打对望 → `straight-cut`（保留张力）
- 从特写到大全景 → `cross-dissolve` 1.0s（呼应节奏放慢）
- 闪回进入/退出 → `flash-white` 0.3s

**示例（白娘子断桥）：**
```
Shot 1→2: flash-white 0.3s（进入闪回）
Shot 2→3: flash-white 0.3s（退出闪回）
Shot 3→4: straight-cut（"忽然"脚步声）
Shot 4→5: cross-dissolve 0.5s（同场景视角切换）
Shot 5→6: cross-dissolve 0.3s（链式+溶解兜底）
Shot 6→7: null（链式连续递伞动作）
Shot 7→8: cross-dissolve 1.0s（特写→大全景）
Shot 8→9: cross-dissolve 0.5s（空镜→人物）
Shot 9→10: straight-cut（反打对望）
```

### 4.7 action_prompt 规则（只写视觉动作！）

**首帧图已经确定了画面，action_prompt 只描述运动/动作变化。** 角色外貌由代码通过结构化 prompt 自动注入到视频生成请求中，此字段**禁止任何外貌描述**。

关键原则 — **"首帧决定视觉"：**
- 首帧图锚定了角色外貌、场景环境、光影氛围
- action_prompt 只需要告诉 Seedance "从首帧到尾帧之间发生了什么运动"
- 描述的动作必须在该镜头的 estimated_duration 内可完成
- 代码会自动构建结构化视频 prompt：`【角色外观设定】+ 【场景动作】+ 【一致性要素】`

**示例：**
- ❌ "The 40-foot tall blue-red robot warrior with riveted armor in the destroyed city at sunset slowly pulls its blade..." （重复了外貌和场景）
- ✅ "The robot slowly pulls its energy blade out of the enemy's chest with a grinding metal sound, the enemy's optics flash once then go permanently dark, the enemy's body tips backward and crashes onto the asphalt sending dust upward"

### 4.8 PONYO 6D 物理系统

光线和天气/物理效果已移至 scene 层级（`lighting` 和 `weather` 字段），同场景所有镜头共享。Shot 层保留镜头相关的参数：

| 字段 | 层级 | 说明 | 必填 |
|------|------|------|------|
| `lighting` | **Scene** | 光源方向+色温+光比+布光风格 | 必填 |
| `weather` | **Scene** | 天气/粒子/物理细节（至少3个要素） | 必填 |
| `camera_movement` | Shot | 运镜方式（tracking/dolly/crane/static/handheld） | 必填 |
| `camera_technical` | Shot | 焦距+光圈（如 "50mm, f/2.8"） | 必填 |
| `speed_baseline` | Shot | 动作速度（慢镜头0.7x / 正常1.0x / 快节奏1.3x） | 必填 |

**weather 三要素（写在 scene 层）：**
1. **粒子材质** — 雨丝、雪花、火花、碎片、灰尘、液体
2. **物理交互** — 碰撞、重力、水面涟漪、雨滴溅射
3. **环境反应** — 湿地面反光、水面波纹、雾气弥漫、温度变化

### 4.9 narration 规则（Seedance 音画同轨）

Seedance 1.5 Pro 支持音画同轨生成（`generate_audio: true`），narration 文本会直接拼入 Seedance 的 prompt。

- 叙事类/解说类内容：写旁白（中文，≤20字/10秒镜头）
- 纯动作/氛围类：留空 `""`
- narration 过长会导致语速过快读不完，严格控制字数

### 4.10 consistency_anchors（一致性锚点）

每个 shot 必须声明"必须出现的视觉元素"，这些锚点会被注入到图片和视频 prompt 中，强制保证一致性。

```json
"consistency_anchors": {
    "characters": [
        {
            "id": "blue_mecha",
            "must_show": ["glowing blue optics", "chest insignia", "shoulder exhaust pipes"],
            "expression": "determined"
        }
    ],
    "environment": ["destroyed intersection", "golden hour lighting"]
}
```

规则：
- `must_show`：从该角色的 `key_features` 中选取 2-3 个在此镜头中必须可见的特征
- `expression`：该角色在此镜头中的情绪/表情状态
- `environment`：该镜头必须出现的环境要素（保证跨镜头场景连贯）

### 4.11 分镜连续性规则（Shot-to-Shot Continuity）

**分镜不是独立的幻灯片，是一条连续的视觉流。** 写完所有分镜后，必须逐对检查连续性。

#### 规则1：相邻镜头状态连续

**Shot N 的 scene_prompt（首帧）必须与 Shot N-1 的 end_frame_description（尾帧）在以下维度保持一致：**

| 维度 | 说明 | 违规示例 |
|------|------|----------|
| 角色位置 | 角色在画面中的位置不能跳变 | 尾帧角色在桥左端 → 首帧角色在桥右端 |
| 角色状态 | 湿/干、站/坐、持有物品 | 尾帧衣服湿透 → 首帧衣服干燥 |
| 道具持有 | 手中物品不能凭空出现/消失 | 尾帧没有伞 → 首帧突然撑伞 |
| 角色数量 | 画面中角色不能无理由增减 | 尾帧2人 → 首帧3人（无交代） |
| 环境状态 | 场景中的变化应该延续 | 尾帧地上有散落书页 → 首帧地面干净 |

**跨场景时同样适用** — 前一个 scene 最后一个 shot 的 end_frame 和下一个 scene 第一个 shot 的 scene_prompt 之间也必须连续。

#### 规则2：跨场景视觉过渡

当两个相邻 scene 的光线、天气、时间存在显著差异时（如雨天→晴天、白天→黄昏），**不能硬切**。必须满足以下任一条件：

1. **渐变过渡** — 前一 scene 的最后一个 shot 的 end_frame 已经开始暗示变化（如"云层开始散开，一丝金光从云缝透出"），下一 scene 的第一个 shot 延续这个趋势
2. **过渡镜头** — 在两个 scene 之间插入一个独立的过渡 shot（空镜：天空延时、水面光影变化、云层流动等），作为视觉桥梁
3. **时间跳跃标记** — 如果确实需要大幅时间跳跃（如白天→夜晚），在前一 scene 的最后一个 shot 的 end_frame 中加入收束画面（如淡出、远景缩小），暗示段落结束

**禁止：** 前一个 shot 还在大雨倾盆，下一个 shot 突然阳光灿烂、雨停风止。

#### 规则3：首帧-动作-尾帧因果链

每个 shot 内部：
```
scene_prompt（起始状态）→ action_prompt（运动过程）→ end_frame_description（结果状态）
```
必须构成**因果链**：起始状态 + 运动 = 结果。不能出现结果中包含起始状态不存在的元素（除非 action_prompt 中交代了来源）。

### 4.12 分镜自检清单（写完后必须执行）

**写完所有分镜后，逐对执行以下检查。发现问题必须修正后再输出。**

```
□ 逐对检查 Shot N end_frame → Shot N+1 scene_prompt 的状态连续性
  - 角色位置、状态、道具持有是否一致？
  - 是否有凭空出现/消失的元素？

□ 检查每个 scene_prompt 是否是静态起始状态
  - 是否描述了"即将发生"而非"正在发生"的动作？
  - 是否适合生成一张静态首帧图？

□ 检查跨 scene 边界的视觉过渡
  - 光线/天气/时间是否有突变？
  - 如有突变，是否有渐变、过渡镜头或时间跳跃标记？

□ 检查首帧-动作-尾帧因果链
  - 每个 shot 的 scene_prompt + action_prompt 能否自然导出 end_frame_description？
  - end_frame_description 中是否有无中生有的元素？
```

### 4.13 全剧本一次性生成

**必须在一次输出中生成所有 shots 的完整 JSON。不要逐场景分批生成。**

一次性生成的目的是让你拥有全局视角，确保：
- 跨镜头的角色行为一致性（同一角色不能在不同镜头表现出矛盾的性格）
- 首尾帧衔接的物理连贯性（Shot N 的 end_frame 必须和 Shot N+1 的 scene_prompt 匹配）
- 情绪弧线的自然过渡（不能突然跳跃）
- consistency_anchors 中 must_show 特征在相邻镜头间保持一致

生成前，先回顾所有 scenes，在心中规划好每个 shot 的内容和衔接，然后一次性输出。

---

## 步骤5: 素材生成 + 视频合成

调用 Python 脚本生成素材，然后合成最终视频。

```bash
# 素材生成
cd /path/to/skills/xyz-video-skill/scripts
python3 ad_assets.py \
    --storyboard {output_dir}/storyboard.json \
    --output_dir {output_dir}/assets \
    [--verbose]

# 视频合成
python3 ad_compose.py \
    --storyboard {output_dir}/storyboard.json \
    --assets {output_dir}/assets/assets.json \
    --output_dir {output_dir}/videos \
    [--platform youtube douyin wechat]
```

**输出：** `{output_dir}/assets/assets.json` → `{output_dir}/videos/{platform}.mp4`

素材生成脚本会自动：
1. 按 `scenes > shots` 结构迭代（向下兼容旧的 flat `shots` 格式）
2. 校验所有 shot 的 `end_frame_description` 不为空（空则报错拒绝运行）
3. 从 `storyboard.characters` 读取角色外貌（**唯一真相来源**），自动注入到每个 prompt
4. 将 scene 层的 `lighting`、`weather`、`props`、`environment_description` 注入到同场景所有 shot 的 prompt 中（**视觉基底共享**）
5. 用 ContentFilter 过滤 `scene_prompt` 中残留的外貌描述（安全网）
6. **全局 prompt 提取**：将完整 narrative + 所有 shot 的 narrative_segment / scene_prompt / action_prompt / end_frame_description 一次性交给 LLM，为每个 shot 提取：
   - `first_frame_prompt` — 带前后文衔接的首帧视觉描述
   - `last_frame_prompt` — 带前后文衔接的尾帧视觉描述
   - `video_action_prompt` — 带故事方向的动作描述（给 Seedance 用）
   LLM 看到完整故事线 + 所有镜头上下文，提取出的 prompt 天然连贯。
7. 按 `characters_in_shot` 逐个传角色参考图给图片模型
8. 每个 shot：用 first_frame_prompt 生成首帧图 → 用 last_frame_prompt 生成尾帧图 → Seedance I2V 视频（首帧+尾帧+video_action_prompt）
9. 如果 shot 标记了 `chain_from_previous: true`，从前一 shot 视频提取实际尾帧作为首帧（否则独立生成）
10. 尾帧图根据 `continuity_mode` 决定是否生成：`"strict"` 强制生成，`"scene_end"` 仅 scene 末尾生成，`"free"` 跳过
11. **视频质量自动检测**：每段视频生成后自动执行三重检测（详见下方），不合格则自动重试或裁剪
12. 生成 BGM

### 6.1 两阶段视频质量审查

每段视频生成后，系统会自动执行两阶段质量审查：**阶段1 - 粗筛检测**自动标记风险片段，**阶段2 - LLM 视觉判断**由你（母模型）看图做最终裁定。

**阶段1：粗筛检测（自动）**

粗筛层使用规则检测潜在问题，导出风险片段和关键帧供 LLM 判断：

**第一重：帧间突变 + 闪烁检测**

- 提取全视频帧（缩小到 160px 加速），计算相邻帧 MSE（像素均方差）
- 用前 1/4 帧的中位数建立"稳定基准"，超过基准 8 倍判定为突变
- 滑动窗口平滑 MSE，消除自然运动振荡的干扰
- 正向扫描找第一个连续异常段，反向扫描找最后一个稳定点
- 闪烁检测：如果 MSE 出现奇偶帧交替跳变（高-低-高-低，连续 8 次以上），判定为闪烁伪影

**第二重：局部突变检测（spike detection）**

- 用 ±12 帧（约 0.5 秒）滑动窗口计算局部 MSE 均值
- 某帧 MSE 超过局部均值 2.5 倍且绝对值 >150 → 标记为 spike
- 多个 spike 聚集 → 标记为风险片段
- 用于捕捉单帧或少数帧的面部变形、画面撕裂

**第三重：人脸变形检测（OpenCV DNN）**

- 使用 OpenCV DNN SSD 人脸检测器
- 每 3 帧采样一次，追踪人脸置信度变化
- 如果曾连续 3 个采样点检测到人脸（置信度 ≥0.5），后来置信度骤降到 <0.3 且不再回来 → 判定为人脸变形
- 如果脸消失后 2 秒内又回来（如角色转身），不判定为变形

**第四重：重复角色检测（HOG + 人脸相似度）**

- HOG 人体检测：检测画面中的多个人体
- 人脸相似度：对检测到的多个人体/人脸计算相似度
- 相似度 ≥ 0.70 → 标记为 identity_hallucination（重复角色）
- 用于捕捉同一角色在画面中重复出现的问题

**粗筛输出：**
- `quality_audit.json` - 审计报告，status: `pending_judgment`
- `vision_bundle_attempt_N/` - 风险片段的关键帧图片
- `vision_judge_request.json` - 判断请求

**阶段2：LLM 视觉判断（你来执行）**

当粗筛检测到风险片段后，系统会暂停并等待你的判断。

**⚠️ 关键要求：你必须认真查看每一帧图片**

1. **使用 Read 工具读取 vision_bundle 中的所有关键帧图片**
2. **详细描述你看到的画面内容**：
   - 画面中有几个角色？
   - 每个角色在哪里？（左侧、右侧、前景、背景）
   - 角色在做什么动作？
   - 是否有重复的角色？（同一角色出现多次）
   - 人脸是否变形或模糊？
3. **对比粗筛报告**：粗筛说的问题是否真实存在？
4. **给出判断**：创建 `vision_judge_result.json`

**⚠️ 严禁的错误做法：**
- ❌ 看到 "Tool ran without output or errors" 就认为图片读取成功，然后不描述内容直接下结论
- ❌ 只看一两帧就判断整个片段
- ❌ 不描述画面内容，直接说"没问题"或"有问题"

**正确的判断流程：**
```
1. Read 第1帧 → 描述："我看到画面左侧有一个大的悟空头部特写，画面下方地面上有一个小的悟空身影"
2. Read 第2帧 → 描述："两个悟空仍然存在，位置略有变化"
3. 对比粗筛：粗筛报告说 identity_hallucination，我确实看到了两个悟空
4. 判断：确认是重复角色问题，action: cut_segment
```

**判断结果格式（vision_judge_result.json）：**

```json
{
  "shot_id": "002",
  "segments": [
    {
      "start": 2.5,
      "end": 3.125,
      "issue_type": "identity_hallucination",
      "severity": "high",
      "confidence": 0.95,
      "action": "cut_segment",
      "reason": "我看到画面左侧有大的悟空头部特写，画面下方有小的悟空身影，确认是重复角色"
    }
  ],
  "overall_action": "cut_segment",
  "fallback_used": false
}
```

**决策规则：**
- `keep` - 所有片段都没问题
- `cut_segment` - 问题片段总时长 < 50% 且剩余视频 ≥ 3秒
- `regenerate` - 问题片段总时长 ≥ 50% 或剩余视频 < 3秒

**系统会自动合并相邻片段**（间隔 < 0.5s）

**质量审查完整流程：**

```
视频生成完成
  ↓
阶段1：粗筛检测 → 导出 vision_bundle → status: pending_judgment
  ↓
【暂停，等待你的判断】
  ↓
阶段2：你看图判断 → 创建 vision_judge_result.json
  ↓
系统读取判断结果 → 执行决策：
  - keep → 通过（status: finalized）
  - cut_segment → 裁剪问题片段（status: finalized）
  - regenerate → 重新生成视频（status: applied，继续循环）
```

素材生成完毕后，运行合成脚本：
- 读取每个 shot 的 `transition_in` 字段，按转场类型拼接（straight-cut / cross-dissolve / flash-white）
- 混合 BGM
- 输出最终视频

---

## 配置

API 密钥配置在 `config/api_keys.yaml` 或通过环境变量：
- `VOLCENGINE_API_KEY` — 火山引擎（图片生成）
- `APIMART_API_KEY` — ApiMart（Gemini 图片生成）
- `FAL_KEY` — fal.ai（图片生成 fallback + BGM）
- `BYTEPLUS_API_KEY` — BytePlus Seedance（视频生成）

---

## 常见错误自查

| 问题 | 原因 | 解决 |
|------|------|------|
| 角色在不同镜头变样 | scene_prompt 里写了外貌导致漂移 | 遵守单一真相源：scene_prompt 禁止写外貌，外貌由代码从 characters 自动注入 |
| 视频里角色倒退/瞬移 | 首帧到尾帧的动作路径不合理 | 检查 end_frame 和下一帧 scene_prompt 是否物理连贯 |
| 动作做不完 | 一个镜头塞了太多动作 | 拆成多个镜头，每个只做一个动作 |
| 旁白读不完 | narration 文字太长 | ≤20字/10秒镜头 |
| 视频画面随机漫游 | end_frame_description 为空 | 每个 shot 必须填写 end_frame_description |
| 图片风格不一致 | prompt 风格描述不统一 | 用 style_anchor 统一风格 |
| 启动报错 end_frame | storyboard 中某 shot 缺少 end_frame_description | 每个 shot（包括最后一个）都必须填 |
| 图片是黑色占位图 | API 密钥过期或余额不足 | 检查 config/api_keys.yaml 中的 key 是否有效 |
