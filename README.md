# xyz-video-skill

`xyz-video-skill` 是一个面向 OpenClaw 的视频生成 skill。

它不是“一键广告片 CLI”，而是一条由宿主 LLM 驱动、由 Python 脚本执行的端到端视频生产流水线：

- 宿主 LLM 负责故事创作、结构化剧本、角色设计、分镜脚本
- Python 脚本负责角色参考图、镜头素材、品牌化处理和 FFmpeg 合成

## Current Scope

当前仓库已经覆盖的核心能力：

- `story.json` → `framework.json` → `storyboard.json` 的 skill 工作流约束
- `storyboard.json` 的新协议字段：
  - `shot_type`
  - `subject_constraints`
  - `continuity_mode`
  - `chain_from_previous`
  - `keyframes`
- 角色参考图生成
- 镜头图片 / 视频 / BGM 素材生成
- 品牌化处理（Logo、字幕条、水印、产品贴图）
- 多平台视频合成（竖版 / 方版 / 横版）
- **storyboard 兼容迁移层**：
  - `run_pipeline.py` 会在 `_normalized/storyboard.json` 中回填 legacy storyboard 缺失的 `shot_type`
  - 同时输出 `_normalized/storyboard_migration_report.json`
  - `pipeline_result.json` 会暴露 `storyboard_migration.summary`
- **连续性控制策略**：通过 `continuity_mode` 字段控制尾帧生成
  - `strict`：关键镜头强约束终点（情绪转折、状态大变化）
  - `scene_end`：默认行为，仅 scene 末尾生成尾帧
  - `free`：氛围空镜，不生成尾帧约束
- **中间关键帧策略**：可选 `keyframes` 描述镜头中间状态
  - `keyframes` 现在被视为“中间阶段参考”的兼容字段
  - 执行层会把它映射成 `video_references[].usage = "reference_stage"`
  - 其他模型会按用途优先级裁剪参考素材
- **强动作镜头协议已强化**
  - 强动作 shot 不能再写成一句剧情摘要
  - 必须按“起势 → 爆发/碰撞 → 结果落点”组织 `action_prompt` 和 `motion_control.phase_beats`
  - 这类 shot 默认应提高 `keyframes` 密度，避免模型退化成慢速补间
- **当前视频 provider 主路径已切到 Ark Seedance 2.0**
  - storyboard 仍保留 `scene_prompt` / `end_frame_description` / `keyframes` / `continuity_mode`
  - 但执行层不再按“首尾帧+关键帧插值”组织请求，而是按用途驱动参考素材组织 Ark `content`
  - 当前默认模型为 `doubao-seedance-2-0-fast-260128`
- **视频参考协议已升级为用途驱动**
  - 执行层内部不再把所有参考图都视为同一种 `reference_image`
  - 参考素材会先归一化成 `video_references`
  - 常用用途包括：
    - `first_frame`
    - `reference_character`
    - `reference_prop`
    - `reference_composition`
    - `reference_style`
    - `reference_stage`
    - `reference_target_state`
  - Seedance prompt 会显式写出 `@图片N 作为首帧 / 参考角色 / 参考构图 ...`
  - 旧的 `keyframes` 字段仍兼容，但会被映射成 `reference_stage`
- **两阶段视频质量审查**：每段视频生成后自动检测
  - **阶段1 - 粗筛检测**：
    - 帧间突变 + 闪烁检测（MSE 分析）
    - 局部突变检测（spike detection，捕捉面部变形/画面撕裂）
    - 人脸变形检测（OpenCV DNN，追踪人脸置信度骤降）
    - HOG 人体检测（检测重复角色/identity hallucination）
    - 按 camera movement 自动选择质量 profile：`static` / `medium_motion` / `heavy_motion`
    - 导出风险片段和关键帧到 `vision_bundle`
  - **阶段2 - LLM 视觉判断**：
    - LLM 查看风险帧图片，做最终裁定
    - 默认由当前 skill 对话中的母模型做人审式最终裁定
    - 外部 `vision_judge.py` 现在只是可选工程化接口，不是默认主路径
    - 支持 `keep` / `cut_segment` / `regenerate`
  - **状态追踪**：
    - `audited`
    - `pending_judgment`
    - `judged`
    - `applied`
    - `finalized`
