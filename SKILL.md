---
name: xyz-video-skill
description: "视频生成 skill。对话驱动的端到端视频生成流程：输入主题或故事，完成角色设计、剧本框架、分镜脚本、素材生成与视频合成。"
---

# XYZ Video Skill

## 触发条件
用户提到"生成视频"、"广告片"、"短片"、"商业视频"、"产品广告"、"做一个视频"等关键词。

## 工作流程概览

你（宿主 LLM）负责所有思考任务，Python 脚本只负责调用外部 API 生成素材和合成视频。

**视频质量检测标准：** 在检查单个 shot、分析风险片段、执行视觉裁定、以及整片复检时，必须参考 [reference/video_quality_standard.md](/Users/huangzongning/openclaw/skills/xyz-video-skill/reference/video_quality_standard.md)。不要只依据数值异常判断，必须按标准文件中的维度逐项检查。

**视频生成参考图策略标准：** 在生成 storyboard 时，必须参考 [reference/video_generation_strategy.md](/Users/huangzongning/openclaw/skills/xyz-video-skill/reference/video_generation_strategy.md)。先做语义判断，再把结果优先落成 `video_references`；`shot_type`、`continuity_mode`、`keyframes`、`chain_from_previous` 仍然要写，但它们已经不是视频参考协议的主脑。

**当前 skill 的关键协议已经升级：**

- `storyboard.json` 现在不是旧的松散 shot 列表，而是以 `scenes > shots` 为主结构
- 每个 shot 默认应该显式包含：
  - `shot_type`
  - `subject_constraints`
  - `continuity_mode`
  - `chain_from_previous`
- legacy storyboard 会在执行时被 `run_pipeline.py` 规范化到 `_normalized/storyboard.json`
- 规范化过程会输出 `_normalized/storyboard_migration_report.json`

**当前成片流程已经不是”素材生成完直接合成”那么简单，而是：**

1. 分镜生成
2. 导演写帧级 prompt
3. 角色参考图生成
4. 图片/视频素材生成
5. 导演审图（可选）
6. 视频质量粗筛
7. 母模型视觉裁定
8. pair-level 编辑决策
9. FFmpeg 合成

也就是说：
- 思考在母模型脑子里完成，生图模型只管执行
- 导演审图是主流程的一部分（可选）
- 质量审查是主流程的一部分
- 编辑决策也是主流程的一部分
- 不能再把 compose 理解成”按每个 shot 的 transition_in 直接拼起来”
- 当前视频生成 provider 主路径以 Ark Seedance 2.0 为主；对你来说，storyboard 现在应优先按 `video_references` 的用途协议来约束视频，而不是把“首帧 / 尾帧 / keyframes”当成唯一主结构

```
步骤1: 故事创作              → 你与用户对话讨论方向，输出 story.json
步骤2: 剧本框架 + 角色设计    → 你思考，输出 framework.json（从 narrative 切分 scenes + 设计角色）
步骤3: 角色参考图生成        → python3 scripts/ad_assets.py --mode character_refs
步骤4: 分镜脚本              → 你思考，输出 storyboard.json（scenes > shots，从 narrative 派生）
步骤4.5: 导演写帧级 prompt    → 你思考，输出 director_prompts.json（逐帧画面描述）
步骤5: 素材生成              → python3 scripts/ad_assets.py [--review-mode director_review]
步骤5.5: 导演审图（可选）     → 你查看生成的图片，判断 keep/regenerate
步骤6: 编辑决策 + 视频合成    → python3 scripts/ad_compose.py
```

**审图模式选择建议：**
- `metrics_only`（默认）- 只做自动质量检测，适合快速迭代或对生图质量有信心
- `director_review` - 每张图片生成后暂停，由你审图。适合：
  - 需要严格把控画面质量的商业项目
  - 复杂的角色姿态或情感表达
  - 对连续性要求高的场景（如角色在不同镜头间的一致性）
  - 第一次使用新的角色设定或场景风格
- `hybrid_judge` - 图片质量粗筛 + 视频视觉判断，介于两者之间

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

**narrative 是整个视频的故事主线。** 后续步骤2的场景拆分、步骤4的分镜脚本、步骤5的帧级 prompt / 动作描述提取，都从 narrative 派生。它不是摘要，不是大纲，而是一段**可以直接朗读的、有画面感的连贯叙事**。

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
            "ref_description": "传给图片模型的角色说明（可用英文，供图片模型理解角色参考图）"
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
- **ref_description**：这是给图片模型的角色参考说明，允许使用英文，因为角色参考图阶段仍可能由图片模型受益于英文描述。

### 2.3 语言规则

从现在开始，必须明确区分两类文本：

1. **视频侧 / storyboard 侧字段：默认使用中文**
   - `narrative_segment`
   - `scene_prompt`
   - `end_frame_description`
   - `action_prompt`
   - `lighting`
   - `weather`
   - `environment_description`
   - `props`
   - `motion_control.phase_beats`
   - `subject_constraints.semantic_rules`
   - 其他直接服务于视频生成和视频审查的描述字段

2. **图片侧字段：允许使用英文**
   - `characters.*.ref_description`
   - 如后续存在专门传给图片模型的 image prompt，可按图片模型需要使用英文

原则：
- 字节 Seedance 视频链路支持中文，所以 storyboard 和视频提示词文件不再默认使用英文
- 以后只要是视频生成相关的文本描述，默认都写中文
- 不要再把“英文 prompt 习惯”带回 storyboard 主文件

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

**⚠️ 当前必须额外考虑两个层面的结构化约束：**

1. `shot_type`
- `visible_subject`
- `offscreen_reaction`
- `transition_reveal`
- `free_atmosphere`

2. `subject_constraints`
- `required_visible_subjects`
- `optional_visible_subjects`
- `offscreen_subjects`
- `continuity_subjects`
- `forbidden_visible_subjects`
- `semantic_rules`

这两个字段不是注释，它们会直接进入图片 prompt、视频 prompt、质量审查和后续编辑决策。

**⚠️ 强导演模式只用于复杂 shot，普通 shot 保持轻量。**

以下 shot 必须进入**强导演模式**，先写 `director_plan` 再写字段：

1. `shot_type = "transition_reveal"`
2. 强动作 shot
3. 明显情绪转折、身份确认、关系转折 shot
4. `chain_from_previous = true` 的高衔接依赖 shot
5. 中间需要 2 张及以上 `keyframes` 的 shot

