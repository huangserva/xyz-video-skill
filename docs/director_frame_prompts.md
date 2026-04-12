# 导演帧级 Prompt 架构设计

> **实现状态：主链已接通，兼容链仍保留。**
>
> 当前真实状态：
>
> - `director_prompts.json` 运行时加载优先级：已实现
> - 图片生成优先读取导演帧级描述：已实现
> - 审图 / 参考图前置审查 bundle：已实现
> - `_extract_all_shot_prompts`：仍保留为 fallback，不是主链

---

## 核心理念

**思考在母模型脑子里完成，不甩给生图模型。**

母模型负责：

- 设计 shot 的戏核
- 写出起始画面、目标状态和动作推进
- 在复杂 shot 中明确中间阶段
- 审查生成结果是否真的完成了这些职责

生图模型负责执行，不负责替导演补思路。

---

## 当前正确流程

```
storyboard.json
  + director_prompts.json（优先）
  -> 图片生成
  -> 参考图前置审查 / 图片审片
  -> 视频生成
```

兼容 fallback：

```
storyboard.json
  -> _extract_all_shot_prompts（仅当缺少 director_prompts 时）
  -> 图片生成
```

因此，`_extract_all_shot_prompts` 现在的定位不是“主链核心”，而是：

- 旧项目兼容
- 导演帧级描述缺失时的兜底

---

## 导演帧级描述应该怎么理解

导演写的不是“模型时间点协议”，而是 shot 内部的画面职责分配。

推荐理解为：

- `first_frame`
  - 起始画面
- `last_frame`
  - 目标状态
- `keyframes`（如果有）
  - 中间阶段的兼容表达

其中：

- `first_frame` 最终会进入 `video_references` 的 `first_frame`
- `last_frame` 最终会进入 `reference_target_state`
- `keyframes` 最终会映射为 `reference_stage`

所以导演要先想的是：

- 这一帧在这个 shot 里负责什么

不是：

- 这是不是某个必须命中的硬时间点

---

## director_prompts.json 结构

```json
{
  "shots": {
    "1": {
      "first_frame": {
        "prompt": "这一镜起始画面的静态描述。"
      },
      "keyframes": [
        {
          "timestamp": 3.0,
          "goal": "中间阶段职责（可选）",
          "prompt": "中间阶段参考图的静态描述。"
        }
      ],
      "last_frame": {
        "prompt": "这一镜目标状态的静态描述。"
      },
      "video_action": "从起始画面到目标状态之间的动作推进；如存在中间阶段，描述阶段之间的推进关系。"
    }
  }
}
```

---

## storyboard 与 director_prompts 的分工

| 文件 | 写什么 | 服务于 |
|------|--------|--------|
| `storyboard.json` | 故事、约束、连续性、参考素材协议 | 生成计划 + 审查标准 |
| `director_prompts.json` | 每一帧的精确画面描述 | 图片生成直接输入 |

两者关系：

- `storyboard` 回答“这个 shot 要做什么、有什么限制”
- `director_prompts` 回答“这一帧画面到底长什么样”

---

## 对复杂 shot 的要求

复杂 shot 不应只写一条模糊 `video_action`。

应先明确：

1. 起始画面是什么
2. 目标状态是什么
3. 是否存在不可省略的中间阶段
4. 每个中间阶段让模型学什么

只有当第 3 条答案明确时，才需要 `keyframes`。

也就是说：

- `keyframes` 不是默认入口
- 它只是复杂 shot 的阶段兼容表达

---

## 审查时看什么

导演审查图片或参考 bundle 时，应重点检查：

1. 起始画面是否完成了 `first_frame` 的职责
2. 中间阶段是否真的只承担 `reference_stage` 的职责
3. 目标状态是否完成了 `reference_target_state` 的职责
4. 角色、道具、构图、连续性约束是否一致

如果流程停在参考图前置审查，优先查看：

- `image_audit/shot_{id}/reference_bundle/reference_review_request.json`
- `image_audit/shot_{id}/reference_bundle/video_prompt.txt`

其中 `video_prompt.txt` 会直接展示最终的：

- `@图片N 作为首帧`
- `@图片N 参考角色`
- `@图片N 参考构图`
- `@图片N 参考中间阶段`
- `@图片N 参考目标状态`

---

## 结论

这份架构文档现在的正确结论不是：

- “首帧 / 关键帧 / 尾帧三点插值更重要”

而是：

- 导演先定义画面职责
- 执行层再把这些职责落成 `video_references` 和 `@图片N` 调用

一句话：

**导演写的是画面职责，执行层用用途协议去落实。**
