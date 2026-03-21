# ad-generator 资产库与时间线编辑器实施方案

**版本**: v1.0
**创建日期**: 2026-03-20
**状态**: 规划中

---

## 目录

1. [总体目标](#总体目标)
2. [架构设计](#架构设计)
3. [资产库方案](#资产库方案)
4. [时间线编辑器方案](#时间线编辑器方案)
5. [技术选型](#技术选型)
6. [实施步骤](#实施步骤)
7. [测试计划](#测试计划)

---

## 总体目标

### 核心目标

1. **资产库系统**
   - 建立可复用的角色/场景/道具/服装资产库
   - 支持跨项目复用，避免重复定义
   - 提供搜索、标签、版本管理功能

2. **时间线编辑器**
   - 可视化的视频后期编辑界面
   - 支持多轨道（视频/音频）编辑
   - 拖拽操作、裁剪、转场效果

### 成功标准

- ✅ 资产创建一次，可在 10+ 个项目中复用
- ✅ 搜索资产响应时间 < 1 秒
- ✅ 时间线支持 50+ 个片段流畅操作
- ✅ 导出视频支持 4K 分辨率


---

## 架构设计

### 整体架构

```
用户交互层
├── Web UI (React)      ├── CLI (Python)
│   - 资产库管理器      │   - ad_assets.py
│   - 时间线编辑器      │   - ad_compose.py
└───────────────────────┴──────────────────────┘
              │ REST API / JSON
              ↓
        服务层
├── AssetLibraryService  ├── TimelineService
│   - CRUD 操作         │   - 片段管理
│   - 搜索与标签       │   - 轨道管理
│   - 版本控制         │   - 转场效果
└───────────────────────┴──────────────────────┘
              │
              ↓
        数据层
├── SQLite / JSON 文件  ├── FFmpeg
│   - 资产库           │   - 视频处理
│   - 时间线项目       │   - 导出成片
```

### 目录结构

```
skills/ad-generator/
├── config/                    # 配置文件
├── scripts/                   # 核心脚本
│   ├── ad_assets.py
│   ├── ad_compose.py
│   └── new/                  # 新增模块
│       ├── asset_library.py    # 资产库管理
│       ├── timeline_editor.py  # 时间线编辑器
│       └── web_server.py      # Web 服务
├── data/                      # 数据目录（新增）
│   ├── asset_library.json     # 资产库
│   ├── projects/             # 项目数据
│   └── cache/               # 生成缓存
├── web/                      # Web UI（新增）
│   ├── src/
│   │   ├── components/
│   │   ├── services/
│   │   └── App.tsx
│   └── package.json
└── IMPLEMENTATION_PLAN.md    # 本文件
```


---

## 资产库方案

### 1. 核心概念

#### 资产类型

- ACTOR（演员/角色）
- SCENE（场景/环境）
- PROP（道具/物品）
- COSTUME（服装/配饰）

### 2. 数据结构

#### asset_library.json

```json
{
  "version": "1.0",
  "created_at": "2026-03-20T10:00:00Z",
  "updated_at": "2026-03-20T10:00:00Z",
  "assets": {
    "actors": {
      "actor_001": {
        "id": "actor_001",
        "name": "白素贞",
        "type": "actor",
        "description": "千年白蛇精，温婉善良，白衣胜雪",
        "tags": ["古代", "女性", "神仙", "白蛇传"],
        "thumbnail": "data/assets/actors/actor_001_thumb.png",
        "images": {
          "front": "data/assets/actors/actor_001_front.png",
          "side": "data/assets/actors/actor_001_side.png"
        },
        "version": 1,
        "usage_count": 15
      }
    },
    "scenes": {
      "scene_001": {
        "id": "scene_001",
        "name": "西湖断桥",
        "type": "scene",
        "description": "烟雨西湖，断桥残雪",
        "tags": ["自然", "古代", "西湖"],
        "thumbnail": "data/assets/scenes/scene_001_thumb.png",
        "images": {
          "wide": "data/assets/scenes/scene_001_wide.png"
        },
        "version": 1,
        "usage_count": 8
      }
    }
  }
}
```

### 3. 核心功能

#### 3.1 CRUD 操作

- create_asset(): 创建资产
- get_asset(): 获取资产
- update_asset(): 更新资产（创建新版本）
- delete_asset(): 删除资产（软删除）
- list_assets(): 列出资产（支持分页）

#### 3.2 搜索功能

- search(): 全文搜索
- suggest_assets(): 基于上下文推荐

#### 3.3 项目关联

- link_to_project(): 关联到项目
- get_project_assets(): 获取项目资产

### 4. 与现有工作流集成

#### storyboard.json 支持资产引用

```json
{
  "characters": {
    "bainiangzi": {
      "$ref": "asset_library.json#/assets/actors/actor_001"
    }
  }
}
```


---

## 时间线编辑器方案

### 1. 核心概念

#### 时间线结构

```
Timeline
├── Tracks (轨道)
│   ├── VideoTrack (视频轨道)
│   │   └── Clips (片段)
│   │       ├── Clip 1: shot_1.mp4 (0-8s)
│   │       ├── Clip 2: shot_2.mp4 (8-16s)
│   │       └── Clip 3: shot_3.mp4 (16-24s)
│   ├── AudioTrack (音频轨道)
│   │   └── Clips
│   │       ├── BGM: bgm.wav (0-24s)
│   │       └── SFX: rain.mp3 (8-12s)
│   └── EffectsTrack (效果轨道)
│       └── Transitions
│           ├── cross-dissolve (8s, 0.5s)
│           └── flash-white (16s, 0.3s)
└── Metadata (元数据)
    ├── duration: 24s
    ├── resolution: 1920x1080
    └── fps: 30
```

### 2. 数据结构

#### timeline.json

```json
{
  "version": "1.0",
  "project_id": "dunhuang_feitian",
  "created_at": "2026-03-20T10:00:00Z",
  "updated_at": "2026-03-20T10:00:00Z",
  "metadata": {
    "duration": 49.1,
    "resolution": "1920x1080",
    "fps": 30,
    "export_format": "mp4",
    "export_codec": "h264"
  },
  "tracks": [
    {
      "id": "track_video_1",
      "type": "video",
      "name": "视频轨道",
      "clips": [
        {
          "id": "clip_001",
          "source": "data/projects/dunhuang_feitian/assets/videos/shot_001.mp4",
          "start_time": 0,
          "end_time": 8,
          "in_point": 0,
          "out_point": 8,
          "volume": 1.0,
          "opacity": 1.0
        },
        {
          "id": "clip_002",
          "source": "data/projects/dunhuang_feitian/assets/videos/shot_002.mp4",
          "start_time": 8,
          "end_time": 16,
          "in_point": 0,
          "out_point": 8,
          "transition_in": {
            "type": "cross-dissolve",
            "duration": 0.5
          }
        }
      ]
    },
    {
      "id": "track_audio_1",
      "type": "audio",
      "name": "音频轨道",
      "clips": [
        {
          "id": "clip_bgm",
          "source": "data/projects/dunhuang_feitian/assets/bgm/bgm.wav",
          "start_time": 0,
          "end_time": 49.1,
          "volume": 0.8
        }
      ]
    }
  ]
}
```

### 3. 核心功能

#### 3.1 片段管理

- add_clip(): 添加片段
- remove_clip(): 删除片段
- move_clip(): 移动片段
- trim_clip(): 裁剪片段
- split_clip(): 分割片段

#### 3.2 轨道管理

- add_track(): 添加轨道
- remove_track(): 删除轨道
- reorder_tracks(): 重排轨道

#### 3.3 转场效果

- add_transition(): 添加转场
- remove_transition(): 删除转场
- supported_transitions:
  - straight-cut (硬切)
  - cross-dissolve (溶解)
  - flash-white (闪白)
  - fade-to-black (淡黑)

### 4. 导出功能

#### FFmpeg 命令生成

```python
def export_timeline(timeline_path: str, output_path: str):
    timeline = load_timeline(timeline_path)
    
    # 生成 FFmpeg 滤镜图
    filter_complex = build_filter_complex(timeline)
    
    # 执行导出
    cmd = [
        "ffmpeg",
        "-y",
        *build_inputs(timeline),
        "-filter_complex", filter_complex,
        "-map", "[final_video]",
        "-map", "[final_audio]",
        "-c:v", timeline.metadata["export_codec"],
        "-preset", "medium",
        "-crf", "23",
        output_path
    ]
    
    subprocess.run(cmd)
```


---

## 技术选型

### 后端

| 组件 | 技术选型 | 理由 |
|------|----------|------|
| Web 框架 | FastAPI | 高性能、异步支持、自动生成 OpenAPI 文档 |
| 数据存储 | SQLite (可选 JSON) | 轻量级、无需额外服务、适合中小型项目 |
| 视频处理 | FFmpeg | 行业标准、功能强大、跨平台 |
| 前端框架 | React + TypeScript | 成熟生态、类型安全、组件化 |
| UI 库 | Ant Design | 组件丰富、中文友好、企业级 |
| 状态管理 | Zustand | 轻量级、简单易用 |
| 构建工具 | Vite | 快速开发、热更新 |
| 拖拽库 | react-beautiful-dnd | 成熟稳定、API 简洁 |

### 前端依赖

```json
{
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "antd": "^5.10.0",
    "zustand": "^5.0.0",
    "react-beautiful-dnd": "^13.1.1",
    "axios": "^1.13.6",
    "dayjs": "^1.11.0"
  },
  "devDependencies": {
    "typescript": "^5.2.0",
    "vite": "^5.0.0",
    "@vitejs/plugin-react": "^4.2.0"
  }
}
```

---

## 实施步骤

### 阶段 1：资产库 MVP（2-3 周）

#### Week 1: 数据层 + 核心功能

- [ ] 设计 asset_library.json 结构
- [ ] 实现 AssetLibrary 类（CRUD）
- [ ] 实现搜索功能
- [ ] 实现项目关联
- [ ] 单元测试

#### Week 2: CLI 工具

- [ ] asset_lib.py 命令行工具
- [ ] 集成到 ad_assets.py
- [ ] storyboard.json 支持资产引用
- [ ] 文档编写

#### Week 3: Web UI 基础

- [ ] 初始化 React + Vite 项目
- [ ] 资产列表页面
- [ ] 资产创建/编辑页面
- [ ] 图片预览组件

### 阶段 2：时间线编辑器 MVP（3-4 周）

#### Week 4: 数据层 + 核心功能

- [ ] 设计 timeline.json 结构
- [ ] 实现 Timeline 类（片段/轨道管理）
- [ ] 实现 FFmpeg 滤镜生成
- [ ] 单元测试

#### Week 5-6: Web UI 基础

- [ ] 时间线组件结构
- [ ] 轨道渲染
- [ ] 片段拖拽
- [ ] 片段裁剪界面

#### Week 7: 转场 + 导出

- [ ] 转场效果组件
- [ ] 导出功能
- [ ] 进度显示
- [ ] 错误处理

### 阶段 3：集成与优化（2 周）

#### Week 8: 集成

- [ ] ad-assets.py 生成资产到库
- [ ] 时间线导入 storyboard 生成的视频
- [ ] 一键导出工作流
- [ ] 端到端测试

#### Week 9: 优化

- [ ] 性能优化（大量片段）
- [ ] 用户体验优化
- [ ] 文档完善
- [ ] 示例项目

---

## 测试计划

### 单元测试

```python
# test_asset_library.py

def test_create_asset():
    lib = AssetLibrary()
    asset = Asset(
        id="test_actor",
        type=AssetType.ACTOR,
        name="测试演员",
        tags=["测试"]
    )
    asset_id = lib.create_asset(asset)
    assert asset_id == "test_actor"

def test_search_assets():
    lib = AssetLibrary()
    results = lib.search("白")
    assert len(results) > 0
    assert any("白素贞" in a.name for a in results)

def test_link_to_project():
    lib = AssetLibrary()
    lib.link_to_project("test_project", AssetType.ACTOR, "actor_001")
    project_assets = lib.get_project_assets("test_project")
    assert len(project_assets[AssetType.ACTOR]) == 1
```

### 集成测试

```python
# test_integration.py

def test_full_workflow():
    # 1. 创建资产
    lib = AssetLibrary()
    asset_id = lib.create_asset(test_actor)
    
    # 2. 关联到项目
    lib.link_to_project("test_project", AssetType.ACTOR, asset_id)
    
    # 3. 生成 storyboard
    storyboard = generate_storyboard_with_refs()
    
    # 4. 生成素材
    assets = generate_assets(storyboard)
    
    # 5. 导入时间线
    timeline = import_assets_to_timeline(assets)
    
    # 6. 导出视频
    export_timeline(timeline, "output.mp4")
    
    assert Path("output.mp4").exists()
```

---

## 总结

本实施方案分为三个阶段：

1. **资产库 MVP**：建立可复用的资产管理系统
2. **时间线编辑器 MVP**：提供可视化视频编辑能力
3. **集成与优化**：与现有工作流无缝整合

预计总开发时间：7-9 周
核心交付物：
- 资产库 CLI 工具 + Web UI
- 时间线编辑器 Web UI
- 完整文档和示例