其他普通 shot 可以继续轻量写法，只要：
- `scene_prompt` 清楚定义起点
- `action_prompt` 清楚定义过程
- `end_frame_description` 清楚定义落点

**⚠️ 强导演模式下，先做导演设计，再做语义决策，最后才写字段。**

在强导演模式下，母模型必须先完成一层 `director_plan`：

1. 这镜**只完成什么戏剧动作**
2. 这镜**明确不完成什么**
3. 观众**按什么顺序获得信息**
4. 谁是**变化主体**，谁是**稳定主体**
5. 这镜要拆成**几个必要阶段**

只有 `director_plan` 清楚后，才进入参考图策略判断。

在写每个 shot 之前，必须先按 [reference/video_generation_strategy.md](/Users/huangzongning/openclaw/skills/xyz-video-skill/reference/video_generation_strategy.md) 判断这 4 个维度：

1. 动作复杂度
2. 状态变化幅度
3. 起止姿态约束强度
4. 与前后镜头的衔接依赖

然后再决定该 shot 需要哪些用途驱动参考素材：

- `first_frame`
- `reference_character`
- `reference_prop`
- `reference_composition`
- `reference_style`
- `reference_stage`
- `reference_target_state`

最后才把这个决策落成字段：

- `video_references`
- `reference_strategy`（可选的分析字段）
- `shot_type`
- `continuity_mode`
- `keyframes`（兼容表达中间阶段）
- `time_beats`
- `chain_from_previous`

