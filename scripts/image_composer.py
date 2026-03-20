"""参考板合成器。

移植自 xyz-video-creator 的 image_composer.py，
简化为 PIL-only 实现（不依赖 OpenCV），只支持本地文件路径。

用途：将角色参考图 + 场景图拼成一张参考板，作为单张参考图传给 Gemini 生成场景图。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


class ReferenceBoard:
    """参考板合成器 — 角色全身图 + 特写 + 场景图拼成一张。"""

    def create_board(
        self,
        character_images: list[Path],
        output_path: Path,
        scene_image: Path | None = None,
        character_closeups: list[Path] | None = None,
        output_size: tuple[int, int] = (1280, 720),
    ) -> Path:
        """创建参考板。

        布局（来自 oii/image_composer.py）：
        ┌─────────────────────────────────────────────────────┐
        │   ┌────────┐                      ┌────────┐       │
        │   │ 角色1  │                      │ 角色1  │       │
        │   │ 全身图 │      (留白)          │ 特写图 │       │
        │   └────────┘                      └────────┘       │
        │   ┌────────────────────┐                           │
        │   │      场景图        │          (留白)           │
        │   └────────────────────┘                           │
        │   ┌────────┐                      ┌────────┐       │
        │   │ 角色2  │                      │ 角色2  │       │
        │   │ 全身图 │      (留白)          │ 特写图 │       │
        │   └────────┘                      └────────┘       │
        └─────────────────────────────────────────────────────┘

        Args:
            character_images: 角色参考图文件路径列表
            output_path: 输出参考板路径
            scene_image: 可选场景图
            character_closeups: 可选特写图（没有则自动裁剪）
            output_size: 输出尺寸 (width, height)

        Returns:
            输出文件路径
        """
        width, height = output_size
        canvas = Image.new("RGB", (width, height), "white")

        # 加载角色图
        char_imgs = []
        for p in character_images:
            img = self._load_local(p)
            if img:
                char_imgs.append(img)

        if not char_imgs:
            logger.warning("无角色图片，生成空白参考板")
            canvas.save(output_path, "PNG")
            return output_path

        # 加载场景图
        scene_img = self._load_local(scene_image) if scene_image else None

        # 特写图：优先使用提供的，缺失的从全身图裁剪
        closeup_imgs: list[Image.Image] = []
        if character_closeups:
            for p in character_closeups:
                img = self._load_local(p)
                if img:
                    closeup_imgs.append(img)
        # 补充缺失的
        for i in range(len(closeup_imgs), len(char_imgs)):
            closeup_imgs.append(self._crop_closeup(char_imgs[i]))

        # 布局参数（来自 oii，标准双角色板）
        margin = 15
        gap = 10
        char_max_h = int(height * 0.26)
        char_max_w = int(width * 0.10)
        scene_max_w = int(width * 0.32)
        scene_max_h = int(height * 0.28)
        detail_max_h = int(height * 0.32)
        detail_max_w = int(width * 0.22)
        right_x = int(width * 0.62)

        if len(char_imgs) == 1:
            # 单角色
            img1 = self._resize_contain(char_imgs[0], char_max_w, char_max_h)
            canvas.paste(img1, (margin, margin))

            if scene_img:
                sr = self._resize_contain(scene_img, scene_max_w, scene_max_h)
                canvas.paste(sr, (margin, margin + char_max_h + gap))

            if closeup_imgs:
                d1 = self._resize_contain(closeup_imgs[0], detail_max_w, detail_max_h)
                canvas.paste(d1, (right_x, margin))

        elif len(char_imgs) >= 2:
            # 双角色
            img1 = self._resize_contain(char_imgs[0], char_max_w, char_max_h)
            canvas.paste(img1, (margin, margin))

            if closeup_imgs:
                d1 = self._resize_contain(closeup_imgs[0], detail_max_w, detail_max_h)
                canvas.paste(d1, (right_x, margin))

            if scene_img:
                sr = self._resize_contain(scene_img, scene_max_w, scene_max_h)
                canvas.paste(sr, (margin, margin + char_max_h + gap))

            img2 = self._resize_contain(char_imgs[1], char_max_w, char_max_h)
            y_row3 = height - margin - char_max_h
            canvas.paste(img2, (margin, y_row3))

            if len(closeup_imgs) >= 2:
                d2 = self._resize_contain(closeup_imgs[1], detail_max_w, detail_max_h)
                d2_y = height - margin - d2.height
                canvas.paste(d2, (right_x, d2_y))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, "PNG")
        logger.info(f"参考板已生成: {output_path}")
        return output_path

    # ── 内部方法 ──────────────────────────────────────────────

    @staticmethod
    def _resize_contain(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
        """等比例缩放，使图片适应指定尺寸。"""
        ratio = min(max_w / img.width, max_h / img.height)
        new_w = int(img.width * ratio)
        new_h = int(img.height * ratio)
        return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    @staticmethod
    def _crop_closeup(img: Image.Image) -> Image.Image:
        """从全身图裁剪特写（PIL-only，不依赖 OpenCV）。

        策略：裁取上 40% 高度，两侧各内缩 15%。
        """
        w, h = img.size
        crop_h = int(h * 0.4)
        margin_x = int(w * 0.15)
        return img.crop((margin_x, 0, w - margin_x, crop_h))

    @staticmethod
    def _load_local(path: Path | str | None) -> Optional[Image.Image]:
        """加载本地图片文件。"""
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            logger.warning(f"图片不存在: {p}")
            return None
        try:
            return Image.open(p).convert("RGB")
        except Exception as e:
            logger.warning(f"加载图片失败 {p}: {e}")
            return None
