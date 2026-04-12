from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from PIL import Image, ImageDraw

from utils import get_api_credentials, get_model_config

logger = logging.getLogger(__name__)

GEMINI_IMAGE_SYSTEM_PROMPT = (
    "You are a cinematic image generator for a professional video production pipeline. "
    "Your task is to generate a single cinematic frame that will be used as "
    "a keyframe for AI video generation (Seedance I2V).\n\n"
    "CRITICAL RULES:\n"
    "1. STYLE LOCK: The style_anchor provided in the prompt defines the EXACT visual style - "
    "color grading, rendering quality, art direction, and aesthetic. You MUST match this style "
    "precisely in EVERY frame, whether it is a wide shot, close-up, or detail shot. "
    "NEVER switch between illustrated/animated and photorealistic styles within the same project. "
    "If the style anchor says 'painterly quality', ALL frames must have that painterly quality.\n"
    "2. CHARACTER FIDELITY: When reference images are provided, the generated character MUST be "
    "visually identical - same face, proportions, colors, textures, and distinguishing features.\n"
    "3. VISUAL INFERENCE: Infer logically necessary details that the prompt may not explicitly state. "
    "Examples: rain -> wet surfaces, reflective puddles, damp hair and clothes; "
    "holding an umbrella in rain; battle scene -> debris, dust, scorch marks; "
    "cold weather -> visible breath vapor, reddened cheeks.\n"
    "4. NO TEXT: Never include any text, labels, watermarks, or annotations in the image.\n"
    "5. CINEMATIC COMPOSITION: Frame the shot as a professional cinematographer would - "
    "follow the rule of thirds, use depth of field, and create visual depth."
)