不要反过来先凭感觉写字段，再让字段替代判断。

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
            "lighting": "光线参数（色温、方向、质感，中文）",
            "weather": "天气/粒子效果（中文）",
            "props": ["道具1", "道具2"],
            "environment_description": "环境视觉描述（中文，80-150字。同场景所有镜头共享的视觉基底）",
            "scene_continuity": {
                "stable_facts": {
                    "spatial_layout": ["稳定空间关系事实1", "稳定空间关系事实2"],
                    "prop_states": ["稳定道具状态事实1"],
                    "environment_states": ["稳定环境状态事实1"],
                    "character_states": ["稳定角色状态事实1"]
                },
                "entity_registry": {
                    "prop_id": {
                        "count": 1,
                        "holder": "character_id_1.right_hand",
                        "persistent_state": "持续状态描述"
                    }
                },
                "carry_forward_subjects": ["character_id_1", "character_id_2", "prop_id"]
            },
            "shots": [
                {
                    "id": 1,
                    "characters_in_shot": ["character_id_1", "character_id_2"],
                    "narrative_segment": "本镜头对应的 narrative 片段（从 scene 的 narrative_segment 中进一步切分）",
                    "director_plan": {
                        "dramatic_core": "这镜只完成什么戏剧动作",
                        "not_this_shot": "这镜明确不完成什么",
                        "viewer_information_flow": [
                            "观众先知道什么",
                            "观众再知道什么",
                            "观众最后情绪落到哪里"
                        ],
                        "camera_intent": {
                            "shot_purpose": "观察|揭示|压迫|跟随|等待",
                            "framing_base": "基础景别和构图",
                            "camera_contract": "机位、轴线、构图变化允许范围"
                        },
                        "stable_subjects": ["这一镜里基本不变的人或物"],
                        "changing_subjects": ["这一镜里主要发生变化的人或物"],
                        "invariants": ["整镜不变项1", "整镜不变项2"],
                        "allowed_progressions": ["整镜允许推进的变化1"],
                        "nodes": [
                            {
                                "id": "n1",
                                "story_function": "第一个阶段负责什么",
                                "visual_focus": "观众先看哪里",
                                "must_show": ["该阶段必须出现的内容"],
                                "must_not_show": ["该阶段不能提前完成的内容"],
                                "delta_from_previous": "相对上一阶段的主变化"
                            }
                        ]
                    },
                    "scene_prompt": "故事起点 + 起始画面状态（中文，从 narrative_segment 派生。见 5.5 规则）",
                    "end_frame_description": "故事终点 + 结束画面状态（必填！从 narrative_segment 派生。见 5.6 规则）",
                    "action_prompt": "动作描述（中文，从 narrative_segment 派生，只写运动过程）",
                    "camera_movement": "运镜方式",
                    "camera_technical": "焦距+光圈",
                    "speed_baseline": "1.0x",
                    "narration": "",
                    "tts_text": "",
                    "subtitle": "",
                    "estimated_duration": 8,
                    "reference_strategy": "anchor_with_end",
                    "shot_type": "visible_subject",
                    "chain_from_previous": false,
                    "continuity_mode": "scene_end",
                    "time_beats": [
                        "0-2s：主体保持当前状态，镜头先建立画面",
                        "2-4s：出现第一段明确的动作或状态推进",
                        "4-6s：镜头落到最终结果状态"
                    ],
                    "subject_constraints": {
                        "required_visible_subjects": ["character_id_1"],
                        "optional_visible_subjects": ["character_id_2"],
                        "offscreen_subjects": [],
                        "continuity_subjects": ["character_id_1"],
                        "forbidden_visible_subjects": [],
                        "semantic_rules": ["保持本镜头内的动作与主体关系连续。"],
                        "pose_contract": ["如果该角色在首帧、关键帧、尾帧之间必须保持同一承重姿态，就在这里写固定身体支撑关系。"],
                        "gaze_contract": {
                            "character_id_1": {
                                "primary_target": "character_id_2",
                                "target_zone": "画面右侧近处"
                            }
                        }
                    },
                    "shot_delta": [
                        "本镜头只允许发生的变化1",
                        "其他空间关系、姿态、道具状态保持不变"
                    ],
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

### 4.x 当前分镜额外约束

- `shot_type` 必须当成生成策略字段，而不是普通标签
- `offscreen_reaction` shot 必须明确写清楚谁不能露出
- `transition_reveal` shot 必须明确写清楚 reveal 前后谁要保持连续
- 对多人强交互镜头，`action_prompt` 和 `motion_control.phase_beats` 必须能表达一条连续动作链
- 如果这是同一时刻的连续切镜，要用 `chain_from_previous=true`
- 如果只是同场景连续动作但不是同一视觉时刻，不要滥用 `chain_from_previous`
- 如果下一镜 `chain_from_previous=true`，当前镜必须产出可复用的结束状态，通常应强制尾帧
- `keyframes` 是否需要，不由“有没有这个字段”决定，而由该 shot 的语义复杂度决定；字段只是承载决策结果

### 4.4 director_plan（强导演模式启用）

`director_plan` 是复杂 shot 的导演设计层。它首先服务于母模型，不直接给最终用户看；但它决定后续 `scene_prompt / action_prompt / end_frame_description / keyframes` 应该怎么写。

不要对所有 shot 一刀切。普通镜头可以轻量写法；只有高风险、高阶段依赖镜头才强制进入这层。

核心规则：

1. 一个 shot 只完成**一个戏剧动作**
2. `director_plan.nodes` 是这个 shot 的**阶段节点**
3. 第一个节点 = 首帧锚点
4. 最后一个节点 = 尾帧锚点
5. 中间节点 = `keyframes`

也就是说：
- 不先想“要几张 keyframe”
- 先想“这个 shot 必须经过几个阶段”
- 如果阶段太多、跳变太大，不是继续加 keyframe，而是这个 shot 应该拆开

最小判断顺序：

1. `dramatic_core`：这镜只完成什么
2. `not_this_shot`：这镜明确不完成什么
3. `viewer_information_flow`：观众先知道什么、再知道什么、最后落在哪里
4. `nodes`：把这个过程拆成 2-4 个必要阶段
5. 再把这些阶段投影成现有字段

投影规则：
- `scene_prompt` = `nodes[0]` 的故事起点和起始画面状态
- `end_frame_description` = `nodes[-1]` 的明确结果落点
- `keyframes` = `nodes[1:-1]`
- `action_prompt` = 各节点之间的过渡过程
- `continuity_mode` = 最终落点约束强度
- 建议每个 shot 显式产出 `reference_strategy`，作为“语义决策层”的可见结果
- 如果镜头内部存在明确阶段推进，建议显式产出 `time_beats`
- 连续场景应优先声明 `scene_continuity`
- 人物支撑姿态不能漂移的镜头应写 `subject_constraints.pose_contract`
- “看向谁”本身是叙事推进关键时，应写 `subject_constraints.gaze_contract`
- 每个 shot 最好显式写出 `shot_delta`，明确“这一镜只改变什么”

### 4.x 当前视频 prompt 的额外 guardrails

当前视频 prompt 应区分两层：

- 审阅层：可保留分镜主旨、策略判断、设计理由，供人审阅
- 模型层：只保留给 Seedance 的可执行视觉描述

所以你在写 `action_prompt`、`time_beats`、`motion_control` 时，不要混入“让观众感受到”“为后面做准备”“执拗感更强”这类抽象导演评论。模型层只写谁在动、动哪里、画面关系如何变化。

### 4.1 场景（Scene）层级 — 视觉基底共享

**同一个场景下的所有镜头共享完全相同的视觉基底**：光线、天气、道具、环境描述。这是风格一致性的核心机制。

**Scene 字段说明：**

| 字段 | 说明 | 示例 |
|------|------|------|
| `id` | 场景唯一ID | `"scene_1"` |
| `name` | 场景名称 | `"断桥雨中"` |
| `location` | 地点 | `"杭州西湖断桥"` |
| `narrative_segment` | 对应 narrative 中的原文段落 | 从 framework scenes 复制 |
| `lighting` | 光线参数（色温、方向、质感，中文） | `"阴天漫射冷光，偏冷蓝灰，约6500K"` |
| `weather` | 天气/粒子效果（中文） | `"细密冷雨，远处湖面薄雾"` |
| `props` | 场景核心道具列表 | `["oil-paper umbrellas", "stone bridge"]` |
| `environment_description` | 环境视觉描述（中文，80-150字） | 地面材质、远景、建筑、水面等完整描述 |

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
- **每个镜头 5-12 秒** — 但 `estimated_duration` 不应主要按动作复杂度拍脑袋决定，而应按**导演节奏**决定。Seedance API 支持 [5, 12] 区间任意整数。
- **首帧到尾帧的动作路径必须物理合理** — 不能出现角色瞬移、位置互换、违反惯性的运动
- 总镜头数 ≈ total_duration ÷ 平均镜头时长（通常 7-10 秒/镜头）

**导演版时长判断顺序：**
1. 先看这镜有几个必要阶段
2. 再看观众需要多久看清关键信息
3. 再看最终落点是否需要停留半拍到一拍
4. 最后再看它在整段剪辑里是快切镜还是收束镜

**不要再这样想：**
- 轻动作 = 5-6 秒
- 中动作 = 7-9 秒
- 重动作 = 10-12 秒

**要改成这样想：**
- **2 个阶段 + 无明显停留** → 可短
- **3 个阶段 + 需要完成确认 / reveal** → 中等
- **3 个阶段 + 情绪落点需要停留** → 中偏长
- **4 个阶段以上** → 先考虑拆 shot，不先靠拉长时长解决

一句话：
- **estimated_duration = 阶段推进时间 + 落点停留时间**
- 不是 **estimated_duration = 动作复杂度区间**

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

**scene_prompt = 第一个阶段的故事起点 + 视觉起始状态。** 环境/光线/天气不需要写——由 scene 层自动提供。默认用中文撰写。

**⚠️ 核心原则：scene_prompt 不是孤立的画面描述，它是故事线上的一个锚点。**

代码会用 scene_prompt + action_prompt + end_frame_description 三个字段的完整故事上下文，通过 LLM 提取出精确的首帧生图 prompt。所以 scene_prompt 的职责是**讲清楚这个镜头第一个阶段的故事起点**：

1. **第一阶段的任务** — 这个阶段只负责建立什么
2. **视觉起始状态** — 角色的精确位置、姿态、朝向、手持物品、空间关系
3. **构图** — 景别、角度

不要把整个 shot 的发展过程塞进 `scene_prompt`。如果写成“这镜发生什么”，首帧就会偷跑到中段甚至终点。

**三段式故事线：**
| 字段 | 故事作用 | 描述什么 |
|------|----------|----------|
| `scene_prompt` | 第一个阶段 | 动作即将开始时的画面状态 |
| `action_prompt` | 阶段过渡 | 阶段1如何到阶段2，阶段2如何到阶段3 |
| `end_frame_description` | 最后一个阶段 | 动作完成后的明确落点 |

三个字段构成一条**因果链**。代码会将完整因果链交给 LLM 提取首帧/尾帧生图 prompt，使首尾帧天然带有故事方向，Seedance I2V 能顺着这个方向做运动插值。

**示例 — 递伞场景：**
- ❌ "女人把伞递向书生"（纯动作，无故事上下文，无起始状态）
- ✅ "她看见书生浑身湿透地蜷在亭边石阶上，终于压不住心里的怜悯，已经从桥上走到亭边。她手里稳稳举着油纸伞，低头看向仍在喘息的书生，脚边散着被雨打湿的纸页。她正准备再向前一步，把伞递过去。平视中景双人镜头，两人相距约两米。"（有意图、有状态、有空间关系）

**示例 — 战斗场景：**
- ❌ "机器人挥刀砍向敌人"（动作正在发生，无故事起点）
- ✅ "它已经看准敌方防线的破口，身体在路口中央稳稳锁定目标，刀臂高高抬起，蓄势待发，正处于致命一击落下前的那一瞬。低机位英雄式构图。"（有意图、有蓄力姿态、有叙事张力）

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

代码会用完整故事上下文（scene_prompt + action_prompt + end_frame_description）通过 LLM 提取出精确的尾帧生图 prompt。所以 end_frame_description 的职责是**讲清楚这个镜头最后一个阶段的明确落点**：

1. **结果状态** — 动作完成后，故事推进到了什么状态
2. **视觉结束状态** — 角色的精确位置、姿态、表情、手持物品
3. **下镜接口**（如需要）— 为下一镜头的视觉衔接留下稳定落点

不要把“也许会发生什么”写进 `end_frame_description`。它负责的是最后一个阶段已经到达的状态，不是泛泛的结尾气氛。

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

每个 shot 必须标注 `continuity_mode`，但这个字段应当是语义决策的结果，不是随手填写。代码根据它决定是否生成尾帧图。

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

先说新的主原则：

- Seedance 2.0 的多参考图主脑不是“首帧 / 尾帧 / 中间硬关键帧插值”
- 执行层现在会把所有视频参考素材统一归一成 `video_references`
- 每张参考图都必须先定义**用途**，再在视频 prompt 里用 `@图片N` 显式调用
- `keyframes` 仍保留，但它现在是“中间阶段参考”的兼容写法，不再代表模型必须命中的时间点

`keyframes` 是否需要，先看导演阶段，不先看字段。

核心规则：
- `keyframes` 不是“中间插几张图”
- `keyframes` 是这个 shot **不能省略的中间阶段**
- 第一个阶段属于 `scene_prompt`
- 最后一个阶段属于 `end_frame_description`
- 只有中间阶段才写成 `keyframes`

也就是说：
- `director_plan.nodes = [起点, 中段1, 中段2, 终点]`
- 那么 `keyframes = [中段1, 中段2]`

只有当以下至少一类需求成立时，才应该写 `keyframes`：

- 动作复杂度中到高
- 状态变化幅度中到高
- 中间过程比终点本身更重要
- 首尾两点不足以稳定约束镜头过程

`continuity_mode: "strict"` 往往会和 `keyframes` 同时出现，但不是唯一前提。

如果一个镜头内有多个不能省略的中间阶段，不是简单的首帧 A → 尾帧 B 单向运动，可以标注中间关键帧：

```json
"keyframes": [
  {"timestamp": 3.0, "description": "角色转身面对镜头，表情从平静变为惊讶"},
  {"timestamp": 6.0, "description": "角色举起手中的伞，伞面完全展开"}
]
```

**规则：**
- `timestamp` 必须在 `0` 到 `estimated_duration` 之间，且按时间递增
- `description` 遵守单一真相源：写姿态、位置、动作、场景，不写外貌
- `keyframes` 是 storyboard 的兼容字段，用来表达“镜头中间关键状态”
- 当前执行层会把它们映射成 `video_references[].usage = "reference_stage"`
- Seedance 2.0 看到的是“用途驱动参考素材 + @引用调用”，不是旧式时间点插值协议
- 其他只支持有限参考图的视频模型会按用途优先级裁剪参考素材

### 4.6.3.0 video_references（视频参考素材协议，推荐）

从现在开始，写 storyboard 时推荐把视频参考素材显式写成 `video_references`。

推荐用途：
- `first_frame`
- `reference_character`
- `reference_prop`
- `reference_composition`
- `reference_style`
- `reference_color`
- `reference_stage`
- `reference_target_state`

执行层会优先读取 `video_references`；如果没写，才会从首帧 / 角色参考图 / 场景图 / `keyframes` / 尾帧自动合成一份用途驱动参考清单。

示例：

```json
"video_references": [
  {"source_type": "frame", "source_id": "first_frame", "usage": "first_frame"},
  {"source_type": "scene", "usage": "reference_composition"},
  {"source_type": "character", "source_id": "medical_girl", "usage": "reference_character", "subject": "medical_girl"},
  {"source_type": "prop", "source_id": "oil_paper_umbrella", "usage": "reference_prop", "subject": "oil_paper_umbrella"},
  {"source_type": "stage", "source_id": "1", "usage": "reference_stage", "stage": "confirm_source"},
  {"source_type": "frame", "source_id": "target_state", "usage": "reference_target_state"}
]
```

语义原则：
- 先定义“这张图拿来干什么”
- 再让执行层在视频 prompt 中写成 `@图片1 作为首帧`、`@图片2 参考角色` 这类显式调用
- 不再把所有图片都当成同权重的 `reference_image`

**何时标注 `keyframes`：**
- 镜头内有明确的阶段性姿态变化（如：蹲下 → 起身 → 转身）
- 需要精确控制中间某个时刻的画面状态
- 动作路径复杂，首尾帧不足以约束
- 情绪或关系变化需要经过明确的多个状态节点

**何时应该拆 shot，而不是继续加 `keyframes`：**
- 中间阶段已经超过 4 个
- 相邻阶段之间不止一个主变化
- 人物状态、镜头尺度、空间关系同时大跳
- 某个中间阶段已经像“另一镜”而不是“同一镜中的一步”

**何时不要标注：**
- 简单的 A → B 运动（走、转头、伸手）
- 连续流畅的动作（跑步、挥手）
- 大部分普通推进镜头
- `offscreen_reaction` / 纯氛围镜头 / 不应出现中间实体 reveal 的镜头

### 4.6.3.1 time_beats（时间节拍，可选但重要）

`time_beats` 用来表达镜头内部的时间推进，主要服务于最终给 Seedance 的视频 prompt。

推荐格式：

```json
"time_beats": [
  "0-2s：固定近景，主体保持当前状态",
  "2-4s：出现第一段明确动作或状态变化",
  "4-6s：镜头落到最终结果状态"
]
```

规则：
- `time_beats` 是给模型看的时间脚本，必须写成可执行的视觉描述
- 不要写“让观众感到”“情绪更强”“为后面做准备”这类抽象导演语言
- 要写清楚谁在动、动哪里、画面位置是否变化、物件是否变化
- `time_beats` 可以存在，但不一定生成 `keyframes`
- 如果某一段视觉差异不足，就只保留为时间节拍，不要硬升成关键帧

#### 4.6.3.2 参考图分配规则（最多 9 张）

当当前视频模型支持多参考图时，代码会先组装 `video_references`，再按用途优先级裁剪。

**默认自动组装来源：**
1. **首帧图** → `first_frame`
2. **scene 图** → `reference_composition`
3. **角色参考图** → `reference_character`
4. **道具参考图** → `reference_prop`
5. **中间阶段图**（通常来自 `keyframes`）→ `reference_stage`
6. **目标状态图**（根据 `continuity_mode` 决定是否存在）→ `reference_target_state`

**代码行为：**
- 代码会根据当前视频模型的 `max_reference_images` 自动计算可用槽位
- 如果参考素材超限，不再按“首帧 / keyframes / 尾帧”裁，而是按用途优先级裁
- 当前优先级大致是：
  1. `first_frame`
  2. `reference_character`
  3. `reference_prop`
  4. `reference_composition`
  5. `reference_style`
  6. `reference_color`
  7. `reference_target_state`
  8. `reference_stage`
- 因此，真正容易被裁掉的通常是中间阶段参考，而不是首帧、角色或关键道具

**写 storyboard 时的指导：**
- 真正关键的参考素材，优先显式写进 `video_references`
- `keyframes` 只用来表达“中间阶段确实重要”，不要再把它当唯一的中段控制入口
- 如果某个中间状态没有独立用途，就不要为了“多给一张图”而写进去

**母模型在写 storyboard 时的实际判断顺序应该是：**

1. 先看这个 shot 的 narrative 任务是什么
2. 再按 4 个语义维度判断参考图策略
3. 再决定是否需要 `chain_from_previous`
4. 产出 `video_references`
5. 如有需要，再补 `reference_strategy`
6. 判断是否需要 `time_beats`
7. 最后输出 `shot_type`、`continuity_mode`、`keyframes`

### 4.6.3.3 `reference_strategy`（建议显式产出）

为避免“明明做了语义判断，但 storyboard 里看不出来”，建议每个 shot 额外显式写出：

```json
"reference_strategy": "single_anchor"
```

允许值：
- `single_anchor`：仅首帧
- `anchor_with_end`：首帧 + 尾帧
- `anchor_with_keyframes`：首帧 + 中间 keyframes
- `anchor_keyframes_end`：首帧 + 中间 keyframes + 尾帧

这个字段当前主要用于让母模型的策略判断结果可见、可审核。
执行层真正优先读取的是：
- `video_references`
- `shot_type`
- `continuity_mode`
- `keyframes`
- `chain_from_previous`

因此从现在开始，写 storyboard 时应优先先产出 `video_references`，再决定是否额外显式写 `reference_strategy`。

**示例 — 适合链式：**
- Shot 3: 两人对话中景 → Shot 4: 同一场景两人继续对话，镜头略推近

**示例 — 不适合链式（必须 false）：**
- Shot 1（白素贞+小青）→ Shot 2（许仙独自）：角色不同
- Shot 3（递伞手部特写）→ Shot 4（断桥全景）：景别跳转
- Shot 2（雨天亭下）→ Shot 3（雨中桥上）：场景变化

### 4.6.4 强动作镜头规则（必须当成导演调度稿来写）

**这是当前视频生成里最容易写差、也最影响模型表现的一类 shot。**  
如果镜头包含扑击、打斗、摔倒、脱手、追逐、爆发式转身、强烈肢体冲突，不能只写一句“事件摘要”，必须把它写成一个有节拍的镜头内动作设计。

**先判断这是不是强动作 shot：**
- 镜头内存在明显的爆发动作，而不是平缓位移
- 至少两个主体之间存在高强度交互或对抗
- 结果姿态与起始姿态差异很大
- 如果只给首帧和尾帧，模型大概率会用平滑补间糊过去

**一旦属于强动作 shot，必须同时满足以下规则：**
1. `continuity_mode` 不能写 `"free"`，默认写 `"strict"`
2. `action_prompt` 不能只写一句结果摘要，必须体现镜头内的节奏变化
3. `motion_control.phase_beats` 不能少于 3 段
4. `keyframes` 不应只有 0 张；原则上至少 2 段关键状态
5. `scene_prompt` / `action_prompt` / `end_frame_description` 必须能对应到同一条动作链，不能只写开头和结尾

**强动作 shot 的最小结构：**
- 起势：谁先动，如何蓄力，危险从哪里来
- 爆发：碰撞、闪躲、扑击、失衡、脱手、翻滚等关键事件
- 落点：这一个 shot 结束时，角色和道具落到什么状态

**强动作 shot 不要这样写：**
- “老虎扑向武松，武松闪开，木棒脱手。”
- 这是剧情摘要，不是镜头调度。模型只会把它理解成一条模糊的 A→B 变化。

**强动作 shot 应该这样写：**
- `scene_prompt` 交代爆发前 0.5-1 秒的紧绷状态
- `action_prompt` 写出动作的节奏曲线：突然爆发、瞬间失衡、短促碰撞、结果落点
- `end_frame_description` 只负责定义这一 shot 必须精确到达的最终状态
- `motion_control.phase_beats` 至少拆成：
  - 起势
  - 爆发/碰撞
  - 结果姿态

**`keyframes` 规则要升级：**
- 普通叙事镜头：`keyframes` 可以没有或只有 1 张
- 强动作镜头：默认至少 2 张，分别锚定“爆发中段”和“结果前一拍”
- 如果镜头里还有明显的武器脱手、主体位置互换、压制关系反转，应该继续增加关键帧，而不是让模型自行脑补

**写 `action_prompt` 时必须补足这些维度：**
- 爆发方式：sudden / explosive / violent / abrupt，而不是 only “moves”
- 力度变化：猛扑、急闪、撞击、失衡、翻滚、压制
- 节奏变化：先静后爆、短促碰撞、落地后的余势
- 镜头关系：镜头是稳跟、被动作带动，还是保持观察位

**判断写得够不够的自检标准：**
- 如果把 `action_prompt` 拿掉，只剩首尾帧，模型会不会变成慢吞吞的补间？
- 如果答案是“会”，那这个强动作 shot 写得还不够。

**示例 — 武松打虎 shot 3：**
- 不够好的写法：
  - “The tiger lunges in a single explosive pounce while Wu Song dodges, swings the staff, and loses his weapon in the collision.”
- 更符合强动作规则的写法：
  - 起势：虎伏身蓄力，武松刚意识到扑击方向
  - 爆发：老虎突然前扑，武松侧闪半步并本能横棒格挡，冲击把木棒震飞
  - 落点：虎身擦落前景，木棒旋出画面，武松重心下坠进入失衡后的防守姿态

**一句话原则：**
- 普通 shot 可以写“发生了什么”
- 强动作 shot 必须写“这个镜头内部是怎么打起来的”

### 4.6.5 transition_in（剪辑转场）

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

**首帧图已经确定了画面，action_prompt 只描述阶段之间的运动/动作变化。** 角色外貌由代码通过结构化 prompt 自动注入到视频生成请求中，此字段**禁止任何外貌描述**。

关键原则 — **"首帧决定视觉"：**
- 首帧图锚定了角色外貌、场景环境、光影氛围
- action_prompt 只需要告诉 Seedance "各阶段之间发生了什么过渡"
- 不要再把 `action_prompt` 写成剧情摘要
- 推荐按顺序写：阶段1如何到阶段2，阶段2如何到阶段3
- 描述的动作必须在该镜头的 estimated_duration 内可完成
- 代码会自动构建结构化视频 prompt：`【角色外观设定】+ 【场景动作】+ 【一致性要素】`

**示例：**
- ❌ "The 40-foot tall blue-red robot warrior with riveted armor in the destroyed city at sunset slowly pulls its blade..." （重复了外貌和场景）
- ✅ "The robot slowly pulls its energy blade out of the enemy's chest with a grinding metal sound, the enemy's optics flash once then go permanently dark, the enemy's body tips backward and crashes onto the asphalt sending dust upward"

**强动作镜头额外要求：**
- 不能只写“谁打了谁”，必须写出动作的节拍和冲击链
- 优先使用能表达节奏的动词：`surges`, `snaps`, `slams`, `jerks`, `whips`, `bursts`, `crashes`, `stumbles`, `locks`
- 避免只用平缓词：`moves`, `goes`, `turns`, `changes position`
- 如果镜头内有明显的先静后爆、碰撞后失衡、武器脱手、角色压制关系变化，必须写进 `action_prompt`

**错误示例（强动作 shot）：**
- “He fights the tiger and gains the upper hand.”
- “The tiger attacks and Wu Song responds.”

**正确方向（强动作 shot）：**
- “The tiger explodes forward from a low crouch; Wu Song snaps sideways into a hurried dodge, catches the pounce on the staff for a fraction of a beat, and the impact jerks the weapon out of his hands.”
- “Wu Song surges chest-first into the tiger, collides hard at shoulder level, both bodies tumble through leaves and rock dust, and he fights to climb into partial top control.”

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

### 4.10.1 scene_continuity（场景级连续性事实）

当一个 scene 内存在不应漂移的空间关系、道具状态、环境状态或角色稳定状态时，必须在 scene 层声明 `scene_continuity`。

```json
"scene_continuity": {
    "stable_facts": {
        "spatial_layout": ["角色A始终在角色B身后偏右"],
        "prop_states": ["油纸伞始终由角色A右手持有"],
        "environment_states": ["崖壁位置和道路朝向保持不变"],
        "character_states": ["角色A始终靠近墙根区域"]
    },
    "entity_registry": {
        "oil_paper_umbrella": {
            "count": 1,
            "holder": "character_a.right_hand",
            "persistent_state": "滴水、暖黄色、微微歪斜"
        }
    },
    "carry_forward_subjects": ["character_a", "character_b", "oil_paper_umbrella"]
}
```

规则：
- `stable_facts` 不能写成一大段散文，必须拆成逐条稳定事实
- 每条事实都应能被单独注入 prompt、单独检查
- 这层表达“默认继承的稳定基线”，不是表达镜头内部变化
- `entity_registry` 用来声明 scene 级唯一实体及其持续状态，避免同一把伞、同一把刀被重复生成

### 4.10.2 pose_contract（角色姿态合同）

当同一角色在首帧、keyframes、尾帧之间必须维持同一种身体支撑状态时，必须在 `subject_constraints.pose_contract` 中写出固定姿态合同。

示例：
- `身体重心始终落在地面与右侧墙根交界处`
- `上身持续斜靠墙面，支撑点不变`
- `下肢保持坐地承重，不转为站立承重`

规则：
- 只写物理支撑关系，不写抽象情绪词
- 优先写重心、支撑点、承重方式、与墙地或关键道具的关系
- 如果缺少这层，模型很容易把“倚靠”“虚弱举伞”分别画成坐姿、半蹲或站姿

### 4.10.2.1 gaze_contract（角色视线合同）

当“角色看向谁”本身就是叙事推进的一部分时，必须在 `subject_constraints.gaze_contract` 中写出正向视线合同。

```json
"gaze_contract": {
    "medical_girl": {
        "primary_target": "swordsman",
        "target_zone": "身后右侧崖壁附近"
    }
}
```

规则：
- `gaze_contract` 属于 `subject_constraints` 的一部分，不单独新增顶层字段
- 推荐只写：
  - `primary_target`
  - `target_zone`
- 不写负向 `forbidden_gaze`
- 如果镜头里存在“回头、认出、盯住、对视、发现来源”这类推进，强烈建议填写

### 4.10.3 shot_delta（本镜头变化边界）

每个 shot 应尽量写 `shot_delta`，明确“这一镜到底允许改变什么”。

```json
"shot_delta": [
    "本镜头只改变女孩的视线方向与上半身朝向",
    "男主姿态、持伞手、空间位置保持不变"
]
```

规则：
- `shot_delta` 只写本镜头允许发生的变化
- 没写进 `shot_delta` 的变化，不应被模型随意新增
- 这层用于把“该推进的叙事变化”和“必须保持稳定的事实”拆开

### 4.10.4 参考图叙事锚点规则

每张参考图都必须是时间线上的一个明确叙事状态节点，而不是模糊的动作摘要。

规则：
- `scene_prompt` 对应首帧叙事锚点
- `keyframes[].description` 对应中间叙事锚点
- `end_frame_description` 对应结果叙事锚点
- 每个锚点都应尽量写清：
  - 主体姿态
  - 主体视线落点
  - 他者揭示程度
  - 空间关系推进到哪一步

如果一张参考图无法回答“它对应时间线的哪个节点”，说明这个锚点还不够可执行。

### 4.10.5 参考图前置验证卡口

在 Seedance 2.0 链路下，图片验证必须先于视频生成。

规则：
- 对于存在 `pose_contract`、`gaze_contract`、`scene_continuity`、`entity_registry` 的镜头，代码会先导出参考图验证 bundle
- 参考图验证通过后，才允许进入视频生成
- 验证重点是：
  - 参考图是否对应明确叙事节点
  - 是否违反姿态、视线、空间稳定事实
  - 是否破坏实体唯一性

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
  - 是否违反 `scene_continuity.stable_facts`？
  - 是否违反 `subject_constraints.pose_contract`？
  - 是否超出 `shot_delta` 允许的变化范围？

□ 检查每个 scene_prompt 是否是静态起始状态
  - 是否描述了"即将发生"而非"正在发生"的动作？
  - 是否适合生成一张静态首帧图？

□ 检查每个 shot 的阶段职责是否清楚
  - 每个节点是否只承担一个阶段任务？
  - 中间节点是否偷跑到了终点？
  - 是否存在“几张都和剧情有关，但不像同一个镜头过程”的情况？

□ 检查跨 scene 边界的视觉过渡
  - 光线/天气/时间是否有突变？
  - 如有突变，是否有渐变、过渡镜头或时间跳跃标记？

□ 检查首帧-动作-尾帧因果链
  - 每个 shot 的 scene_prompt + action_prompt 能否自然导出 end_frame_description？
  - end_frame_description 中是否有无中生有的元素？

□ 检查这个 shot 是否其实应该拆开
  - 是否存在过多阶段？
  - 是否需要靠增加大量 keyframes 才能勉强讲清？
  - 如果删掉某个中间节点，这镜是否仍然成立？若成立，说明该节点不该保留；若不成立但变化又过大，说明该镜应拆分。
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

## 步骤 4.5: 导演写帧级 prompt（核心新增）

**思考在母模型脑子里完成，不甩给生图模型。**

你（母模型）是导演。你设计了每个 shot 的姿态、情绪、构图、视线——这些画面在你脑子里。你应该把脑子里看到的画面用精确的语言写出来，交给生图模型执行。

生图模型不会思考，只会执行。给它越精确的画面描述，出来的图越接近你的设想。

### 4.5.1 导演逐帧写画面

对每个 shot 的每一帧（首帧 / 关键帧 / 尾帧），按以下顺序思考：

**1. 闭上眼睛，想象这个画面**

- 这是第几拍？之前一拍画面是什么样的？
- 这个镜头要表达什么情绪？观众此刻应该感受到什么？

**2. 你看到了什么？**

- 构图：主体在画面什么位置？占多大比例？
- 光线：从哪个方向来？什么质感？
- 人物：什么姿态？什么表情？视线看向哪里？
- 环境：前景/中景/远景有什么？

**3. 把这个画面写出来**

- 写画面，不写故事（不是"她心生怜悯"，而是"她的表情从警惕变为柔和，眉头舒展"）
- 写状态，不写过程（不是"她走向亭子"，而是"她站在亭子入口，身体朝向亭内"）
- 写精确，不写模糊（不是"远处有个人"，而是"画面右三分之一处，一个模糊的人影，只露出轮廓"）

### 4.5.2 输出格式

创建 `director_prompts.json`：

```json
{
  "shots": {
    "1": {
      "first_frame": {
        "goal": "建立冷雨山道与女孩孤绝采药的第一拍",
        "beat": 1,
        "prompt": "全景建立镜头。冷雨压住喀斯特群山，潮湿发暗的青石古道向远处延伸。女孩独自蹲伏在画面左下区域，身体内收抵御寒冷，注意力压在地面的草药上。整张图先建立环境压迫感与人物的孤绝状态，不引入剑客和油纸伞。"
      },
      "last_frame": {
        "goal": "建立镜头收束到女孩仍在执拗坚持的状态",
        "beat": 2,
        "prompt": "镜头推近后的全景偏中景。冷雨与群山的压迫感仍未改变，女孩依旧蹲伏在原地，肩背绷紧，只维持细小采药动作。画面终点仍停留在她独自硬撑的状态，不出现新的外部变量。"
      },
      "video_action": "镜头在冷雨中极缓向前推进，环境压迫感逐步加强。女孩基本保持蹲伏姿态，只保留克制的采药细小动作，整镜停留在孤绝而执拗的氛围里。"
    },
    "2": {
      "first_frame": {
        "goal": "把注意力压到手部和草药上",
        "beat": 1,
        "prompt": "近景固定镜头。画面焦点锁在女孩冻得发白的手指和那株带泥草药上，背景只保留被雨水浸透的深蓝衣袖和模糊冷雨。整张图只强调寒冷中的执拗专注，不引入剑客和油纸伞。"
      },
      "keyframes": [
        {
          "goal": "开始确认来源",
          "beat": 2,
          "timestamp": 5.0,
          "prompt": "女孩身体已经转过大半，视线锁向身后右侧，情绪仍停留在确认中的紧绷阶段。剑客倚靠崖壁、歪斜举伞的状态开始清楚可辨，但画面还没有进入最终认出后的情绪收束。"
        }
      ],
      "last_frame": {
        "goal": "完成认出后的落点",
        "beat": 3,
        "prompt": "女孩已经完成急促回眸，眼神从警惕滑向湿润的微怔。画面右后方清楚交代出倚靠崖壁、歪斜举伞的重伤剑客，他为她挡住了雨。整张图进入被震动后的认出与松动状态，但还没有进入下一镜的庇护收束。"
      },
      "video_action": "女孩被遮雨动作突然打断后，头部和视线迅速转向身后右侧。随着回眸完成，画面逐步揭示出倚靠崖壁、歪斜举伞的重伤剑客，情绪从警惕滑向认出后的湿润微怔。"
    }
  }
}
```

### 4.5.3 storyboard 与 director_prompts 的分工

| 文件 | 写什么 | 服务于 |
|------|--------|--------|
| storyboard.json | 故事 + 约束 + 结构 + 连续性事实 | 生成计划 + 审片检查清单 |
| director_prompts.json | 每一帧的精确画面描述 | 图片生成的直接输入 |

- storyboard 回答"这个 shot 要做什么、有什么约束"
- director_prompts 回答"这一帧画面上到底长什么样"

同一个 shot，两种视角：一个是导演的计划书，一个是导演脑海里看到的画面。

### 4.5.4 优先级规则

**如果 director_prompts 存在：**
- 生图时直接使用 director_prompts 中的帧级描述
- 跳过 Flash 全局提取步骤
- storyboard 的约束仍用于审片检查

**如果 director_prompts 不存在：**
- 回退到 Flash 全局提取（旧流程）
- 从 storyboard 中提取画面描述

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
8. 每个 shot：用 first_frame_prompt 生成首帧图 → 用 last_frame_prompt 生成尾帧图 → 归一化生成 `video_references`
9. Seedance I2V 不再按“首帧+尾帧+keyframes”硬编码组织，而是按用途驱动参考素材组织，并在 prompt 里显式调用：
   - `@图片1 作为首帧`
   - `@图片2 参考角色`
   - `@图片3 参考构图`
   - `@图片4 参考目标状态`
10. 如果 shot 标记了 `chain_from_previous: true`，从前一 shot 视频提取实际尾帧作为首帧（否则独立生成）
11. 尾帧图根据 `continuity_mode` 决定是否生成：`"strict"` 强制生成，`"scene_end"` 仅 scene 末尾生成，`"free"` 跳过
12. **视频质量自动检测**：每段视频生成后自动执行三重检测（详见下方），不合格则自动重试或裁剪
13. 生成 BGM

---

## 步骤 5.5: 导演审图（可选）

如果启用 `--review-mode director_review`，每个 shot 的图片生成后会暂停，等待你（母模型）审图。

### 5.5.1 审图流程

```
图片生成完成
  ↓
导出审图上下文到 image_audit/shot_{shot_id}/：
  - review_context.json（shot、director_entry、scene_context）
  - generated_image.png（生成的图片）
  ↓
【暂停，等待你的审图】
  ↓
你看图判断 → 创建 director_judge_result.json
  ↓
系统读取判断结果 → 执行决策：
  - keep → 通过，继续生成视频
  - regenerate → 使用 adjustment_prompt 重新生成图片（最多重试 3 次）
```

### 5.5.2 审图步骤

**第一步：理解导演意图**

读取 `review_context.json` 中的 `director_entry`（来自 `director_prompts.json`）：
- `first_frame.prompt`：首帧你想要的画面
- `last_frame.prompt`：尾帧你想要的画面
- `video_action`：从首帧到尾帧的动作过程

**第二步：理解约束条件**

读取 `review_context.json` 中的 `shot.constraints`：
- `pose_contract`：姿态合同——身体支撑关系必须保持
- `gaze_contract`：视线合同——视线方向要求
- `subject_constraints`：出镜控制（谁必须在、谁不能在）
- `shot_delta`：允许的变化边界

**第三步：查看生成的图片**

**⚠️ 必须使用 Read 工具仔细查看图片内容。**

描述你看到的：
- 谁在画面里？什么姿态？什么表情？什么构图？
- 有没有不该出现的角色或物体？
- 环境要素对不对？

**第四步：对比判断**

这张图跟你的 `director_prompts` 描述的画面一致吗？

**重点检查：**
- pose_contract 违反了吗？（姿态支撑关系是否保持）
- gaze_contract 违反了吗？（视线方向对不对）
- subject_constraints 违反了吗？（不该出现的出现了吗？该在的不在吗？）
- shot_delta 违反了吗？（发生了不该变的变化吗？）

**第五步：输出判断**

创建 `director_judge_result.json`：

```json
{
  "shot_id": 1,
  "overall_action": "keep" | "regenerate",
  "reason": "（如果不通过，简要说明原因）",
  "adjustment_prompt": "（如果需要重新生成，写出生图调整建议）"
}
```

### 5.5.3 审图检查清单

| 检查维度 | 数据来源 | 审什么 |
|---------|---------|--------|
| 出镜控制 | `subject_constraints` | 该在的在不在？不该在的有没有出现？ |
| 姿态 | `pose_contract` | 身体重心、支撑关系对不对？ |
| 视线 | `gaze_contract` | 看的方向对不对？ |
| 构图 | `motion_control` | 朝向、镜头角度对不对？ |
| 场景连续性 | `scene_continuity` | 空间关系有没有漂移？道具状态对不对？ |
| 变化边界 | `shot_delta` | 有没有发生不该变的变化？ |
| 角色一致性 | `consistency_anchors` | 关键识别特征都在不在？ |
| 叙事意图 | `director_plan` | 这张图有没有表达出戏核？ |
| 情绪 | `emotion_arc` | 情绪基调对不对？ |
| 衔接 | 前后 shot 状态 | 跟上一帧接得上吗？ |

### 5.5.4 严禁的错误做法

- ❌ 不看图片直接下结论
- ❌ 只看一两句话的描述就判断
- ❌ 不描述画面内容，直接说"没问题"或"有问题"
- ❌ 使用 overall_action 以外的任何值

### 5.5.5 正确的审片流程示例

```
1. Read 图片 → 描述："画面左侧是女孩蹲伏在崖壁根部，身体重心靠在右脚上..."
2. 对比 director_prompts → 首帧 prompt 要求"身体重心落在右脚和墙根交界处" ✓
3. 检查 pose_contract → "身体重心始终落在地面与右侧墙根交界处" ✓
4. 判断：通过，输出 {"shot_id": 1, "overall_action": "keep"}
```

---

## 步骤 5.6: 两阶段视频质量审查

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

**⚠️ 判断流程：必须先理解动作意图，再检查画面质量**

**第一步：理解叙事语义（维度9优先）**
1. 先查看该 shot 的分镜描述（storyboard.json 中的 scene_prompt / action_prompt / narrative_segment）
2. 理解这个镜头要表达什么动作：是静止特写？快速移动？剧烈打斗？
3. 明确动作意图后，才能区分"质量问题"和"正常的动作效果"

**第二步：查看帧图片并判断**
- 如果是快速运动镜头（如"急剧上升"、"飞跃"），运动模糊、画面暗是正常效果 ✓
- 如果是静止特写镜头，画面暗、模糊才是质量问题 ❌
- 重复角色、身体变形等问题，无论什么动作都是质量问题 ❌

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
