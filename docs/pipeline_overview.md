# xyz-video-skill 完整流程

## 整体架构

```
用户输入主题/想法
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤1: 故事创作（LLM 思考）                                │
│  • 与用户对话确认方向                                       │
│  • 输出 story.json                                        │
│    - title / synopsis / narrative（200-400字连贯叙事）      │
│    - story_beats（开端→发展→高潮→结尾）                      │
│    - visual_tone / suggested_duration_per_beat             │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤2: 剧本框架 + 角色设计（LLM 思考）                       │
│  • 从 narrative 切分 scenes（地点/光线/天气变化时切场景）      │
│  • 设计角色（外貌描述精确到"看完就能画"的程度）                 │
│  • 输出 framework.json                                    │
│    - scenes: narrative_segment 直接引用 narrative 原文       │
│    - suggested_characters: appearance / key_features        │
│    - visual_style_anchor                                   │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤3: 角色参考图生成（Python 脚本）                         │
│  python3 ad_assets.py --mode character_refs               │
│  • 为每个角色生成白底正面全身参考图                            │
│  • 输出 character_refs/ref_{id}.png                       │
│  • 这步是视觉一致性的基石                                    │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤4: 分镜脚本（LLM 思考，最核心步骤）                       │
│  • 从 narrative → scenes → shots 逐层切分                  │
│  • 每个 shot 先做语义决策（动作复杂度/状态变化/衔接依赖）       │
│  • 输出 storyboard.json（scenes > shots 结构）              │
│  • 关键字段：                                               │
│    - shot_type / continuity_mode / video_references        │
│    - scene_prompt（故事起点+起始画面）                        │
│    - action_prompt（运动过程）                               │
│    - end_frame_description（故事终点+结束画面）               │
│    - motion_control（空间关系硬约束）                         │
│    - subject_constraints / consistency_anchors              │
│    - chain_from_previous / keyframes / transition_in       │
│  • 写完必须执行自检清单（逐对检查连续性）                       │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤5: 素材生成 + 质量审查（Python 脚本 + LLM 裁定）          │
│  python3 ad_assets.py --mode assets                      │
│                                                          │
│  5a. Storyboard 规范化                                    │
│    • 回填 legacy 缺失的 shot_type                          │
│    • 自动补全 / 规范化 video_references                    │
│    • 绑定角色参考图路径                                      │
│    • 输出 _normalized/storyboard.json                     │
│                                                          │
│  5b. Prompt 提取（LLM 辅助）                               │
│    • 全局提取：narrative + 所有 shot 上下文 → LLM            │
│    • 为每个 shot 生成 first_frame_prompt / last_frame_prompt│
│    • 生成 video_action_prompt                              │
│                                                          │
│  5c. 图片/视频素材生成                                      │
│    • 生成首帧图（注入角色外貌+场景视觉基底）                    │
│    • 按 continuity_mode 决定是否生成尾帧图                   │
│    • chain_from_previous → 从前一 shot 视频提取实际尾帧       │
│    • 组装 video_references → @图片N 用途调用 → 视频         │
│                                                          │
│  5d. 两阶段质量审查                                        │
│    阶段1 粗筛（自动）：                                     │
│    • 帧间突变 + 闪烁检测 (MSE)                              │
│    • 局部突变 spike detection                              │
│    • 人脸变形检测 (OpenCV DNN)                              │
│    • 重复角色检测 (HOG)                                     │
│    → 导出 vision_bundle 关键帧                             │
│                                                          │
│    阶段2 LLM 视觉裁定（母模型看图）：                        │
│    • Read 每一帧 → 描述画面内容                             │
│    • 理解叙事语义再判断质量                                  │
│    • 输出：keep / cut_segment / regenerate                 │
│    → 状态: finalized                                      │
└──────────────────────┬───────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│ 步骤6: 编辑决策 + 视频合成（Python 脚本）                    │
│  python3 ad_compose.py                                   │
│                                                          │
│  6a. Pair-level 编辑决策                                   │
│    • 每对相邻 shot 生成 edit_decisions.json                │
│    • 语义类型：same_moment_overlap / continuous_action /    │
│      reverse_shot / reaction_cut / impact_cut             │
│    • 决策：trim_in / trim_out / transition_type            │
│                                                          │
│  6b. FFmpeg 合成                                          │
│    • 按 transition_in 拼接（straight-cut / cross-dissolve /│
│      flash-white）                                        │
│    • 混合 BGM                                             │
│    • 输出多平台版本（竖版/方版/横版）                         │
│    • 可选品牌化（Logo/字幕/水印/产品贴图）                    │
└──────────────────────┬───────────────────────────────────┘
                       ▼
              最终视频输出
         ~/video-output/{timestamp}/
         ├── videos/{platform}.mp4
         ├── assets/assets.json
         ├── _normalized/storyboard.json
         ├── character_refs/
         └── pipeline_result.json
```

