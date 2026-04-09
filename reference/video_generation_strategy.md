# Video Generation Strategy

本文件定义本 skill 在视频生成阶段的统一参考图协议。

从现在开始，视频生成的默认主脑不再是“首帧 + 尾帧 + keyframes”的时间点协议，而是 `video_references` 的**用途驱动协议**：

- 先判断这个 shot 需要模型学习什么
- 再决定需要哪些参考素材
- 再在视频 prompt 中用 `@图片N` 显式声明用途
- 最后才把这些决策落成执行字段

一句话：

- 旧思路：按时间点组织图片
- 新思路：按用途组织图片

## 核心原则

1. `video_references` 是视频参考协议的主入口。
2. 每张参考图都必须先定义用途，再进入模型。
3. `keyframes` 只作为兼容性的“中间阶段表达”，不再是主结构。
4. `reference_strategy` 只作为分析/审计字段，不再驱动执行主链。
5. 参考图越多不代表越稳；用途不清晰时，少图优于乱图。
6. Seedance 2.0 的正确用法是“多参考语义控制”，不是“多帧硬插值控制”。

## 当前实现对应关系

当前执行链路已经按以下方式工作：

- 执行层会优先读取 `shot.video_references`
- 如果 storyboard 里没有显式写 `video_references`，会在规范化或执行时自动回填
- 视频 prompt 会把参考素材写成显式调用语句，例如：
  - `@图片1 作为首帧。`
  - `@图片2 参考角色。`
  - `@图片3 参考构图。`
  - `@图片4 参考目标状态。`
- provider 参考图超限时，会按用途优先级裁剪，而不是按旧的“首/中/尾”逻辑裁剪

因此，本文件定义的不是“未来设想”，而是当前 skill 应当遵守的工作标准。

## 用途驱动协议

### 1. 推荐用途枚举

当前实现支持并推荐以下用途：

- `first_frame`
  - 视频起点参考
- `reference_character`
  - 角色身份/外观一致性参考
- `reference_prop`
  - 关键道具一致性参考
- `reference_composition`
  - 场景构图/空间布局参考
- `reference_style`
  - 整体视觉风格参考
- `reference_color`
  - 色调/配色倾向参考
- `reference_stage`
  - 中间阶段参考
- `reference_target_state`
  - 目标落点/最终状态参考
- `reference_motion`
  - 镜头语言或运动方式参考（仅在确有素材时使用）

### 2. 用途解释

#### `first_frame`

作用：
- 定义视频从哪一帧开始

说明：
- 这是唯一天然带有“时间起点”意义的参考图
- 不能再把所有参考图都当作“首帧的变体”

#### `reference_character`

作用：
- 锁定角色是谁、长什么样、穿什么、材质和识别特征是什么

说明：
- 这类参考图负责身份一致性，不负责当前 shot 的时间推进

#### `reference_prop`

作用：
- 锁定关键道具的外形和持续存在关系

说明：
- 这类参考图负责“这个东西长什么样”，不负责“这一帧怎么拿”

#### `reference_composition`

作用：
- 锁定场景空间布局、镜位关系、景别基础和环境构图

说明：
- 场景图更适合承担这一用途
- 不要让场景图承担角色身份和时间节点任务

#### `reference_style` / `reference_color`

作用：
- 锁定风格媒介和色彩倾向

说明：
- 当 scene 图已经足够表达风格时，不必重复增加风格图
- 风格参考与构图参考可以来自同一素材，但用途语义仍应清楚

#### `reference_stage`

作用：
- 提供中间阶段的语义锚点

说明：
- 这是旧 `keyframes` 的正确归宿
- 它的含义是“中间阶段参考”，不是“模型必须命中的硬时间点”

#### `reference_target_state`

作用：
- 提供结尾希望收束到的结果状态

说明：
- 它不是“保证最后一帧百分之百精确命中”
- 它表达的是目标落点参考，而不是刚性终帧锁定器

## 决策顺序

每个 shot 在设计视频参考协议时，应按下面顺序思考。

### A. 先判断这镜要控制什么

先回答：

- 角色身份要不要稳
- 道具身份要不要稳
- 空间构图要不要稳
- 风格媒介要不要稳
- 中间过程要不要明确经过某个阶段
- 结果状态要不要明确收束

### B. 再决定需要哪些用途

推荐决策顺序：

1. 是否需要 `first_frame`
2. 是否需要 `reference_character`
3. 是否需要 `reference_prop`
4. 是否需要 `reference_composition`
5. 是否需要 `reference_style` / `reference_color`
6. 是否需要 `reference_stage`
7. 是否需要 `reference_target_state`

### C. 最后才落字段

优先落成：

