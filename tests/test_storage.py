from datetime import date
from pathlib import Path

from zalo_order_crawler.models import (
    CleanMessage,
    ImageOcrResult,
    MediaAsset,
    OcrLineItem,
    OrderDecision,
)
from zalo_order_crawler.storage import safe_slug, write_order_ocr_csv, write_orders_csv


def test_safe_slug() -> None:
    assert safe_slug("Nhóm Order Quận 1") == "Nhóm-Order-Quận-1"


def test_write_orders_csv_has_excel_bom_and_vietnamese(tmp_path: Path) -> None:
    message = CleanMessage(
        message_id="m1",
        sequence=0,
        sender="Lan",
        content="Chốt 2 áo",
        media=[
            MediaAsset(
                path="assets/m1.jpg",
                mime_type="image/jpeg",
                role="message_image",
            )
        ],
    )
    decision = OrderDecision(
        message_id="m1",
        is_order=True,
        confidence=0.95,
        data_confidence=0.87,
        needs_review=True,
        reason="Có ý định chốt",
        branch_name="Chi nhánh Tân Phú",
        products=["áo"],
        quantities=["2"],
    )
    output = tmp_path / "orders.csv"

    write_orders_csv(output, [message], [decision])

    payload = output.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert "Chốt 2 áo" in payload.decode("utf-8-sig")
    assert "assets/m1.jpg" in payload.decode("utf-8-sig")
    assert "order_confidence_percent" in payload.decode("utf-8-sig")
    assert "data_confidence_percent" in payload.decode("utf-8-sig")
    assert "branch_name" in payload.decode("utf-8-sig")
    assert "Chi nhánh Tân Phú" in payload.decode("utf-8-sig")
    assert ",95.0,0.87,87.0,True," in payload.decode("utf-8-sig")


def test_write_order_ocr_csv_skips_non_applicable_results(tmp_path: Path) -> None:
    results = [
        ImageOcrResult(
            message_id="m1",
            media_path="assets/m1.jpg",
            applicable=True,
            items=[
                OcrLineItem(
                    customer_code="S6",
                    customer_name="Quán Rau",
                    product_name="Rau muống",
                    unit="kg",
                    quantity=2,
                )
            ],
        ),
        ImageOcrResult(
            message_id="m2",
            media_path="assets/m2.jpg",
            applicable=False,
            skip_reason="Ảnh có đơn giá, là phiếu nhận hàng",
        ),
    ]
    output = tmp_path / "order_ocr.csv"

    write_order_ocr_csv(output, "Rau SMO", date(2026, 8, 30), results)

    payload = output.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    assert "Rau muống" in text
    assert "30-08-2026" in text
    assert "image_quality_percent" in text
    assert ",100.0,False,False," in text
    assert "m2" not in text


def test_write_order_ocr_csv_keeps_low_quality_result_without_items(
    tmp_path: Path,
) -> None:
    output = tmp_path / "order_ocr.csv"

    write_order_ocr_csv(
        output,
        "Rau SMO",
        date(2026, 8, 30),
        [
            ImageOcrResult(
                message_id="m1",
                media_path="assets/m1.jpg",
                applicable=False,
                image_quality_score=0.6,
                image_quality_affects_output=True,
                image_quality_reason="Ảnh mờ",
                needs_review=True,
            )
        ],
    )

    text = output.read_text(encoding="utf-8-sig")
    assert "m1" in text
    assert "60.0" in text
    assert "Ảnh mờ" in text