---

## 各步骤详解

### 步骤1: 故事创作

与用户对话讨论创意方向，然后展开为完整故事。

**输入：** 用户给出的主题/想法

**输出：** `story.json`

| 字段 | 说明 |
|------|------|
| `title` | 故事标题 |
| `synopsis` | 一句话梗概（30字以内） |
| `narrative` | 完整连贯叙事（200-400字，整个视频的唯一故事主线） |
| `story_beats` | 结构化切分（开端/发展/高潮/结尾），每个 beat 标注 `narrative_range` |
| `visual_tone` | 整体视觉基调建议 |
| `suggested_duration_per_beat` | 每个 beat 的建议时长 |

**narrative 写作规则：**
- **因果链** — 每个事件必须由前一个事件触发
- **角色动机** — 每个行为必须有情感/意图驱动
- **感官细节** — 写出能看到、听到、感受到的东西
- **环境渐变** — 时间/天气/光线变化必须渐变
- **连续动作线** — 角色位置移动必须有交代

---

### 步骤2: 剧本框架 + 角色设计

将故事展开为完整剧本框架。角色设计是本步骤的核心——角色外貌描述的质量直接决定后续所有视觉一致性。

**输入：** `story.json`

**输出：** `framework.json`

**scenes 切分原则：** 从 narrative 切分（不是独立创作）。以下任一条件变化时切为新场景：
- 地点变化
- 光线质变
- 天气变化
- 时间跨度导致视觉环境显著不同

**角色设计要求：** 外貌描述必须精确到"只看文字就能画出完全一样的角色"。
- 形体特征（身高/体型/比例）
- 颜色方案（主色+辅色+点缀色）
- 材质纹理（光滑/粗糙/磨损/反光）
- 关键识别标记（至少3个独特视觉锚点）
- `ref_description`（给图片模型的角色说明，可用英文）

---

### 步骤3: 角色参考图生成

视觉一致性的基石。在写分镜之前，必须先为每个角色生成参考图。

```bash
cd /path/to/skills/xyz-video-skill/scripts
python3 ad_assets.py \
    --mode character_refs \
    --framework {output_dir}/framework.json \
    --output_dir {output_dir}/character_refs
```

**输出：**
- `character_refs/ref_{character_id}.png` — 白底正面全身参考图
- `character_refs/character_refs.json` — 角色参考图清单

生成后检查参考图质量，不满意可重新生成单个角色：
```bash
python3 ad_assets.py --mode character_refs --framework framework.json --output_dir character_refs --character_id blue_mecha
```

---

### 步骤4: 分镜脚本（最核心步骤）

将剧本框架拆解为逐镜头的分镜脚本。分镜质量直接决定最终视频效果。

**输入：** `framework.json` + 角色参考图

**输出：** `storyboard.json`

#### 4.1 结构层级

```
storyboard
├── 全局：narrative / style_anchor / characters / bgm_style
└── scenes[]
    ├── 场景层：lighting / weather / props / environment_description
    │           scene_continuity（stable_facts / entity_registry）
    └── shots[]
        ├── 导演层：director_plan（dramatic_core / viewer_information_flow / nodes）
        ├── 叙事：narrative_segment / scene_prompt / action_prompt / end_frame_description
        ├── 策略：shot_type / continuity_mode / reference_strategy / chain_from_previous
        ├── 约束：subject_constraints / consistency_anchors / shot_delta
        ├── 运动：motion_control / time_beats / keyframes
        └── 剪辑：transition_in / estimated_duration
```

#### 4.2 语义决策顺序

复杂 shot 在写之前，必须先完成一层导演设计；普通 shot 可以保持轻量写法。

强导演模式适用：
- `transition_reveal`
- 强动作 shot
- 明显情绪/身份/关系转折 shot
- `chain_from_previous=true` 的高衔接依赖 shot
- 需要 2 张及以上 `keyframes` 的 shot

这些 shot 先完成导演设计，再判断以下 4 个维度。

先做导演设计：

1. 这镜只完成什么戏剧动作
2. 这镜明确不完成什么
3. 观众按什么顺序获得信息
4. 这镜要拆成几个必要阶段

