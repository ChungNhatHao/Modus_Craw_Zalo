from pathlib import Path

import pytest

from zalo_order_crawler.models import (
    CleanMessage,
    ImageOcrResult,
    MediaAsset,
    OcrLineItem,
    OrderDecision,
    ProductCatalogEntry,
)
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

    def fake_extract_image(
        message_id: str,
        media_path: str,
        mime_type: str,
        decision: OrderDecision,
    ):
        assert decision.is_order is True
        calls.append(message_id)
        return ocr._to_result(
            message_id,
            media_path,
            _OcrExtraction(
                applicable=True,
                image_quality_score=0.96,
                image_quality_affects_output=False,
                image_quality_reason="Ảnh rõ",
                items=[OcrLineItem(product_name="Rau muống")],
            ),
        )

    monkeypatch.setattr(ocr, "_extract_image", fake_extract_image)

    results = ocr.extract(messages, decisions)

    assert calls == ["order-1"]
    assert len(results) == 1
    assert results[0].message_id == "order-1"
    assert results[0].needs_review is False


def test_extract_routes_high_quality_ocr_when_order_data_needs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "order.jpg").write_bytes(b"order-image")
    message = CleanMessage(
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
    )
    decision = OrderDecision(
        message_id="order-1",
        is_order=True,
        confidence=0.95,
        data_confidence=0.8,
        needs_review=True,
        reason="Danh sách sản phẩm và số lượng chưa khớp.",
    )
    ocr = make_ocr(tmp_path)
    high_quality_result = ocr._to_result(
        "order-1",
        "assets/order.jpg",
        _OcrExtraction(
            applicable=True,
            image_quality_score=0.9,
            image_quality_affects_output=False,
            image_quality_reason="Ảnh rõ",
            items=[OcrLineItem(product_name="Rau muống", quantity=2)],
        ),
    )
    monkeypatch.setattr(ocr, "_extract_image", lambda *_: high_quality_result)

    result = ocr.extract([message], [decision])[0]

    assert result.image_quality_score == 0.9
    assert result.image_quality_affects_output is False
    assert result.needs_review is True
    assert result.review_reason is not None
    assert "80%" in result.review_reason


def test_to_result_clears_items_when_not_applicable() -> None:
    parsed = _OcrExtraction(
        applicable=False,
        skip_reason="Ảnh có đơn giá, là phiếu nhận hàng",
        image_quality_score=0.97,
        image_quality_affects_output=False,
        image_quality_reason="Ảnh rõ",
        items=[OcrLineItem(product_name="Rau muống")],
    )

    result = GeminiOrderImageOcr._to_result("m1", "assets/m1.jpg", parsed)

    assert result.applicable is False
    assert result.items == []
    assert result.skip_reason == "Ảnh có đơn giá, là phiếu nhận hàng"


@pytest.mark.parametrize(
    ("score", "affects_output", "expected_review"),
    [
        (0.84, True, True),
        (0.85, True, False),
        (0.40, False, False),
    ],
)
def test_to_result_applies_image_quality_review_threshold(
    score: float,
    affects_output: bool,
    expected_review: bool,
) -> None:
    parsed = _OcrExtraction(
        applicable=True,
        image_quality_score=score,
        image_quality_affects_output=affects_output,
        image_quality_reason="Ảnh bị mờ",
        items=[OcrLineItem(product_name="Rau muống", quantity=2)],
    )

    result = GeminiOrderImageOcr._to_result("m1", "assets/m1.jpg", parsed)

    assert result.image_quality_score == score
    assert result.image_quality_affects_output is affects_output
    assert result.image_quality_reason == "Ảnh bị mờ"
    assert result.needs_review is expected_review
    if expected_review:
        assert result.review_reason == "Ảnh bị mờ"
    else:
        assert result.review_reason is None


def test_resolve_media_path_cannot_escape_result_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"image")
    ocr = make_ocr(tmp_path)

    with pytest.raises(ValueError, match="nằm ngoài"):
        ocr._resolve_media_path(str(outside))


