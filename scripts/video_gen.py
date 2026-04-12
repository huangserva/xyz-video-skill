from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from content_filter import VideoPromptBuilder
from utils import get_api_credentials, get_model_config

logger = logging.getLogger(__name__)

VIDEO_REFERENCE_USAGE_PRIORITY = {
    "first_frame": 0,
    "reference_character": 1,
    "reference_prop": 2,
    "reference_composition": 3,
    "reference_style": 4,
    "reference_color": 5,
    "reference_target_state": 6,
    "reference_stage": 7,
    "reference_motion": 8,
    "reference": 99,
}


class VideoGenerator:
    def __init__(
        self,
        *,
        video_dir: Path,
        cfg: dict[str, Any],
        get_session: Callable[[], Awaitable[aiohttp.ClientSession]],
        download: Callable[[aiohttp.ClientSession, str, Path], Awaitable[bool]],
        reference_builder: Any,
        normalize_usage: Callable[[Any], str],
    ) -> None:
        self.video_dir = video_dir
        self.cfg = cfg
        self._get_session = get_session
        self._download = download
        self.reference_builder = reference_builder
        self._normalize_usage = normalize_usage

    def fallback_chain(self) -> list[str]:
        video_cfg = get_model_config("video")
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            provider = str(video_cfg.get("provider", "byteplus")).strip()
            return [provider] if provider else ["byteplus"]
        return list(video_cfg.get("fallback_chain", ["byteplus"]))

    def provider_config(self, provider: str) -> dict[str, Any]:
        video_cfg = get_model_config("video")
        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            selected = str(video_cfg.get("provider", "")).strip()
            return video_cfg if provider == selected else {}
        return dict(video_cfg.get(provider, {}))

    def any_provider_supports_stage_references(self) -> bool:
        return any(
            int(self.provider_config(provider).get("max_reference_images", 2)) > 2
            for provider in self.fallback_chain()
        )

    def max_reference_images(self) -> int:
        max_refs = 2
        for provider in self.fallback_chain():
            pcfg = self.provider_config(provider)
            if not pcfg:
                continue
            max_refs = max(max_refs, int(pcfg.get("max_reference_images", 2)))
        return max_refs

    def prepare_provider_inputs(
        self,
        provider: str,
        *,
        image_path: Path,
        prompt: str,
        last_frame_path: Path | None = None,
        video_references: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        provider_references = list(video_references or [])
        if not provider_references:
            provider_references = [{"path": image_path, "usage": "first_frame", "source_type": "frame"}]
            if last_frame_path and last_frame_path.exists():
                provider_references.append(
                    {
                        "path": last_frame_path,
                        "usage": "reference_target_state",
                        "source_type": "frame",
                    }
                )

        original_count = len(provider_references)
        pcfg = self.provider_config(provider)
        max_refs = int(pcfg.get("max_reference_images", 2))
        if max_refs > 0 and len(provider_references) > max_refs:
            ranked = sorted(
                enumerate(provider_references),
                key=lambda pair: (self._video_reference_priority(pair[1]), pair[0]),
            )
            keep_indices = {idx for idx, _ in ranked[:max_refs]}
            provider_references = [ref for idx, ref in enumerate(provider_references) if idx in keep_indices]

        final_prompt = self.reference_builder.compose_prompt_with_reference_mentions(prompt, provider_references)
        return {
            "provider": provider,
            "prompt_text": final_prompt,
            "references": provider_references,
            "serialized_references": self.reference_builder.serialize_video_references(provider_references),
            "original_count": original_count,
            "trimmed": len(provider_references) != original_count,
        }

    async def generate_video(
        self,
        image_path: Path,
        prompt: str,
        shot_id: int,
        *,
        estimated_duration: int = 10,
        last_frame_path: Path | None = None,
        video_references: list[dict[str, Any]] | None = None,
    ) -> tuple[Path | None, str]:
        """图生视频：dispatch + fallback_chain 模式。"""
        out = self.video_dir / f"shot_{shot_id:03d}.mp4"
        video_cfg = get_model_config("video")

        if "provider" in video_cfg and "fallback_chain" not in video_cfg:
            fallback_chain = [video_cfg["provider"]]
            max_retries = 1
            retry_delay = 5
        else:
            fallback_chain = video_cfg.get("fallback_chain", ["byteplus"])
            max_retries = video_cfg.get("max_retries", 2)
            retry_delay = video_cfg.get("retry_delay", 5)

        dispatch = {
            "volcengine_seedance2": lambda img, p, o, dur, lf, refs: self._video_seedance(
                "volcengine_seedance2", img, p, o, duration=dur, last_frame_path=lf, video_references=refs
            ),
            "byteplus": lambda img, p, o, dur, lf, refs: self._video_seedance(
                "byteplus", img, p, o, duration=dur, last_frame_path=lf, video_references=refs
            ),
        }

        for provider in fallback_chain:
            fn = dispatch.get(provider)
            if not fn:
                logger.warning(f"shot_{shot_id}: 未知视频 provider '{provider}'，跳过")
                continue

            if "provider" in video_cfg and "fallback_chain" not in video_cfg:
                pcfg = video_cfg
            else:
                pcfg = video_cfg.get(provider, {})

            min_dur = pcfg.get("min_duration", 5)
            max_dur = pcfg.get("max_duration", 12)
            clamped = max(min_dur, min(max_dur, estimated_duration))
            logger.info(
                f"shot_{shot_id}: estimated_duration={estimated_duration}s -> clamped={clamped}s (provider={provider})"
            )

            prepared_inputs = self.prepare_provider_inputs(
                provider,
                image_path=image_path,
                prompt=prompt,
                last_frame_path=last_frame_path,
                video_references=video_references,
            )
            provider_references = prepared_inputs["references"]
            if prepared_inputs["trimmed"]:
                kept_usages = [self._normalize_usage(ref.get("usage")) for ref in provider_references]
                logger.info(
                    f"shot_{shot_id}: {provider} 参考图超限，按用途优先级保留 {len(provider_references)} 张"
                    f" ({', '.join(kept_usages)})"
                )

            for attempt in range(max_retries):
                if await fn(
                    image_path,
                    prepared_inputs["prompt_text"],
                    out,
                    clamped,
                    last_frame_path,
                    provider_references,
                ):
                    return out, provider
                if attempt < max_retries - 1:
                    logger.warning(f"shot_{shot_id}: {provider} 第 {attempt + 1} 次失败，{retry_delay}s 后重试")
                    await asyncio.sleep(retry_delay)
            logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        logger.warning(f"视频生成失败 shot_{shot_id}，将使用静态图 fallback")
        return None, "none"

    async def _video_seedance(
        self,
        provider_name: str,
        image_path: Path,
        prompt: str,
        output: Path,
        *,
        duration: int = 10,
        last_frame_path: Path | None = None,
        video_references: list[dict[str, Any]] | None = None,
        prompt_includes_reference_mentions: bool = True,
    ) -> bool:
        """Ark Seedance I2V（图生视频），支持 per-provider 配置。"""
        video_cfg = get_model_config("video")
        if provider_name in video_cfg and isinstance(video_cfg[provider_name], dict):
            pcfg = video_cfg[provider_name]
        else:
            pcfg = video_cfg

        api_provider = str(pcfg.get("provider", "byteplus")).strip() or "byteplus"
        creds = get_api_credentials(api_provider, self.cfg)
        if not creds.get("api_key"):
            logger.warning(f"{api_provider} api_key 未配置，跳过 Seedance")
            return False

        try:
            api_base = creds["api_base"]
            api_key = creds["api_key"]

            raw_model = pcfg.get("model", "seedance-1-5-pro-251215")
            batch_mode = raw_model.endswith("-batch")
            model = raw_model.removesuffix("-batch") if batch_mode else raw_model
            if batch_mode:
                logger.info(f"Seedance batch mode: {raw_model} -> {model}")

            refs = list(video_references or [])
            if not refs:
                refs = [{"path": image_path, "usage": "first_frame", "source_type": "frame"}]
                if last_frame_path and last_frame_path.exists():
                    refs.append(
                        {
                            "path": last_frame_path,
                            "usage": "reference_target_state",
                            "source_type": "frame",
                        }
                    )

            final_prompt = prompt
            content: list[dict[str, Any]] = []
            ordered_refs: list[dict[str, Any]] = []
            for ref in refs:
                ref_path = ref.get("path")
                if not isinstance(ref_path, Path) or not ref_path.exists():
                    continue
                media_type = str(ref.get("media_type", "image")).strip() or "image"
                if media_type != "image":
                    logger.info(f"Seedance: 当前执行层仅发送图片参考，跳过 {media_type} 参考素材")
                    continue
                mime = mimetypes.guess_type(str(ref_path))[0] or "image/png"
                img_b64 = base64.b64encode(ref_path.read_bytes()).decode()
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img_b64}"},
                        "role": "reference_image",
                    }
                )
                ordered_refs.append(ref)
            if ordered_refs and not prompt_includes_reference_mentions:
                prompt_ready_refs: list[dict[str, Any]] = []
                for idx, ref in enumerate(ordered_refs, start=1):
                    prompt_ref = dict(ref)
                    prompt_ref["mention"] = f"@图片{idx}"
                    prompt_ready_refs.append(prompt_ref)
                final_prompt = VideoPromptBuilder.compose_video_generation_prompt(final_prompt, prompt_ready_refs)
            content.insert(0, {"type": "text", "text": final_prompt})
            usages = [self._normalize_usage(ref.get("usage")) for ref in ordered_refs]
            logger.info(f"Seedance: 使用 {len(usages)} 张参考图 ({', '.join(usages)})")

            payload = {
                "model": model,
                "content": content,
                "duration": duration,
                "ratio": pcfg.get("ratio", "16:9"),
                "resolution": pcfg.get("resolution", "720p"),
                "generate_audio": pcfg.get("generate_audio", True),
                "watermark": pcfg.get("watermark", False),
            }
            if batch_mode:
                payload["service_tier"] = "flex"
                payload["execution_expires_after"] = 86400

            submit_timeout = pcfg.get("submit_timeout", 30)
            session = await self._get_session()
            async with session.post(
                f"{api_base}/contents/generations/tasks",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=submit_timeout),
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

            poll_timeout = pcfg.get("poll_timeout", 15)
            poll_interval = pcfg.get("poll_interval", 5)
            default_poll_max = 720 if batch_mode else 60
            poll_max = pcfg.get("poll_max_attempts", default_poll_max)
            for attempt in range(poll_max):
                await asyncio.sleep(poll_interval)
                async with session.get(
                    f"{api_base}/contents/generations/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=poll_timeout),
                ) as resp:
                    if resp.status != 200:
                        continue
                    result = await resp.json()
                    status = result.get("status", "")

                    if status == "succeeded":
                        video_url = (result.get("content") or {}).get("video_url")
                        if video_url:
                            logger.info("Seedance 生成成功，下载视频...")
                            return await self._download(session, video_url, output)
                        logger.warning("Seedance 成功但无 video_url")
                        return False

                    if status in ("failed", "cancelled"):
                        error = result.get("error")
                        logger.warning(f"Seedance 任务 {status}: {error}")
                        return False

                    if attempt % 6 == 0:
                        elapsed = attempt * poll_interval
                        logger.info(f"Seedance{mode_tag} 生成中... ({elapsed}s)")

            logger.warning(f"Seedance 超时（{poll_max * poll_interval}s）")
            return False

        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
            logger.warning(f"Seedance 异常: {e}")
            return False

    def _video_reference_priority(self, ref: dict[str, Any]) -> tuple[int, int]:
        usage = self._normalize_usage(ref.get("usage"))
        source_type = str(ref.get("source_type", "")).strip()
        bonus = -1 if usage == "reference_composition" and source_type == "scene" else 0
        return VIDEO_REFERENCE_USAGE_PRIORITY.get(usage, 99), bonus
