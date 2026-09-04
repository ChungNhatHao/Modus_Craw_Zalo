from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError


@dataclass(frozen=True)
class PreparedOcrTile:
    data: bytes
    mime_type: str
    position: int
    total: int


def prepare_ocr_tiles(
    payload: bytes,
    *,
    tile_count: int = 4,
    overlap_ratio: float = 0.35,
    target_long_edge: int = 2_000,
) -> list[PreparedOcrTile]:
    """Orient a dense landscape order form, then split and enhance it."""
    if tile_count < 2 or tile_count > 8:
        raise ValueError("OCR_TILE_COUNT phải nằm trong khoảng 2..8.")
    if not 0 <= overlap_ratio < 1:
        raise ValueError("Tỷ lệ chồng lấn tile OCR không hợp lệ.")

    try:
        with Image.open(BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, UnidentifiedImageError):
        return []

    # The restaurant order forms handled by this crawler are landscape tables,
    # but phone cameras commonly store their pixels in portrait orientation
    # without a useful EXIF orientation tag.  Cropping that sideways image made
    # Gemini associate quantities with rows from a neighbouring product block.
    if image.height > image.width:
        image = image.transpose(Image.Transpose.ROTATE_90)

    width, height = image.size
    long_edge = max(width, height)
    short_edge = min(width, height)
    if long_edge < 600 or short_edge < 300:
        return []

    split_horizontally = width >= height
    span = width if split_horizontally else height
    base_span = span / tile_count
    overlap = max(1, round(base_span * overlap_ratio))
    tiles: list[PreparedOcrTile] = []

    for index in range(tile_count):
        start = max(0, round(index * base_span) - overlap)
        end = min(span, round((index + 1) * base_span) + overlap)
        box = (
            (start, 0, end, height)
            if split_horizontally
            else (0, start, width, end)
        )
        tile = image.crop(box)
        tile = ImageOps.autocontrast(tile, cutoff=1)
        tile = ImageEnhance.Contrast(tile).enhance(1.2)
        tile = ImageEnhance.Sharpness(tile).enhance(1.5)
        tile = tile.filter(ImageFilter.UnsharpMask(radius=1.2, percent=125, threshold=3))

        scale = min(3.0, max(1.0, target_long_edge / max(tile.size)))
        if scale > 1.05:
            tile = tile.resize(
                (round(tile.width * scale), round(tile.height * scale)),
                Image.Resampling.LANCZOS,
            )

        output = BytesIO()
        tile.save(output, format="JPEG", quality=95, optimize=True)
        tiles.append(
            PreparedOcrTile(
                data=output.getvalue(),
                mime_type="image/jpeg",
                position=index + 1,
                total=tile_count,
            )
        )
    return tiles
