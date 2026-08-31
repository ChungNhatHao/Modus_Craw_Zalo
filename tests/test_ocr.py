from pathlib import Path

import pytest

from zalo_order_crawler.models import CleanMessage, MediaAsset, OcrLineItem, OrderDecision
from zalo_order_crawler.ocr import GeminiOrderImageOcr, _OcrExtraction


def make_ocr(tmp_path: Path) -> GeminiOrderImageOcr:
    return GeminiOrderImageOcr(
        api_key="test-key",
        model="test-model",
        cache_dir=tmp_path / "cache",
        media_base_dir=tmp_path,
    )


def test_extract_only_processes_images_from_order_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "order.jpg").write_bytes(b"order-image")
    (assets / "chitchat.jpg").write_bytes(b"chitchat-image")
    messages = [
        CleanMessage(
            message_id="order-1",
            sequence=1,
            content="[Hình ảnh]",
            message_type="image",
            media=[
                MediaAsset(
                    path="assets/order.jpg",
                    mime_type="image/jpeg",
                    role="message_image",
                )
            ],
        ),
        CleanMessage(
            message_id="chitchat-1",
            sequence=2,
            content="[Hình ảnh]",
            message_type="image",
            media=[
                MediaAsset(
                    path="assets/chitchat.jpg",
                    mime_type="image/jpeg",
                    role="message_image",
                )
            ],
        ),
    ]
    decisions = [
        OrderDecision(
            message_id="order-1",
            is_order=True,
            confidence=0.98,
            data_confidence=0.9,
            reason="Có đơn",
        ),
        OrderDecision(
            message_id="chitchat-1",
            is_order=False,
            confidence=0.95,
            data_confidence=0,
            reason="Xã giao",
        ),
    ]
    ocr = make_ocr(tmp_path)
    calls: list[str] = []

    def fake_extract_image(message_id: str, media_path: str, mime_type: str):
        calls.append(message_id)
        return ocr._to_result(
            message_id,
            media_path,
            _OcrExtraction(applicable=True, items=[OcrLineItem(product_name="Rau muống")]),
        )

    monkeypatch.setattr(ocr, "_extract_image", fake_extract_image)

    results = ocr.extract(messages, decisions)

    assert calls == ["order-1"]
    assert len(results) == 1
    assert results[0].message_id == "order-1"


def test_to_result_clears_items_when_not_applicable() -> None:
    parsed = _OcrExtraction(
        applicable=False,
        skip_reason="Ảnh có đơn giá, là phiếu nhận hàng",
        items=[OcrLineItem(product_name="Rau muống")],
    )

    result = GeminiOrderImageOcr._to_result("m1", "assets/m1.jpg", parsed)

    assert result.applicable is False
    assert result.items == []
    assert result.skip_reason == "Ảnh có đơn giá, là phiếu nhận hàng"


def test_resolve_media_path_cannot_escape_result_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"image")
    ocr = make_ocr(tmp_path)

    with pytest.raises(ValueError, match="nằm ngoài"):
        ocr._resolve_media_path(str(outside))
