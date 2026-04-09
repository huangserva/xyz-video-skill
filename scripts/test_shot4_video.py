#!/usr/bin/env python3
"""直接用 shot_004 的 4 张参考图调 Seedance 2.0 生成视频，验证图片连贯性。"""

import asyncio
import base64
import json
import mimetypes
import sys
from pathlib import Path

import aiohttp

# ── 路径 ──
IMAGE_DIR = Path("/Users/huangzongning/video-output/medical_reunion/shot_image_probe/images")
OUTPUT_DIR = Path("/Users/huangzongning/video-output/medical_reunion/shot4_video_test")

REFERENCE_IMAGES = [
    {"path": IMAGE_DIR / "shot_004.png", "role": "first_frame"},
    {"path": IMAGE_DIR / "shot_004_keyframe_1_2_2s.png", "role": "keyframe", "timestamp": 2.2},
    {"path": IMAGE_DIR / "shot_004_keyframe_2_4_6s.png", "role": "keyframe", "timestamp": 4.6},
    {"path": IMAGE_DIR / "shot_004_end.png", "role": "last_frame"},
]

# shot_004 的 video action prompt（从 storyboard 提取）
VIDEO_PROMPT = (
    "The girl is startled by the sudden shelter from rain. She quickly half-turns to look behind her. "
    "Her gaze shifts from alert vigilance to stunned recognition. "
    "As she turns, the wounded swordsman is gradually revealed — he is slumped against the wet cliff wall, "
    "weakly raising an oil-paper umbrella with his right hand to shield her from the rain. "
    "The camera follows her turning motion in a quick lateral pan with a slight arc. "
    "The emotional progression within this shot: startled tension → urgent half-turn → eyes lock on the swordsman → recognition and emotional crack."
)


# ── 凭据 ──
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import get_api_credentials, load_providers_config, get_model_config  # noqa: E402


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    video_cfg = get_model_config("video")
    # 尝试 byteplus 端点（volcengine 误判真人）
    use_byteplus = "--byteplus" in sys.argv
    if use_byteplus:
        pcfg = video_cfg.get("byteplus", {})
        api_provider = "byteplus"
    else:
        pcfg = video_cfg.get("volcengine_seedance2", {})
        api_provider = str(pcfg.get("provider", "volcengine"))
    creds = get_api_credentials(api_provider)

    api_key = creds.get("api_key")
    api_base = creds.get("api_base")
    if not api_key:
        print(f"ERROR: {api_provider} API key 未配置 (env: VOLCENGINE_API_KEY)")
        return

    model = pcfg.get("model", "doubao-seedance-2-0-fast-260128")
    duration = 7  # shot_004 estimated_duration
    ratio = pcfg.get("ratio", "16:9")
    resolution = pcfg.get("resolution", "720p")

    # ── 构建 content ──
    content = []
    prompt_prefix_lines = []

    for idx, ref in enumerate(REFERENCE_IMAGES, start=1):
        ref_path = ref["path"]
        if not ref_path.exists():
            print(f"WARNING: 参考图不存在: {ref_path}")
            continue

        mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
        img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
        data_url = f"data:{mime};base64,{img_b64}"

        role = ref["role"]
        if role == "first_frame":
            prompt_prefix_lines.append(f"图片{idx}作为首帧锚点。")
        elif role == "last_frame":
            prompt_prefix_lines.append(f"图片{idx}作为尾帧目标。")
        elif role == "keyframe":
            ts = ref.get("timestamp")
            if ts is not None:
                prompt_prefix_lines.append(f"图片{idx}作为约 {ts:.2f} 秒的中间关键帧参考。")
            else:
                prompt_prefix_lines.append(f"图片{idx}作为中间关键帧参考。")

        content.append({
            "type": "image_url",
            "image_url": {"url": data_url},
            "role": "reference_image",
        })

    final_prompt = "".join(prompt_prefix_lines) + VIDEO_PROMPT
    content.insert(0, {"type": "text", "text": final_prompt})

    payload = {
        "model": model,
        "content": content,
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "generate_audio": True,
        "watermark": False,
    }

    print(f"提交 Seedance 2.0 任务...")
    print(f"  model: {model}")
    print(f"  duration: {duration}s")
    print(f"  参考图: {len(REFERENCE_IMAGES)} 张")
    print(f"  prompt: {final_prompt[:200]}...")

    # ── 提交任务 ──
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(
            f"{api_base}/contents/generations/tasks",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"ERROR: 提交失败 ({resp.status}): {body[:500]}")
                return
            data = await resp.json()
            task_id = data.get("id")
            if not task_id:
                print(f"ERROR: 无 task_id: {data}")
                return
            print(f"任务已提交: {task_id}")

    # ── 轮询结果 ──
    poll_interval = pcfg.get("poll_interval", 5)
    poll_max = pcfg.get("poll_max_attempts", 120)
    output_path = OUTPUT_DIR / "shot_004_test.mp4"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for attempt in range(poll_max):
            await asyncio.sleep(poll_interval)
            async with session.get(
                f"{api_base}/contents/generations/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    continue
                result = await resp.json()
                status = result.get("status", "")

                if status == "succeeded":
                    video_url = (result.get("content") or {}).get("video_url")
                    if video_url:
                        print(f"生成成功! 下载视频...")
                        async with aiohttp.ClientSession() as dl_session:
                            async with dl_session.get(video_url) as dl_resp:
                                output_path.write_bytes(await dl_resp.read())
                        print(f"视频已保存: {output_path}")

                        # 保存元数据
                        meta = {
                            "task_id": task_id,
                            "model": model,
                            "duration": duration,
                            "prompt": final_prompt,
                            "reference_images": [str(r["path"]) for r in REFERENCE_IMAGES],
                            "output": str(output_path),
                        }
                        meta_path = OUTPUT_DIR / "shot_004_test_meta.json"
                        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                        print(f"元数据已保存: {meta_path}")
                        return
                    else:
                        print(f"ERROR: 成功但无 video_url: {result}")
                        return

                elif status == "failed":
                    error = result.get("error", {})
                    print(f"ERROR: 生成失败: {error}")
                    return

                else:
                    if attempt % 6 == 0:
                        print(f"  轮询中... status={status} ({attempt * poll_interval}s)")

    print("ERROR: 超时")


if __name__ == "__main__":
    asyncio.run(main())
