# Ad Generator Skill - Development Task

## 目标
创建 `ad-generator` Skill，实现广告片生成线的 MVP Day 1-3。

## 文件结构
```
skills/ad-generator/
├── SKILL.md                    # Skill 入口定义
├── scripts/
│   ├── ad_creative.py          # 创意策划模块
│   ├── ad_storyboard.py        # 分镜生成模块
│   ├── ad_assets.py            # 素材生成模块（图/音/BGM）
│   ├── ad_brand.py             # 品牌适配模块（Logo/配色叠加）
│   ├── ad_compose.py           # 合成编排模块
│   └── ad_publish.py           # 投放文案生成模块
├── templates/
│   ├── creative_prompt.md      # 创意策划 prompt 模板
│   ├── storyboard_prompt.md    # 分镜生成 prompt 模板
│   └── publish_prompt.md       # 投放文案 prompt 模板
├── config/
│   └── platforms.yaml          # 平台尺寸/规格配置
└── examples/
    └── sample_output/          # 示例产出
```

## Day 1: 创意+脚本引擎

### ad_creative.py
功能：
- 输入：产品名称、核心卖点、目标人群、投放平台
- 输出：3-5 套创意方案（情感向/功能向/悬念向/口播种草向）
- 每套包含：type, title, storyline, estimated_duration, scenes_preview, tone, target_audience_fit
- 输出结构化 JSON

### ad_storyboard.py
功能：
- 输入：选定的创意方案
- 输出：完整分镜脚本，含逐镜头分镜
- 每个分镜：id, duration, shot_type, visual_description, image_prompt, tts_text, subtitle, transition
- 支持 15s/30s/60s 三个版本
- BGM 风格建议

### templates/creative_prompt.md
广告创意策划的 prompt 模板，引导 LLM 按 4 种类型（emotional/functional/suspense/review）输出

### templates/storyboard_prompt.md
分镜生成的 prompt 模板，按镜头逐帧输出

## Day 2: 素材生成 pipeline

### ad_assets.py
功能：
- 调用 AI 图像生成 API（豆包4 doubao-seedream-4-0 via ark）
- 调用 TTS（IndexTTS2 或 Edge TTS）
- 调用 BGM 生成（MiniMax via fal.ai）
- 并行化生成
- fallback 机制：豆包 → ApiMart → Flux

参考代码（可从这里抽取逻辑）：
- ~/development/xyz-video-creator/backend/oii/image_composer.py
- ~/development/xyz-video-creator/backend/oii/tts_service.py
- ~/development/xyz-video-creator/backend/oii/bgm_service.py
- ~/development/xyz-video-creator/backend/universal_ai_client.py
- ~/development/xyz-video-creator/config.yaml （API 配置）

### ad_brand.py
功能：
- Logo 叠加（PIL/Pillow，右下角 + 片头/片尾）
- 品牌色应用到字幕和标题
- 产品图融入指定分镜
- 水印保护

## Day 3: 合成+输出

### ad_compose.py
功能：
- 编排整个流程：创意→脚本→素材→合成
- 生成 Remotion AdConfig（从分镜 JSON → TypeScript 配置）
- 调用 Remotion 渲染或 FFmpeg 合成
- 多尺寸输出（竖屏 1080x1920 / 方屏 1080x1080 / 横屏 1920x1080）

参考代码：
- ~/development/xyz-video-creator/backend/oii/video_composer.py
- FFmpeg 合成逻辑

### ad_publish.py
功能：
- 根据脚本+平台特性生成投放文案
- 生成标签建议
- A/B 测试方案

### config/platforms.yaml
```yaml
platforms:
  douyin:
    name: 抖音/快手
    width: 1080
    height: 1920
    ratio: "9:16"
    max_duration: 60
  wechat:
    name: 朋友圈/微博
    width: 1080
    height: 1080
    ratio: "1:1"
    max_duration: 30
  youtube:
    name: YouTube/B站
    width: 1920
    height: 1080
    ratio: "16:9"
    max_duration: 120
```

### SKILL.md
```markdown
---
name: ad-generator
description: "广告片生成线 - 对话驱动的端到端广告片生成。输入产品信息，自动完成创意策划→脚本分镜→素材生成→多版本成片。"
---

# 广告片生成线 Ad Generator

## 触发条件
用户提到"生成广告"、"广告片"、"商业视频"、"产品广告"等关键词

## 流程
1. 收集产品信息（名称、卖点、人群、平台）
2. 生成 3-5 套创意方案
3. 用户选定方向后生成分镜脚本（15s/30s/60s）
4. 并行生成素材（AI图、TTS、BGM）+ 品牌适配
5. 多版本合成（竖屏/方屏/横屏）
6. 生成投放文案和 A/B 建议

## 使用方式
```bash
python3 scripts/ad_compose.py --product "产品名" --selling_points "卖点" --audience "目标人群" --platform "douyin"
```
```

## 重要说明
1. 所有 Python 脚本使用 Python 3.14（/opt/homebrew/bin/python3.14）
2. 需要的依赖：Pillow, aiohttp, pyyaml, edge-tts
3. API Key 从环境变量或 xyz-video-creator/config.yaml 读取
4. 每个模块可独立运行和测试
5. 输出目录：~/ad-output/{timestamp}/
6. 注释和 docstring 用中文
