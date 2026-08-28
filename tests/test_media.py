import base64
from pathlib import Path

from zalo_order_crawler.crawler import ZaloCrawler


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakePage:
    def evaluate(self, _: str, __: object) -> dict[str, str]:
        return {
            "base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            # Zalo đôi khi khai báo image/jpg dù payload thực tế là PNG.
            "mimeType": "image/jpg",
        }


def test_save_blob_image_to_assets_and_rewrite_html(tmp_path: Path) -> None:
    crawler = ZaloCrawler(FakePage(), None, {})  # type: ignore[arg-type]
    node = {
        "sourceId": "bb_msg_id_123",
        "html": '<div><img src="blob:https://chat.zalo.me/photo-1"></div>',
        "media": [
            {
                "src": "blob:https://chat.zalo.me/photo-1",
                "htmlSrc": "blob:https://chat.zalo.me/photo-1",
                "role": "message_image",
                "domIndex": 0,
            }
        ],
    }

    media = crawler._save_node_media(node, "bb_msg_id_123", tmp_path / "assets")
    rewritten = crawler._rewrite_media_sources(node["html"])

    assert len(media) == 1
    assert media[0].role == "message_image"
    assert media[0].mime_type == "image/png"
    assert media[0].size_bytes == len(PNG_BYTES)
    assert (tmp_path / media[0].path).read_bytes() == PNG_BYTES
    assert "blob:" not in rewritten
    assert media[0].path in rewritten
