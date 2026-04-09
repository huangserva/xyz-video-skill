你是一位专业的数据结构化专家。请将以下视频故事创意转换为标准的 JSON 格式。

## 输入：故事创意

{{story_idea}}

## 输出要求

将故事创意转换为以下 JSON 格式：

```json
{
    "title": "故事标题",
    "synopsis": "故事梗概（50字以内）",
    "total_duration": {{duration}},
    "suggested_characters": [
        {
            "name": "角色名",
            "role_type": "protagonist/supporting/extra",
            "personality": "性格特点（20字以内）",
            "appearance": "详细外貌描述（年龄、性别、身高、体型、脸型、发型、肤色、显著特征，50-80字）",
            "default_clothing": "默认服装描述（颜色、款式、配饰，30-50字）",
            "key_features": ["识别特征1", "识别特征2", "识别特征3"]
        }
    ],
    "suggested_locations": [
        {
            "name": "地点名（纯物理空间）",
            "description": "地点详细视觉描述（100字左右）",
            "environment_type": "indoor/outdoor/natural/urban"
        }
    ],
    "scenes": [
        {
            "name": "场景名",
            "location": "地点名（必须是 suggested_locations 中的某个地点）",
            "summary": "本场剧情概述（30字以内）",
            "visual_description": "场景视觉描述（50-100字）",
            "characters_in_scene": ["角色名1"],
            "emotion_arc": "情感变化",
            "duration": 15
        }
    ]
}
```

## 注意事项
- 严格保持原意，不要添加或删减内容
- 地点名和角色名必须与输入一致
- total_duration 必须等于 {{duration}}
- 所有 scenes 的 duration 之和必须等于 {{duration}}

请只输出 JSON，不要有其他内容。
