# Cross-Shot Continuity Constraints

本文件定义跨镜头连续性约束系统，解决相邻镜头之间的视觉连续性问题。

## 问题定义

单个 shot 的 prompt 只约束镜头内部，无法保证相邻镜头之间的连续性：

**常见问题：**
1. **角色姿态跳变**：重伤角色在"瘫坐/半站/倚墙"之间来回跳
2. **空间关系不稳定**：两人的距离和相对位置在相邻镜头中不一致
3. **道具状态不一致**：举着的伞突然消失、换手、或角度剧变
4. **物理状态违反**：湿透的衣服突然变干、伤口突然消失

这些问题的根源是：**每个 shot 独立生成图片，没有"场景级记忆"**。

## 解决方案：场景级约束系统

### 核心思路

在 scene 层定义"稳定元素"（stable elements），自动注入到该 scene 下所有 shot 的 prompt 中。

### 约束层级

```
framework.json
  └─ scenes[]
      └─ scene_constraints (场景级约束)
          ├─ stable_character_states (稳定角色状态)
          ├─ stable_spatial_layout (稳定空间布局)
          ├─ stable_props (稳定道具状态)
          └─ continuity_rules (连续性规则)
```

## scene_constraints 字段结构

### 1. stable_character_states

定义在整个 scene 中**不应改变**的角色状态。

```json
{
  "stable_character_states": {
    "character_id": {
      "posture": "姿态描述",
      "physical_state": "物理状态描述",
      "clothing_state": "服装状态描述",
      "injury_state": "伤势状态描述（如果有）"
    }
  }
}
```

**示例：**

```json
{
  "stable_character_states": {
    "swordsman": {
      "posture": "瘫靠在湿滑崖壁上，无法直立，右手歪斜举着伞柄，左手垂落，身体重心完全靠墙支撑",
      "physical_state": "重伤脱力，面色苍白，呼吸急促",
      "clothing_state": "深色衣袍被雨水和泥痕浸透",
      "injury_state": "肩背有重伤痕迹，无法做大幅动作"
    }
  }
}
```
