# xyz-video-skill

`xyz-video-skill` 是一个由宿主 LLM 驱动、由 Python 执行层落地的视频生成 skill。

它的定位不是“一键广告片生成器”，而是：

- 宿主 LLM 负责故事创作、角色设计、剧本框架、分镜脚本、导演级审片判断
- Python 脚本负责角色参考图、图片/视频素材、质量审查、品牌化处理、FFmpeg 合成

当前主链路已经收口为一条可执行的视频生产流水线：`story.json -> framework.json -> storyboard.json -> refs -> assets -> brand(optional) -> compose`。

## 当前能力

仓库当前已经覆盖：

- `story.json` / `framework.json` / `storyboard.json` 校验
- `storyboard.json` 规范化与兼容迁移
  - 自动回填 legacy storyboard 缺失的 `shot_type`
  - 自动补全或规范化 `video_references`
  - 输出 `_normalized/storyboard.json`
  - 输出 `_normalized/storyboard_migration_report.json`
- 角色参考图生成
- 图片生成、视频生成、BGM 生成
- 用途驱动的视频参考协议
  - `first_frame`
  - `reference_character`
  - `reference_prop`
  - `reference_composition`
  - `reference_style`
  - `reference_stage`
  - `reference_target_state`
- `continuity_mode` / `chain_from_previous` / `keyframes` 驱动的连续性控制
- 视频质量两阶段审查
  - 自动粗筛：MSE、spike、人脸变形、重复角色风险等
  - 视觉裁定：`metrics_only` / `hybrid_judge` / `director_review`
- pair-level 编辑决策与 FFmpeg 合成
- 可选品牌化处理：Logo、标题条、字幕条、水印、产品贴图
- 多平台输出：`douyin` / `wechat` / `youtube`

当前没有自动完成的部分：

- 自动生成 `story.json` / `framework.json` / `storyboard.json`
- 自动生成 `publish.json`
- 自动导出 Remotion 配置

## 整体流程

### 1. 宿主 LLM 产出结构化 JSON

按 `SKILL.md` 的协议，先产出：

1. `story.json`
2. `framework.json`
3. `storyboard.json`

这一步是 skill 协议的一部分，不是仓库里的 CLI 自动生成。

### 2. 生成角色参考图

```bash
python3 scripts/ad_assets.py \
  --mode character_refs \
  --framework /path/to/framework.json \
  --output_dir /path/to/output/character_refs
```

### 3. 规范化 storyboard 并生成素材

推荐直接走 orchestrator：

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --platform douyin wechat youtube
```

执行阶段顺序：

1. `validate`
2. `refs`
3. `assets`
4. `brand`
5. `compose`

### 4. 可选品牌化

```bash
python3 scripts/ad_brand.py \
  --assets_manifest /path/to/output/assets/assets.json \
  --storyboard_file /path/to/storyboard.json \
  --logo_path /path/to/logo.png \
  --brand_color '#FF6A00' \
  --output_dir /path/to/output/brand
```

### 5. 合成成片

```bash
python3 scripts/ad_compose.py \
  --storyboard /path/to/output/_normalized/storyboard.json \
  --assets /path/to/output/assets/assets.json \
  --platform douyin wechat youtube \
  --output_dir /path/to/output/videos
```

如果要使用品牌化素材，`--assets` 传 `brand_manifest.json` 即可。

## Quick Start

### 运行环境

- Python `3.11+`
- `ffmpeg` / `ffprobe`
- 可用的图像、视频、BGM API Key

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置 API

先复制模板：

```bash
cp config/api_keys.yaml.example config/api_keys.yaml
```

配置优先级：

1. 环境变量
2. `AD_GENERATOR_CONFIG` 指向的 YAML
3. `config/api_keys.yaml`
4. `config/providers.yaml` 中的默认 `api_base`

常用环境变量：

- `VOLCENGINE_API_KEY`
- `APIMART_API_KEY`
- `FAL_KEY`
- `BYTEPLUS_API_KEY`
- `EVOLINK_API_KEY`
- `OPENROUTER_API_KEY`
- `VIDEO_OUTPUT_ROOT`
- `AD_GENERATOR_CONFIG`

### 先校验 JSON

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

### 跑完整执行链路

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --platform douyin wechat youtube \
  --review_mode metrics_only
```

### 常用选项

只跑某一段阶段：

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --from assets \
  --to compose
```

断点续跑素材生成：

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --resume
```

离线占位调试：

```bash
python3 scripts/run_pipeline.py \
  --framework /path/to/framework.json \
  --storyboard /path/to/storyboard.json \
  --no_api \
  --skip_compose
```