- **视频 prompt 约束增强**：
  - `shot_type` / `subject_constraints` 已正式接入图片和视频 prompt
  - 对多人强交互 shot，会自动追加 interaction guardrails，抑制“脱离交战再返回”等长时逻辑错误
- **pair-level 编辑决策层**：
  - 合成前会为每对相邻 shot 生成 `edit_decisions.json`
  - 决策字段包括：
    - `pair_type`
    - `confidence`
    - `trim_in`
    - `trim_out`
    - `transition_type`
    - `transition_candidates`
  - 当前已支持的语义类型包括：
    - `same_moment_overlap`
    - `continuous_action_same_scene`
    - `reverse_shot_same_scene`
    - `reaction_cut`
    - `impact_cut`
    - `scene_transition_soft`
    - `scene_transition_hard`
  - compose 会先执行 pair-level 裁边，再执行转场
  - 支持通过 `--edit_judgments` 让母模型覆盖场景转场候选的最终选择

当前仓库还没有覆盖的能力：

- 自动生成 `publish.json`
- 自动导出 Remotion 配置

## Repository Layout

```text
.
├── SKILL.md                  # OpenClaw skill 主入口
├── TASK.md                   # 仓库定位和现状说明
├── config/
│   ├── api_keys.yaml.example # API 配置模板
│   ├── platforms.yaml        # 平台尺寸配置
│   └── providers.yaml        # provider / model 配置
├── scripts/
│   ├── ad_assets.py          # 角色参考图 / 素材 / BGM 生成
│   ├── ad_brand.py           # 品牌化处理
│   ├── ad_compose.py         # FFmpeg 合成
│   ├── vision_judge.py       # LLM 视觉质量判断
│   ├── re_audit_videos.py    # 离线重新审计视频
│   ├── run_pipeline.py       # 执行编排器
│   ├── validate_json.py      # JSON 结构校验
│   ├── content_filter.py     # prompt 构造与过滤
│   ├── image_composer.py     # 图像辅助处理
│   ├── models.py             # 数据模型
│   └── utils.py              # 通用工具
├── templates/                # 提示词模板
├── examples/sample_output/   # 历史样例输出
└── models/                   # 本地视觉模型资源
```

## Requirements

- Python `3.11+`，当前脚本默认 shebang 使用 `/opt/homebrew/bin/python3.14`
- `ffmpeg` / `ffprobe`
- 可用的图像、视频、BGM 外部 API Key

建议先安装依赖：

```bash
cd skills/xyz-video-skill
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

复制 API 配置模板：

```bash
cp config/api_keys.yaml.example config/api_keys.yaml
```

主要配置文件：

- `config/api_keys.yaml`：本地 API Key
- `config/providers.yaml`：provider 和模型 fallback 链
- `config/platforms.yaml`：输出平台尺寸

也可以通过环境变量覆盖部分 API 设置，例如：

- `VOLCENGINE_API_KEY`
- `APIMART_API_KEY`
- `BYTEPLUS_API_KEY`
- `EVOLINK_API_KEY`
- `OPENROUTER_API_KEY`
- `VIDEO_OUTPUT_ROOT`

## Workflow

### 1. 由宿主 LLM 生成结构化 JSON

按照 `SKILL.md` 流程产出：

1. `story.json`
2. `framework.json`
3. `storyboard.json`

### 2. 生成角色参考图

```bash
python3 scripts/ad_assets.py \
  --mode character_refs \
  --framework /path/to/framework.json \
  --output_dir /path/to/output/character_refs
```

### 3. 生成镜头素材

```bash
python3 scripts/ad_assets.py \
  --mode assets \
  --storyboard /path/to/storyboard.json \
  --output_dir /path/to/output/assets
```

### 4. 可选品牌化

```bash
python3 scripts/ad_brand.py \
  --assets_manifest /path/to/output/assets/assets.json \
  --storyboard_file /path/to/storyboard.json \
  --brand_color '#FF6A00' \
  --output_dir /path/to/output/brand
```

### 5. 合成成片

```bash
python3 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /path/to/output/assets/assets.json \
  --platform douyin wechat youtube \
  --output_dir /path/to/output/videos
```

### 6. 使用 orchestrator 串执行步骤

如果你已经准备好了 `framework.json` 和 `storyboard.json`，可以直接用执行编排器：

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --platform douyin wechat youtube
```

