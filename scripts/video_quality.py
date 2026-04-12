from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

DETECTOR_VERSION = "quality_profiles_v5"

QUALITY_PROFILES: dict[str, dict[str, float | int]] = {
    "static": {
        "threshold_base": 250,
        "threshold_multiplier": 6,
        "required_stable": 6,
        "consecutive_bad": 8,
        "grace_window_frames": 8,
        "confirm_bad_frames": 6,
        "trim_backoff_frames": 2,
        "min_amplitude_base": 80,
        "min_amplitude_multiplier": 4,
        "reversal_count": 8,
        "spike_factor": 2.0,
        "spike_abs_min": 120,
    },
    "medium_motion": {
        "threshold_base": 350,
        "threshold_multiplier": 10,
        "required_stable": 6,
        "consecutive_bad": 14,
        "grace_window_frames": 12,
        "confirm_bad_frames": 8,
        "trim_backoff_frames": 3,
        "min_amplitude_base": 120,
        "min_amplitude_multiplier": 5,
        "reversal_count": 10,
        "spike_factor": 3.0,
        "spike_abs_min": 180,
    },
    "heavy_motion": {
        "threshold_base": 500,
        "threshold_multiplier": 14,
        "required_stable": 8,
        "consecutive_bad": 20,
        "grace_window_frames": 18,
        "confirm_bad_frames": 10,
        "trim_backoff_frames": 4,
        "min_amplitude_base": 180,
        "min_amplitude_multiplier": 6,
        "reversal_count": 12,
        "spike_factor": 3.5,
        "spike_abs_min": 250,
    },
}


def quality_profile_for_camera(camera_movement: str) -> str:
    movement = camera_movement.strip().lower()
    if not movement or movement == "static":
        return "static"
    if any(token in movement for token in ["orbital", "crane", "whip", "rapid", "fast", "handheld", "tracking"]):
        return "heavy_motion"
    return "medium_motion"


