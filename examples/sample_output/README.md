# sample_output

示例产出来自以下命令（离线兜底模式）：

```bash
/opt/homebrew/bin/python3.14 scripts/ad_compose.py \
  --product "灵感咖啡机" \
  --selling_points "3秒出咖啡,智能温控,低噪音" \
  --audience "都市白领" \
  --platform douyin \
  --no_llm --no_api \
  --output_dir /tmp/ad-generator-mvp-test
```

包含：
- `storyboard.json`
- `assets_manifest.json`
- `brand_manifest.json`
- `publish.json`
- `compose_result.json`
- `ad_config.ts`