- `video_references`
- `continuity_mode`
- `chain_from_previous`
- `shot_type`
- `time_beats`
- `subject_constraints`
- `shot_delta`

仅在需要兼容或审计时补充：

- `keyframes`
- `reference_strategy`

## 四个语义维度

每个 shot 至少从以下四个维度判断。

### 1. 动作复杂度

问题：
- 这是静态展示、轻动作，还是多阶段强动作
- 是否存在必须经过的动作阶段

建议：
- 低：单一姿态、轻微观察、环境镜头
- 中：回头、递物、转身、短距离移动
- 高：打斗、碰撞、连续压制、追逐、明显三段式动作链

决策倾向：
- 复杂度越高，越可能需要 `reference_stage`
- 但仍然必须先说明“中间阶段的用途是什么”

### 2. 状态变化幅度

问题：
- 情绪、姿态、视线、角色关系、空间关系是否发生显著变化

建议：
- 低：状态基本稳定
- 中：存在清晰变化，但阶段不多
- 高：需要明确经过多个状态节点

决策倾向：
- 只有变化节点具有独立视觉语义时，才值得增加 `reference_stage`

### 3. 起止约束强度

问题：
- 起点是否必须明确
- 终点是否必须明确
- 终点是否会被下一镜头复用

决策倾向：
- 起点强：必须有 `first_frame`
- 终点强：应增加 `reference_target_state`

### 4. 前后镜头衔接依赖

问题：
- 是否依赖上一镜的实际结束画面
- 是否要把当前镜的结束状态交给下一镜

决策倾向：
- 衔接依赖越强，越要清楚写 `chain_from_previous`、`continuity_mode` 和 `reference_target_state`

## 推荐组装模式

以下不是硬编码策略名，而是实际可执行的参考素材组合思路。

### 1. 轻量镜头

适用：
- 动作简单
- 状态变化小
- 终点无强约束

推荐：
- `first_frame`
- 视情况增加 `reference_character` / `reference_composition`

不推荐：
- 为了“更稳”硬加 `reference_stage`

### 2. 明确收束镜头

适用：
- 动作不复杂
- 但结尾构图或姿态必须稳定

推荐：
- `first_frame`
- `reference_character` / `reference_prop` / `reference_composition`
- `reference_target_state`

### 3. 中过程重要镜头

适用：
- 中间状态比结尾更重要
- 需要模型经过一个或少数几个关键阶段

推荐：
- `first_frame`
- 必要的 `reference_character` / `reference_prop` / `reference_composition`
- 少量 `reference_stage`

说明：
- 不要用“多张关键帧”替代清晰的阶段定义

### 4. 强动作或强转折镜头

适用：
- 高动作复杂度
- 高状态变化幅度
- 起点、过程、落点都重要

推荐：
- `first_frame`
- 必要的 `reference_character` / `reference_prop` / `reference_composition`
- 1~N 张 `reference_stage`
- `reference_target_state`

说明：
- 如果阶段过多，不是继续堆参考图，而是应考虑拆镜

## `keyframes` 的新定位

`keyframes` 不再是视频协议的中心字段。

现在的正确理解是：

- `keyframes` = 旧 storyboard 里的中间阶段表达
- 执行层会把它映射成 `video_references[].usage = "reference_stage"`
- 是否存在 `keyframes`，本质上取决于是否存在不可省略的中间阶段

判断标准：
- 如果一张中间图不能回答“它在这个 shot 里负责什么”，就不该写成 `keyframes`
- 如果它只是“和首尾差不多”，也不该写
- 如果它表达的是明确的中间状态、动作阶段、识别阶段或关系变化，才适合写

## Prompt 调用规则

Seedance 2.0 的关键不只是“上传参考图”，更是“上传后显式调用参考图”。

因此视频 prompt 必须体现：

- `@图片1 作为首帧`
- `@图片2 参考角色`
- `@图片3 参考构图`
- `@图片4 参考道具`
- `@图片5 参考目标状态`

规则：
- 每张参考素材都应在 prompt 中有明确用途声明
- 不要把多张图都当成同一种 `reference_image` 却不说明用途
- 不要让模型自己猜“这张图到底是人设、构图还是终点”

## 超限裁剪原则

当 provider 的参考图数量有限时，应按用途优先级裁剪，而不是按旧的时间点逻辑裁剪。

实践原则：
- 优先保留真正承担结构职责的参考图
- 通常优先级应让起点、角色、终点这类强约束参考先留下
- 风格/色调参考在 scene 图已足够表达时可后裁
- 没有独立语义价值的中间阶段参考应最先被删减

## 连续性相关规则

### 1. `chain_from_previous=true`

这是强输入依赖信号。

