# XYZ Video Skill - 当前仓库说明

## 当前定位

这个仓库当前更准确的定位是一个**由宿主 LLM 驱动的视频生成 Skill**，而不是一个已经完整收口的一键式广告片 CLI。

- 宿主 LLM 负责故事创作、剧本框架、角色设计、分镜脚本等思考任务
- Python 脚本负责角色参考图、镜头素材、品牌化处理和 FFmpeg 合成
- 现有代码已经覆盖执行层 MVP，但“创意策划 → 分镜脚本 → 投放文案”仍主要靠 `SKILL.md` 约束宿主 LLM 产出 JSON

如果把当前仓库理解成“视频生成 skill / 视频执行管线”，会比“广告片一键生成器”更准确。

## 实际文件结构

```text
skills/xyz-video-skill/
├── SKILL.md                     # 主入口说明，定义宿主 LLM 的工作流
├── TASK.md                      # 当前仓库说明（本文件）
├── scripts/
│   ├── ad_assets.py             # 角色参考图 / 镜头素材 / BGM 生成
│   ├── ad_brand.py              # 品牌适配（Logo、标题条、字幕条、水印、产品贴图）
│   ├── ad_compose.py            # FFmpeg 合成（按平台尺寸输出）
│   ├── content_filter.py        # prompt 过滤与构造辅助
│   ├── image_composer.py        # 图像生成辅助逻辑
│   ├── models.py                # 数据模型
│   └── utils.py                 # 通用工具
├── templates/                   # 提示词资产，包含多个迭代阶段留下的模板
├── config/
│   ├── platforms.yaml           # 平台尺寸配置
│   ├── providers.yaml           # 模型和 provider 配置
│   └── api_keys.yaml.example    # API 配置示例
└── examples/
    └── sample_output/           # 历史样例输出（包含部分旧版广告链路产物）
```

## 当前真实工作流

### 1. 宿主 LLM 产出结构化 JSON

按 [`SKILL.md`](./SKILL.md) 中定义的流程，由宿主 LLM 逐步产出：

1. `story.json`
2. `framework.json`
3. `storyboard.json`

这一步不是由当前仓库内某个 `ad_creative.py` 或 `ad_storyboard.py` 自动完成，而是由 skill 提示词和宿主 LLM 的推理完成。

### 2. 生成角色参考图

```bash
/opt/homebrew/bin/python3.14 scripts/ad_assets.py \
  --mode character_refs \
  --framework /path/to/framework.json \
  --output_dir /path/to/output/character_refs
```

用途：

- 读取 `framework.json` 中的 `suggested_characters`
- 为每个角色生成参考图
- 输出 `character_refs.json` 和角色图片

### 3. 生成镜头素材

```bash
/opt/homebrew/bin/python3.14 scripts/ad_assets.py \
  --mode assets \
  --storyboard /path/to/storyboard.json \
  --output_dir /path/to/output/assets
```

用途：

- 读取 `storyboard.json`
- 生成每个 shot 的图片 / 视频片段 / BGM
- 输出 `assets.json`

当前实现支持：

- `scenes > shots` 嵌套结构
- 向下兼容 legacy 的 flat `shots`
- `chain_from_previous` 尾帧衔接
- `end_frame_description` 校验
- 视频质量三重检测（帧间突变/闪烁、局部突变 spike、人脸变形 DNN）、自动裁剪和重试
- 图像生成 fallback provider 链

### 4. 可选品牌化处理

```bash
/opt/homebrew/bin/python3.14 scripts/ad_brand.py \
  --assets_manifest /path/to/output/assets/assets.json \
  --storyboard_file /path/to/storyboard.json \
  --logo_path /path/to/logo.png \
  --brand_color '#FF6A00' \
  --output_dir /path/to/output/brand
```

用途：

- 将图片素材做品牌化二次处理
- 叠加标题条、字幕条、Logo、水印
- 可将产品图插入指定镜头
- 输出 `brand_manifest.json`

说明：

- `ad_compose.py` 的 `--assets` 参数只要求传入 manifest JSON
- 因此如果想用品牌化后的图片继续合成，可以直接传 `brand_manifest.json`

### 5. 合成成片

```bash
/opt/homebrew/bin/python3.14 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /path/to/output/assets/assets.json \
  --platform douyin wechat youtube \
  --output_dir /path/to/output/videos
```

