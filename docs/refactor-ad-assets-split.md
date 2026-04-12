# 拆分 ad_assets.py 详细方案

## 目标

将 4,500 行的 `scripts/ad_assets.py` 拆分成 5 个文件，每个文件 < 800 行，职责单一。

## 当前文件结构

```
scripts/ad_assets.py (4556 行)
  ├─ 模块级常量 (L35-117)
  ├─ class AssetGenerator (L119-4228)
  │   ├─ __init__ + checkpoint + session (L122-207)
  │   ├─ 配置读取 (L208-276)
  │   ├─ Director prompt/plan 相关 (L278-518)
  │   ├─ run() 主编排 (L520-916)
  │   ├─ 视频质量扫描 (L917-2025)
  │   ├─ 参考图构建 + 审阅 (L2026-2735)
  │   ├─ 全局 prompt 提取 (L2736-2962)
  │   ├─ _generate_shot() (L2963-3490)
  │   ├─ 视频生成 (L3491-3733)
  │   ├─ 图片生成 (L3734-4076)
  │   ├─ TTS/BGM/下载/占位 (L4077-4228)
  ├─ class CharacterRefGenerator (L4230-4472)
  └─ CLI 入口 (L4475-4556)
```

## 拆分方案：5 个文件

### 1. `scripts/video_quality.py` (~600 行)

**职责：** 视频质量扫描、语义审计、帧操作

**从 AssetGenerator 提取为独立函数（全部是 @staticmethod，不依赖 self）：**

| 函数 | 当前行号 | 说明 |
|---|---|---|
| `extract_video_last_frame()` | L917-930 | ffmpeg 提取视频尾帧 |
| `scan_video_quality()` | L932-1502 | 核心质量扫描（MSE/闪烁/spike/DNN） |
| `face_crop_duplicate_score()` | L1503-1517 | 人脸裁切重复评分 |
| `face_crop_identity_score()` | L1518-1539 | 人脸身份评分 |
| `box_iou()` | L1540-1554 | 边界框 IoU |
| `dedupe_boxes()` | L1555-1570 | 去重边界框 |
| `merge_segments()` | L1571-1588 | 合并风险片段 |
| `semantic_audit_video()` | L1589-1854 | 语义审计（HOG/DNN/朝向） |
| `save_audit_frames()` | L1855-1900 | 保存审查帧截图 |
| `trim_video_at()` | L1901-1932 | 视频裁剪 |
| `remove_video_segments()` | L1933-1991 | 删除视频片段 |
| `extract_segment_frames()` | L1992-2025 | 提取片段帧 |

**模块级常量也移过来：**
- `QUALITY_PROFILES` (L50-117)
- `DETECTOR_VERSION` (L35)

**依赖：** `subprocess`, `tempfile`, `logging`, `numpy`, `PIL.Image`, 可选 `cv2`

**不依赖 self，不依赖 aiohttp，不依赖任何其他 scripts/ 文件。** 这是最干净的拆分。

**提取后，`ad_assets.py` 中的调用改为：**
```python
from video_quality import (
    scan_video_quality, extract_video_last_frame, save_audit_frames,
    trim_video_at, remove_video_segments, extract_segment_frames,
    semantic_audit_video, merge_segments,
)
```

原来的 `AssetGenerator._scan_video_quality(...)` 调用改为 `scan_video_quality(...)`，去掉 `AssetGenerator.` 前缀。

---

### 2. `scripts/image_gen.py` (~500 行)

**职责：** 图片生成（3 个 provider + placeholder）

**提取的方法（改为接收 session 参数的独立异步函数）：**

| 函数 | 当前行号 | 说明 |
|---|---|---|
| `generate_image()` | L3734-3786 | 图片生成调度（fallback 链） |
| `image_volcengine()` | L3787-3867 | 火山引擎 Seedream |
| `image_apimart()` | L3868-4076 | APIMart Gemini multimodal |
| `image_flux()` | L4077-4124 | fal Flux |
| `placeholder_image()` | L4211-4218 | 灰色占位图 |