def test_catalog_alias_normalises_product_and_fills_unit() -> None:
    catalog = (
        ProductCatalogEntry(
            branch_name="Chi nhánh Tân Phú",
            product_name="Ngò gai",
            unit="kg",
            aliases=["ngà gai"],
        ),
    )
    result = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="NGÀ GAI", quantity=0.5)],
    )

    normalised = GeminiOrderImageOcr._canonicalise_result(result, catalog)

    assert normalised.items[0].product_name == "Ngò gai"
    assert normalised.items[0].unit == "kg"
    assert normalised.needs_review is False


def test_catalog_routes_unknown_product_to_review() -> None:
    catalog = (ProductCatalogEntry(product_name="Ngò gai", unit="kg"),)
    result = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="Bong nha", quantity=0.5)],
    )

    normalised = GeminiOrderImageOcr._canonicalise_result(result, catalog)

    assert normalised.needs_review is True
    assert normalised.review_reason is not None
    assert "Bong nha" in normalised.review_reason


def test_merge_results_accepts_matching_full_and_tiled_readings() -> None:
    full = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        image_quality_score=0.92,
        items=[
            OcrLineItem(
                customer_name="Sườn 6",
                product_name="Cần tàu",
                quantity=0.5,
            )
        ],
    )
    tiled = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        image_quality_score=0.95,
        items=[OcrLineItem(product_name="Cần tàu", quantity=0.5)],
    )

    merged = GeminiOrderImageOcr._merge_results(full, [tiled], ())

    assert len(merged.items) == 1
    assert merged.items[0].customer_name == "Sườn 6"
    assert merged.needs_review is False


def test_merge_results_routes_quantity_disagreement_to_review() -> None:
    full = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="Cần tàu", quantity=0.3)],
    )
    tiled = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="Cần tàu", quantity=0.5)],
    )

    merged = GeminiOrderImageOcr._merge_results(full, [tiled], ())

    assert merged.items[0].quantity == 0.3
    assert merged.needs_review is True
    assert merged.review_reason is not None
    assert "xác minh cuối" in merged.review_reason


def test_merge_results_uses_focused_verification_and_preserves_full_quality() -> None:
    full = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        image_quality_score=0.95,
        image_quality_affects_output=False,
        items=[
            OcrLineItem(
                customer_name="Sườn 6",
                product_name="Cần tàu",
                quantity=0.3,
            )
        ],
    )
    tiled = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        image_quality_score=0.7,
        image_quality_affects_output=True,
        items=[OcrLineItem(customer_name="Tên sai", product_name="Cần tàu", quantity=0.5)],
    )
    verification = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="Cần tàu", quantity=0.5)],
    )

    merged = GeminiOrderImageOcr._merge_results(
        full,
        [tiled],
        (),
        verification_result=verification,
    )

    assert merged.items[0].quantity == 0.5
    assert merged.items[0].customer_name == "Sườn 6"
    assert merged.image_quality_score == 0.95
    assert merged.image_quality_affects_output is False
    assert merged.needs_review is False


def test_verification_candidates_only_include_disagreements() -> None:
    full = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[
            OcrLineItem(product_name="Cần tàu", quantity=0.5),
            OcrLineItem(product_name="Rau muống", quantity=2),
        ],
    )
    tiled = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[
            OcrLineItem(product_name="Cần tàu", quantity=0.5),
            OcrLineItem(product_name="Rau muống", quantity=3),
            OcrLineItem(product_name="Cải ngọt", quantity=1),
        ],
    )

    candidates = GeminiOrderImageOcr._verification_candidates(full, [tiled])

    assert candidates == [
        {
            "product_name": "Rau muống",
            "full_quantity": 2,
            "tile_quantity": 3,
        },
        {
            "product_name": "Cải ngọt",
            "full_quantity": None,
            "tile_quantity": 1,
        },
    ]


def test_merge_drops_unverified_tile_only_product() -> None:
    full = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[OcrLineItem(product_name="Cần tàu", quantity=0.5)],
    )
    tiled = ImageOcrResult(
        message_id="m1",
        media_path="assets/m1.jpg",
        applicable=True,
        items=[
            OcrLineItem(product_name="Cần tàu", quantity=0.5),
            OcrLineItem(product_name="Dòng trống bị đọc nhầm", quantity=1),
        ],
    )

    merged = GeminiOrderImageOcr._merge_results(full, [tiled], ())

    assert [item.product_name for item in merged.items] == ["Cần tàu"]
    assert merged.needs_review is True
    assert "Dòng trống bị đọc nhầm" in (merged.review_reason or "")
