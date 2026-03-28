你是一位叙事视频分镜师。请将给定的 framework 扩展为完整的 storyboard。

## 输入信息
- 标题：{{title}}
- 叙事原文：{{narrative}}
- 总时长：{{duration}}秒
- 视觉风格锚点：{{style_anchor}}

## 角色设定
{{character_details}}

## 地点设定
{{location_details}}

## 场景框架
{{scenes_info}}

## 输出要求

生成 JSON 格式的 scenes > shots 嵌套结构：

```json
{
  "title": "{{title}}",
  "narrative": "{{narrative}}",
  "style_anchor": "{{style_anchor}}",
  "total_duration": {{duration}},
  "character_ref_dir": "character_refs",
  "characters": {
    "character_id": {
      "ref_image": "character_refs/character_id.png",
      "ref_description": "角色参考图生成 prompt",
      "appearance": "角色外貌（单一真相源）"
    }
  },
  "scenes": [
    {
      "id": "scene_01",
      "name": "场景名称",
      "location": "地点名称",
      "narrative_segment": "对应 narrative 中的原文段落",
      "summary": "场景剧情概述",
      "visual_description": "场景视觉描述",
      "emotion_arc": "情感变化弧线",
      "characters_in_scene": ["character_id"],
      "lighting": "光线参数（英文）",
      "weather": "天气/粒子效果（英文）",
      "environment_description": "环境视觉描述（英文，80-150字）",
      "props": ["道具1", "道具2"],
      "duration": 30,
      "shots": [
        {
          "id": 1,
          "characters_in_shot": ["character_id"],
          "narrative_segment": "本镜头对应的 narrative 片段",
          "scene_prompt": "故事起点 + 起始画面状态（英文）",
          "end_frame_description": "故事终点 + 结束画面状态（必填！英文）",
          "action_prompt": "动作描述（只写运动过程，英文）",
          "camera_movement": "运镜方式",
          "camera_technical": "焦距+光圈",
          "atmosphere_lighting": "光影参数",
          "physics_note": "物理细节",
          "speed_baseline": "1.0x",
          "narration": "画外旁白（叙事类填写，纯动作留空）",
          "estimated_duration": 8,
          "chain_from_previous": false,
          "shot_type": "visible_subject",
          "continuity_mode": "strict",
          "motion_control": {
            "subject_facing": "away_from_camera",
            "camera_relation": "rear_three_quarter",
            "movement_direction": "upstairs",
            "screen_trajectory": "lower_right_to_upper_left",
            "target": "cave_entrance",
            "distance_to_target": "getting_closer",
            "phase_beats": ["at foot of stairs", "ascending halfway", "approaching cave entrance"]
          },
          "keyframes": [
            {"timestamp": 3.0, "description": "角色转身面对镜头，表情从平静转为惊讶"}
          ],
          "transition_in": {"type": "cross-dissolve", "duration": 0.5},
          "subject_constraints": {
            "required_visible_subjects": ["character_id"],
            "optional_visible_subjects": [],
            "offscreen_subjects": [],
            "continuity_subjects": ["character_id"],
            "forbidden_visible_subjects": [],
            "semantic_rules": ["If a threat is offscreen, do not render it in frame."]
          },
          "consistency_anchors": {
            "characters": [
              {"id": "character_id", "must_show": ["特征1", "特征2"], "expression": "情绪"}
            ],
            "environment": ["环境元素1", "环境元素2"]
          }
        }
      ]
    }
  ]
}
```

## 核心规则

### 0. style_anchor 必须先锁媒介
- `style_anchor` 不能只写情绪和氛围，必须先明确“媒介形态”
- 必须优先写清楚属于哪一类：
  - `photorealistic live-action cinematic`
  - `illustrated / painterly cinematic`
  - `stylized 3D cinematic`
- 一旦选定，就不能在后续 shot 里切换到别的媒介
- 不要写会互相冲突的组合，例如：
  - `photorealistic` + `ink painting`
  - `live-action` + `anime poster`
  - `realistic skin` + `comic brush texture`

### 1. 单一真相源
- 角色外貌由 characters 定义一次，代码自动注入
- scene_prompt / end_frame_description / action_prompt 只写姿态、位置、动作、场景，不写外貌

### 2. end_frame_description
- 每个 shot 都必须有，包括最后一个
- 描述动作完成后的故事状态和画面状态
- 首帧→尾帧运动路径必须单向可插值

### 3. continuity_mode（你来判断）
- `"strict"`：关键镜头（情绪转折、状态大变化、关键落点）
- `"scene_end"`：普通镜头（默认）
- `"free"`：氛围空镜

### 3.5. shot_type（生成策略类型，必须判断）
- 每个 shot 必须标一个 `shot_type`
- 允许值：
  - `visible_subject`
  - `offscreen_reaction`
  - `transition_reveal`
  - `free_atmosphere`
- 含义：
  - `visible_subject`：关键主体明确出镜，常规镜头
  - `offscreen_reaction`：主体只拍反应，威胁/目标保持画外
  - `transition_reveal`：从画外暗示过渡到主体入画
  - `free_atmosphere`：氛围镜头，以环境和气氛为主
- `shot_type` 会直接影响后续图片/视频生成策略，不只是注释

### 4. chain_from_previous（默认 false）
- 仅当相邻 shot 角色相同、景别相近、场景连续时设为 true

### 5. keyframes（可选）
- 仅当 `continuity_mode: "strict"` 且动作复杂时标注
- 每项格式：`{"timestamp": 秒数, "description": "中间状态描述"}`
- 只写姿态、位置、动作、场景，不写外貌
- 只有支持多参考图的视频模型会使用；其他模型自动忽略

### 6. motion_control（结构控制层，人物运动镜头必填）
- 运动镜头必须补一层结构化控制，避免模型把朝向、位移方向和目标关系脑补错
- 必填字段：
  - `subject_facing`
  - `camera_relation`
  - `movement_direction`
  - `screen_trajectory`
  - `target`
  - `distance_to_target`
  - `phase_beats`
- 如果自然语言 prompt 和 `motion_control` 冲突，以 `motion_control` 为准

### 7. subject_constraints（主体语义约束层，关键叙事镜头强烈建议填写）
- 这层不是风格约束，而是“这个 shot 里谁必须出现、谁不能出现、谁只能画外存在”
- 推荐字段：
  - `required_visible_subjects`
  - `optional_visible_subjects`
  - `offscreen_subjects`
  - `continuity_subjects`
  - `forbidden_visible_subjects`
  - `semantic_rules`
- 当 narrative 提到重要主体但该主体暂时不入镜时，必须显式写入 `offscreen_subjects`
- 当某主体绝不能被模型脑补进画面时，必须写入 `forbidden_visible_subjects`
- 当某主体虽未完整出镜，但身份必须与后续镜头保持一致时，必须写入 `continuity_subjects`
- 例子：
  - 悬念镜头：武松听到虎吼，但老虎不出镜
    - `required_visible_subjects: ["wusong"]`
    - `offscreen_subjects: ["tiger"]`
    - `forbidden_visible_subjects: ["bear", "other_beast", "visible_tiger"]`
  - 交战镜头：武松与老虎同时在画面内
    - `required_visible_subjects: ["wusong", "tiger"]`

请只输出 JSON，不要有其他内容。