class ImageGenerator:
    def __init__(
        self,
        *,
        storyboard: dict[str, Any],
        image_dir: Path,
        image_width: int,
        image_height: int,
        use_api: bool,
        cfg: dict[str, Any],
        get_session: Callable[[], Awaitable[aiohttp.ClientSession]],
        download: Callable[[aiohttp.ClientSession, str, Path], Awaitable[bool]],
    ) -> None:
        self.storyboard = storyboard
        self.image_dir = image_dir
        self.image_width = image_width
        self.image_height = image_height
        self.use_api = use_api
        self.cfg = cfg
        self._get_session = get_session
        self._download = download

    async def generate_image(
        self,
        prompt: str,
        shot_id: int,
        *,
        output_path: Path | None = None,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> tuple[Path, str]:
        """生成图片。同一模型重试最多 3 次，不切换到其他风格不同的模型。"""
        out = output_path or (self.image_dir / f"shot_{shot_id:03d}.png")
        img_cfg = get_model_config("image")
        max_retries = img_cfg.get("max_retries", 3)
        retry_delay = img_cfg.get("retry_delay", 5)
        fallback_chain = img_cfg.get("fallback_chain", ["apimart", "volcengine", "fal"])

        if self.use_api:
            dispatch = {
                "volcengine": lambda p, o: self._image_volcengine(
                    p,
                    o,
                    characters_in_shot=characters_in_shot or [],
                    props_in_shot=props_in_shot or [],
                    scene_image=scene_image,
                    style_reference_image=style_reference_image,
                ),
                "apimart": lambda p, o: self._image_apimart(
                    p,
                    o,
                    characters_in_shot=characters_in_shot or [],
                    props_in_shot=props_in_shot or [],
                    scene_image=scene_image,
                    style_reference_image=style_reference_image,
                ),
                "fal": lambda p, o: self._image_flux(p, o),
            }
            for provider in fallback_chain:
                fn = dispatch.get(provider)
                if not fn:
                    continue
                for attempt in range(max_retries):
                    if await fn(prompt, out):
                        return out, provider
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"shot_{shot_id}: {provider} 第 {attempt + 1} 次失败，{retry_delay}s 后重试"
                        )
                        await asyncio.sleep(retry_delay)
                logger.warning(f"shot_{shot_id}: {provider} {max_retries} 次全部失败，尝试下一个 provider")

        self.placeholder_image(out, shot_id, prompt)
        return out, "placeholder"

    async def _image_volcengine(
        self,
        prompt: str,
        output: Path,
        *,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> bool:
        """Volcengine Seedream 图像生成（支持角色参考图）。"""
        img_cfg = get_model_config("image").get("volcengine", {})
        creds = get_api_credentials("volcengine", self.cfg)
        if not creds.get("api_key"):
            return False

        image_urls: list[str] = []
        characters_cfg = self.storyboard.get("characters", {})
        ref_dir = self.storyboard.get("character_ref_dir", "")
        for char_id in (characters_in_shot or []):
            char_info = characters_cfg.get(char_id, {})
            ref_file = char_info.get("ref_image", "")
            if ref_file and ref_dir:
                ref_path = Path(ref_dir) / ref_file
                if ref_path.exists():
                    image_urls.append(self._path_to_data_url(ref_path))

        prop_cfg = self.storyboard.get("prop_refs", {})
        if isinstance(prop_cfg, dict):
            for prop_id in (props_in_shot or []):
                prop_info = prop_cfg.get(prop_id, {})
                if not isinstance(prop_info, dict):
                    continue
                ref_path_value = str(prop_info.get("ref_path", "")).strip()
                ref_path = Path(ref_path_value).expanduser() if ref_path_value else None
                if ref_path and ref_path.exists():
                    image_urls.append(self._path_to_data_url(ref_path))

        if scene_image and scene_image.exists():
            image_urls.append(self._path_to_data_url(scene_image))

        if style_reference_image and style_reference_image.exists():
            image_urls.append(self._path_to_data_url(style_reference_image))

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

            session = await self._get_session()
            async with session.post(
                f"{creds['api_base']}/images/generations",
                headers={"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                url = data.get("data", [{}])[0].get("url")
                if url:
                    return await self._download(session, url, output)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
            logger.warning(f"volcengine 失败: {e}")
        return False

    async def _image_apimart(
        self,
        prompt: str,
        output: Path,
        *,
        characters_in_shot: list[str] | None = None,
        props_in_shot: list[str] | None = None,
        scene_image: Path | None = None,
        style_reference_image: Path | None = None,
    ) -> bool:
        """ApiMart 图像生成 - Gemini 模型走 chat/completions，其他走 images/generations。"""
        img_cfg = get_model_config("image").get("apimart", {})
        creds = get_api_credentials("apimart", self.cfg)
        if not creds.get("api_key"):
            return False

        model = img_cfg.get("model", "gemini-3.1-flash-image-preview")
        is_gemini = "gemini" in model.lower()

        try:
            timeout = img_cfg.get("timeout", 180)
            headers = {"Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"}
            session = await self._get_session()
            if is_gemini:
                user_content: list[dict[str, Any]] | str = []
                characters_cfg = self.storyboard.get("characters", {})
                prop_cfg = self.storyboard.get("prop_refs", {})
                chars_to_use = characters_in_shot or []
                ref_dir = self.storyboard.get("character_ref_dir", "")

                has_refs = False
                if (chars_to_use and ref_dir) or scene_image or style_reference_image:
                    user_content = []

                    if scene_image and scene_image.exists():
                        user_content.append(
                            {
                                "type": "text",
                                "text": "场景参考图——这是当前镜头的环境与背景基底。请把人物放入这张场景中，保持建筑、地形、光线、天气和空间关系一致。",
                            }
                        )
                        user_content.append(
                            {"type": "image_url", "image_url": {"url": self._path_to_data_url(scene_image)}}
                        )
                        has_refs = True

                    if style_reference_image and style_reference_image.exists():
                        user_content.append(
                            {
                                "type": "text",
                                "text": "风格连续性参考图——保持同一种人物绘制媒介、面部身份、材质处理和整体视觉媒介，但不要直接复制它的构图和背景。",
                            }
                        )
                        user_content.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": self._path_to_data_url(style_reference_image)},
                            }
                        )
                        has_refs = True

                    if chars_to_use and ref_dir:
                        ref_base = Path(ref_dir)
                        for char_id in chars_to_use:
                            char_info = characters_cfg.get(char_id, {})
                            ref_file = char_info.get("ref_image", "")
                            ref_desc = char_info.get("ref_description", char_info.get("appearance", ""))
                            ref_path = ref_base / ref_file if ref_file else None

                            if ref_path and ref_path.exists():
                                user_content.append({"type": "text", "text": f"角色参考图——{char_id}: {ref_desc}"})
                                user_content.append(
                                    {"type": "image_url", "image_url": {"url": self._path_to_data_url(ref_path)}}
                                )
                                has_refs = True

                    if isinstance(prop_cfg, dict):
                        for prop_id in (props_in_shot or []):
                            prop_info = prop_cfg.get(prop_id, {})
                            if not isinstance(prop_info, dict):
                                continue
                            ref_path_value = str(prop_info.get("ref_path", "")).strip()
                            ref_desc = str(
                                prop_info.get("ref_description", "") or prop_info.get("appearance", "")
                            ).strip()
                            ref_path = Path(ref_path_value).expanduser() if ref_path_value else None
                            if ref_path and ref_path.exists():
                                user_content.append({"type": "text", "text": f"道具参考图——{prop_id}: {ref_desc}"})
                                user_content.append(
                                    {"type": "image_url", "image_url": {"url": self._path_to_data_url(ref_path)}}
                                )
                                has_refs = True

                    instruction = "现在生成一张新的电影感镜头图片。"
                    if scene_image and scene_image.exists():
                        instruction += (
                            "使用场景参考图作为环境基底，保持同样的建筑、地形、山道、光线与天气，把人物放入这张环境中。"
                        )
                    if style_reference_image and style_reference_image.exists():
                        instruction += (
                            "使用风格连续性参考图来保持同一种人物媒介、面部身份、材质处理和整体视觉感受；"
                            "这张图只用于风格连续，不用于复制原构图和原背景。"
                        )
                    instruction += (
                        "关键要求："
                        "1）人物身份必须与各自参考图一致；"
                        "2）关键道具应与道具参考图一致；"
                        "3）环境与场景参考图保持连续；"
                        "4）不要出现任何文字、标签或注释；"
                        "5）基于下面的提示词生成新的镜头构图。\n\n"
                        f"{prompt}"
                    )
                    user_content.append({"type": "text", "text": instruction})

                    if not has_refs:
                        user_content = f"生成一张图片：{prompt}"
                else:
                    user_content = f"生成一张图片：{prompt}"

                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": GEMINI_IMAGE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": 4096,
                    "stream": False,
                }
                async with session.post(
                    f"{creds['api_base']}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"apimart gemini 返回 {resp.status}: {body[:200]}")
                        return False
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        return False
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "")

                    if isinstance(content, str):
                        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)", content)
                        if m:
                            b64_str = m.group(1).replace("\n", "").replace(" ", "")
                            output.write_bytes(base64.b64decode(b64_str))
                            return True
                        m_url = re.search(r"https?://\S+", content)
                        if m_url:
                            return await self._download(session, m_url.group(0), output)
                        logger.warning(f"apimart gemini 返回内容无图片: {content[:200]}")
                        return False
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "image_url":
                                url_or_b64 = part.get("image_url", {}).get("url", "")
                                if url_or_b64.startswith("data:"):
                                    b64_str = url_or_b64.split(",", 1)[1]
                                    output.write_bytes(base64.b64decode(b64_str))
                                    return True
                                if url_or_b64.startswith("http"):
                                    return await self._download(session, url_or_b64, output)
                        return False
                    return False

            poll_interval = img_cfg.get("poll_interval", 5)
            poll_max = img_cfg.get("poll_max_attempts", 30)
            async with session.post(
                f"{creds['api_base']}/images/generations",
                headers=headers,
                json={"model": model, "prompt": prompt, "size": f"{self.image_width}x{self.image_height}", "n": 1},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                first = data.get("data", [{}])[0]

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

                url = first.get("url")
                if isinstance(url, list):
                    url = url[0] if url else None
                if url:
                    return await self._download(session, url, output)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
            logger.warning(f"apimart 失败: {e}")
        return False

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
            session = await self._get_session()
            async with session.post(
                f"{creds['api_base']}/fal-ai/flux/schnell",
                headers={"Authorization": f"Key {creds['api_key']}", "Content-Type": "application/json"},
                json={
                    "prompt": prompt,
                    "image_size": {"width": self.image_width, "height": self.image_height},
                    "num_inference_steps": img_cfg.get("num_inference_steps", 4),
                    "num_images": img_cfg.get("num_images", 1),
                },
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()

                if data.get("images"):
                    url = data["images"][0].get("url")
                    if url:
                        return await self._download(session, url, output)

                if request_id := data.get("request_id"):
                    for _ in range(poll_max):
                        await asyncio.sleep(poll_interval)
                        async with session.get(
                            f"{creds['api_base']}/fal-ai/flux/schnell/requests/{request_id}",
                            headers={"Authorization": f"Key {creds['api_key']}"},
                        ) as poll:
                            if poll.status == 200:
                                result = await poll.json()
                                if result.get("images"):
                                    url = result["images"][0].get("url")
                                    if url:
                                        return await self._download(session, url, output)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, OSError) as e:
            logger.warning(f"flux 失败: {e}")
        return False

    def placeholder_image(self, output: Path, shot_id: int, prompt: str) -> None:
        """生成占位图。"""
        img = Image.new("RGB", (self.image_width, self.image_height), (21, 27, 38))
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 16, self.image_width - 16, self.image_height - 16), outline=(255, 180, 0), width=4)
        draw.multiline_text((40, 50), f"SHOT {shot_id}\n{prompt[:120]}", fill=(240, 240, 240))
        img.save(output)

    @staticmethod
    def _path_to_data_url(path: Path) -> str:
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{encoded}"