**函数签名变化：**

原来：
```python
async def _image_apimart(self, prompt, output, ...) -> bool:
    session = await self._get_session()
    # 用 self.storyboard, self.image_width, self.image_height
```

改为：
```python
async def image_apimart(
    session: aiohttp.ClientSession,
    prompt: str,
    output: Path,
    creds: dict,
    img_cfg: dict,
    image_width: int,
    image_height: int,
    storyboard: dict,
    characters_in_shot: list[str] | None = None,
    props_in_shot: list[str] | None = None,
    scene_image: Path | None = None,
    style_reference_image: Path | None = None,
) -> bool:
```

**关键：** `self.storyboard.get("characters", {})` 和 `self.storyboard.get("prop_refs", {})` 和 `self.storyboard.get("character_ref_dir", "")` 这些在 `_image_apimart` 内部读取的 storyboard 字段，改为从参数传入。

**模块级常量也移过来：**
- `GEMINI_IMAGE_SYSTEM_PROMPT`（如果存在的话——搜一下当前文件）

**依赖：** `aiohttp`, `base64`, `mimetypes`, `logging`, `PIL.Image`, `PIL.ImageDraw`

**提取后，`AssetGenerator._generate_image` 的调用改为：**
```python
from image_gen import generate_image, placeholder_image

# 在 _generate_shot 中：
success = await generate_image(
    session=await self._get_session(),
    prompt=prompt,
    output=output,
    fallback_chain=self._image_fallback_chain(),
    image_width=self.image_width,
    image_height=self.image_height,
    storyboard=self.storyboard,
    cfg=self.cfg,
    characters_in_shot=characters_in_shot,
    ...
)
```

---

### 3. `scripts/video_gen.py` (~250 行)

**职责：** 视频生成（Seedance + fallback）

**提取的方法：**

| 函数 | 当前行号 | 说明 |
|---|---|---|
| `generate_video()` | L3491-3576 | 视频生成调度（构建参考图 payload + fallback） |
| `video_seedance()` | L3577-3733 | Seedance API submit + poll + download |

**函数签名变化：**

原来：
```python
async def _generate_video(self, shot, image_path, ...) -> tuple[Path | None, str]:
    session = await self._get_session()
    # 用 self._video_fallback_chain(), self._video_provider_config(), ...
```

改为：
```python
async def generate_video(
    session: aiohttp.ClientSession,
    shot: dict,
    image_path: Path,
    video_prompt: str,
    video_references: list[dict],
    output_dir: Path,
    fallback_chain: list[str],
    provider_config_fn: Callable[[str], dict],
    download_fn: Callable,
    ...,
) -> tuple[Path | None, str]:
```

或者更简单的方式：传入一个 `VideoGenConfig` dataclass 包含所有配置。

**依赖：** `aiohttp`, `base64`, `mimetypes`, `logging`, `pathlib`

**注意：** `_generate_video` 内部调用了 `self._download`，需要把 download 作为参数传入或者也提取到公共位置。

---

### 4. `scripts/reference_builder.py` (~400 行)

**职责：** 视频参考图构建、审阅 bundle 导出、prompt 组合

**提取的方法：**

