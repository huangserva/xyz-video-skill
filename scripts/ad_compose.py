#!/opt/homebrew/bin/python3.14
"""视频合成模块 — FFmpeg 拼接 + BGM 混合。

宿主 LLM 生成 storyboard.json，ad_assets.py 生成素材后，调用本脚本合成视频：
    python3 ad_compose.py --storyboard storyboard.json --assets assets.json --output_dir ./output/videos
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from utils import config_dir, read_yaml, setup_logging, write_json

logger = logging.getLogger(__name__)


async def compose_all(
    storyboard: dict[str, Any],
    assets: dict[str, Any],
    output_dir: Path,
    platforms: list[str],
) -> dict[str, str]:
    """按平台列表合成视频。"""
    platforms_cfg = read_yaml(config_dir() / "platforms.yaml")
    videos = {}

    for platform_key, spec in platforms_cfg.get("platforms", {}).items():
        if platform_key not in platforms:
            continue
        video_path = await _compose_video(
            storyboard=storyboard,
            assets=assets,
            output_dir=output_dir,
            platform=platform_key,
            width=spec["width"],
            height=spec["height"],
        )
        if video_path:
            videos[platform_key] = str(video_path)
            logger.info(f"合成完成: {platform_key} → {video_path}")

    return videos


async def _compose_video(
    storyboard: dict[str, Any],
    assets: dict[str, Any],
    output_dir: Path,
    platform: str,
    width: int,
    height: int,
) -> Path | None:
    """使用 FFmpeg 合成视频。优先使用 Seedance 视频片段，fallback 到图片。"""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg 未安装，跳过合成")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    segments = []

    images = {img["shot_id"]: img for img in assets.get("images", [])}
    videos = {vid["shot_id"]: vid for vid in assets.get("videos", [])}

    # 支持 scenes > shots 嵌套格式，向下兼容 flat shots
    shots = []
    scenes = storyboard.get("scenes", [])
    if scenes:
        for scene in scenes:
            shots.extend(scene.get("shots", []))
    else:
        shots = storyboard.get("shots", [])

    for shot in shots:
        shot_id = shot["id"]
        seg_path = output_dir / f"seg_{shot_id}.mp4"
        duration = shot.get("estimated_duration", shot.get("duration", 5))

        # 优先使用 Seedance 生成的视频片段
        vid = videos.get(shot_id)
        if vid:
            vid_path = Path(vid["path"])
            if vid_path.exists():
                cmd = [
                    "ffmpeg", "-y", "-i", str(vid_path),
                    "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(seg_path),
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    segments.append(seg_path)
                    logger.info(f"片段 {shot_id}: 使用 Seedance 视频")
                    continue
                except subprocess.CalledProcessError as e:
                    logger.warning(f"片段 {shot_id} 视频缩放失败，fallback 到图片: {e}")

        # Fallback：图片 → 静态视频片段
        img = images.get(shot_id)
        if not img:
            continue

        img_path = Path(img["path"])
        if not img_path.exists():
            continue

        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(img_path),
            "-t", str(duration),
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(seg_path),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            segments.append(seg_path)
            logger.info(f"片段 {shot_id}: fallback 图片模式")
        except subprocess.CalledProcessError as e:
            logger.warning(f"片段 {shot_id} 合成失败: {e}")

    if not segments:
        return None

    # 拼接片段（支持转场效果）
    merged = output_dir / f"{platform}_merged.mp4"
    transitions = [shot.get("transition_in") for shot in shots]

    merged = _merge_segments_with_transitions(segments, transitions, merged)
    if not merged:
        return None

    # 混合 BGM
    final = output_dir / f"{platform}.mp4"
    bgm_path = Path(assets.get("bgm", {}).get("path", ""))

    if bgm_path.exists():
        # 检查 merged 视频是否有音轨
        has_audio = _has_audio_stream(merged)

        if has_audio:
            # 视频有音轨：混合视频音轨 + BGM
            cmd = [
                "ffmpeg", "-y", "-i", str(merged), "-stream_loop", "-1", "-i", str(bgm_path),
                "-filter_complex", "[0:a]volume=1[a0];[1:a]volume=0.2[a1];[a0][a1]amix=inputs=2:duration=first[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", str(final),
            ]
        else:
            # 视频无音轨：直接叠加 BGM
            cmd = [
                "ffmpeg", "-y", "-i", str(merged), "-stream_loop", "-1", "-i", str(bgm_path),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                "-shortest", str(final),
            ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return final
        except subprocess.CalledProcessError:
            return merged
    else:
        shutil.copy(merged, final)
        return final


def _merge_segments_with_transitions(
    segments: list[Path],
    transitions: list[dict | str | None],
    output: Path,
) -> Path | None:
    """按转场类型拼接视频片段。

    transitions[i] 对应第 i 个 shot 的 transition_in（即从 segment[i-1] 到 segment[i] 的过渡）。
    transitions[0] 通常为 None（第一个片段无前序）。

    支持的转场类型：
    - None / "straight-cut"：硬切（简单拼接）
    - "cross-dissolve"：交叉溶解（xfade filter）
    - "flash-white"：白闪（fadeout→白→fadein）

    transition 可以是字符串或 dict: {"type": "cross-dissolve", "duration": 0.5}
    """
    if not segments:
        return None
    if len(segments) == 1:
        shutil.copy(segments[0], output)
        return output

    # 规范化 transitions 列表，使其与 segments 等长
    norm: list[tuple[str, float]] = []
    for i in range(len(segments)):
        t = transitions[i] if i < len(transitions) else None
        if t is None:
            norm.append(("straight-cut", 0.0))
        elif isinstance(t, str):
            norm.append((t, 0.3 if t != "straight-cut" else 0.0))
        elif isinstance(t, dict):
            norm.append((t.get("type", "straight-cut"), t.get("duration", 0.3)))
        else:
            norm.append(("straight-cut", 0.0))

    # 检查是否全部为硬切 → 走快速 concat 路径
    all_cuts = all(t == "straight-cut" for t, _ in norm)
    if all_cuts:
        return _simple_concat(segments, output)

    # 逐对拼接（使用 xfade filter chain）
    # ffmpeg xfade 只能两两处理，所以我们迭代地合并
    current = segments[0]
    tmp_dir = output.parent / "_transition_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, len(segments)):
        t_type, t_dur = norm[i]
        next_seg = segments[i]
        tmp_out = tmp_dir / f"merged_{i}.mp4"

        if t_type == "straight-cut" or t_dur <= 0:
            # 硬切：简单拼接两段
            tmp_out = _simple_concat([current, next_seg], tmp_out)
            if not tmp_out:
                logger.warning(f"硬切拼接失败 (段 {i})，尝试跳过")
                continue
        elif t_type == "cross-dissolve":
            tmp_out = _apply_xfade(current, next_seg, tmp_out, "fade", t_dur)
            if not tmp_out:
                logger.warning(f"交叉溶解失败 (段 {i})，fallback 硬切")
                tmp_out = tmp_dir / f"merged_{i}.mp4"
                tmp_out = _simple_concat([current, next_seg], tmp_out)
                if not tmp_out:
                    continue
        elif t_type == "flash-white":
            tmp_out = _apply_flash_white(current, next_seg, tmp_out, t_dur)
            if not tmp_out:
                logger.warning(f"白闪失败 (段 {i})，fallback 硬切")
                tmp_out = tmp_dir / f"merged_{i}.mp4"
                tmp_out = _simple_concat([current, next_seg], tmp_out)
                if not tmp_out:
                    continue
        else:
            logger.warning(f"未知转场类型 '{t_type}'，使用硬切")
            tmp_out = _simple_concat([current, next_seg], tmp_out)
            if not tmp_out:
                continue

        current = tmp_out

    # 复制最终结果到输出路径
    if current and current.exists():
        shutil.copy(current, output)
        # 清理临时文件
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return output

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _simple_concat(segments: list[Path], output: Path) -> Path | None:
    """简单拼接（硬切），使用 ffmpeg concat demuxer。"""
    filelist = output.parent / f"{output.stem}_list.txt"
    filelist.write_text(
        "\n".join(f"file '{s.resolve()}'" for s in segments),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(filelist), "-c", "copy", str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        filelist.unlink(missing_ok=True)
        return output
    except subprocess.CalledProcessError as e:
        logger.warning(f"concat 拼接失败: {e}")
        filelist.unlink(missing_ok=True)
        return None


def _get_duration(video: Path) -> float:
    """用 ffprobe 获取视频时长（秒）。"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 5.0  # 合理默认值


