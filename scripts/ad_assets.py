#!/opt/homebrew/bin/python3.14
"""素材生成模块 — 图片/视频/BGM 生成。

宿主 LLM 生成 storyboard.json 后，调用本脚本生成素材：
    python3 ad_assets.py --storyboard storyboard.json --output_dir ./output/assets
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import mimetypes
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw

from utils import get_api_credentials, get_model_config, load_external_api_config, setup_logging, timestamp_id, write_json
from content_filter import ContentFilter, VideoPromptBuilder

logger = logging.getLogger(__name__)

# ── Gemini 图片生成 system prompt ────────────────────────────────

GEMINI_IMAGE_SYSTEM_PROMPT = (
    "You are a cinematic image generator for a professional video production pipeline. "
    "Your task is to generate a single photorealistic cinematic frame that will be used as "
    "a keyframe for AI video generation (Seedance I2V).\n\n"
    "CRITICAL RULES:\n"
    "1. STYLE LOCK: The style_anchor provided in the prompt defines the EXACT visual style — "
    "color grading, rendering quality, art direction, and aesthetic. You MUST match this style "
    "precisely in EVERY frame, whether it is a wide shot, close-up, or detail shot. "
    "NEVER switch between illustrated/animated and photorealistic styles within the same project. "
    "If the style anchor says 'painterly quality', ALL frames must have that painterly quality.\n"
    "2. CHARACTER FIDELITY: When reference images are provided, the generated character MUST be "
    "visually identical — same face, proportions, colors, textures, and distinguishing features.\n"
    "3. VISUAL INFERENCE: Infer logically necessary details that the prompt may not explicitly state. "
    "Examples: rain → wet surfaces, reflective puddles, damp hair and clothes; "
    "holding an umbrella in rain; battle scene → debris, dust, scorch marks; "
    "cold weather → visible breath vapor, reddened cheeks.\n"
    "4. NO TEXT: Never include any text, labels, watermarks, or annotations in the image.\n"
    "5. CINEMATIC COMPOSITION: Frame the shot as a professional cinematographer would — "
    "follow the rule of thirds, use depth of field, and create visual depth."
)


class AssetGenerator:
    """素材生成器。"""

    def __init__(
        self,
        storyboard: dict[str, Any],
        output_root: Path,
        image_width: int = 1024,
        image_height: int = 1024,
        parallel: int = 4,
        use_api: bool = True,
    ):
        self.storyboard = storyboard
        self.output_root = output_root
        self.image_width = image_width
        self.image_height = image_height
        self.use_api = use_api
        self.sem = asyncio.Semaphore(parallel)

        self.image_dir = output_root / "images"
        self.voice_dir = output_root / "voiceovers"
        self.bgm_dir = output_root / "bgm"
        self.video_dir = output_root / "videos"

        for d in [self.image_dir, self.voice_dir, self.bgm_dir, self.video_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self.cfg = load_external_api_config()

    async def run(self) -> dict[str, Any]:
        """执行素材生成。"""
        # ── 支持 scenes > shots 结构，向下兼容 flat shots ──
        scenes = self.storyboard.get("scenes", [])
        if not scenes:
            flat_shots = self.storyboard.get("shots", [])
            if not flat_shots:
                raise ValueError("storyboard 缺少 scenes 或 shots")
            # 向下兼容：flat shots → 包进一个默认 scene
            scenes = [{"id": "default", "name": "default", "shots": flat_shots}]

        # 收集所有 shots 用于校验和计数
        all_shots = []
        for scene in scenes:
            for shot in scene.get("shots", []):
                all_shots.append(shot)

        # 校验：所有 shot 必须有 end_frame_description（包括最后一个）
        for shot in all_shots:
            sid = shot.get("id", "?")
            efd = str(shot.get("end_frame_description", "")).strip()
            if not efd:
                raise ValueError(
                    f"shot_{sid} 缺少 end_frame_description。"
                    "所有镜头（包括最后一个）都必须有 end_frame_description，"
                    "否则首尾帧衔接会断裂。请修改 storyboard.json 后重试。"
                )

        # ── 全局 prompt 提取：一次性为所有 shot 生成首帧/尾帧/动作 prompt ──
        extracted_prompts = await self._extract_all_shot_prompts(all_shots)

        # 串行处理：按 scene > shots 顺序
        shot_results = []
        previous_video_path: Path | None = None
        shot_index = 0

        for scene in scenes:
            # 跨场景重置：不将上一场景的尾帧传递到新场景
            previous_video_path = None

            # 提取 scene 层级环境上下文（同场景所有镜头共享）
            scene_context = {
                "environment_description": scene.get("environment_description", ""),
                "lighting": scene.get("lighting", ""),
                "weather": scene.get("weather", ""),
                "props": scene.get("props", []),
            }

            # ── 生成场景图（纯环境，无角色）——同场景所有镜头共享 ──
            scene_id = scene.get("id", "default")
            scene_image_path = await self._generate_scene_image(scene)
            if scene_image_path:
                scene_context["scene_image"] = scene_image_path
                logger.info(f"场景 {scene_id}: 场景图已生成 → {scene_image_path}")

            scene_shots = scene.get("shots", [])
            for i, shot in enumerate(scene_shots):
                shot_index += 1
                is_last_in_scene = (i == len(scene_shots) - 1)
                logger.info(f"处理镜头 {shot_index}/{len(all_shots)} (scene_last={is_last_in_scene})")

                # chain_from_previous: 从上一 shot 的实际视频提取尾帧作为首帧
                chain = shot.get("chain_from_previous", False)
                if chain and previous_video_path:
                    # 从视频提取实际最后一帧（比生成的尾帧图更连贯）
                    extracted_frame_path = self.image_dir / f"shot_{shot.get('id', 0):03d}_chained.png"
                    first_frame = self._extract_video_last_frame(previous_video_path, extracted_frame_path)
                    if first_frame:
                        logger.info(f"shot_{shot.get('id')}: chain_from_previous=true，从上一视频提取实际尾帧")
                    else:
                        logger.warning(f"shot_{shot.get('id')}: 视频尾帧提取失败，fallback 到独立生成")
                        first_frame = None
                else:
                    first_frame = None

                result = await self._generate_shot(
                    shot, provided_first_frame=first_frame,
                    scene_context=scene_context, extracted_prompts=extracted_prompts,
                    is_last_in_scene=is_last_in_scene,
                )

                # 视频质量检测 + 自动重试/裁剪
                max_retries = 2
                for attempt in range(max_retries + 1):
                    if "video" not in result:
                        break

                    vid_path = Path(result["video"]["path"])
                    quality = self._scan_video_quality(vid_path)

                    if quality["needs_regeneration"] and attempt < max_retries:
                        logger.warning(
                            f"shot_{shot.get('id')}: 质量不合格，重新生成 "
                            f"(尝试 {attempt + 2}/{max_retries + 1})"
                        )
                        result = await self._generate_shot(
                            shot, provided_first_frame=first_frame,
                            scene_context=scene_context, extracted_prompts=extracted_prompts,
                            is_last_in_scene=is_last_in_scene,
                        )
                        continue
                    elif quality["needs_regeneration"]:
                        logger.error(
                            f"shot_{shot.get('id')}: 重试 {max_retries} 次仍不合格，保留当前结果"
                        )

                    # 裁剪到最后一个稳定点
                    if quality["trim_to"] is not None:
                        self._trim_video_at(vid_path, quality["trim_to"])

                    break

                shot_results.append(result)

                # 提取尾帧供同场景链式使用
                if "video" in result:
                    previous_video_path = Path(result["video"]["path"])
                else:
                    previous_video_path = None

        images = [r["image"] for r in shot_results]
        videos = [r["video"] for r in shot_results if "video" in r]

        # BGM
        duration = int(self.storyboard.get("total_duration", 60))
        bgm_style = str(self.storyboard.get("bgm_style", "upbeat"))
        bgm_path, bgm_provider = await self._generate_bgm(bgm_style, duration)

        return {
            "generated_at": timestamp_id(),
            "asset_root": str(self.output_root),
            "images": images,
            "videos": videos,
            "bgm": {"path": str(bgm_path), "provider": bgm_provider, "style": bgm_style},
        }

    @staticmethod
    def _extract_video_last_frame(video_path: Path, output_path: Path) -> Path | None:
        """用 ffmpeg 提取视频的最后一帧作为图片。"""
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", str(output_path)],
                check=True, capture_output=True,
            )
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning(f"提取视频尾帧失败: {e}")
        return None

    @staticmethod
    def _scan_video_quality(video_path: Path, min_keep: float = 3.0) -> dict:
        """全帧扫描视频质量，找到最后一个稳定点。

        策略：从后往前找到第一个"稳定区域"（连续 N 帧 MSE 都在阈值以下），
        裁到那个点。如果裁完剩余不足 min_keep 秒，标记为需要重新生成。

        同时检测奇偶帧交替闪烁（高-低-高-低模式）。

        返回:
            {
                "ok": bool,                    # 整体质量是否合格
                "needs_regeneration": bool,     # 是否需要重新生成（裁完太短）
                "trim_to": float | None,        # 裁剪到的时间点（秒），None 表示不裁
                "duration": float,
                "fps": float,
            }
        """
        result = {"ok": True, "needs_regeneration": False, "trim_to": None, "duration": 0, "fps": 24}

        # 获取视频信息
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "json", str(video_path)],
                check=True, capture_output=True, text=True,
            )
            info = json.loads(probe.stdout)
            duration = float(info["format"]["duration"])
            rfr = info["streams"][0]["r_frame_rate"]
            num, den = rfr.split("/")
            fps = float(num) / float(den)
        except (subprocess.CalledProcessError, KeyError, ValueError, ZeroDivisionError) as e:
            logger.warning(f"质量检测: 无法获取视频信息 {e}")
            return result

        result["duration"] = duration
        result["fps"] = fps

        if duration < 2.0:
            return result

        # 提取全视频帧（缩小版加速）
        with tempfile.TemporaryDirectory() as tmp_dir:
            frame_pattern = str(Path(tmp_dir) / "frame_%04d.png")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(video_path),
                     "-vf", "scale=160:-1", "-q:v", "2", frame_pattern],
                    check=True, capture_output=True, timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logger.warning(f"质量检测: 帧提取失败 {e}")
                return result

            frame_files = sorted(Path(tmp_dir).glob("frame_*.png"))
            if len(frame_files) < 6:
                return result

            frames = []
            for ff in frame_files:
                try:
                    img = Image.open(ff).convert("RGB")
                    frames.append(img)
                except Exception:
                    continue

            if len(frames) < 6:
                return result

            # 计算所有相邻帧 MSE
            diffs = []
            for j in range(1, len(frames)):
                px_a = list(frames[j - 1].getdata())
                px_b = list(frames[j].getdata())
                mse = sum(
                    (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
                    for a, b in zip(px_a, px_b)
                ) / (len(px_a) * 3)
                diffs.append(mse)

            if not diffs:
                return result

            # 用前 1/4 帧的中位数作为稳定基准
            stable_count = max(3, len(diffs) // 4)
            sorted_stable = sorted(diffs[:stable_count])
            median_diff = sorted_stable[len(sorted_stable) // 2]

            threshold = max(300, median_diff * 8)

            # ── 用滑动窗口平滑 MSE，消除闪烁的高低交替 ──
            # 窗口大小 5：每帧的"有效 MSE"= 周围 5 帧的最大值
            # 这能有效消除奇偶帧交替闪烁（高-1-高-1 模式）
            smoothed = []
            for j in range(len(diffs)):
                start = max(0, j - 2)
                end = min(len(diffs), j + 3)
                smoothed.append(max(diffs[start:end]))

            # ── 从后往前找最后一个稳定点 ──
            # "稳定"定义：连续 6 帧平滑后的 MSE 都在阈值以下
            required_stable = 6
            stable_run = 0
            last_stable_idx = len(smoothed)  # 默认：整个视频都稳定

            for j in range(len(smoothed) - 1, -1, -1):
                if smoothed[j] <= threshold:
                    stable_run += 1
                    if stable_run >= required_stable:
                        last_stable_idx = j + stable_run
                        break
                else:
                    stable_run = 0

            if stable_run < required_stable:
                # 从头找稳定区域的结束点
                last_stable_idx = 0
                for j in range(len(smoothed)):
                    if smoothed[j] <= threshold:
                        last_stable_idx = j + 1
                    else:
                        break

            # ── 正向扫描：找第一个连续异常段 ──
            # 解决中间段出问题但尾部恢复、导致反向扫描漏检的情况
            consecutive_bad = 0
            first_bad_start = None
            for j in range(len(smoothed)):
                if smoothed[j] > threshold:
                    if consecutive_bad == 0:
                        first_bad_start = j
                    consecutive_bad += 1
                else:
                    if consecutive_bad >= 12 and first_bad_start is not None:
                        forward_keep = (first_bad_start + 1) / fps
                        if forward_keep < (last_stable_idx + 1) / fps:
                            last_stable_idx = first_bad_start
                            logger.info(
                                f"质量检测: 正向扫描发现异常段 @{forward_keep:.1f}s "
                                f"({consecutive_bad} 帧)"
                            )
                        break
                    consecutive_bad = 0
                    first_bad_start = None
            else:
                if consecutive_bad >= 12 and first_bad_start is not None:
                    forward_keep = (first_bad_start + 1) / fps
                    if forward_keep < (last_stable_idx + 1) / fps:
                        last_stable_idx = first_bad_start

            # 计算可保留的时长
            keep_time = (last_stable_idx + 1) / fps  # +1 因为 diffs[j] 对应帧 j+1

            # ── 闪烁检测：奇偶帧交替跳变（高-低-高-低模式） ──
            # 正常运动的 MSE 连续渐变，闪烁模式下相邻帧 MSE 反复反转
            # 从后往前找最后一个无闪烁的稳定点
            min_amplitude = max(100, median_diff * 5)  # 反转幅度阈值（100+过滤自然运动振荡）
            reversals = 0
            flicker_end = len(diffs)

            for j in range(len(diffs) - 1, 1, -1):
                going_up = diffs[j] > diffs[j - 1]
                was_up = diffs[j - 1] > diffs[j - 2]
                amplitude = abs(diffs[j] - diffs[j - 1])
                if going_up != was_up and amplitude > min_amplitude:
                    reversals += 1
                else:
                    if reversals >= 8:
                        # 发现闪烁段，更新 keep_time
                        flicker_start_time = (j + 1) / fps
                        if flicker_start_time < keep_time:
                            keep_time = flicker_start_time
                            logger.info(
                                f"质量检测: 检测到闪烁 ({reversals} 次反转 @{flicker_start_time:.1f}s)，"
                                f"裁剪到 {keep_time:.1f}s"
                            )
                    reversals = 0

            # 检查开头处的闪烁
            if reversals >= 8:
                flicker_start_time = 2 / fps
                if flicker_start_time < keep_time:
                    keep_time = flicker_start_time

            # ── 局部突变检测：单帧或少数帧的面部变形/跳变 ──
            # 用滑动窗口计算局部均值，如果某帧 MSE 超过局部均值的 spike_factor 倍
            # 且绝对值超过 spike_abs_min，标记为 spike
            spike_factor = 2.5
            spike_abs_min = 150
            spike_window = 12  # 前后各 12 帧（约 0.5s）计算局部基准
            spike_count = 0
            first_spike_time = None

            for j in range(len(diffs)):
                # 局部窗口（排除自身）
                w_start = max(0, j - spike_window)
                w_end = min(len(diffs), j + spike_window + 1)
                neighbors = [diffs[k] for k in range(w_start, w_end) if k != j]
                if not neighbors:
                    continue
                local_mean = sum(neighbors) / len(neighbors)

                if diffs[j] > max(spike_abs_min, local_mean * spike_factor):
                    spike_count += 1
                    t = (j + 1) / fps
                    if first_spike_time is None:
                        first_spike_time = t
                    logger.debug(
                        f"质量检测: 局部突变 @{t:.2f}s "
                        f"(MSE={diffs[j]:.0f}, 局部均值={local_mean:.0f}, "
                        f"倍率={diffs[j]/local_mean:.1f}x)"
                    )

            if spike_count >= 2:
                # 多个突变点 → 标记需要裁剪或重新生成
                spike_trim = first_spike_time - 0.1  # 在第一个突变前 0.1s 裁
                if spike_trim > 0 and spike_trim < keep_time:
                    keep_time = spike_trim
                    logger.info(
                        f"质量检测: 检测到 {spike_count} 个局部突变，"
                        f"首个 @{first_spike_time:.1f}s，裁剪到 {keep_time:.1f}s"
                    )

            # ── 人脸变形检测：DNN 置信度骤降 ──
            # 用 OpenCV DNN SSD 人脸检测器，追踪人脸置信度变化
            # 如果曾经稳定检测到人脸，后来置信度骤降 → 脸部变形
            try:
                import cv2
                model_dir = Path(__file__).parent.parent / "models"
                proto = model_dir / "deploy.prototxt"
                weights = model_dir / "res10_300x300_ssd_iter_140000.caffemodel"
                if proto.exists() and weights.exists():
                    net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
                    face_confs = []  # (time, confidence)
                    sample_interval = 3  # 每 3 帧检测一次
                    with tempfile.TemporaryDirectory() as face_td:
                        subprocess.run(
                            ["ffmpeg", "-i", str(video_path), "-vf", "scale=480:-1",
                             f"{face_td}/f%05d.png", "-loglevel", "error"],
                            check=True,
                        )
                        face_frames = sorted(Path(face_td).glob("f*.png"))
                        for fi, ff in enumerate(face_frames):
                            if fi % sample_interval != 0:
                                continue
                            img = cv2.imread(str(ff))
                            if img is None:
                                continue
                            blob = cv2.dnn.blobFromImage(
                                img, 1.0, (300, 300), (104.0, 177.0, 123.0)
                            )
                            net.setInput(blob)
                            detections = net.forward()
                            best_conf = 0.0
                            for di in range(detections.shape[2]):
                                c = float(detections[0, 0, di, 2])
                                if c > best_conf:
                                    best_conf = c
                            face_confs.append((fi / fps, best_conf))

                    # 分析置信度变化：找"曾有脸→脸消失"的骤降点
                    if face_confs:
                        # 用滑动窗口找到最后一段"稳定有脸"区间
                        high_threshold = 0.5
                        low_threshold = 0.3
                        window = 3  # 连续 3 个采样点有脸 = 稳定有脸

                        # 找最后一个 conf >= high 的连续段
                        last_good_face_time = None
                        consecutive_high = 0
                        had_stable_face = False
                        for t, c in face_confs:
                            if c >= high_threshold:
                                consecutive_high += 1
                                if consecutive_high >= window:
                                    had_stable_face = True
                                last_good_face_time = t
                            else:
                                consecutive_high = 0

                        if had_stable_face and last_good_face_time is not None:
                            # 检查 last_good_face_time 之后是否还有高置信度人脸
                            # 如果后面脸又回来了，说明只是转身，不是变形
                            face_returned = False
                            for t, c in face_confs:
                                if t > last_good_face_time + 2.0 and c >= low_threshold:
                                    face_returned = True
                                    break

                            remaining = duration - last_good_face_time
                            if remaining > 1.5 and not face_returned:
                                # 脸消失后再也没回来 → 脸部变形
                                face_trim = last_good_face_time + 0.3
                                if face_trim < keep_time:
                                    keep_time = face_trim
                                    logger.info(
                                        f"质量检测: 人脸置信度骤降，"
                                        f"最后稳定 @{last_good_face_time:.1f}s，"
                                        f"裁剪到 {keep_time:.1f}s"
                                    )
            except ImportError:
                pass  # cv2 不可用，跳过人脸检测
            except Exception as e:
                logger.debug(f"质量检测: 人脸检测跳过 ({e})")

            if keep_time >= duration - 0.1:
                # 整个视频都稳定，不需要裁剪
                logger.debug(f"质量检测: 视频质量正常，无需处理")
                return result

            if keep_time >= min_keep:
                # 可以裁剪保留前面的好内容
                result["trim_to"] = keep_time
                result["ok"] = True
                logger.info(
                    f"质量检测: 发现异常帧，裁剪到 {keep_time:.1f}s "
                    f"({duration:.1f}s → {keep_time:.1f}s, "
                    f"median={median_diff:.0f}, threshold={threshold:.0f})"
                )
            else:
                # 裁完太短，需要重新生成
                result["needs_regeneration"] = True
                result["ok"] = False
                logger.warning(
                    f"质量检测: 异常帧过多，稳定内容仅 {keep_time:.1f}s (< {min_keep}s)，需重新生成 "
                    f"(median={median_diff:.0f}, threshold={threshold:.0f})"
                )

            return result

    @staticmethod
    def _trim_video_at(video_path: Path, end_time: float, min_remaining: float = 3.0) -> Path:
        """裁剪视频到指定时长。

        Args:
            video_path: 视频路径
            end_time: 保留到的时间点（秒）
            min_remaining: 裁后最短时长

        Returns:
            裁剪后的路径（原地替换）
        """
        if end_time < min_remaining:
            logger.warning(f"裁剪: 目标时长 {end_time:.1f}s 太短，跳过")
            return video_path

        trimmed_path = video_path.with_suffix(".trimmed.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path),
                 "-t", str(end_time),
                 "-c", "copy", str(trimmed_path)],
                check=True, capture_output=True, timeout=30,
            )
            trimmed_path.replace(video_path)
            logger.info(f"裁剪完成: {video_path} → {end_time:.1f}s")
            return video_path
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"裁剪失败: {e}")
            trimmed_path.unlink(missing_ok=True)
            return video_path

    def _get_character_appearances(self, characters_in_shot: list[str]) -> list[tuple[str, str]]:
        """从 storyboard.characters 读取角色外貌（单一真相源）。"""
        characters_cfg = self.storyboard.get("characters", {})
        appearances = []
        for char_id in characters_in_shot:
            char = characters_cfg.get(char_id, {})
            appearance = char.get("appearance", "")
            if appearance:
                appearances.append((char_id, appearance))
        return appearances

    async def _generate_scene_image(self, scene: dict[str, Any]) -> Path | None:
        """生成纯环境场景图（无角色），同场景所有镜头共享。"""
        scene_id = scene.get("id", "default")
        env_desc = scene.get("environment_description", "")
        if not env_desc:
            return None

        style_anchor = str(self.storyboard.get("style_anchor", ""))
        lighting = scene.get("lighting", "")
        weather = scene.get("weather", "")
        props = scene.get("props", [])

        prompt_parts = []
        if style_anchor:
            prompt_parts.append(style_anchor)
        prompt_parts.append("")
        prompt_parts.append("Generate a PURE BACKGROUND/ENVIRONMENT scene image.")
        prompt_parts.append("⚠️ CRITICAL: DO NOT draw any characters, people, or figures. This is ONLY the environment/background.")
        prompt_parts.append("")
        prompt_parts.append(f"ENVIRONMENT: {env_desc}")
        if lighting:
            prompt_parts.append(f"LIGHTING: {lighting}")
        if weather:
            prompt_parts.append(f"WEATHER/ATMOSPHERE: {weather}")
        if props:
            prompt_parts.append(f"PROPS (must be visible): {', '.join(props)}")
        prompt_parts.append("")
        prompt_parts.append("REQUIREMENTS:")
        prompt_parts.append("1. NO characters, people, animals, or figures — ONLY environment")
        prompt_parts.append("2. Include ALL props mentioned above")
        prompt_parts.append("3. Rich environmental details with cinematic lighting")
        prompt_parts.append("4. Leave space where characters would naturally be positioned")
        prompt_parts.append("5. High quality background suitable as reference for subsequent shot generation")

        prompt = "\n".join(prompt_parts)
        out = self.image_dir / f"scene_{scene_id}.png"

        logger.info(f"场景 {scene_id}: 生成纯环境场景图...")
        async with self.sem:
            path, provider = await self._generate_image(prompt, shot_id=0, output_path=out)
        if path and path.exists() and path.stat().st_size > 1000:
            return path
        logger.warning(f"场景 {scene_id}: 场景图生成失败，分镜将不使用场景参考图")
        return None

    async def _extract_all_shot_prompts(
        self, all_shots: list[dict[str, Any]],
    ) -> dict[int, dict[str, str]]:
        """全局 prompt 提取：一次性为所有 shot 生成首帧/尾帧/动作 prompt。

        将完整 narrative + 所有 shot 的上下文一次性交给 LLM，
        LLM 看到完整故事线 + 前后镜头上下文，提取出的 prompt 天然连贯。

        返回: {shot_id: {"first_frame_prompt": ..., "last_frame_prompt": ..., "video_action_prompt": ...}}
        如果 LLM 调用失败，fallback 到各 shot 的原始字段。
        """
        narrative = str(self.storyboard.get("narrative", ""))

        # 构建 fallback 结果（用原始字段）
        fallback: dict[int, dict[str, str]] = {}
        for shot in all_shots:
            sid = int(shot.get("id", 0))
            fallback[sid] = {
                "first_frame_prompt": str(shot.get("scene_prompt") or shot.get("image_prompt", "")),
                "last_frame_prompt": str(shot.get("end_frame_description", "")),
                "video_action_prompt": str(shot.get("action_prompt", "")),
            }

        if not all_shots:
            return fallback

        # LLM 凭据
        llm_cfg = get_model_config("llm")
        creds = get_api_credentials(llm_cfg.get("provider", "apimart"), self.cfg)
        if not creds.get("api_key"):
            logger.warning("LLM API 无凭据，fallback 到原始 prompt")
            return fallback

        model = llm_cfg.get("model", "gemini-2.5-flash")
        timeout = llm_cfg.get("timeout", 120)

        # 构建 system prompt
        system_prompt = (
            "You are a visual prompt engineer for a cinematic video production pipeline.\n\n"
            "You will receive the COMPLETE NARRATIVE (the single source of truth for the entire story) "
            "and ALL SHOTS in sequence. Your job is to extract precise prompts for EACH shot that are "
            "mutually coherent — because you see the full story and all shots at once.\n\n"
            "For EACH shot, extract THREE prompts:\n\n"
            "1. FIRST_FRAME: A precise visual description of the MOMENT BEFORE action begins.\n"
            "   - Exact character positions, postures, facing directions, hand-held objects\n"
            "   - Spatial relationships between characters\n"
            "   - The pose should ANTICIPATE the coming action\n"
            "   - Include composition/framing (shot type, angle)\n"
            "   - If this is NOT the first shot, ensure continuity with the previous shot's LAST_FRAME\n"
            "   - This is a STILL image — do NOT describe motion\n\n"
            "2. LAST_FRAME: A precise visual description of the MOMENT AFTER action completes.\n"
            "   - The RESULT of the action — where characters ended up, final postures\n"
            "   - Must be PHYSICALLY REACHABLE from FIRST_FRAME within the shot duration\n"
            "   - If this is NOT the last shot, prepare for the next shot's beginning\n"
            "   - This is a STILL image — do NOT describe motion\n\n"
            "3. VIDEO_ACTION: A motion description connecting FIRST_FRAME to LAST_FRAME.\n"
            "   - Only describe MOVEMENT and CHANGE — not static visual state\n"
            "   - Include story context: WHY the character moves (motivation/emotion)\n"
            "   - Must connect smoothly to adjacent shots' actions\n"
            "   - This text goes to a video generation model (Seedance I2V) that already sees the first/last frame images\n\n"
            "CROSS-SHOT CONTINUITY RULES:\n"
            "- Shot N's LAST_FRAME and Shot N+1's FIRST_FRAME must describe the SAME visual state\n"
            "  (same character positions, same held objects, same environmental state)\n"
            "- Environmental transitions (rain→clearing, day→dusk) must be GRADUAL across shots\n"
            "- Character state changes must be physically plausible\n\n"
            "RULES:\n"
            "- Write ALL prompts in English\n"
            "- DO NOT include character appearance/clothing (injected separately by code)\n"
            "- Keep each prompt 2-4 sentences, precise and visual\n"
            "- Respond ONLY in the exact format below, no extra text\n\n"
            "OUTPUT FORMAT:\n"
            "===SHOT_{id}===\n"
            "FIRST_FRAME: <prompt>\n"
            "LAST_FRAME: <prompt>\n"
            "VIDEO_ACTION: <prompt>\n"
            "(repeat for each shot)"
        )

        # 构建 user message：narrative + all shots
        user_parts = []
        if narrative:
            user_parts.append(f"COMPLETE NARRATIVE:\n{narrative}")
            user_parts.append("")

        for i, shot in enumerate(all_shots):
            sid = shot.get("id", i + 1)
            user_parts.append(f"--- SHOT {sid} ---")
            ns = shot.get("narrative_segment", "")
            if ns:
                user_parts.append(f"Narrative segment: {ns}")
            sp = shot.get("scene_prompt") or shot.get("image_prompt", "")
            if sp:
                user_parts.append(f"Scene prompt: {sp}")
            ap = shot.get("action_prompt", "")
            if ap:
                user_parts.append(f"Action: {ap}")
            ef = shot.get("end_frame_description", "")
            if ef:
                user_parts.append(f"End frame: {ef}")
            dur = shot.get("estimated_duration", 8)
            user_parts.append(f"Duration: {dur}s")
            cm = shot.get("camera_movement", "")
            if cm:
                user_parts.append(f"Camera: {cm}")
            user_parts.append("")

        user_msg = "\n".join(user_parts)

        # 调用 LLM
        try:
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 4096,
                "temperature": 0.3,
                "stream": False,
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"全局 prompt 提取失败 ({resp.status}): {body[:200]}，fallback")
                        return fallback
                    data = await resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            if not content:
                logger.warning("全局 prompt 提取：LLM 返回空内容，fallback")
                return fallback

            # 解析输出 — 按 ===SHOT_{id}=== 分块
            result = dict(fallback)  # 以 fallback 为底，逐个覆盖
            import re
            blocks = re.split(r'===SHOT[_\s]*(\d+)===', content)
            # blocks: ['', '1', 'block1_content', '2', 'block2_content', ...]
            i = 1
            while i < len(blocks) - 1:
                try:
                    shot_id = int(blocks[i])
                except (ValueError, IndexError):
                    i += 2
                    continue
                block = blocks[i + 1]

                first_frame = ""
                last_frame = ""
                video_action = ""
                for line in block.split("\n"):
                    line = line.strip()
                    upper = line.upper()
                    if upper.startswith("FIRST_FRAME:"):
                        first_frame = line.split(":", 1)[1].strip()
                    elif upper.startswith("LAST_FRAME:"):
                        last_frame = line.split(":", 1)[1].strip()
                    elif upper.startswith("VIDEO_ACTION:"):
                        video_action = line.split(":", 1)[1].strip()

                if first_frame or last_frame or video_action:
                    extracted = {}
                    if first_frame:
                        extracted["first_frame_prompt"] = first_frame
                    else:
                        extracted["first_frame_prompt"] = fallback.get(shot_id, {}).get("first_frame_prompt", "")
                    if last_frame:
                        extracted["last_frame_prompt"] = last_frame
                    else:
                        extracted["last_frame_prompt"] = fallback.get(shot_id, {}).get("last_frame_prompt", "")
                    if video_action:
                        extracted["video_action_prompt"] = video_action
                    else:
                        extracted["video_action_prompt"] = fallback.get(shot_id, {}).get("video_action_prompt", "")
                    result[shot_id] = extracted
                    logger.info(f"shot_{shot_id}: 全局提取成功")
                    logger.debug(f"  first_frame: {first_frame[:80]}...")
                    logger.debug(f"  last_frame: {last_frame[:80]}...")
                    logger.debug(f"  video_action: {video_action[:80]}...")

                i += 2

            logger.info(f"全局 prompt 提取完成: {len(result)} shots")
            return result

        except Exception as e:
            logger.warning(f"全局 prompt 提取异常: {e}，fallback")
            return fallback

    async def _generate_shot(
        self, shot: dict[str, Any],
        provided_first_frame: Path | None = None,
        scene_context: dict[str, Any] | None = None,
        extracted_prompts: dict[int, dict[str, str]] | None = None,
        is_last_in_scene: bool = True,
    ) -> dict[str, Any]:
        """生成单镜头素材。

        Args:
            provided_first_frame: 上一镜头的尾帧图路径，直接复用为本镜头首帧。
            scene_context: 场景层级环境上下文（lighting, weather, props, environment_description）。
            extracted_prompts: 全局提取的 prompt dict {shot_id: {first_frame_prompt, last_frame_prompt, video_action_prompt}}。
            is_last_in_scene: 是否为 scene 最后一个 shot。只有 scene 末尾才生成尾帧图约束终点。
        """
        if scene_context is None:
            scene_context = {}
        if extracted_prompts is None:
            extracted_prompts = {}

        shot_id = int(shot.get("id", 0))
        # 兼容新旧字段名：scene_prompt（新）/ image_prompt（旧）
        scene_prompt = str(shot.get("scene_prompt") or shot.get("image_prompt", ""))
        end_frame_desc = str(shot.get("end_frame_description", ""))
        estimated_duration = int(shot.get("estimated_duration", 10))
        characters_in_shot = shot.get("characters_in_shot", [])
        consistency_anchors = shot.get("consistency_anchors")

        # 6D 增强字段
        camera_move = str(shot.get("camera_movement", ""))
        camera_tech = str(shot.get("camera_technical", ""))
        atmosphere = str(shot.get("atmosphere_lighting", ""))
        physics = str(shot.get("physics_note", ""))
        raw_action = str(shot.get("action_prompt", ""))

        # narration: 优先 narration，fallback 到 tts_text → subtitle
        narration = str(shot.get("narration", "") or shot.get("tts_text", "") or shot.get("subtitle", ""))

        style_anchor = str(self.storyboard.get("style_anchor", ""))

        # ── 单一真相源：从 characters 读取外貌 ──
        char_appearances = self._get_character_appearances(characters_in_shot)

        # ── 从全局提取结果获取首帧/尾帧/动作 prompt ──
        shot_prompts = extracted_prompts.get(shot_id, {})
        first_frame_text = shot_prompts.get("first_frame_prompt", "") or scene_prompt
        last_frame_text = shot_prompts.get("last_frame_prompt", "") or end_frame_desc
        video_action_text = shot_prompts.get("video_action_prompt", "") or raw_action

        # ── 构建结构化图片 prompt（首帧） ──
        image_prompt = VideoPromptBuilder.build_image_prompt(
            style_anchor=style_anchor,
            character_appearances=char_appearances,
            scene_description=first_frame_text,  # 全局提取的首帧视觉描述
            camera_technical=camera_tech,
            atmosphere=atmosphere,
            physics=physics,
            consistency_anchors=consistency_anchors,
            action_hint=raw_action,  # 让首帧为接下来的动作做好姿态准备
            scene_environment=scene_context.get("environment_description", ""),
            scene_lighting=scene_context.get("lighting", ""),
            scene_weather=scene_context.get("weather", ""),
            scene_props=scene_context.get("props"),
        )

        # ── 场景参考图（同场景所有镜头共享的视觉基底）──
        scene_image = scene_context.get("scene_image")

        # ── 首帧图：优先复用上一镜头尾帧，否则自己生成 ──
        if provided_first_frame and provided_first_frame.exists():
            image_path = provided_first_frame
            image_provider = "chained"
            logger.info(f"shot_{shot_id}: 复用上一镜头尾帧作为首帧")
        else:
            async with self.sem:
                image_path, image_provider = await self._generate_image(
                    image_prompt, shot_id,
                    characters_in_shot=characters_in_shot,
                    scene_image=scene_image,
                )

        # ── 尾帧图（只有 scene 末尾 shot 才生成，作为终点锚） ──
        end_frame_path: Path | None = None
        end_frame_provider = ""
        if is_last_in_scene and last_frame_text:
            end_prompt = VideoPromptBuilder.build_image_prompt(
                style_anchor=style_anchor,
                character_appearances=char_appearances,
                scene_description=last_frame_text,  # 全局提取的尾帧视觉描述
                camera_technical=camera_tech,
                atmosphere=atmosphere,
                physics=physics,
                consistency_anchors=consistency_anchors,
                scene_environment=scene_context.get("environment_description", ""),
                scene_lighting=scene_context.get("lighting", ""),
                scene_weather=scene_context.get("weather", ""),
                scene_props=scene_context.get("props"),
            )
            end_out = self.image_dir / f"shot_{shot_id:03d}_end.png"
            async with self.sem:
                end_frame_path, end_frame_provider = await self._generate_image(
                    end_prompt, shot_id, output_path=end_out,
                    characters_in_shot=characters_in_shot,
                    scene_image=scene_image,
                )
            logger.info(f"shot_{shot_id}: scene 末尾，生成尾帧图作为终点锚")
        elif not is_last_in_scene:
            logger.info(f"shot_{shot_id}: 非 scene 末尾，跳过尾帧图生成，Seedance 自由运动")

        # ── 构建结构化视频 prompt（使用全局提取的 video_action_prompt） ──
        video_prompt = VideoPromptBuilder.build_video_prompt(
            character_appearances=char_appearances,
            action_description=video_action_text,
            camera_movement=camera_move,
            consistency_anchors=consistency_anchors,
            narration=narration,
            scene_environment=scene_context.get("environment_description", ""),
        )

        # ── Seedance I2V：首帧 + 尾帧 + prompt → 视频片段 ──
        result = {
            "image": {"shot_id": shot_id, "path": str(image_path), "provider": image_provider},
        }

        if self.use_api and image_path.exists():
            video_path, video_provider = await self._generate_video(
                image_path, video_prompt, shot_id,
                estimated_duration=estimated_duration,
                last_frame_path=end_frame_path,
            )
            if video_path:
                result["video"] = {
                    "shot_id": shot_id,
                    "path": str(video_path),
                    "provider": video_provider,
                }

        # 返回尾帧路径，供下一镜头复用
        if end_frame_path and end_frame_path.exists():
            result["end_frame"] = {"shot_id": shot_id, "path": str(end_frame_path), "provider": end_frame_provider}

        return result

    # ── 视频生成（I2V） ────────────────────────────────────────────────

    async def _generate_video(
        self, image_path: Path, prompt: str, shot_id: int,
        estimated_duration: int = 10,
        last_frame_path: Path | None = None,
    ) -> tuple[Path | None, str]:
        """图生视频：dispatch + fallback_chain 模式。"""
        out = self.video_dir / f"shot_{shot_id:03d}.mp4"
        video_cfg = get_model_config("video")

        # ── Backward compat: 旧扁平格式 → 新嵌套格式 ──
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            old_provider = video_cfg["provider"]
            fallback_chain = [old_provider]
            max_retries = 1
            retry_delay = 5
        else:
            fallback_chain = video_cfg.get("fallback_chain", ["byteplus"])
            max_retries = video_cfg.get("max_retries", 2)
            retry_delay = video_cfg.get("retry_delay", 5)

        dispatch = {
            "byteplus": lambda img, p, o, dur, lf: self._video_seedance(
                img, p, o, duration=dur, last_frame_path=lf
            ),
        }

        for provider in fallback_chain:
            fn = dispatch.get(provider)
            if not fn:
                logger.warning(f"shot_{shot_id}: 未知视频 provider '{provider}'，跳过")
                continue

            # 读取 per-provider 配置（兼容旧格式）
            if "provider" in video_cfg and "fallback_chain" not in video_cfg:
                pcfg = video_cfg
            else:
                pcfg = video_cfg.get(provider, {})

            min_dur = pcfg.get("min_duration", 5)
            max_dur = pcfg.get("max_duration", 12)
            clamped = max(min_dur, min(max_dur, estimated_duration))
            logger.info(f"shot_{shot_id}: estimated_duration={estimated_duration}s → clamped={clamped}s (provider={provider})")

            for attempt in range(max_retries):
                if await fn(image_path, prompt, out, clamped, last_frame_path):
                    return out, provider
                if attempt < max_retries - 1:
                    logger.warning(f"shot_{shot_id}: {provider} 第 {attempt+1} 次失败，{retry_delay}s 后重试")
                    await asyncio.sleep(retry_delay)
            logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        logger.warning(f"视频生成失败 shot_{shot_id}，将使用静态图 fallback")
        return None, "none"

    async def _video_seedance(self, image_path: Path, prompt: str, output: Path, duration: int = 10, last_frame_path: Path | None = None) -> bool:
        """BytePlus Seedance I2V（图生视频），支持 batch 模式和 per-provider 配置。"""
        video_cfg = get_model_config("video")
        # Per-provider config: 新格式用 video_cfg["byteplus"]，旧格式用 video_cfg 自身
        if "byteplus" in video_cfg and isinstance(video_cfg["byteplus"], dict):
            pcfg = video_cfg["byteplus"]
        else:
            pcfg = video_cfg  # 旧扁平格式兼容

        creds = get_api_credentials("byteplus", self.cfg)
        if not creds.get("api_key"):
            logger.warning("byteplus api_key 未配置，跳过 Seedance")
            return False

        # narration 已在 VideoPromptBuilder.build_video_prompt() 中拼入 prompt

        try:
            # 图片转 base64 data URL
            import mimetypes
            mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
            img_b64 = base64.b64encode(image_path.read_bytes()).decode()
            data_url = f"data:{mime};base64,{img_b64}"

            api_base = creds["api_base"]
            api_key = creds["api_key"]

            # Batch 模式：model 以 -batch 结尾时启用 flex tier
            raw_model = pcfg.get("model", "seedance-1-5-pro-251215")
            batch_mode = raw_model.endswith("-batch")
            model = raw_model.removesuffix("-batch") if batch_mode else raw_model
            if batch_mode:
                logger.info(f"Seedance batch mode: {raw_model} → {model}")

            # 构建 content 数组：首帧 + (尾帧) + prompt
            content = [
                {"type": "image_url", "image_url": {"url": data_url}, "role": "first_frame"},
                {"type": "text", "text": prompt},
            ]
            if last_frame_path and last_frame_path.exists():
                end_mime = mimetypes.guess_type(str(last_frame_path))[0] or "image/png"
                end_b64 = base64.b64encode(last_frame_path.read_bytes()).decode()
                end_data_url = f"data:{end_mime};base64,{end_b64}"
                content.insert(1, {"type": "image_url", "image_url": {"url": end_data_url}, "role": "last_frame"})
                logger.info("Seedance: 已添加 last_frame 约束")

            # 1) 提交任务
            payload = {
                "model": model,
                "content": content,
                "duration": duration,
                "ratio": pcfg.get("ratio", "16:9"),
                "resolution": pcfg.get("resolution", "720p"),
                "generate_audio": pcfg.get("generate_audio", True),
            }
            if batch_mode:
                payload["service_tier"] = "flex"
                payload["execution_expires_after"] = 86400

            submit_timeout = pcfg.get("submit_timeout", 30)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=submit_timeout)) as session:
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
                        logger.warning(f"Seedance 提交失败 ({resp.status}): {body[:200]}")
                        return False
                    data = await resp.json()
                    task_id = data.get("id")
                    if not task_id:
                        logger.warning(f"Seedance 返回无 task_id: {data}")
                        return False
                    mode_tag = " [batch]" if batch_mode else ""
                    logger.info(f"Seedance{mode_tag} 任务已提交: {task_id}")

            # 2) 轮询任务
            poll_timeout = pcfg.get("poll_timeout", 15)
            poll_interval = pcfg.get("poll_interval", 5)
            default_poll_max = 720 if batch_mode else 60
            poll_max = pcfg.get("poll_max_attempts", default_poll_max)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=poll_timeout)) as session:
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
                                logger.info(f"Seedance 生成成功，下载视频...")
                                return await self._download(
                                    session, video_url, output
                                )
                            logger.warning("Seedance 成功但无 video_url")
                            return False

                        if status in ("failed", "cancelled"):
                            error = result.get("error")
                            logger.warning(f"Seedance 任务 {status}: {error}")
                            return False

                        # running / pending — 继续等
                        if attempt % 6 == 0:
                            elapsed = attempt * poll_interval
                            logger.info(f"Seedance{mode_tag} 生成中... ({elapsed}s)")

            logger.warning(f"Seedance 超时（{poll_max * poll_interval}s）")
            return False

        except Exception as e:
            logger.warning(f"Seedance 异常: {e}")
            return False

    async def _generate_image(self, prompt: str, shot_id: int, output_path: Path | None = None, characters_in_shot: list[str] | None = None, scene_image: Path | None = None) -> tuple[Path, str]:
        """生成图片。同一模型重试最多 3 次，不切换到其他风格不同的模型。"""
        out = output_path or (self.image_dir / f"shot_{shot_id:03d}.png")
        img_cfg = get_model_config("image")
        max_retries = img_cfg.get("max_retries", 3)
        retry_delay = img_cfg.get("retry_delay", 5)
        fallback_chain = img_cfg.get("fallback_chain", ["apimart", "volcengine", "fal"])

        if self.use_api:
            dispatch = {
                "volcengine": lambda p, o: self._image_volcengine(p, o, characters_in_shot=characters_in_shot or [], scene_image=scene_image),
                "apimart": lambda p, o: self._image_apimart(p, o, characters_in_shot=characters_in_shot or [], scene_image=scene_image),
                "fal": lambda p, o: self._image_flux(p, o),
            }
            for provider in fallback_chain:
                fn = dispatch.get(provider)
                if not fn:
                    continue
                # 同一 provider 重试多次
                for attempt in range(max_retries):
                    if await fn(prompt, out):
                        return out, provider
                    if attempt < max_retries - 1:
                        logger.warning(f"shot_{shot_id}: {provider} 第 {attempt+1} 次失败，{retry_delay}s 后重试")
                        await asyncio.sleep(retry_delay)
                logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        self._placeholder_image(out, shot_id, prompt)
        return out, "placeholder"

    async def _image_volcengine(self, prompt: str, output: Path, characters_in_shot: list[str] | None = None, scene_image: Path | None = None) -> bool:
        """Volcengine Seedream 图像生成（支持角色参考图）。"""
        img_cfg = get_model_config("image").get("volcengine", {})
        creds = get_api_credentials("volcengine", self.cfg)
        if not creds.get("api_key"):
            return False

        # 构建参考图 image_urls（角色参考图 + 场景参考图）
        image_urls: list[str] = []
        characters_cfg = self.storyboard.get("characters", {})
        ref_dir = self.storyboard.get("character_ref_dir", "")
        for char_id in (characters_in_shot or []):
            char_info = characters_cfg.get(char_id, {})
            ref_file = char_info.get("ref_image", "")
            if ref_file and ref_dir:
                ref_path = Path(ref_dir) / ref_file
                if ref_path.exists():
                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                    image_urls.append(f"data:{mime};base64,{img_b64}")

        if scene_image and scene_image.exists():
            mime = mimetypes.guess_type(str(scene_image))[0] or "image/png"
            img_b64 = base64.b64encode(scene_image.read_bytes()).decode()
            image_urls.append(f"data:{mime};base64,{img_b64}")

        try:
            timeout = img_cfg.get("timeout", 120)
            payload = {
                "model": img_cfg.get("model", "doubao-seedream-4-0-250828"),
                "prompt": prompt,
                "size": img_cfg.get("size", "2K"),
                "response_format": img_cfg.get("response_format", "url"),
                "watermark": img_cfg.get("watermark", True),
            }
            if image_urls:
                payload["image_urls"] = image_urls

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/images/generations",
                    headers={"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    url = data.get("data", [{}])[0].get("url")
                    if url:
                        return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"volcengine 失败: {e}")
        return False

    async def _image_apimart(self, prompt: str, output: Path, characters_in_shot: list[str] | None = None, scene_image: Path | None = None) -> bool:
        """ApiMart 图像生成 — Gemini 模型走 chat/completions，其他走 images/generations。"""
        img_cfg = get_model_config("image").get("apimart", {})
        creds = get_api_credentials("apimart", self.cfg)
        if not creds.get("api_key"):
            return False

        model = img_cfg.get("model", "gemini-3.1-flash-image-preview")
        is_gemini = "gemini" in model.lower()

        try:
            timeout = img_cfg.get("timeout", 180)
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                if is_gemini:
                    # 构建多模态 content：场景参考图 + 角色参考图 + 结构化 prompt
                    user_content: list[dict] | str = []
                    characters_cfg = self.storyboard.get("characters", {})
                    chars_to_use = characters_in_shot or []
                    ref_dir = self.storyboard.get("character_ref_dir", "")

                    has_refs = False
                    if (chars_to_use and ref_dir) or scene_image:
                        user_content = []

                        # 场景参考图（纯环境，同场景共享的视觉基底）
                        if scene_image and scene_image.exists():
                            mime = mimetypes.guess_type(str(scene_image))[0] or "image/png"
                            img_b64 = base64.b64encode(scene_image.read_bytes()).decode()
                            user_content.append({"type": "text", "text": "SCENE REFERENCE — This is the environment/background. Place characters INTO this exact scene. Keep the same architecture, lighting, weather, and props."})
                            user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                            has_refs = True

                        # 角色参考图
                        if chars_to_use and ref_dir:
                            ref_base = Path(ref_dir)
                            for char_id in chars_to_use:
                                char_info = characters_cfg.get(char_id, {})
                                ref_file = char_info.get("ref_image", "")
                                ref_desc = char_info.get("ref_description", char_info.get("appearance", ""))
                                ref_path = ref_base / ref_file if ref_file else None

                                if ref_path and ref_path.exists():
                                    mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                                    img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                                    user_content.append({"type": "text", "text": f"CHARACTER REFERENCE — {char_id}: {ref_desc}"})
                                    user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
                                    has_refs = True

                        instruction = (
                            "Now generate a NEW cinematic scene image. "
                        )
                        if scene_image and scene_image.exists():
                            instruction += (
                                "Use the SCENE REFERENCE image as the environment base — keep the same buildings, bridge, pavilion, landscape, lighting, and weather. "
                                "Place the characters INTO this environment. "
                            )
                        instruction += (
                            "CRITICAL RULES: "
                            "1) Match each character's IDENTITY from their reference (face, clothing, proportions, details) but render in the TARGET ART STYLE specified below — do NOT copy the reference image's rendering style. "
                            "2) Keep the environment visually consistent with the scene reference. "
                            "3) Do NOT include any text, labels, or annotations. "
                            "4) Create a completely new cinematic composition.\n\n"
                            f"{prompt}"
                        )
                        user_content.append({"type": "text", "text": instruction})

                        if not has_refs:
                            user_content = f"Generate an image: {prompt}"
                    else:
                        user_content = f"Generate an image: {prompt}"

                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": GEMINI_IMAGE_SYSTEM_PROMPT},
                            {"role": "user", "content": user_content}
                        ],
                        "max_tokens": 4096,
                        "stream": False,
                    }
                    async with session.post(
                        f"{creds['api_base']}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.warning(f"apimart gemini 返回 {resp.status}: {body[:200]}")
                            return False
                        data = await resp.json()
                        # 从 choices[0].message.content 中提取图片
                        choices = data.get("choices", [])
                        if not choices:
                            return False
                        msg = choices[0].get("message", {})
                        content = msg.get("content", "")

                        # content 可能是 str 或 list（multimodal）
                        if isinstance(content, str):
                            # Gemini 返回 markdown 格式: ![image](data:image/jpeg;base64,...)
                            import re
                            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)', content)
                            if m:
                                b64_str = m.group(1).replace('\n', '').replace(' ', '')
                                output.write_bytes(base64.b64decode(b64_str))
                                return True
                            # 也可能直接返回 URL
                            m_url = re.search(r'https?://\S+', content)
                            if m_url:
                                return await self._download(session, m_url.group(0), output)
                            logger.warning(f"apimart gemini 返回内容无图片: {content[:200]}")
                            return False
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict):
                                    if part.get("type") == "image_url":
                                        url_or_b64 = part.get("image_url", {}).get("url", "")
                                        if url_or_b64.startswith("data:"):
                                            b64_str = url_or_b64.split(",", 1)[1]
                                            output.write_bytes(base64.b64decode(b64_str))
                                            return True
                                        elif url_or_b64.startswith("http"):
                                            return await self._download(session, url_or_b64, output)
                            return False
                        return False
                else:
                    # 非 Gemini 模型走标准 images/generations 接口
                    poll_interval = img_cfg.get("poll_interval", 5)
                    poll_max = img_cfg.get("poll_max_attempts", 30)
                    async with session.post(
                        f"{creds['api_base']}/images/generations",
                        headers=headers,
                        json={"model": model, "prompt": prompt, "size": f"{self.image_width}x{self.image_height}", "n": 1},
                    ) as resp:
                        if resp.status != 200:
                            return False
                        data = await resp.json()
                        first = data.get("data", [{}])[0]

                        # 异步任务模式
                        if task_id := first.get("task_id"):
                            for _ in range(poll_max):
                                await asyncio.sleep(poll_interval)
                                async with session.get(f"{creds['api_base']}/tasks/{task_id}", headers=headers) as poll:
                                    if poll.status == 200:
                                        task_data = await poll.json()
                                        if task_data.get("data", {}).get("status") == "completed":
                                            url_val = task_data.get("data", {}).get("result", {}).get("images", [{}])[0].get("url")
                                            if isinstance(url_val, list):
                                                url_val = url_val[0] if url_val else None
                                            if url_val:
                                                return await self._download(session, url_val, output)
                            return False

                        # 同步模式
                        url = first.get("url")
                        if isinstance(url, list):
                            url = url[0] if url else None
                        if url:
                            return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"apimart 失败: {e}")
        return False

    # ─── Video generation (Seedance I2V) ───────────────────────────────

    # NOTE: 第二组 _generate_video / _video_seedance 已废弃，
    # 使用上方的版本（支持 estimated_duration 动态映射）。

    # ─── Image generation (Flux via fal.ai) ──────────────────────────

    async def _image_flux(self, prompt: str, output: Path) -> bool:
        """Fal.ai Flux 图像生成。"""
        img_cfg = get_model_config("image").get("fal", {})
        creds = get_api_credentials("fal", self.cfg)
        if not creds.get("api_key"):
            return False

        timeout = img_cfg.get("timeout", 180)
        poll_interval = img_cfg.get("poll_interval", 1)
        poll_max = img_cfg.get("poll_max_attempts", 90)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}/fal-ai/flux/schnell",
                    headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                    json={
                        "prompt": prompt,
                        "image_size": {"width": self.image_width, "height": self.image_height},
                        "num_inference_steps": img_cfg.get("num_inference_steps", 4),
                        "num_images": img_cfg.get("num_images", 1),
                    },
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()

                    # 直接返回
                    if data.get("images"):
                        url = data["images"][0].get("url")
                        if url:
                            return await self._download(session, url, output)

                    # 轮询模式
                    if request_id := data.get("request_id"):
                        for _ in range(poll_max):
                            await asyncio.sleep(poll_interval)
                            async with session.get(f"{creds['api_base']}/fal-ai/flux/schnell/requests/{request_id}", headers={"Authorization": f"Key {creds['api_key']}"}) as poll:
                                if poll.status == 200:
                                    result = await poll.json()
                                    if result.get("images"):
                                        url = result["images"][0].get("url")
                                        if url:
                                            return await self._download(session, url, output)
        except Exception as e:
            logger.warning(f"flux 失败: {e}")
        return False

    async def _generate_tts(self, text: str, shot_id: int) -> tuple[Path, str]:
        """生成语音：Edge TTS → 静音。"""
        output = self.voice_dir / f"shot_{shot_id:03d}.wav"
        text = text.strip() or "请关注"

        if self.use_api and await self._tts_edge(text, output):
            return output, "edge-tts"

        self._silence_wav(output, 2)
        return output, "silence"

    async def _tts_edge(self, text: str, output: Path) -> bool:
        """Edge TTS 生成语音。"""
        try:
            import edge_tts

            tts_cfg = get_model_config("tts")
            voice = os.getenv("EDGE_TTS_VOICE", tts_cfg.get("voice", "zh-CN-XiaoxiaoNeural"))
            await edge_tts.Communicate(text=text, voice=voice).save(str(output))
            return output.exists() and output.stat().st_size > 0
        except Exception as e:
            logger.warning(f"edge-tts 失败: {e}")
            return False

    async def _generate_bgm(self, style: str, duration: int) -> tuple[Path, str]:
        """生成 BGM：MiniMax → 静音。"""
        output = self.bgm_dir / "bgm.wav"

        if self.use_api and await self._bgm_minimax(style, duration, output):
            return output, "minimax"

        self._silence_wav(output, duration)
        return output, "silence"

    async def _bgm_minimax(self, style: str, duration: int, output: Path) -> bool:
        """MiniMax BGM 生成（via fal.ai）。"""
        bgm_cfg = get_model_config("bgm")
        creds = get_api_credentials(bgm_cfg.get("provider", "fal"), self.cfg)
        if not creds.get("api_key"):
            return False

        timeout = bgm_cfg.get("timeout", 240)
        poll_interval = bgm_cfg.get("poll_interval", 1)
        poll_max = bgm_cfg.get("poll_max_attempts", 120)
        endpoint = bgm_cfg.get("endpoint", "/fal-ai/minimax-music/v2")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{creds['api_base']}{endpoint}",
                    headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                    json={"prompt": f"{style}, instrumental, no vocals", "lyrics_prompt": "[Instrumental]", "duration": duration},
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()

                    # 直接返回
                    if audio := data.get("audio", {}).get("url"):
                        return await self._download(session, audio, output)

                    # 轮询
                    poll_endpoint = endpoint.replace("/v2", "")
                    if request_id := data.get("request_id"):
                        for _ in range(poll_max):
                            await asyncio.sleep(poll_interval)
                            async with session.get(f"{creds['api_base']}{poll_endpoint}/requests/{request_id}", headers={"Authorization": f"Key {creds['api_key']}"}) as poll:
                                if poll.status == 200:
                                    result = await poll.json()
                                    if audio := result.get("audio", {}).get("url"):
                                        return await self._download(session, audio, output)
        except Exception as e:
            logger.warning(f"minimax 失败: {e}")
        return False

    async def _download(self, session: aiohttp.ClientSession, url: str, output: Path) -> bool:
        """下载文件。"""
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    output.write_bytes(await resp.read())
                    return output.exists() and output.stat().st_size > 0
        except Exception as e:
            logger.warning(f"下载失败: {e}")
        return False

    def _placeholder_image(self, output: Path, shot_id: int, prompt: str) -> None:
        """生成占位图。"""
        img = Image.new("RGB", (self.image_width, self.image_height), (21, 27, 38))
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 16, self.image_width - 16, self.image_height - 16), outline=(255, 180, 0), width=4)
        draw.multiline_text((40, 50), f"SHOT {shot_id}\n{prompt[:120]}", fill=(240, 240, 240))
        img.save(output)

    def _silence_wav(self, path: Path, seconds: int) -> None:
        """生成静音 WAV。"""
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(b"\x00\x00" * (16000 * seconds))


# ── 角色参考图生成 ──────────────────────────────────────────────

class CharacterRefGenerator:
    """从 framework.json 生成角色参考图。"""

    def __init__(
        self,
        framework: dict[str, Any],
        output_dir: Path,
        use_api: bool = True,
    ):
        self.framework = framework
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.use_api = use_api
        self.cfg = load_external_api_config()

    def _build_ref_prompt(self, character: dict[str, Any]) -> str:
        """构建角色参考图 prompt。"""
        name = character.get("name", "Character")
        appearance = character.get("appearance", "")
        clothing = character.get("default_clothing", "")
        key_features = character.get("key_features", [])
        features_str = ", ".join(key_features) if key_features else ""
        style_anchor = self.framework.get("visual_style_anchor", "")

        style_instruction = (
            f"ART STYLE (MANDATORY): {style_anchor}. "
            f"The character must be rendered in this exact art style. "
            if style_anchor
            else "Photorealistic cinematic style. "
        )

        return (
            f"Character design reference sheet on a plain white background. "
            f"Full body front view and 3/4 side view of {name}. "
            f"{style_instruction}"
            f"{appearance}. {clothing}. "
            f"Key identifying features: {features_str}. "
            f"Highly detailed, no text labels, "
            f"no annotations, no background elements, studio lighting, "
            f"sharp focus on character details and proportions."
        )

    def _character_id(self, character: dict[str, Any]) -> str:
        """从角色信息生成 ID（英文小写下划线）。"""
        cid = character.get("id", "")
        if cid:
            return cid
        # fallback: 从 name 生成
        name = character.get("name", "char")
        return name.lower().replace(" ", "_").replace("-", "_")

    async def generate(self, character_id_filter: str | None = None) -> dict[str, Any]:
        """生成角色参考图。"""
        characters = self.framework.get("suggested_characters", [])
        if not characters:
            raise ValueError("framework.json 缺少 suggested_characters")

        results = {}
        for char in characters:
            cid = self._character_id(char)
            if character_id_filter and cid != character_id_filter:
                continue

            out_path = self.output_dir / f"ref_{cid}.png"
            prompt = self._build_ref_prompt(char)
            logger.info(f"生成角色参考图: {cid}")

            if self.use_api:
                success = await self._generate_ref_image(prompt, out_path)
                provider = "apimart" if success else "placeholder"
            else:
                success = False
                provider = "placeholder"

            if not success:
                # 生成占位图
                img = Image.new("RGB", (1024, 1024), (255, 255, 255))
                draw = ImageDraw.Draw(img)
                draw.rectangle((16, 16, 1008, 1008), outline=(200, 200, 200), width=2)
                draw.multiline_text(
                    (40, 50),
                    f"CHARACTER REF\n{cid}\n{char.get('name', '')}\n\n{prompt[:200]}",
                    fill=(100, 100, 100),
                )
                img.save(out_path)
                provider = "placeholder"

            results[cid] = {
                "ref_image": out_path.name,
                "path": str(out_path),
                "provider": provider,
                "ref_description": char.get("ref_description", ""),
            }
            logger.info(f"角色参考图完成: {cid} → {out_path} ({provider})")

        return {
            "character_ref_dir": str(self.output_dir),
            "characters": results,
        }

    async def _generate_ref_image(self, prompt: str, output: Path) -> bool:
        """调用图片 API 生成参考图。"""
        img_cfg = get_model_config("image")
        # 优先用 apimart（Gemini）
        apimart_cfg = img_cfg.get("apimart", {})
        creds = get_api_credentials("apimart", self.cfg)
        if not creds.get("api_key"):
            return False

        model = apimart_cfg.get("model", "gemini-3.1-flash-image-preview")
        is_gemini = "gemini" in model.lower()
        timeout = apimart_cfg.get("timeout", 180)
        headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                if is_gemini:
                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": f"Generate an image: {prompt}"}],
                        "max_tokens": 4096,
                        "stream": False,
                    }
                    async with session.post(
                        f"{creds['api_base']}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            logger.warning(f"角色参考图生成失败 ({resp.status}): {body[:200]}")
                            return False
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if not choices:
                            return False
                        content = choices[0].get("message", {}).get("content", "")
                        if isinstance(content, str):
                            import re
                            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)', content)
                            if m:
                                b64_str = m.group(1).replace('\n', '').replace(' ', '')
                                output.write_bytes(base64.b64decode(b64_str))
                                return True
                        return False
                else:
                    async with session.post(
                        f"{creds['api_base']}/images/generations",
                        headers=headers,
                        json={"model": model, "prompt": prompt, "size": "1024x1024", "n": 1},
                    ) as resp:
                        if resp.status != 200:
                            return False
                        data = await resp.json()
                        url = data.get("data", [{}])[0].get("url")
                        if url:
                            async with session.get(url) as dl:
                                if dl.status == 200:
                                    output.write_bytes(await dl.read())
                                    return True
                        return False
        except Exception as e:
            logger.warning(f"角色参考图 API 异常: {e}")
            return False


# ── CLI 入口 ─────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="素材生成（图片/视频/BGM/角色参考图）")
    parser.add_argument("--mode", choices=["assets", "character_refs"], default="assets",
                        help="运行模式：assets（默认，生成镜头素材）| character_refs（生成角色参考图）")
    parser.add_argument("--storyboard", help="storyboard.json 路径（assets 模式必需）")
    parser.add_argument("--framework", help="framework.json 路径（character_refs 模式必需）")
    parser.add_argument("--output_dir", required=True, help="素材输出目录")
    parser.add_argument("--character_id", help="只生成指定角色的参考图（character_refs 模式可选）")
    parser.add_argument("--image_width", type=int, default=1024, help="图片宽度")
    parser.add_argument("--image_height", type=int, default=1024, help="图片高度")
    parser.add_argument("--parallel", type=int, default=4, help="并行任务数")
    parser.add_argument("--no_api", action="store_true", help="禁用外部 API，生成占位素材")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).expanduser().resolve()

    if args.mode == "character_refs":
        # ── 角色参考图模式 ──
        if not args.framework:
            raise SystemExit("--mode character_refs 需要 --framework 参数")
        framework_path = Path(args.framework).expanduser().resolve()
        with open(framework_path, encoding="utf-8") as f:
            framework = json.load(f)

        gen = CharacterRefGenerator(
            framework=framework,
            output_dir=output_dir,
            use_api=not args.no_api,
        )
        result = await gen.generate(character_id_filter=args.character_id)

        result_path = output_dir / "character_refs.json"
        write_json(result_path, result)
        print(json.dumps({"character_refs_json": str(result_path)}, ensure_ascii=False))

    else:
        # ── 素材生成模式（默认） ──
        if not args.storyboard:
            raise SystemExit("--mode assets 需要 --storyboard 参数")
        storyboard_path = Path(args.storyboard).expanduser().resolve()
        with open(storyboard_path, encoding="utf-8") as f:
            storyboard = json.load(f)

        generator = AssetGenerator(
            storyboard=storyboard,
            output_root=output_dir,
            image_width=args.image_width,
            image_height=args.image_height,
            parallel=args.parallel,
            use_api=not args.no_api,
        )
        assets = await generator.run()

        assets_path = output_dir / "assets.json"
        write_json(assets_path, assets)
        print(json.dumps({"assets_json": str(assets_path)}, ensure_ascii=False))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
