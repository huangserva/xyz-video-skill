#!/opt/homebrew/bin/python3.14
"""品牌适配模块：Logo、配色、产品图融入与水印保护。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw

from utils import create_output_dir, read_json, setup_logging, timestamp_id, write_json

LOGGER = logging.getLogger("ad_brand")


class BrandAdapter:
    """执行品牌视觉适配。"""

    def __init__(
        self,
        manifest: dict[str, Any],
        output_dir: Path,
        storyboard: dict[str, Any] | None = None,
        logo_path: Path | None = None,
        brand_color: str = "#FF6A00",
        product_image: Path | None = None,
        product_shots: set[int] | None = None,
        watermark_text: str = "Brand Protected",
        title_text: str = "品牌广告",
    ):
        self.manifest = manifest
        self.output_dir = output_dir
        self.storyboard = storyboard or {}
        self.logo_path = logo_path
        try:
            ImageColor.getrgb(brand_color)
        except ValueError:
            LOGGER.warning(f"无效的 brand_color '{brand_color}'，回退到 #FF6A00")
            brand_color = "#FF6A00"
        self.brand_color = brand_color
        self.product_image = product_image
        self.product_shots = product_shots or set()
        self.watermark_text = watermark_text
        self.title_text = title_text

        self.out_images = output_dir / "images"
        self.out_images.mkdir(parents=True, exist_ok=True)

        self.logo_img = self._load_rgba(self.logo_path)
        self.product_img = self._load_rgba(self.product_image)

        self.subtitle_map = self._build_subtitle_map()

    def _build_subtitle_map(self) -> dict[int, str]:
        """从 storyboard 提取字幕映射，支持 scenes > shots 和 flat shots 两种格式。"""
        if not isinstance(self.storyboard, dict):
            return {}
        result: dict[int, str] = {}
        # 优先从 scenes > shots 读取（当前标准格式）
        for scene in self.storyboard.get("scenes", []):
            if not isinstance(scene, dict):
                continue
            for s in scene.get("shots", []):
                if isinstance(s, dict):
                    result[int(s.get("id", 0))] = str(s.get("subtitle", ""))
        # 向下兼容 flat shots
        if not result:
            for s in self.storyboard.get("shots", []):
                if isinstance(s, dict):
                    result[int(s.get("id", 0))] = str(s.get("subtitle", ""))
        return result

    def apply(self) -> dict[str, Any]:
        """执行全量品牌化。"""
        images = self.manifest.get("images", [])
        branded_images = []

        for item in images:
            if not isinstance(item, dict):
                continue
            shot_id = int(item.get("shot_id", 0) or 0)
            src = Path(str(item.get("path", ""))).expanduser()
            if not src.exists():
                LOGGER.warning("图片不存在，跳过: %s", src)
                continue

            dst = self.out_images / f"shot_{shot_id:03d}_brand.png"
            subtitle = self.subtitle_map.get(shot_id, "")
            self._brand_single_image(src, dst, subtitle, shot_id)

            new_item = dict(item)
            new_item["path"] = str(dst)
            new_item["branded"] = True
            branded_images.append(new_item)

        # 从已品牌化的图片推断标题帧尺寸，回退到 1080x1920
        title_w, title_h = 1080, 1920
        if branded_images:
            first_branded = Path(branded_images[0].get("path", ""))
            if first_branded.exists():
                try:
                    with Image.open(first_branded) as probe:
                        title_w, title_h = probe.size
                except Exception:
                    pass
        intro = self._make_title_frame("品牌开场", filename="intro.png", width=title_w, height=title_h)
        outro = self._make_title_frame("立即行动", filename="outro.png", width=title_w, height=title_h)

        new_manifest = dict(self.manifest)
        new_manifest["images"] = branded_images
        new_manifest["branding"] = {
            "generated_at": timestamp_id(),
            "brand_color": self.brand_color,
            "logo": str(self.logo_path) if self.logo_path else None,
            "watermark_text": self.watermark_text,
            "intro_frame": str(intro),
            "outro_frame": str(outro),
        }
        return new_manifest

    def _load_rgba(self, path: Path | None) -> Image.Image | None:
        """读取并转换为 RGBA（copy 后关闭文件句柄）。"""
        if not path:
            return None
        if not path.exists():
            LOGGER.warning("图片资源不存在: %s", path)
            return None
        with Image.open(path) as img:
            return img.convert("RGBA").copy()

    def _brand_single_image(self, src: Path, dst: Path, subtitle: str, shot_id: int) -> None:
        """品牌化单张图片。"""
        base = Image.open(src).convert("RGBA")
        width, height = base.size

        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        brand_rgb = ImageColor.getrgb(self.brand_color)

        # 顶部标题条（品牌色）
        top_h = max(72, int(height * 0.10))
        draw.rectangle((0, 0, width, top_h), fill=(brand_rgb[0], brand_rgb[1], brand_rgb[2], 180))
        draw.text((24, int(top_h * 0.3)), self.title_text, fill=(255, 255, 255, 230))

        # 底部字幕条
        if subtitle:
            bot_h = max(84, int(height * 0.12))
            draw.rectangle((0, height - bot_h, width, height), fill=(0, 0, 0, 160))
            draw.text((24, height - int(bot_h * 0.65)), subtitle[:40], fill=brand_rgb + (255,))

        # 水印保护（对角平铺）
        step_x = max(220, width // 4)
        step_y = max(160, height // 4)
        for x in range(-width, width * 2, step_x):
            for y in range(-height, height * 2, step_y):
                draw.text((x, y), self.watermark_text, fill=(255, 255, 255, 28))

        base = Image.alpha_composite(base, overlay)

        # 融入产品图（指定镜头）
        if self.product_img and shot_id in self.product_shots:
            base = self._paste_corner(base, self.product_img, corner="left_bottom", scale=0.22)

        # 叠加 logo（右下）
        if self.logo_img:
            base = self._paste_corner(base, self.logo_img, corner="right_bottom", scale=0.16)

        base.convert("RGB").save(dst)

    def _paste_corner(self, base: Image.Image, patch: Image.Image, corner: str, scale: float = 0.2) -> Image.Image:
        """将贴图按比例贴到指定角落。"""
        base_rgba = base.convert("RGBA")
        bw, bh = base_rgba.size
        max_w = int(bw * scale)
        ratio = min(max_w / patch.width, (bh * 0.28) / patch.height)
        resized = patch.resize((max(1, int(patch.width * ratio)), max(1, int(patch.height * ratio))))

        margin = max(18, int(min(bw, bh) * 0.02))
        if corner == "right_bottom":
            pos = (bw - resized.width - margin, bh - resized.height - margin)
        elif corner == "left_bottom":
            pos = (margin, bh - resized.height - margin)
        else:
            pos = (margin, margin)

        base_rgba.paste(resized, pos, resized)
        return base_rgba

    def _make_title_frame(self, text: str, filename: str, width: int = 1080, height: int = 1920) -> Path:
        """生成片头/片尾品牌画面，尺寸适配目标平台。"""
        size = (width, height)
        img = Image.new("RGBA", size, color=ImageColor.getrgb(self.brand_color) + (255,))
        draw = ImageDraw.Draw(img)
        draw.text((80, 180), self.title_text, fill=(255, 255, 255, 235))
        draw.text((80, 320), text, fill=(255, 255, 255, 215))

        if self.logo_img:
            img = self._paste_corner(img, self.logo_img, corner="right_bottom", scale=0.18)

        out = self.output_dir / filename
        img.convert("RGB").save(out)
        return out


def _parse_shot_ids(raw: str) -> set[int]:
    """解析镜头 ID 列表。"""
    result = set()
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数。"""
    parser = argparse.ArgumentParser(description="品牌适配模块")
    parser.add_argument("--assets_manifest", required=True, help="素材 manifest JSON")
    parser.add_argument("--storyboard_file", help="分镜 JSON（用于字幕）")
    parser.add_argument("--logo_path", help="Logo 图片路径")
    parser.add_argument("--brand_color", default="#FF6A00", help="品牌主色（HEX）")
    parser.add_argument("--product_image", help="产品图路径")
    parser.add_argument("--product_shots", default="", help="产品图插入镜头 ID，例如 2,4")
    parser.add_argument("--watermark_text", default="Brand Protected", help="水印文案")
    parser.add_argument("--title_text", default="品牌广告", help="标题文案")
    parser.add_argument("--output", help="输出 manifest 路径")
    parser.add_argument("--output_dir", help="输出目录（默认 ~/ad-output/{timestamp}/brand）")
    parser.add_argument("--verbose", action="store_true", help="打印调试日志")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    manifest = read_json(Path(args.assets_manifest).expanduser().resolve())
    storyboard = read_json(Path(args.storyboard_file).expanduser().resolve()) if args.storyboard_file else None

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else create_output_dir() / "brand"
    output_dir.mkdir(parents=True, exist_ok=True)

    adapter = BrandAdapter(
        manifest=manifest,
        output_dir=output_dir,
        storyboard=storyboard,
        logo_path=Path(args.logo_path).expanduser().resolve() if args.logo_path else None,
        brand_color=args.brand_color,
        product_image=Path(args.product_image).expanduser().resolve() if args.product_image else None,
        product_shots=_parse_shot_ids(args.product_shots),
        watermark_text=args.watermark_text,
        title_text=args.title_text,
    )

    result = adapter.apply()

    output_path = Path(args.output).expanduser().resolve() if args.output else output_dir / "brand_manifest.json"
    write_json(output_path, result)
    print(output_path)


if __name__ == "__main__":
    main()