如果要使用品牌化后的图片：

```bash
/opt/homebrew/bin/python3.14 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /path/to/output/brand/brand_manifest.json \
  --platform douyin wechat youtube \
  --output_dir /path/to/output/videos
```

当前实现支持：

- 按 [`config/platforms.yaml`](./config/platforms.yaml) 中的平台尺寸输出
- 优先使用生成的视频片段，失败时回退到静态图片
- 基于 `transition_in` 的转场拼接
- BGM 混合
- FFmpeg 合成

## 当前已实现的脚本职责

### `scripts/ad_assets.py`

当前是仓库里最核心的执行脚本，承担两类任务：

1. `character_refs` 模式：从 `framework.json` 生成角色参考图
2. `assets` 模式：从 `storyboard.json` 生成图片、视频和 BGM

它不是“只生成广告素材”的薄封装，而是已经包含较完整的素材执行逻辑。

### `scripts/ad_brand.py`

负责视觉品牌化：

- 标题条和字幕条
- 品牌主色
- Logo 贴角
- 水印保护
- 产品图贴入指定镜头
- 片头 / 片尾静态图

### `scripts/ad_compose.py`

当前职责是**纯合成**，不是全流程 orchestrator。

它当前做的事情：

- 读取 `storyboard.json`
- 读取素材 manifest
- 生成每个平台尺寸的视频文件
- 输出 `result.json`

它当前**不负责**：

- 根据产品信息自动生成创意
- 自动写 `storyboard.json`
- 自动生成 `publish.json`
- 自动生成 Remotion `ad_config.ts`

### `scripts/run_pipeline.py`

当前新增的执行编排入口。

它当前负责：

- 校验 `story.json` / `framework.json` / `storyboard.json`
- 串联角色参考图、素材生成、品牌化、视频合成
- 支持用 `--from` / `--to` 截取执行阶段
- 统一产出 `pipeline_result.json`
- 在运行前规范化 `storyboard.character_ref_dir`

它当前**不负责**：

- 自动生成故事
- 自动生成 `framework.json`
- 自动生成 `storyboard.json`
- 自动生成 `publish.json`

## 当前未实现或未收口的部分

以下内容在旧设计中出现过，但与当前代码不一致：

- `scripts/ad_creative.py`：不存在
- `scripts/ad_storyboard.py`：不存在
- `scripts/ad_publish.py`：不存在
- `python3 scripts/ad_compose.py --product ... --selling_points ...` 这种一键入口：当前不存在
- Remotion `ad_config.ts` 自动导出：当前代码未实现
- `publish.json` 自动生成：当前代码未实现

## 关于 `templates/`

`templates/` 目录里保留了多套 prompt 资产，来源于不同阶段的设计迭代。

当前状态更接近：

- 一部分模板是广告导向的旧 prompt
- 一部分模板是结构化故事/分镜生成草稿
- 它们不是当前 Python 脚本的严格运行时依赖
- 当前主入口仍然是 [`SKILL.md`](./SKILL.md) 中对宿主 LLM 的流程约束

如果后续继续整理仓库，`templates/` 建议单独做一次归档或命名梳理。

## 运行环境

- Python：`/opt/homebrew/bin/python3.14`
- 主要依赖：`Pillow`、`aiohttp`、`pyyaml`、`edge-tts`
- 输出目录：默认 `~/video-output/{timestamp}/`
- API Key：环境变量或 `config/api_keys.yaml`

## 样例输出目录说明

[`examples/sample_output/`](./examples/sample_output/) 里保留的是一套**历史广告样例产物**，其中部分文件来自旧版设计口径。

这意味着：

- 目录中的文件可以作为结构参考
- 但不能把该目录中的 README 命令直接当作当前 CLI 说明
- `publish.json`、`ad_config.ts`、`compose_result.json` 中的部分字段代表旧版广告链路设想，不是当前脚本的直接输出合同

## 建议的后续整理方向

如果继续维护这个仓库，优先级建议如下：

1. 保持 `SKILL.md` 作为唯一真实工作流说明
2. 将旧版广告文档和当前视频执行流明确分层
3. 决定是否真的要补齐一键式广告入口
4. 对 `templates/` 和 `examples/` 做一次“当前可用 / 历史遗留”标注