然后再判断以下4个维度：

1. **动作复杂度** — 静态/中等/强动作
2. **状态变化幅度** — 小/中/大
3. **起止姿态约束强度** — 弱/中/强
4. **与前后镜头的衔接依赖** — 独立/链式/关键衔接

最后才把导演阶段落成字段：
- `scene_prompt` = 第一个阶段
- `keyframes` = 中间阶段（兼容表达）
- `end_frame_description` = 最后一个阶段
- `action_prompt` = 阶段之间的过渡

然后决定这一镜实际需要哪些**用途驱动参考素材**：
- `first_frame`
- `reference_character`
- `reference_prop`
- `reference_composition`
- `reference_style`
- `reference_stage`
- `reference_target_state`

这些决策优先落成 `video_references`；`reference_strategy` 可以保留，但更适合作为分析/审核字段，而不是执行层主协议。

#### 4.3 核心约束

| 约束 | 说明 |
|------|------|
| **单一真相源** | 角色外貌只在 `storyboard.characters` 定义，分镜禁止重复描述 |
| **先导演后字段** | 先定义戏核和阶段，再把阶段投影成 prompt / `video_references` |
| **因果链** | scene_prompt → action_prompt → end_frame_description 必须因果连贯 |
| **单向运动** | 一个 shot 内只允许一个运动方向，禁止折返（违反则拆成两个 shot） |
| **一镜头一动作** | 一个镜头 = 一个清晰的视觉事件 |
| **5-12秒/镜头** | 根据导演节奏估算 `estimated_duration`：先看阶段数、信息揭示和落点停留，再决定时长 |
| **3x3 法则** | 建立（慢）→ 冲突（快）→ 结局（慢），每阶段2-3镜头 |

#### 4.4 shot_type 分类

| 类型 | 说明 |
|------|------|
| `visible_subject` | 默认，主体可见 |
| `offscreen_reaction` | 画外角色反应，必须明确谁不能露出 |
| `transition_reveal` | 过渡揭示，必须明确 reveal 前后谁保持连续 |
| `free_atmosphere` | 氛围空镜 |

#### 4.5 continuity_mode

| 模式 | 含义 | 尾帧行为 |
|------|------|---------|
| `strict` | 关键镜头，必须精确落到目标画面 | 强制生成尾帧图 |
| `scene_end` | 普通镜头（默认） | 仅 scene 末尾生成 |
| `free` | 氛围/空镜/过场 | 不生成尾帧图 |

#### 4.6 强动作镜头规则

扑击、打斗、摔倒等强动作 shot，不能写成剧情摘要，必须当导演调度稿来写：

- `continuity_mode` 必须为 `strict`
- `action_prompt` 必须体现节奏变化（起势→爆发→落点）
- `motion_control.phase_beats` 不少于 3 段
- 如有中间阶段参考，必须能明确说出其用途；不要为了“多给几张图”而堆 `keyframes`
- 如果一个强动作过程已经塞进过多阶段，不是继续堆 `keyframes`，而是拆 shot

#### 4.7 分镜自检清单

写完所有分镜后，必须逐对执行以下检查：

```
□ 逐对检查 Shot N end_frame → Shot N+1 scene_prompt 的状态连续性
  - 角色位置、状态、道具持有是否一致？
  - 是否有凭空出现/消失的元素？
  - 是否违反 scene_continuity.stable_facts？
  - 是否违反 subject_constraints.pose_contract？
  - 是否超出 shot_delta 允许的变化范围？

□ 检查每个 scene_prompt 是否是静态起始状态
  - 是否描述了"即将发生"而非"正在发生"的动作？
  - 是否适合生成一张静态首帧图？

□ 检查每个 shot 的阶段职责
  - 第一个阶段是否只负责起点？
  - 中间阶段是否只负责必要过渡？
  - 最后阶段是否只负责明确落点？

□ 检查跨 scene 边界的视觉过渡
  - 光线/天气/时间是否有突变？
  - 如有突变，是否有渐变、过渡镜头或时间跳跃标记？

□ 检查首帧-动作-尾帧因果链
  - 每个 shot 的 scene_prompt + action_prompt 能否自然导出 end_frame_description？
  - end_frame_description 中是否有无中生有的元素？

□ 检查这个 shot 是否其实应该拆开
  - 是否需要过多中间阶段参考才讲得清？
  - 是否已经像“几张相关剧照”而不是“一个连续镜头”？
```

---

### 步骤5: 素材生成 + 质量审查