规则：
- 当前镜头首帧应优先继承上一镜实际结果
- 当前 shot 的参考协议要围绕“承接”组织，而不是重新发明新的起点
- 如果下一镜依赖当前镜结束状态，则当前镜应尽量提供 `reference_target_state`

### 2. `scene_continuity`

当漂移问题不是单角色，而是整个 scene 的空间、道具、环境和持续状态时，应在 scene 层声明 `scene_continuity`。

它负责：
- 稳定空间关系
- 稳定道具状态
- 稳定环境状态
- 稳定跨镜角色事实

### 3. `shot_delta`

`shot_delta` 只写本镜头允许变化的内容。

规则：
- 没写进 `shot_delta` 的变化，默认不应发生
- `scene_continuity` 提供稳定基线
- `shot_delta` 只负责推进本镜的变化量

### 4. `pose_contract`

当同一角色在多个参考图之间不能随意变换支撑关系时，必须写 `subject_constraints.pose_contract`。

典型场景：
- 不能一会儿坐地，一会儿站立
- 不能一会儿靠墙，一会儿悬空
- 不能一会儿单手持伞，一会儿持伞位置完全漂移

### 5. `gaze_contract`

当叙事推进依赖“看向谁、有没有认出、是否锁定目标”时，必须写 `subject_constraints.gaze_contract`。

规则：
- 写正向视线目标
- 写目标区域
- 不要只写“回头看过去”这种模糊描述

## `time_beats` 与 `reference_stage` 的关系

一句话：

- `time_beats` 是叙事节拍脚本
- `reference_stage` 是其中被提升为视觉锚点的阶段

规则：
- 可以先写 `time_beats`
- 只有在某个阶段不锁就容易出错时，才把它提升为 `reference_stage`
- 不是每个时间段都值得配一张参考图

## 输出字段建议

推荐把语义判断优先落成下面这些字段：

### 必填/主链字段

- `video_references`
- `continuity_mode`
- `shot_type`
- `chain_from_previous`
- `scene_prompt`
- `action_prompt`
- `end_frame_description`

### 强连续性建议字段

- `scene_continuity`
- `shot_delta`
- `subject_constraints.pose_contract`
- `subject_constraints.gaze_contract`
- `time_beats`

### 兼容/分析字段

- `keyframes`
- `reference_strategy`

其中：
- `video_references` 是执行主协议
- `keyframes` 是兼容性中间阶段表达
- `reference_strategy` 是策略分析结果，不是执行主入口

## 推荐数据结构

推荐每个 shot 显式写出：

```json
{
  "video_references": [
    {"source_type": "frame", "source_id": "first_frame", "usage": "first_frame"},
    {"source_type": "scene", "usage": "reference_composition"},
    {"source_type": "character", "source_id": "medical_girl", "usage": "reference_character", "subject": "medical_girl"},
    {"source_type": "prop", "source_id": "oil_paper_umbrella", "usage": "reference_prop", "subject": "oil_paper_umbrella"},
    {"source_type": "stage", "source_id": "1", "usage": "reference_stage", "stage": "confirm_source"},
    {"source_type": "frame", "source_id": "target_state", "usage": "reference_target_state"}
  ]
}
```

说明：
- `subject` 用于标明角色或道具归属
- `stage` 用于标明中间阶段语义
- `source_type/source_id` 是当前 storyboard 推荐的显式写法
- 如果你手里只有外部文件路径，也可以直接写 `path`
- 如果某个 shot 没有明确中间阶段，就不要写 `reference_stage`

## 与旧协议的兼容关系

旧字段不会立刻消失，但定位已经变化：

- `scene_prompt`
  - 仍然负责起点叙事描述
- `end_frame_description`
  - 仍然负责目标落点描述
- `keyframes`
  - 兼容表达中间阶段，执行层会映射为 `reference_stage`
- `reference_strategy`
  - 仅作为分析/审计字段保留

因此，正确的工作方式是：

- 不要再先想“要几个 keyframes”
- 要先想“这镜需要模型从哪些参考里学什么”

## 与 Skill 的关系

本文件是 storyboard 设计、规范化、执行、审查的统一参考标准。

执行要求：

1. 设计 storyboard 时，先做语义判断，再决定 `video_references`
2. 写 prompt 时，必须把参考素材用途显式写进 `@图片N` 调用语句
3. 规范化阶段可以为 legacy storyboard 自动回填 `video_references`
4. 执行阶段应优先使用 `video_references`，而不是把旧时间点字段当成唯一主脑
5. 审查阶段应检查每张参考图的用途是否清楚、是否互相冲突、是否超出 provider 可承载范围

一句话总结：

- 不是“给模型几张图”最重要
- 而是“每张图让模型学什么”最重要
