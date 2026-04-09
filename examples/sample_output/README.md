# sample_output

这个目录保留的是一套**历史广告样例输出**，主要用于参考文件结构，不代表当前脚本的完整输出合同。

## 先看结论

- 这里的 `storyboard.json`、素材 manifest、品牌化图片可以作为结构参考
- 这里的 `publish.json`、`ad_config.ts`、`compose_result.json` 反映的是旧版广告链路设想
- README 里旧的一键命令已不适用于当前 `scripts/ad_compose.py`

## 当前代码库中可复现的流程

### 1. 先准备 `storyboard.json`

当前仓库中，`storyboard.json` 由宿主 LLM 按 `SKILL.md` 生成，不是由 `ad_compose.py` 自动生成。

### 2. 生成素材

```bash
/opt/homebrew/bin/python3.14 scripts/ad_assets.py \
  --mode assets \
  --storyboard /path/to/storyboard.json \
  --output_dir /tmp/xyz-video-skill-sample/assets \
  --no_api
```

说明：

- `--no_api` 会走离线兜底路径，便于本地调试
- 输出文件为 `/tmp/xyz-video-skill-sample/assets/assets.json`

### 3. 可选品牌化

```bash
/opt/homebrew/bin/python3.14 scripts/ad_brand.py \
  --assets_manifest /tmp/xyz-video-skill-sample/assets/assets.json \
  --storyboard_file /path/to/storyboard.json \
  --brand_color '#FF6A00' \
  --output_dir /tmp/xyz-video-skill-sample/brand
```

输出文件为 `/tmp/xyz-video-skill-sample/brand/brand_manifest.json`。

### 4. 合成视频

如果直接使用原始素材：

```bash
/opt/homebrew/bin/python3.14 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /tmp/xyz-video-skill-sample/assets/assets.json \
  --platform douyin wechat youtube \
  --output_dir /tmp/xyz-video-skill-sample/videos
```

如果使用品牌化后的图片：

```bash
/opt/homebrew/bin/python3.14 scripts/ad_compose.py \
  --storyboard /path/to/storyboard.json \
  --assets /tmp/xyz-video-skill-sample/brand/brand_manifest.json \
  --platform douyin wechat youtube \
  --output_dir /tmp/xyz-video-skill-sample/videos
```

## 目录内文件的含义

- `storyboard.json`：旧版广告样例的分镜结构参考
- `assets_manifest.json`：旧版命名下的素材 manifest，当前脚本默认输出名是 `assets.json`
- `brand_manifest.json`：品牌化后的 manifest，结构仍可被当前 `ad_compose.py` 使用
- `publish.json`：旧版广告投放文案产物，当前仓库没有自动生成脚本
- `compose_result.json`：旧版一键编排结果示例，不代表当前 CLI 返回格式
- `ad_config.ts`：旧版 Remotion 配置示例，当前仓库没有自动导出逻辑

## 为什么这里看起来和当前脚本不一致

因为样例目录来自更早的“广告片一键生成 MVP”设计阶段，而当前代码已经收敛成：

- 宿主 LLM 负责故事和分镜
- Python 脚本负责素材、品牌化和 FFmpeg 合成

所以这个目录应理解为“历史参考样例”，而不是“当前 CLI 的精确回放结果”。
