from io import BytesIO

from PIL import Image

from zalo_order_crawler.image_processing import prepare_ocr_tiles


def test_prepare_ocr_tiles_splits_long_edge_with_overlap_and_upscales() -> None:
    source = BytesIO()
    Image.new("RGB", (768, 1024), "white").save(source, format="JPEG")

    tiles = prepare_ocr_tiles(source.getvalue(), tile_count=4)

    assert len(tiles) == 4
    assert [tile.position for tile in tiles] == [1, 2, 3, 4]
    assert all(tile.total == 4 for tile in tiles)
    assert all(tile.mime_type == "image/jpeg" for tile in tiles)
    sizes = []
    for tile in tiles:
        with Image.open(BytesIO(tile.data)) as image:
            sizes.append(image.size)
    assert all(max(size) == 2000 for size in sizes)
    assert all(width < height for width, height in sizes)


def test_prepare_ocr_tiles_ignores_too_small_or_invalid_images() -> None:
    tiny = BytesIO()
    Image.new("RGB", (200, 300), "white").save(tiny, format="PNG")

    assert prepare_ocr_tiles(tiny.getvalue()) == []
    assert prepare_ocr_tiles(b"not-an-image") == []
