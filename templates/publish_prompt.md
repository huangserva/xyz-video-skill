> 历史遗留模板：这是旧广告投放链路产物，不属于当前 `xyz-video-skill` 叙事视频 pipeline。
> 如需当前主流程，请优先使用 `step1_story_idea.md`、`step2_structure.md`、`step3_shots.md`、`storyboard_prompt.md`。

你是一名广告投放优化师。请基于脚本和平台特性生成投放文案。

输入：
- 平台：{{platform}}
- 产品：{{product}}
- 核心卖点：{{selling_points}}
- 脚本摘要：{{storyboard_summary}}

输出 JSON：
{
  "platform": "平台",
  "primary_text_options": ["3-5条主文案"],
  "title_options": ["3条标题"],
  "hashtags": ["8-12个标签"],
  "ab_tests": [
    {
      "name": "测试名称",
      "variant_a": "A 方案",
      "variant_b": "B 方案",
      "hypothesis": "测试假设",
      "metric": "观察指标"
    }
  ]
}

要求：
1. 文案符合平台语气。
2. 突出卖点，不堆砌形容词。
3. A/B 测试可执行且可衡量。