它会按顺序执行：

- 校验 JSON
- 生成角色参考图
- 标准化 storyboard（输出 migration report）
- 生成素材
- 可选品牌化
- 合成视频

注意：

- `run_pipeline.py` 是执行编排器，不会替你生成故事或分镜
- `story.json` 目前仅用于校验和留档，不参与后续执行
- 如果你不想生成参考图，可传 `--skip_refs`
- 如果你只想跑到素材阶段，可传 `--skip_compose`
- 更推荐用 `--from` / `--to` 精确选择阶段范围

## Validate JSON

在调用素材生成或视频合成前，建议先校验结构化 JSON：

```bash
python3 scripts/validate_json.py /path/to/story.json
python3 scripts/validate_json.py /path/to/framework.json
python3 scripts/validate_json.py /path/to/storyboard.json
```

也可以一次校验多个文件：

```bash
python3 scripts/validate_json.py \
  /path/to/story.json \
  /path/to/framework.json \
  /path/to/storyboard.json
```

输出说明：

- `OK`：结构满足当前最小合同
- `WARN`：可运行，但存在 legacy 结构或可疑字段
- `ERROR`：关键字段缺失、引用错误或层级不合法

当前校验器覆盖：

- `story.json` 的核心字段和 `story_beats`
- `framework.json` 的角色、地点、场景与引用关系
- `storyboard.json` 的 `scenes > shots` 主结构
- 兼容 legacy 的 flat `shots` 结构，并给出警告

## Orchestrator

`scripts/run_pipeline.py` 是当前仓库的执行编排入口。

示例：

```bash
python3 scripts/run_pipeline.py \
  --story /path/to/story.json \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --platform douyin wechat youtube \
  --logo_path /path/to/logo.png \
  --brand_color '#FF6A00'
```

常用参数：

- `--from validate|refs|assets|brand|compose`
- `--to validate|refs|assets|brand|compose`
- `--assets_manifest /path/to/assets.json`
- `--brand_manifest /path/to/brand_manifest.json`
- `--skip_validate`
- `--skip_refs`
- `--skip_assets`
- `--skip_brand`
- `--skip_compose`
- `--no_api`
- `--output_dir`

输出：

- `pipeline_result.json`
- `_normalized/storyboard.json`
- `character_refs/`
- `assets/`
- `brand/`
- `videos/`

阶段示例：

```bash
# 只跑参考图到素材
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --from refs \
  --to assets

# 已经有素材，只重跑品牌化和合成
python3 scripts/run_pipeline.py \
  --storyboard /path/to/storyboard.json \
  --assets_manifest /path/to/assets/assets.json \
  --from brand \
  --to compose \
  --logo_path /path/to/logo.png

# 只从合成阶段继续
python3 scripts/run_pipeline.py \
  --storyboard /path/to/storyboard.json \
  --brand_manifest /path/to/brand/brand_manifest.json \
  --from compose
```

## Video Quality Review

视频生成后会自动进行两阶段质量审查：

### 阶段1：粗筛检测（自动）

生成视频时，系统会自动检测问题并导出 `vision_bundle`：

```bash
python3 scripts/ad_assets.py \
  --mode assets \
  --storyboard /path/to/storyboard.json \
  --review_mode hybrid_judge \
  --output_dir /path/to/output/assets
```

输出：
- `assets/audit/shot_XXX/quality_audit.json` - 审计报告
- `assets/audit/shot_XXX/vision_bundle_attempt_N/` - 风险帧图片
- `assets/audit/shot_XXX/vision_bundle_attempt_N/vision_judge_request.json` - 判断请求
- `assets/audit/shot_XXX/raw_attempt_N.mp4` - 原始视频
- `assets/audit/shot_XXX/trimmed_attempt_N.mp4` - 裁剪后视频（如果有）

### 阶段2：LLM 视觉判断

**默认模式（母模型手动判断）**：

1. 查看 `vision_bundle` 中的风险帧图片
2. 创建 `vision_judge_result.json`：

```json
{
  "shot_id": "001",
  "segments": [
    {
      "start": 2.5,
      "end": 3.0,
      "issue_type": "identity_hallucination",
      "severity": "high",
      "confidence": 0.95,
      "action": "cut_segment",
      "reason": "Duplicate character appears"
    }
  ],
  "overall_action": "cut_segment",
  "fallback_used": false
}
```