从已有素材继续品牌化并合成：

```bash
python3 scripts/run_pipeline.py \
  --storyboard /path/to/storyboard.json \
  --assets_manifest /path/to/output/assets/assets.json \
  --from brand \
  --logo_path /path/to/logo.png
```

## Review Mode

`scripts/ad_assets.py` 和 `scripts/run_pipeline.py` 支持三种审查模式：

- `metrics_only`：只做自动质量检测，适合快速迭代
- `hybrid_judge`：自动粗筛后导出风险帧，等待视觉裁定
- `director_review`：逐张图进入导演审图流程，适合高要求项目

当流程进入待裁定状态时，`assets.json` 和 `pipeline_result.json` 会带上 `pending_reviews` / `blocked_reviews`。

## Storyboard 协议要点

当前执行层重点依赖这些字段：

- `shot_type`
- `subject_constraints`
- `continuity_mode`
- `chain_from_previous`
- `video_references`
- `keyframes`
- `end_frame_description`
- `motion_control`

其中：

- `video_references` 已经是主协议
- `keyframes` 仍兼容，但执行层会把它映射成 `reference_stage`
- `continuity_mode=scene_end` 会在 scene 末尾优先生成目标状态参考
- `continuity_mode=strict` 会强约束目标状态
- `chain_from_previous=true` 会尽量从上一镜真实视频尾帧续接

如果传入的是旧版 storyboard，`scripts/run_pipeline.py` 会先做兼容迁移，再进入执行。

## 输出结构

默认输出目录为 `~/video-output/{timestamp}/`；不可写时回退 `/tmp/video-output/`。也可以用 `VIDEO_OUTPUT_ROOT` 覆盖。

一次完整运行通常会生成：

```text
{output_dir}/
├── _normalized/
│   ├── storyboard.json
│   ├── storyboard_migration_report.json
│   └── ref_binding_report.json
├── character_refs/
│   └── character_refs.json
├── assets/
│   ├── assets.json
│   ├── images/
│   ├── videos/
│   ├── bgm/
│   ├── audit/
│   └── image_audit/
├── brand/
│   └── brand_manifest.json
├── videos/
│   ├── result.json
│   └── edit_decisions.json
└── pipeline_result.json
```

关键产物：

- `_normalized/storyboard.json`：执行层实际使用的 storyboard
- `assets/assets.json`：素材 manifest
- `videos/edit_decisions.json`：相邻 shot 的剪辑决策
- `videos/result.json`：合成结果
- `pipeline_result.json`：整条 pipeline 的阶段结果汇总

## 仓库结构

```text
.
├── README.md
├── SKILL.md
├── TASK.md
├── config/
│   ├── api_keys.yaml.example
│   ├── platforms.yaml
│   └── providers.yaml
├── docs/
├── examples/
├── reference/
├── scripts/
│   ├── run_pipeline.py
│   ├── ad_assets.py
│   ├── ad_brand.py
│   ├── ad_compose.py
│   ├── validate_json.py
│   ├── re_audit_videos.py
│   ├── summarize_audit.py
│   ├── image_gen.py
│   ├── video_gen.py
│   ├── reference_builder.py
│   ├── video_quality.py
│   └── ...
└── templates/
```

主要脚本职责：

- `scripts/run_pipeline.py`：执行编排入口
- `scripts/ad_assets.py`：角色参考图、图片、视频、BGM 生成
- `scripts/ad_brand.py`：品牌化处理
- `scripts/ad_compose.py`：编辑决策 + FFmpeg 合成
- `scripts/validate_json.py`：输入 JSON 校验
- `scripts/re_audit_videos.py`：离线复审已有视频
- `scripts/summarize_audit.py`：汇总 audit 结果

## 参考文档

推荐按这个顺序看：

- `SKILL.md`：宿主 LLM 的完整工作流协议
- `docs/pipeline_overview.md`：完整流程总览
- `docs/pipeline-deep-dive.md`：执行链路细节
- `reference/video_generation_strategy.md`：视频参考图与 `video_references` 设计原则
- `reference/video_quality_standard.md`：视频质检标准
- `examples/medical_reunion_storyboard.json`：较新的 storyboard 示例

## 历史样例说明

`examples/sample_output/` 是历史样例输出，只能当结构参考，不能当作当前 CLI 的精确输出合同。

尤其这几个文件代表的是旧链路设想：

- `examples/sample_output/publish.json`
- `examples/sample_output/ad_config.ts`
- `examples/sample_output/compose_result.json`

当前仓库的主入口和主合同，已经切到 `SKILL.md` + `scripts/run_pipeline.py`。