调用 Python 脚本生成图片/视频素材，并执行两阶段质量审查。

```bash
python3 ad_assets.py \
    --storyboard {output_dir}/storyboard.json \
    --output_dir {output_dir}/assets \
    [--verbose]
```

#### 5a. Storyboard 规范化

`run_pipeline.py` 自动处理：
- 回填 legacy storyboard 缺失的 `shot_type`
- 优先保留显式 `video_references`，否则自动从 shot 字段推导一份用途驱动参考清单
- 绑定角色参考图路径
- 输出到 `_normalized/storyboard.json` + `storyboard_migration_report.json`

#### 5b. Prompt 提取（LLM 辅助）

将完整 narrative + 所有 shot 的上下文一次性交给 LLM，为每个 shot 提取：
- `first_frame_prompt` — 带前后文衔接的首帧视觉描述
- `last_frame_prompt` — 带前后文衔接的尾帧视觉描述
- `video_action_prompt` — 带故事方向的动作描述（给 Seedance 用）

#### 5c. 图片/视频素材生成

1. 按 `characters_in_shot` 传角色参考图给图片模型
2. 先按 `director_plan.nodes` 生成阶段图：
   - 第一个节点 → 首帧图
   - 中间节点 → 中间阶段图（通常来自 `keyframes`）
   - 最后节点 → 目标状态图
3. 按 `continuity_mode` 决定是否生成目标状态图
4. `chain_from_previous: true` → 从前一 shot 视频提取实际尾帧作为首帧
5. 执行层组装 `video_references`：
   - `first_frame`
   - `reference_composition`
   - `reference_character`
   - `reference_prop`
   - `reference_style`
   - `reference_stage`
   - `reference_target_state`
6. 如果参考素材超限，按用途优先级裁剪，而不是按“首尾帧+keyframes”裁剪
7. Seedance 2.0 I2V：把所有参考素材统一作为 `reference_image` 送入 `content`，并在 prompt 里用 `@图片N` 显式声明用途
8. 按 camera movement 自动选择质量 profile：`static` / `medium_motion` / `heavy_motion`
9. 生成 BGM

#### 5d. 两阶段质量审查

**阶段1：粗筛检测（自动）**

| 检测项 | 方法 | 目的 |
|--------|------|------|
| 帧间突变 + 闪烁 | MSE 分析 | 捕捉画面突变/闪烁伪影 |
| 局部突变 spike | 滑动窗口 MSE | 捕捉面部变形/画面撕裂 |
| 人脸变形 | OpenCV DNN SSD | 追踪人脸置信度骤降 |
| 重复角色 | HOG + 人脸相似度 | 检测 identity hallucination |

粗筛输出：`quality_audit.json` + `vision_bundle_attempt_N/` 关键帧图片

**阶段2：LLM 视觉裁定（母模型看图）**

1. 理解叙事语义（先看分镜描述，理解动作意图）
2. Read 每一帧 → 详细描述画面内容
3. 对比粗筛报告，确认问题是否真实存在
4. 额外检查每张参考图 / 风险帧是否完成了它所属阶段的职责，是否偷跑到下一阶段
5. 输出 `vision_judge_result.json`：
   - `keep` — 所有片段没问题
   - `cut_segment` — 问题片段 < 50% 且剩余 ≥ 3秒
   - `regenerate` — 问题片段 ≥ 50% 或剩余 < 3秒

**状态流转：**
```
视频生成 → audited → pending_judgment → judged → applied → finalized
```

当流程停在 `pending_judgment` 时，除了看 JSON 判断请求外，还应直接检查对应 bundle 里的 `video_prompt.txt`。

- 参考图前置审查：
  - `image_audit/shot_{id}/reference_bundle/reference_review_request.json`
  - `image_audit/shot_{id}/reference_bundle/video_prompt.txt`
- 视频风险审查：
  - `assets/audit/shot_{id}/vision_bundle_attempt_{n}/vision_judge_request.json`

---

### 步骤6: 编辑决策 + 视频合成

```bash
python3 ad_compose.py \
    --storyboard {output_dir}/storyboard.json \
    --assets {output_dir}/assets/assets.json \
    --output_dir {output_dir}/videos \
    [--platform youtube douyin wechat]
```

#### 6a. Pair-level 编辑决策

每对相邻 shot 生成 `edit_decisions.json`，包含：
- `pair_type`：语义关系类型
- `confidence`：决策置信度
- `trim_in` / `trim_out`：入出点裁剪
- `transition_type`：转场类型
- `transition_candidates`：候选转场