| 函数 | 当前行号 | 说明 |
|---|---|---|
| `get_character_appearances()` | L2090-2100 | 角色外貌查询 |
| `get_prop_appearances()` | L2101-2115 | 道具外貌查询 |
| `collect_character_ref_bindings()` | L2157-2179 | 角色参考图绑定 |
| `collect_prop_ref_bindings()` | L2180-2204 | 道具参考图绑定 |
| `append_video_reference()` | L2205-2237 | 追加视频参考 |
| `resolve_explicit_video_reference()` | L2238-2331 | 解析显式参考声明 |
| `build_video_references()` | L2332-2438 | 构建完整参考列表 |
| `assess_image_review_risk()` | L2439-2515 | 图片审阅风险评估 |
| `collect_hard_constraint_summary()` | L2516-2557 | 硬约束摘要 |
| `needs_reference_validation()` | L2558-2565 | 是否需要参考验证 |
| `export_reference_review_bundle()` | L2566-2625 | 导出参考审阅 bundle |
| `serialize_video_references()` | L2626-2635 | 序列化参考列表 |
| `compose_prompt_with_reference_mentions()` | L2636-2646 | 组合 prompt + @图片N |
| `export_video_prompt_artifact()` | L2647-2655 | 导出 prompt 文件 |
| `export_image_review_bundle()` | L2656-2735 | 导出图片审阅 bundle |
| `export_risk_bundle()` | L2026-2089 | 导出风险 bundle |

**这些方法读取 `self.storyboard`, `self.output_root`, `self.image_dir` 等。** 改为参数传入：

```python
def build_video_references(
    shot: dict,
    storyboard: dict,
    image_dir: Path,
    scene_image: Path | None,
    style_reference_frame: Path | None,
    provided_first_frame: Path | None,
    is_last_in_scene: bool,
    max_reference_images: int,
    ...,
) -> list[dict]:
```

**依赖：** `json`, `logging`, `pathlib`, `shutil`, `base64`, `mimetypes`

---

### 5. `scripts/ad_assets.py` (保留, ~1800 行)

**保留的内容：**

| 部分 | 行号 | 说明 |
|---|---|---|
| AssetGenerator.__init__ | L122-207 | 构造 + checkpoint + session |
| 配置方法 | L208-276 | fallback_chain, provider_config, review_config |
| Director prompt 相关 | L278-518 | load/resolve director prompts, shot contract |
| `run()` | L520-916 | 主编排循环 |
| `_extract_all_shot_prompts()` | L2736-2962 | 全局 prompt 提取 |
| `_generate_shot()` | L2963-3490 | 单 shot 生成（调度图片/视频/审阅） |
| `_generate_scene_image()` | L2116-2156 | 场景图生成 |
| TTS/BGM | L4077-4228 | TTS + BGM + download + placeholder |
| CharacterRefGenerator | L4230-4472 | 角色参考图（独立类） |
| CLI 入口 | L4475-4556 | argparse + main |

`_generate_shot()` 是最大的保留方法（~530 行），它是调度中心，调用 image_gen、video_gen、reference_builder、video_quality 的函数。保留在主文件中是合理的。

---

## 导入关系图

```
ad_assets.py (主编排)
  ├── from video_quality import scan_video_quality, extract_video_last_frame, ...
  ├── from image_gen import generate_image, placeholder_image
  ├── from video_gen import generate_video
  ├── from reference_builder import build_video_references, export_risk_bundle, ...
  ├── from content_filter import ContentFilter, VideoPromptBuilder
  └── from utils import VIDEO_REFERENCE_USAGE_ALIASES, get_api_credentials, ...

video_quality.py (独立，不依赖其他 scripts/)
  └── stdlib + numpy + PIL + cv2

image_gen.py
  └── stdlib + aiohttp + PIL + base64

video_gen.py
  └── stdlib + aiohttp + base64

reference_builder.py
  └── stdlib + json + base64
```

## 执行步骤（建议顺序）

### Step 1: 提取 `video_quality.py`（最干净，零耦合）