3. 重新运行生成流程，系统会读取判断结果并执行决策

补充说明：

- 当前默认主路径是“规则粗筛 + 母模型视觉裁定”
- `vision_judge.py` 和外部 API 只作为后续自动化接口，不是默认强依赖
- `hybrid_judge` 模式下，素材阶段会在 `pending_judgment` 停住，等待母模型裁定后再继续执行
- `metrics_only` 链路下，如果触发参考图前置审查，也会在 `pending_judgment` 停住
- 当素材阶段停在参考图审查时，优先查看：
  - `image_audit/shot_{id}/reference_bundle/reference_review_request.json`
  - `image_audit/shot_{id}/reference_bundle/video_prompt.txt`
  - 其中 `video_prompt.txt` 会直接展示最终的 `@图片N 作为首帧 / 参考角色 / 参考构图 ...` 调用

### 离线重新审计

如果需要用新的检测参数重新审计已生成的视频：

```bash
python3 scripts/re_audit_videos.py \
  --source-audit-root /path/to/output/assets/audit \
  --output-root /path/to/reaudit \
  --review_mode hybrid_judge \
  --chars shot_001=1 shot_002=2 \
  --facing shot_001=toward_camera \
  shot_001 shot_002
```

## Output

默认输出根目录优先使用：

- `VIDEO_OUTPUT_ROOT`
- `AD_OUTPUT_ROOT`
- `~/video-output/`
- 回退到 `/tmp/video-output/`

常见产物：

- `story.json`
- `framework.json`
- `storyboard.json`
- `_normalized/storyboard.json`
- `_normalized/storyboard_migration_report.json`
- `character_refs/`
- `assets/assets.json`
- `assets/prompts/shot_{id}_video_prompt.txt`
- `assets/audit/`
- `assets/image_audit/shot_{id}/reference_bundle/video_prompt.txt`
- `brand/brand_manifest.json`
- `videos/*.mp4`
- `videos/edit_decisions.json`

其中：

- `assets/assets.json`
  - 现在会同时汇总 `images`、`videos`、`shot_prompts`、`shot_references`
- `assets/prompts/shot_{id}_video_prompt.txt`
  - 是每个 shot 的最终视频 prompt 落盘版本
- `assets/image_audit/shot_{id}/reference_bundle/video_prompt.txt`
  - 是参考图前置审查时随 bundle 一起导出的 prompt 副本，方便直接检查用途声明是否正确

## Edit Decisions

`scripts/ad_compose.py` 现在不再只是按每个 shot 自己的 `transition_in` 硬拼。

它会先做 pair-level 编辑判断，再决定如何裁边和衔接：

- 输入信号：
  - `scene` 边界
  - `chain_from_previous`
  - `characters_in_shot`
  - `narrative_segment`
  - 视频边界视觉重叠估计
- 输出文件：
  - `edit_decisions.json`
- 典型字段：
  - `pair_type`
  - `confidence`
  - `trim_out`
  - `trim_in`
  - `transition_type`
  - `transition_candidates`
  - `judgment_applied`

如果你想让母模型覆盖场景转场选择：

```bash
python3 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /path/to/assets.json \
  --edit_judgments /path/to/edit_judgments.json \
  --output_dir /path/to/output/videos
```

`edit_judgments.json` 示例：

```json
[
  {
    "from_shot": 2,
    "to_shot": 3,
    "transition_type": "flash-white",
    "transition_duration": 0.12
  }
]
```

## OpenClaw Integration

在 OpenClaw 中，正式入口是：

- `skills/xyz-video-skill/SKILL.md`

这个 skill 的核心模式是：

- OpenClaw / 宿主模型负责思考和结构化输出
- 本仓库脚本负责外部 API 调用与媒体执行

## Known Gaps

- `templates/` 仍有历史迭代遗留
- `examples/sample_output/` 是历史样例，不是当前严格输出合同
- `README.md` / `SKILL.md` 需要持续和协议、审查流程、编辑决策层一起维护

## Recommended Next Steps

1. 增加 `story/framework/storyboard` schema 校验
2. 增加单命令编排入口
3. 整理 `templates/` 与 `examples/`
4. 增加最小可运行 smoke test