def extract_video_last_frame(video_path: Path, output_path: Path) -> Path | None:
    """用 ffmpeg 提取视频的最后一帧作为图片。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(output_path)],
            check=True,
            capture_output=True,
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"提取视频尾帧失败: {e}")
    return None


def scan_video_quality(
    video_path: Path,
    min_keep: float = 3.0,
    audit_dir: Path | None = None,
    attempt: int = 1,
    camera_movement: str = "",
    expected_character_count: int = 0,
    expected_subject_facing: str = "",
    review_mode: str = "metrics_only",
) -> dict[str, Any]:
    """全帧扫描视频质量，找到最后一个稳定点。"""
    profile_name = quality_profile_for_camera(camera_movement)
    profile = QUALITY_PROFILES[profile_name]
    result: dict[str, Any] = {
        "ok": True,
        "needs_regeneration": False,
        "trim_to": None,
        "duration": 0,
        "fps": 24,
        "profile": profile_name,
        "trigger": "passed",
        "analysis": {},
        "bad_segments": [],
        "cut_segments": [],
        "risk_segments": [],
    }

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
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

    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_pattern = str(Path(tmp_dir) / "frame_%04d.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-vf", "scale=160:-1", "-q:v", "2", frame_pattern],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"质量检测: 帧提取失败 {e}")
            return result

        frame_files = sorted(Path(tmp_dir).glob("frame_*.png"))
        if len(frame_files) < 6:
            return result

        frames: list[Image.Image] = []
        frame_arrays: list[np.ndarray] = []
        for ff in frame_files:
            try:
                img = Image.open(ff).convert("RGB")
                frames.append(img)
                frame_arrays.append(np.asarray(img, dtype=np.float32))
            except Exception as e:
                logger.warning(f"质量检测: 帧 {ff.name} 加载失败: {e}")
                continue

        if len(frames) < 6:
            return result

        diffs: list[float] = []
        for j in range(1, len(frame_arrays)):
            diff = frame_arrays[j] - frame_arrays[j - 1]
            mse = float(np.mean(diff * diff))
            diffs.append(mse)

        if not diffs:
            return result

        stable_count = max(3, len(diffs) // 4)
        sorted_stable = sorted(diffs[:stable_count])
        median_diff = sorted_stable[len(sorted_stable) // 2]

        threshold = max(float(profile["threshold_base"]), median_diff * float(profile["threshold_multiplier"]))
        result["analysis"].update(
            {
                "median_diff": median_diff,
                "threshold": threshold,
                "stable_sample_count": stable_count,
            }
        )

        diffs_arr = np.array(diffs)
        n = len(diffs)
        smoothed_arr = diffs_arr.copy()
        for offset in (-2, -1, 1, 2):
            lo = max(0, offset)
            hi = min(0, offset)
            src_start = max(0, -offset)
            src_end = n + min(0, -offset)
            smoothed_arr[lo : n + hi if hi else n] = np.maximum(
                smoothed_arr[lo : n + hi if hi else n],
                diffs_arr[src_start:src_end],
            )
        smoothed: list[float] = smoothed_arr.tolist()

        required_stable = int(profile["required_stable"])
        stable_run = 0
        last_stable_idx = len(smoothed)
        reverse_first_bad_idx = None

        for j in range(len(smoothed) - 1, -1, -1):
            if smoothed[j] <= threshold:
                stable_run += 1
                if stable_run >= required_stable:
                    last_stable_idx = j + stable_run
                    break
            else:
                reverse_first_bad_idx = j
                stable_run = 0

        if stable_run < required_stable:
            last_stable_idx = 0
            for j in range(len(smoothed)):
                if smoothed[j] <= threshold:
                    last_stable_idx = j + 1
                else:
                    break

        consecutive_bad = 0
        first_bad_start = None
        confirmed_bad_start = None
        grace_window = int(profile["grace_window_frames"])
        confirm_bad_frames = int(profile["confirm_bad_frames"])
        trim_backoff_frames = int(profile["trim_backoff_frames"])
        for j in range(len(smoothed)):
            if smoothed[j] > threshold:
                if consecutive_bad == 0:
                    first_bad_start = j
                consecutive_bad += 1
            else:
                if consecutive_bad >= int(profile["consecutive_bad"]) and first_bad_start is not None:
                    confirm_start = min(len(smoothed), first_bad_start + grace_window)
                    confirm_slice = smoothed[confirm_start:]
                    confirm_hits = 0
                    for value in confirm_slice:
                        if value > threshold:
                            confirm_hits += 1
                            if confirm_hits >= confirm_bad_frames:
                                confirmed_bad_start = confirm_start
                                break
                        else:
                            confirm_hits = 0
                    candidate_idx = max(0, (confirmed_bad_start or first_bad_start) - trim_backoff_frames)
                    forward_keep = (candidate_idx + 1) / fps
                    if forward_keep < (last_stable_idx + 1) / fps and confirmed_bad_start is not None:
                        last_stable_idx = candidate_idx
                        result["trigger"] = "forward_mse_run"
                        logger.info(
                            f"质量检测: 正向扫描确认异常段 @{forward_keep:.1f}s "
                            f"(start={((first_bad_start + 1) / fps):.1f}s, confirm={((confirmed_bad_start + 1) / fps):.1f}s)"
                        )
                    break
                consecutive_bad = 0
                first_bad_start = None
        else:
            if consecutive_bad >= int(profile["consecutive_bad"]) and first_bad_start is not None:
                confirm_start = min(len(smoothed), first_bad_start + grace_window)
                confirm_slice = smoothed[confirm_start:]
                confirm_hits = 0
                for value in confirm_slice:
                    if value > threshold:
                        confirm_hits += 1
                        if confirm_hits >= confirm_bad_frames:
                            confirmed_bad_start = confirm_start
                            break
                    else:
                        confirm_hits = 0
                candidate_idx = max(0, (confirmed_bad_start or first_bad_start) - trim_backoff_frames)
                forward_keep = (candidate_idx + 1) / fps
                if forward_keep < (last_stable_idx + 1) / fps and confirmed_bad_start is not None:
                    last_stable_idx = candidate_idx
                    result["trigger"] = "forward_mse_run"

        keep_time = (last_stable_idx + 1) / fps
        first_over_threshold_idx = next((idx for idx, value in enumerate(smoothed) if value > threshold), None)
        result["analysis"].update(
            {
                "required_stable": required_stable,
                "reverse_first_bad_time": ((reverse_first_bad_idx + 1) / fps if reverse_first_bad_idx is not None else None),
                "forward_bad_start_time": ((first_bad_start + 1) / fps if first_bad_start is not None else None),
                "forward_confirm_time": ((confirmed_bad_start + 1) / fps if confirmed_bad_start is not None else None),
                "first_over_threshold_time": (
                    (first_over_threshold_idx + 1) / fps if first_over_threshold_idx is not None else None
                ),
                "preliminary_keep_time": keep_time,
                "grace_window_frames": grace_window,
                "confirm_bad_frames": confirm_bad_frames,
                "trim_backoff_frames": trim_backoff_frames,
            }
        )

        motion_ramp_exempt = False
        motion_ramp_start = first_bad_start
        if motion_ramp_start is not None and motion_ramp_start < len(smoothed) - 8 and profile_name in {"medium_motion", "heavy_motion"}:
            ramp_segment = smoothed_arr[motion_ramp_start:]
            ramp_deltas = np.diff(ramp_segment)
            up_steps = int(np.sum(ramp_deltas >= 0))
            large_drops = int(np.sum(ramp_deltas < -threshold * 0.2))
            total_steps = max(1, len(ramp_segment) - 1)
            ramp_up_ratio = up_steps / total_steps
            ramp_peak = float(np.max(ramp_segment))
            motion_ramp_exempt = ramp_up_ratio >= 0.6 and large_drops <= 1 and ramp_peak >= threshold * 1.2
            result["analysis"]["motion_ramp"] = {
                "start_time": (motion_ramp_start + 1) / fps,
                "up_ratio": ramp_up_ratio,
                "large_drops": large_drops,
                "peak_smoothed": ramp_peak,
                "exempted": motion_ramp_exempt,
            }

        min_amplitude = max(float(profile["min_amplitude_base"]), median_diff * float(profile["min_amplitude_multiplier"]))
        if n >= 3:
            deltas = np.diff(diffs_arr)
            directions = deltas > 0
            amplitudes = np.abs(deltas)
            is_reversal = (directions[1:] != directions[:-1]) & (amplitudes[1:] > min_amplitude)
        else:
            is_reversal = np.array([], dtype=bool)

        reversals = 0
        reversal_count_threshold = int(profile["reversal_count"])
        for j in range(len(is_reversal) - 1, -1, -1):
            if is_reversal[j]:
                reversals += 1
            else:
                if reversals >= reversal_count_threshold:
                    flicker_start_time = (j + 3) / fps
                    if flicker_start_time < keep_time:
                        keep_time = flicker_start_time
                        result["trigger"] = "flicker"
                        logger.info(f"质量检测: 检测到闪烁 ({reversals} 次反转 @{flicker_start_time:.1f}s)，裁剪到 {keep_time:.1f}s")
                reversals = 0

        if reversals >= reversal_count_threshold:
            flicker_start_time = 2 / fps
            if flicker_start_time < keep_time:
                keep_time = flicker_start_time
                result["trigger"] = "flicker"

        spike_factor = float(profile["spike_factor"])
        spike_abs_min = float(profile["spike_abs_min"])
        spike_window = 12
        spike_count = 0
        first_spike_time = None
        cumsum = np.concatenate(([0.0], np.cumsum(diffs_arr)))
        for j in range(n):
            w_start = max(0, j - spike_window)
            w_end = min(n, j + spike_window + 1)
            window_sum = cumsum[w_end] - cumsum[w_start]
            window_count = w_end - w_start - 1
            if window_count <= 0:
                continue
            local_mean = (window_sum - diffs_arr[j]) / window_count
            if diffs_arr[j] > max(spike_abs_min, local_mean * spike_factor):
                spike_count += 1
                ts = (j + 1) / fps
                if first_spike_time is None:
                    first_spike_time = ts
                logger.debug(
                    f"质量检测: 局部突变 @{ts:.2f}s "
                    f"(MSE={diffs_arr[j]:.0f}, 局部均值={local_mean:.0f}, 倍率={diffs_arr[j]/local_mean:.1f}x)"
                )

        if spike_count >= 2:
            spike_trim = first_spike_time - 0.1
            if spike_trim > 0 and spike_trim < keep_time:
                keep_time = spike_trim
                result["trigger"] = "spike"
                logger.info(f"质量检测: 检测到 {spike_count} 个局部突变，首个 @{first_spike_time:.1f}s，裁剪到 {keep_time:.1f}s")
        result["analysis"]["first_spike_time"] = first_spike_time

        semantic_segments = semantic_audit_video(
            video_path=video_path,
            fps=fps,
            duration=duration,
            expected_character_count=expected_character_count,
            expected_subject_facing=expected_subject_facing,
        )
        result["bad_segments"] = semantic_segments
        risk_segments: list[dict[str, Any]] = []
        if semantic_segments:
            result["analysis"]["semantic_reasons"] = [segment["reason"] for segment in semantic_segments]
            risk_segments.extend(dict(segment) for segment in semantic_segments)
            tail_segment = next(
                (segment for segment in semantic_segments if segment["end"] >= duration - 0.3 and segment["start"] >= min_keep),
                None,
            )
            cut_segment = next(
                (
                    segment
                    for segment in semantic_segments
                    if segment["end"] < duration - 0.3 and duration - (segment["end"] - segment["start"]) >= min_keep
                ),
                None,
            )
            if tail_segment is not None:
                keep_time = min(keep_time, float(tail_segment["start"]))
                result["trigger"] = str(tail_segment["reason"])
            elif cut_segment is not None:
                result["cut_segments"] = [cut_segment]
                result["trigger"] = str(cut_segment["reason"])

        try:
            import cv2

            model_dir = Path(__file__).parent.parent / "models"
            proto = model_dir / "deploy.prototxt"
            weights = model_dir / "res10_300x300_ssd_iter_140000.caffemodel"
            if proto.exists() and weights.exists():
                net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
                face_confs = []
                sample_interval = 3
                with tempfile.TemporaryDirectory() as face_td:
                    subprocess.run(
                        ["ffmpeg", "-i", str(video_path), "-vf", "scale=480:-1", f"{face_td}/f%05d.png", "-loglevel", "error"],
                        check=True,
                    )
                    face_frames = sorted(Path(face_td).glob("f*.png"))
                    for fi, ff in enumerate(face_frames):
                        if fi % sample_interval != 0:
                            continue
                        img = cv2.imread(str(ff))
                        if img is None:
                            continue
                        blob = cv2.dnn.blobFromImage(img, 1.0, (300, 300), (104.0, 177.0, 123.0))
                        net.setInput(blob)
                        detections = net.forward()
                        best_conf = 0.0
                        for di in range(detections.shape[2]):
                            c = float(detections[0, 0, di, 2])
                            if c > best_conf:
                                best_conf = c
                        face_confs.append((fi / fps, best_conf))

                if face_confs:
                    high_threshold = 0.5
                    low_threshold = 0.3
                    window = 3
                    last_good_face_time = None
                    consecutive_high = 0
                    had_stable_face = False
                    for ts, conf in face_confs:
                        if conf >= high_threshold:
                            consecutive_high += 1
                            if consecutive_high >= window:
                                had_stable_face = True
                            last_good_face_time = ts
                        else:
                            consecutive_high = 0

                    if had_stable_face and last_good_face_time is not None:
                        face_returned = False
                        for ts, conf in face_confs:
                            if ts > last_good_face_time + 2.0 and conf >= low_threshold:
                                face_returned = True
                                break

                        remaining = duration - last_good_face_time
                        if remaining > 1.5 and not face_returned:
                            face_trim = last_good_face_time + 0.3
                            result["analysis"]["last_good_face_time"] = last_good_face_time
                            if face_trim < keep_time:
                                result["analysis"]["face_dnn_warning_time"] = face_trim
        except ImportError:
            logger.warning("质量检测: cv2 不可用，跳过 DNN 人脸检测")
        except Exception as e:
            logger.warning(f"质量检测: DNN 人脸检测失败: {e}")

        top_diff_indices = sorted(range(len(diffs)), key=lambda idx: diffs[idx], reverse=True)[:5]
        result["analysis"]["top_diffs"] = [
            {"time": (idx + 1) / fps, "diff": diffs[idx], "smoothed": smoothed[idx]} for idx in top_diff_indices
        ]
        strong_trigger = result["trigger"] in {
            "flicker",
            "spike",
            "identity_hallucination",
            "face_orientation_discontinuity",
            "face_identity_drift",
            "head_body_inconsistency",
        }
        suggested_keep_time = keep_time

        if result["trigger"] == "forward_mse_run" and motion_ramp_exempt and not strong_trigger:
            result["analysis"]["suggested_trim_time"] = suggested_keep_time
            result["analysis"]["exemption_reason"] = "continuous_motion_ramp"
            result["trigger"] = "motion_ramp_exempt"
            keep_time = duration

        if result["trigger"] == "forward_mse_run" and profile_name == "heavy_motion" and not strong_trigger:
            result["analysis"]["suggested_trim_time"] = suggested_keep_time
            result["analysis"]["exemption_reason"] = "heavy_motion_warning_only"
            result["trigger"] = "forward_mse_warning"
            keep_time = duration

        if result["trigger"] == "passed" and keep_time < duration - 0.1:
            result["analysis"]["suggested_trim_time"] = suggested_keep_time
            result["analysis"]["exemption_reason"] = "tail_mse_warning_only"
            result["trigger"] = "reverse_mse_tail_warning"
            keep_time = duration

        if suggested_keep_time < duration - 0.1:
            risk_segments.append(
                {
                    "start": max(0.0, suggested_keep_time - 0.35),
                    "end": min(duration, suggested_keep_time + 0.75),
                    "reason": result["trigger"],
                    "confidence": 0.65,
                }
            )

        result["risk_segments"] = merge_segments(risk_segments)
        result["analysis"]["final_keep_time"] = keep_time

        if review_mode == "hybrid_judge":
            result["analysis"]["review_mode"] = "hybrid_judge"
            result["analysis"]["metrics_action"] = {
                "trim_to": result["trim_to"],
                "cut_segments": result["cut_segments"],
                "trigger": result["trigger"],
            }
            result["trim_to"] = None
            result["cut_segments"] = []
            result["needs_regeneration"] = False
            result["ok"] = True

        if keep_time >= duration - 0.1:
            if audit_dir:
                save_audit_frames(frames, fps, audit_dir, attempt, keep_time, duration, diffs, threshold)
            return result

        if result["cut_segments"]:
            result["ok"] = True
            result["trim_to"] = None
            logger.info(f"质量检测: 检测到可局部裁剪片段 {result['cut_segments']}")
        elif keep_time >= min_keep:
            result["trim_to"] = keep_time
            result["ok"] = True
            if result["trigger"] == "passed":
                result["trigger"] = "reverse_mse_tail"
            logger.info(
                f"质量检测: 发现异常帧，裁剪到 {keep_time:.1f}s "
                f"({duration:.1f}s → {keep_time:.1f}s, "
                f"median={median_diff:.0f}, threshold={threshold:.0f}, profile={profile_name}, trigger={result['trigger']})"
            )
        else:
            result["needs_regeneration"] = True
            result["ok"] = False
            if result["trigger"] == "passed":
                result["trigger"] = "reverse_mse_tail"
            logger.warning(
                f"质量检测: 异常帧过多，稳定内容仅 {keep_time:.1f}s (< {min_keep}s)，需重新生成 "
                f"(median={median_diff:.0f}, threshold={threshold:.0f}, profile={profile_name}, trigger={result['trigger']})"
            )

        if audit_dir:
            save_audit_frames(frames, fps, audit_dir, attempt, keep_time, duration, diffs, threshold)

        return result


def face_crop_duplicate_score(face_a: Any, face_b: Any) -> float:
    try:
        import cv2

        gray_a = cv2.cvtColor(face_a, cv2.COLOR_BGR2GRAY)
        gray_b = cv2.cvtColor(face_b, cv2.COLOR_BGR2GRAY)
        gray_a = cv2.resize(gray_a, (32, 32))
        gray_b = cv2.resize(gray_b, (32, 32))
        diff = cv2.absdiff(gray_a, gray_b)
        mean_diff = float(diff.mean())
        return max(0.0, 1.0 - mean_diff / 255.0)
    except Exception as e:
        logger.warning(f"灰度 MSE 相似度计算失败: {e}")
        return 0.0


def face_crop_identity_score(face_a: Any, face_b: Any) -> float:
    try:
        import cv2

        gray_a = cv2.cvtColor(face_a, cv2.COLOR_BGR2GRAY)
        gray_b = cv2.cvtColor(face_b, cv2.COLOR_BGR2GRAY)
        gray_a = cv2.resize(gray_a, (48, 48))
        gray_b = cv2.resize(gray_b, (48, 48))
        diff = cv2.absdiff(gray_a, gray_b)
        gray_score = max(0.0, 1.0 - float(diff.mean()) / 255.0)

        hist_a = cv2.calcHist([cv2.resize(face_a, (64, 64))], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
        hist_b = cv2.calcHist([cv2.resize(face_b, (64, 64))], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
        cv2.normalize(hist_a, hist_a)
        cv2.normalize(hist_b, hist_b)
        hist_score = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
        hist_score = max(0.0, min(1.0, (hist_score + 1.0) / 2.0))
        return 0.6 * gray_score + 0.4 * hist_score
    except Exception as e:
        logger.warning(f"人脸裁切身份评分失败: {e}")
        return 0.0


def box_iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0
    inter_area = float((inter_x2 - inter_x1) * (inter_y2 - inter_y1))
    area_a = float(max(1, (ax2 - ax1) * (ay2 - ay1)))
    area_b = float(max(1, (bx2 - bx1) * (by2 - by1)))
    return inter_area / (area_a + area_b - inter_area)


def dedupe_boxes(
    boxes: list[tuple[int, int, int, int, float]],
    iou_threshold: float = 0.45,
) -> list[tuple[int, int, int, int, float]]:
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda item: (item[4], (item[2] - item[0]) * (item[3] - item[1])), reverse=True)
    kept: list[tuple[int, int, int, int, float]] = []
    for candidate in ordered:
        candidate_box = candidate[:4]
        if any(box_iou(candidate_box, existing[:4]) >= iou_threshold for existing in kept):
            continue
        kept.append(candidate)
    return kept


def merge_segments(segments: list[dict[str, Any]], gap_tolerance: float = 0.35) -> list[dict[str, Any]]:
    if not segments:
        return []
    segments = sorted(segments, key=lambda item: (item["reason"], item["start"]))
    merged: list[dict[str, Any]] = [dict(segments[0])]
    for segment in segments[1:]:
        current = merged[-1]
        if segment["reason"] == current["reason"] and float(segment["start"]) <= float(current["end"]) + gap_tolerance:
            current["end"] = max(float(current["end"]), float(segment["end"]))
            current["confidence"] = max(float(current.get("confidence", 0.0)), float(segment.get("confidence", 0.0)))
        else:
            merged.append(dict(segment))
    return merged


def semantic_audit_video(
    video_path: Path,
    fps: float,
    duration: float,
    expected_character_count: int = 0,
    expected_subject_facing: str = "",
) -> list[dict[str, Any]]:
    try:
        import cv2
    except ImportError:
        return []

    model_dir = Path(__file__).parent.parent / "models"
    proto = model_dir / "deploy.prototxt"
    weights = model_dir / "res10_300x300_ssd_iter_140000.caffemodel"
    if not (proto.exists() and weights.exists()):
        return []

    try:
        net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))
        frontal = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    except Exception as e:
        logger.warning(f"朝向检测: 模型加载失败: {e}")
        return []

    sample_interval = max(3, int(round(fps / 8)))
    stable_expected_facing = str(expected_subject_facing).strip().lower()
    frontal_expected = stable_expected_facing in {"toward_camera", "front", "front_of_subject"}
    left_expected = stable_expected_facing == "left_profile"
    right_expected = stable_expected_facing == "right_profile"
    orientation_states: list[tuple[float, str, int]] = []
    drift_samples: list[tuple[float, float, tuple[float, float, float, float] | None, str]] = []
    bad_segments: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as face_td:
        try:
            subprocess.run(
                ["ffmpeg", "-i", str(video_path), "-vf", "scale=480:-1", f"{face_td}/f%05d.png", "-loglevel", "error"],
                check=True,
            )
        except Exception as e:
            logger.warning(f"朝向检测: 帧提取失败: {e}")
            return []

        person_hog = cv2.HOGDescriptor()
        person_hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        anchor_face = None

        face_frames = sorted(Path(face_td).glob("f*.png"))
        for fi, ff in enumerate(face_frames):
            if fi % sample_interval != 0:
                continue
            img = cv2.imread(str(ff))
            if img is None:
                continue
            h, w = img.shape[:2]
            blob = cv2.dnn.blobFromImage(img, 1.0, (300, 300), (104.0, 177.0, 123.0))
            net.setInput(blob)
            detections = net.forward()
            faces: list[tuple[int, int, int, int, float]] = []
            for di in range(detections.shape[2]):
                conf = float(detections[0, 0, di, 2])
                if conf < 0.5:
                    continue
                box = detections[0, 0, di, 3:7] * [w, h, w, h]
                x1, y1, x2, y2 = [int(v) for v in box]
                x1 = max(0, min(x1, w - 1))
                x2 = max(0, min(x2, w))
                y1 = max(0, min(y1, h - 1))
                y2 = max(0, min(y2, h))
                if x2 - x1 < 20 or y2 - y1 < 20:
                    continue
                faces.append((x1, y1, x2, y2, conf))
            face_candidates = list(faces)

            sample_time = fi / fps
            person_boxes, weights = person_hog.detectMultiScale(img, winStride=(4, 4), padding=(16, 16), scale=1.02)
            person_boxes = [box for box, weight in zip(person_boxes, weights) if weight > 0.15]
            person_box = None
            if len(person_boxes) > 0:
                px, py, pw, ph = max(person_boxes, key=lambda box: box[2] * box[3])
                person_box = (float(px), float(py), float(px + pw), float(py + ph))

            if len(person_boxes) >= 2:
                logger.debug(f"[{sample_time:.2f}s] HOG detected {len(person_boxes)} persons (expected {expected_character_count})")
                duplicate_person_score = 0.0
                for idx in range(len(person_boxes)):
                    px1, py1, pw1, ph1 = person_boxes[idx]
                    crop_a = img[py1 : py1 + ph1, px1 : px1 + pw1]
                    for jdx in range(idx + 1, len(person_boxes)):
                        px2, py2, pw2, ph2 = person_boxes[jdx]
                        crop_b = img[py2 : py2 + ph2, px2 : px2 + pw2]
                        if crop_a.size > 0 and crop_b.size > 0:
                            score = face_crop_identity_score(crop_a, crop_b)
                            duplicate_person_score = max(duplicate_person_score, score)
                            logger.debug(f"[{sample_time:.2f}s] Person {idx} vs {jdx}: similarity={score:.3f}")
                if duplicate_person_score >= 0.70:
                    bad_segments.append(
                        {
                            "start": sample_time,
                            "end": min(duration, sample_time + sample_interval / fps),
                            "reason": "identity_hallucination",
                            "confidence": duplicate_person_score,
                        }
                    )
                    logger.debug(f"[{sample_time:.2f}s] Duplicate person detected: score={duplicate_person_score:.3f}")

            if expected_character_count > 0 and len(faces) > expected_character_count:
                duplicate_score = 0.0
                for idx in range(len(faces)):
                    x1, y1, x2, y2, _ = faces[idx]
                    crop_a = img[y1:y2, x1:x2]
                    for jdx in range(idx + 1, len(faces)):
                        xx1, yy1, xx2, yy2, _ = faces[jdx]
                        crop_b = img[yy1:yy2, xx1:xx2]
                        duplicate_score = max(duplicate_score, face_crop_duplicate_score(crop_a, crop_b))
                if duplicate_score >= 0.88:
                    bad_segments.append(
                        {
                            "start": sample_time,
                            "end": min(duration, sample_time + sample_interval / fps),
                            "reason": "identity_hallucination",
                            "confidence": duplicate_score,
                        }
                    )

            if faces:
                x1, y1, x2, y2, conf = max(faces, key=lambda item: (item[4], (item[2] - item[0]) * (item[3] - item[1])))
                primary_crop = img[y1:y2, x1:x2]
                if primary_crop.size > 0:
                    if anchor_face is None and (x2 - x1) >= 36 and (y2 - y1) >= 36:
                        anchor_face = primary_crop.copy()
                    if anchor_face is not None:
                        identity_score = face_crop_identity_score(anchor_face, primary_crop)
                        drift_samples.append((sample_time, identity_score, person_box, "face"))

            if frontal_expected or left_expected or right_expected:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                frontal_hits = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                left_hits = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                flipped = cv2.flip(gray, 1)
                right_hits = profile.detectMultiScale(flipped, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                logger.debug(f"[{sample_time:.2f}s] Haar: frontal={len(frontal_hits)}, left={len(left_hits)}, right={len(right_hits)}")
                for x, y, ww, hh in frontal_hits:
                    face_candidates.append((int(x), int(y), int(x + ww), int(y + hh), 0.55))
                for x, y, ww, hh in left_hits:
                    face_candidates.append((int(x), int(y), int(x + ww), int(y + hh), 0.5))
                for x, y, ww, hh in right_hits:
                    x1 = int(w - (x + ww))
                    x2 = int(w - x)
                    face_candidates.append((x1, int(y), x2, int(y + hh), 0.5))
                logger.debug(f"[{sample_time:.2f}s] face_candidates before dedupe: {len(face_candidates)}")

                observed = "unknown"
                if len(frontal_hits) > 0:
                    observed = "frontal"
                elif len(left_hits) > 0 and len(right_hits) == 0:
                    observed = "left"
                elif len(right_hits) > 0 and len(left_hits) == 0:
                    observed = "right"
                orientation_states.append((sample_time, observed, len(faces)))

            dedupe_boxes(face_candidates)

    if drift_samples:
        low_run = 0
        run_start = None
        last_identity = None
        for sample_time, identity_score, person_box, _ in drift_samples:
            if last_identity is not None and identity_score < 0.58 and last_identity < 0.7:
                if low_run == 0:
                    run_start = sample_time
                low_run += 1
            else:
                if low_run >= 2 and run_start is not None:
                    bad_segments.append(
                        {
                            "start": max(0.0, run_start - sample_interval / fps),
                            "end": sample_time,
                            "reason": "face_identity_drift",
                            "confidence": 0.9 - identity_score * 0.2,
                        }
                    )
                low_run = 0
                run_start = None
            last_identity = identity_score

    if orientation_states and (frontal_expected or left_expected or right_expected):
        dominant = "frontal" if frontal_expected else "left" if left_expected else "right"
        mismatch_run = 0
        run_start = None
        last_state = None
        flip_count = 0
        for sample_time, observed, face_count in orientation_states:
            if face_count == 0 or observed == "unknown":
                continue
            if observed != dominant:
                if mismatch_run == 0:
                    run_start = sample_time
                    flip_count = 0
                if last_state and observed != last_state:
                    flip_count += 1
                mismatch_run += 1
            else:
                if mismatch_run >= 2 and flip_count >= 1 and run_start is not None:
                    bad_segments.append(
                        {
                            "start": max(0.0, run_start - sample_interval / fps),
                            "end": sample_time,
                            "reason": "face_orientation_discontinuity",
                            "confidence": 0.85,
                        }
                    )
                mismatch_run = 0
                run_start = None
                flip_count = 0
            last_state = observed
        if mismatch_run >= 2 and flip_count >= 1 and run_start is not None:
            bad_segments.append(
                {
                    "start": max(0.0, run_start - sample_interval / fps),
                    "end": min(duration, run_start + mismatch_run * sample_interval / fps),
                    "reason": "face_orientation_discontinuity",
                    "confidence": 0.85,
                }
            )

    if orientation_states and drift_samples:
        orientation_map = {
            round(sample_time, 2): observed
            for sample_time, observed, face_count in orientation_states
            if face_count > 0
        }
        jump_run = 0
        run_start = None
        last_orientation = None
        for sample_time, identity_score, person_box, _ in drift_samples:
            observed = orientation_map.get(round(sample_time, 2), "unknown")
            if observed == "unknown":
                continue
            if last_orientation and observed != last_orientation and identity_score < 0.62:
                if jump_run == 0:
                    run_start = sample_time
                jump_run += 1
            else:
                if jump_run >= 2 and run_start is not None:
                    bad_segments.append(
                        {
                            "start": max(0.0, run_start - sample_interval / fps),
                            "end": sample_time,
                            "reason": "head_body_inconsistency",
                            "confidence": 0.82,
                        }
                    )
                jump_run = 0
                run_start = None
            last_orientation = observed

    return merge_segments(bad_segments)


def save_audit_frames(
    frames: list[Any],
    fps: float,
    audit_dir: Path,
    attempt: int,
    keep_time: float,
    duration: float,
    diffs: list[float],
    threshold: float,
) -> None:
    """保存质量检测的关键帧截图，供人工审查。"""
    try:
        prefix = f"attempt_{attempt}"
        if frames:
            frames[0].save(audit_dir / f"{prefix}_first_frame.png")

        trim_frame_idx = min(int(keep_time * fps), len(frames) - 1)
        if trim_frame_idx > 0 and trim_frame_idx < len(frames):
            frames[trim_frame_idx].save(audit_dir / f"{prefix}_trim_point_{keep_time:.1f}s.png")

        bad_frame_idx = min(trim_frame_idx + 1, len(frames) - 1)
        if bad_frame_idx < len(frames) and bad_frame_idx != trim_frame_idx:
            frames[bad_frame_idx].save(audit_dir / f"{prefix}_first_bad_frame_{bad_frame_idx / fps:.1f}s.png")

        if len(frames) > 1:
            frames[-1].save(audit_dir / f"{prefix}_last_frame.png")

        if diffs:
            worst_idx = max(range(len(diffs)), key=lambda i: diffs[i])
            worst_time = (worst_idx + 1) / fps
            if worst_idx + 1 < len(frames):
                frames[worst_idx + 1].save(
                    audit_dir / f"{prefix}_worst_frame_{worst_time:.1f}s_mse{diffs[worst_idx]:.0f}.png"
                )

        logger.info(f"质量审查: 关键帧截图已保存 → {audit_dir}/{prefix}_*.png")
    except Exception as e:
        logger.warning(f"质量审查: 截图保存失败: {e}")


def trim_video_at(video_path: Path, end_time: float, min_remaining: float = 3.0) -> Path:
    if end_time < min_remaining:
        logger.warning(f"裁剪: 目标时长 {end_time:.1f}s 太短，跳过")
        return video_path

    trimmed_path = video_path.with_suffix(".trimmed.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-t", str(end_time), "-c", "copy", str(trimmed_path)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        trimmed_path.replace(video_path)
        logger.info(f"裁剪完成: {video_path} → {end_time:.1f}s")
        return video_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"裁剪失败: {e}")
        trimmed_path.unlink(missing_ok=True)
        return video_path


def remove_video_segments(video_path: Path, segments: list[dict[str, Any]], min_remaining: float = 3.0) -> Path:
    if len(segments) != 1:
        logger.warning("局部裁剪: 当前仅支持单个问题片段")
        return video_path
    segment = segments[0]
    start_time = float(segment["start"])
    end_time = float(segment["end"])
    if end_time <= start_time:
        return video_path

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(probe.stdout.strip())
    except Exception as e:
        logger.warning(f"cut_segment: ffprobe 时长获取失败: {e}")
        return video_path

    remaining = duration - (end_time - start_time)
    if remaining < min_remaining:
        logger.warning("局部裁剪: 剩余时长过短，跳过")
        return video_path

    edited_path = video_path.with_suffix(".edited.mp4")
    filter_complex = (
        f"[0:v]trim=start=0:end={start_time},setpts=PTS-STARTPTS[v0];"
        f"[0:v]trim=start={end_time}:end={duration},setpts=PTS-STARTPTS[v1];"
        f"[v0][v1]concat=n=2:v=1:a=0[v]"
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                str(edited_path),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        edited_path.replace(video_path)
        logger.info(f"局部裁剪完成: 删除片段 {start_time:.1f}s-{end_time:.1f}s")
        return video_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"局部裁剪失败: {e}")
        edited_path.unlink(missing_ok=True)
        return video_path


def extract_segment_frames(
    video_path: Path,
    output_dir: Path,
    segment: dict[str, Any],
    max_frames: int = 5,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", start + 1.0))
    if end <= start:
        end = start + 1.0
    if max_frames <= 1:
        frame_times = [start]
    else:
        step = (end - start) / max_frames
        frame_times = [start + idx * step for idx in range(max_frames)]
    extracted: list[str] = []
    for idx, ts in enumerate(frame_times, start=1):
        frame_path = output_dir / f"frame_{idx:02d}_{ts:.2f}s.png"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(frame_path)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            if frame_path.exists():
                extracted.append(str(frame_path))
        except Exception as e:
            logger.warning(f"帧提取失败 @{ts:.2f}s: {e}")
            frame_path.unlink(missing_ok=True)
    return extracted