1. 创建 `scripts/video_quality.py`
2. 移入 `QUALITY_PROFILES`, `DETECTOR_VERSION` 常量
3. 移入 12 个静态方法，去掉 `@staticmethod` 装饰器和 `AssetGenerator.` 前缀，改为模块级函数
4. 在 `ad_assets.py` 顶部添加 `from video_quality import ...`
5. 全文替换调用：
   - `AssetGenerator._scan_video_quality(` → `scan_video_quality(`
   - `AssetGenerator._extract_video_last_frame(` → `extract_video_last_frame(`
   - `AssetGenerator._semantic_audit_video(` → `semantic_audit_video(`
   - `AssetGenerator._save_audit_frames(` → `save_audit_frames(`
   - `AssetGenerator._trim_video_at(` → `trim_video_at(`
   - `AssetGenerator._remove_video_segments(` → `remove_video_segments(`
   - `AssetGenerator._extract_segment_frames(` → `extract_segment_frames(`
   - `AssetGenerator._merge_segments(` → `merge_segments(`
   - `AssetGenerator._face_crop_duplicate_score(` → `face_crop_duplicate_score(`
   - `AssetGenerator._face_crop_identity_score(` → `face_crop_identity_score(`
   - `AssetGenerator._box_iou(` → `box_iou(`
   - `AssetGenerator._dedupe_boxes(` → `dedupe_boxes(`
   - `self._scan_video_quality(` → `scan_video_quality(`（在 run() 中）
   - `self._extract_video_last_frame(` → `extract_video_last_frame(`
   - `self._trim_video_at(` → `trim_video_at(`
   - `self._remove_video_segments(` → `remove_video_segments(`
6. 验证：`python3 -m py_compile scripts/video_quality.py && python3 -m py_compile scripts/ad_assets.py`

### Step 2: 提取 `image_gen.py`

1. 创建 `scripts/image_gen.py`
2. 移入 `GEMINI_IMAGE_SYSTEM_PROMPT` 常量（如果存在）
3. 移入 5 个方法，改签名（self → 显式参数）
4. `_generate_image` 的 fallback 链逻辑保持不变，只是不再从 self 读配置
5. 在 `ad_assets.py` 中 `_generate_shot` 内的 `await self._generate_image(...)` 改为 `await generate_image(session=..., ...)`
6. `_placeholder_image` 也移出（它用 self.image_width/height，改为参数）

### Step 3: 提取 `video_gen.py`

1. 创建 `scripts/video_gen.py`
2. 移入 `_generate_video` 和 `_video_seedance`
3. `_generate_video` 内部调用 `self._download` → 改为参数传入 download 函数，或者把 `_download` 提取到 utils.py 作为公共函数
4. `_video_seedance` 内部的 provider config 查询改为参数传入

### Step 4: 提取 `reference_builder.py`

1. 创建 `scripts/reference_builder.py`
2. 移入 16 个方法
3. 这些方法大部分读取 `self.storyboard` 和 `self.output_root`，改为显式参数
4. `_assess_image_review_risk` 和 `_export_*_bundle` 方法涉及文件 I/O，保持纯函数风格

### Step 5: 清理

1. 从 `ad_assets.py` 删除已移出的方法
2. 确保所有 import 正确
3. `python3 -m py_compile` 所有 5 个文件
4. 用 `--no_api` 模式空跑验证

## 注意事项

1. **`_download` 方法**被 image_gen 和 video_gen 都用到。建议移到 `utils.py` 作为 `async def download_file(session, url, output)` 公共函数。

2. **`_path_to_data_url` 方法** (L238) 被 image_gen 和 video_gen 都用到。建议也移到 utils.py。

3. **`VIDEO_REFERENCE_USAGE_PRIORITY` 常量** (L38-49) 被 reference_builder 用到，建议移到 utils.py（和 `VIDEO_REFERENCE_USAGE_ALIASES` 放一起）。

4. **`REVIEW_MODES` 和 `SHOT_TYPES` 常量** (L36-37) 保留在 ad_assets.py（只被 AssetGenerator 用到）。

5. **CharacterRefGenerator** 保留在 ad_assets.py 或独立为 `character_ref_gen.py`（可选，优先级低）。

6. **不要改变 CLI 接口**——`ad_assets.py` 仍然是唯一入口，`build_parser()` 和 `main()` 保持不变。`run_pipeline.py` 调用 `ad_assets.py` 的方式也不变。