def _has_audio_stream(video: Path) -> bool:
    """检查视频文件是否包含音频流。"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0", str(video),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def _apply_xfade(
    seg_a: Path, seg_b: Path, output: Path,
    transition: str, duration: float,
) -> Path | None:
    """使用 ffmpeg xfade filter 实现两段视频间的过渡效果，同时保留音频。"""
    return _apply_xfade_impl(seg_a, seg_b, output, transition, duration)


def _apply_flash_white(
    seg_a: Path, seg_b: Path, output: Path, duration: float,
) -> Path | None:
    """白闪转场：使用 xfade 的 fadewhite 过渡效果，同时保留音频。"""
    return _apply_xfade_impl(seg_a, seg_b, output, "fadewhite", duration)


def _apply_xfade_impl(
    seg_a: Path, seg_b: Path, output: Path,
    transition: str, duration: float,
) -> Path | None:
    """xfade 统一实现：视频用 xfade，音频用 acrossfade（有音轨时）或静音填充。"""
    dur_a = _get_duration(seg_a)
    offset = max(0, dur_a - duration)

    has_audio_a = _has_audio_stream(seg_a)
    has_audio_b = _has_audio_stream(seg_b)

    if has_audio_a and has_audio_b:
        # 两段都有音频：视频 xfade + 音频 acrossfade
        filter_complex = (
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
            f"[0:a][1:a]acrossfade=d={duration}:c1=tri:c2=tri[a]"
        )
        map_args = ["-map", "[v]", "-map", "[a]"]
    elif has_audio_a or has_audio_b:
        # 只有一段有音频：给无音频的那段加静音，再 acrossfade
        dur_b = _get_duration(seg_b)
        silent_dur = dur_a if not has_audio_a else dur_b
        a_input = "0:a" if has_audio_a else "silent"
        b_input = "1:a" if has_audio_b else "silent"
        filter_complex = (
            f"anullsrc=r=44100:cl=stereo[silent];"
            f"[silent]atrim=0:{silent_dur}[silent];"
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v];"
            f"[{a_input}][{b_input}]acrossfade=d={duration}:c1=tri:c2=tri[a]"
        )
        map_args = ["-map", "[v]", "-map", "[a]"]
    else:
        # 都没有音频：只做视频 xfade
        filter_complex = (
            f"[0:v][1:v]xfade=transition={transition}:duration={duration}:offset={offset}[v]"
        )
        map_args = ["-map", "[v]"]

    cmd = [
        "ffmpeg", "-y", "-i", str(seg_a), "-i", str(seg_b),
        "-filter_complex", filter_complex,
        *map_args, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output
    except subprocess.CalledProcessError as e:
        logger.warning(f"xfade ({transition}) 失败: {e}")
        return None


# ── CLI 入口 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="视频合成（FFmpeg 拼接 + BGM）")
    parser.add_argument("--storyboard", required=True, help="storyboard.json 路径")
    parser.add_argument("--assets", required=True, help="assets.json 路径")
    parser.add_argument("--output_dir", required=True, help="视频输出目录")
    parser.add_argument("--platform", nargs="*", default=["youtube"], help="目标平台（youtube douyin wechat）")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    with open(Path(args.storyboard).expanduser().resolve(), encoding="utf-8") as f:
        storyboard = json.load(f)
    with open(Path(args.assets).expanduser().resolve(), encoding="utf-8") as f:
        assets = json.load(f)

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    videos = await compose_all(
        storyboard=storyboard,
        assets=assets,
        output_dir=output_dir,
        platforms=args.platform,
    )

    result = {"videos": videos, "output_dir": str(output_dir)}
    write_json(output_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