支持的语义类型：
- `same_moment_overlap` — 同一时刻不同角度
- `continuous_action_same_scene` — 同场景连续动作
- `reverse_shot_same_scene` — 正反打
- `reaction_cut` — 反应镜头
- `impact_cut` — 冲击切镜

#### 6b. FFmpeg 合成

- 按 `transition_in` 拼接（straight-cut / cross-dissolve / flash-white）
- 混合 BGM
- 可选品牌化（Logo / 字幕条 / 水印 / 产品贴图）
- 输出多平台版本（竖版 / 方版 / 横版）

---

## 一键执行（run_pipeline.py）

`run_pipeline.py` 是执行编排器，可将步骤3-6串起来一键运行：

```bash
python3 run_pipeline.py \
    --story story.json \
    --framework framework.json \
    --storyboard storyboard.json \
    --output_dir ~/video-output/my_project \
    --platform youtube douyin \
    --logo_path logo.png \
    --product_image product.jpg \
    --product_shots "shot_3,shot_5"
```

**支持的参数：**

| 参数 | 说明 |
|------|------|
| `--from` / `--to` | 指定执行阶段范围（validate/refs/assets/brand/compose） |
| `--skip_*` | 跳过指定阶段 |
| `--assets_manifest` | 复用已有的素材清单（断点续跑） |
| `--brand_manifest` | 复用已有的品牌化清单 |
| `--review_mode` | 质量审查模式（metrics_only / hybrid_judge） |
| `--parallel` | 素材生成并行度（默认4） |
| `--no_api` | 不调用 API（调试模式） |

---

## 关键设计理念

| 理念 | 体现 |
|------|------|
| **LLM 是大脑，脚本只动手** | 步骤1-4 全靠 LLM 思考，Python 只负责调 API 和合成 |
| **narrative 是唯一故事主线** | 所有 JSON 都从 narrative 派生，不允许凭空创造 |
| **单一真相源** | 角色外貌只在 `storyboard.characters` 定义一次，代码自动注入 |
| **先导演后字段** | 每个 shot 先定义戏核和阶段，再落成 `scene_prompt / video_references / end_frame_description` |
| **用途协议优先** | 视频参考素材按用途组织，不再按“首尾帧+硬关键帧”组织 |
| **先语义决策再写字段** | 每个 shot 先判断动作复杂度，再决定 `video_references` / `continuity_mode` / `keyframes` |
| **因果链不可断** | scene_prompt → action_prompt → end_frame_description 必须因果连贯 |
| **单向运动法则** | 一个 shot 内只允许一个运动方向，禁止折返（违反则拆成两个 shot） |
| **质量审查是主流程** | 不是可选的后处理，而是素材生成的必经阶段 |

---

## API 配置

API 密钥配置在 `config/api_keys.yaml` 或通过环境变量：

| 环境变量 | 用途 |
|---------|------|
| `VOLCENGINE_API_KEY` | 火山引擎（图片生成） |
| `APIMART_API_KEY` | ApiMart（Gemini 图片生成） |
| `FAL_KEY` | fal.ai（图片生成 fallback + BGM） |
| `BYTEPLUS_API_KEY` | BytePlus Seedance（视频生成） |

---

## 输出目录结构

```
~/video-output/{timestamp}/
├── story.json                         # 步骤1 输出
├── framework.json                     # 步骤2 输出
├── storyboard.json                    # 步骤4 输出
├── character_refs/                    # 步骤3 输出
│   ├── ref_{character_id}.png
│   └── character_refs.json
├── _normalized/                       # 步骤5a 规范化
│   ├── storyboard.json
│   ├── storyboard_migration_report.json
│   └── ref_binding_report.json
├── assets/                            # 步骤5 输出
│   ├── assets.json                    # 包含 images / videos / shot_prompts / shot_references 汇总
│   ├── prompts/
│   │   └── shot_{id}_video_prompt.txt
│   ├── image_audit/
│   │   └── shot_{id}/reference_bundle/
│   │       ├── reference_review_request.json
│   │       └── video_prompt.txt
│   ├── shot_{id}_first.png
│   ├── shot_{id}_last.png
│   ├── shot_{id}.mp4
│   └── bgm.mp3
├── brand/                             # 品牌化输出（可选）
│   └── brand_manifest.json
├── videos/                            # 步骤6 输出
│   ├── youtube.mp4
│   ├── douyin.mp4
│   └── result.json
└── pipeline_result.json               # 流水线执行结果
```
