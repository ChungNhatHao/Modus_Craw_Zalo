from datetime import date
from pathlib import Path
from typing import Any

import pytest
from requests.exceptions import ReadTimeout

from zalo_order_crawler.drive_output import (
    DriveResource,
    GoogleDriveOutputError,
    GoogleDriveOutputPublisher,
    ImageUploadResult,
)
from zalo_order_crawler.models import (
    CleanMessage,
    ImageOcrResult,
    MediaAsset,
    OcrLineItem,
    OrderDecision,
    ProductCatalogEntry,
)


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def request(self, *_: Any, **__: Any) -> FakeResponse:
        return FakeResponse(self.payload)


class FlakyReadSession:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def request(self, *_: Any, **__: Any) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise ReadTimeout("Google Sheets tạm thời không phản hồi")
        return FakeResponse({"ok": True})


class RecordingPublisher(GoogleDriveOutputPublisher):
    def __init__(self) -> None:
        super().__init__(session=object(), parent_folder_id="folder123")
        self.sheet_name = ""
        self.folder_name = ""
        self.rows: list[list[Any]] = []
        self.uploaded: list[tuple[str, str, str]] = []
        self.ocr_sheet_requested = False
        self.ocr_rows: list[list[Any]] = []
        self.ocr_review_sheet_requested = False
        self.ocr_review_rows: list[list[Any]] = []
        self.ocr_rows_removed_for_review = 0
        self.ocr_review_rows_replaced = 0
        self.positioned_ocr_tabs: tuple[int, int | None] | None = None

    def _ensure_daily_sheet(self, name: str) -> tuple[DriveResource, str, int]:
        self.sheet_name = name
        return (
            DriveResource(
                id="sheet-id",
                name=name,
                url="https://docs.google.com/spreadsheets/d/sheet-id/edit",
            ),
            "Tin nhắn",
            1000,
        )

    def _append_unique_text_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        assert spreadsheet_id == "sheet-id"
        assert sheet_title == "Tin nhắn"
        assert row_count == 1000
        self.rows = rows
        return len(rows)

    def _ensure_image_folder(self, name: str) -> DriveResource:
        self.folder_name = name
        return DriveResource(
            id="image-folder-id",
            name=name,
            url="https://drive.google.com/drive/folders/image-folder-id",
        )

    def _upload_unique_image(
        self,
        folder_id: str,
        *,
        group_name: str,
        message_id: str,
        media: MediaAsset,
        media_path: Path,
    ) -> ImageUploadResult:
        assert folder_id == "image-folder-id"
        self.uploaded.append((group_name, message_id, media_path.name))
        return ImageUploadResult(
            resource=DriveResource(
                id="image-id",
                name=media_path.name,
                url=f"https://drive.google.com/file/d/{message_id}/view",
            ),
            created=True,
        )

    def _ensure_ocr_sheet(self, spreadsheet_id: str) -> tuple[int, str, int]:
        assert spreadsheet_id == "sheet-id"
        self.ocr_sheet_requested = True
        return 42, "Đơn hàng OCR", 1000

    def _append_unique_ocr_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        assert spreadsheet_id == "sheet-id"
        assert sheet_title == "Đơn hàng OCR"
        assert row_count == 1000 - self.ocr_rows_removed_for_review
        self.ocr_rows = rows
        return len(rows)

    def _ensure_ocr_review_sheet(self, spreadsheet_id: str) -> tuple[int, str, int]:
        assert spreadsheet_id == "sheet-id"
        self.ocr_review_sheet_requested = True
        return 84, "OCR cần kiểm tra", 1000

    def _append_unique_ocr_review_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        assert spreadsheet_id == "sheet-id"
        assert sheet_title == "OCR cần kiểm tra"
        assert row_count == 1000 - self.ocr_review_rows_replaced
        self.ocr_review_rows = rows
        return len(rows)

    def _remove_reviewed_orders_from_ocr_sheet(
        self,
        spreadsheet_id: str,
        group_name: str,
        message_ids: set[str],
    ) -> int:
        assert spreadsheet_id == "sheet-id"
        assert group_name == "Nhóm A"
        self.ocr_rows_removed_for_review = len(message_ids)
        return self.ocr_rows_removed_for_review

    def _remove_pending_ocr_review_rows(
        self,
        spreadsheet_id: str,
        group_name: str,
        message_ids: set[str],
    ) -> int:
        assert spreadsheet_id == "sheet-id"
        assert group_name == "Nhóm A"
        self.ocr_review_rows_replaced = len(message_ids)
        return self.ocr_review_rows_replaced

    def _position_ocr_tabs(
        self,
        spreadsheet_id: str,
        ocr_sheet_id: int,
        review_sheet_id: int | None = None,
    ) -> None:
        assert spreadsheet_id == "sheet-id"
        self.positioned_ocr_tabs = (ocr_sheet_id, review_sheet_id)


def test_publish_splits_text_and_message_images_by_selected_date(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    image = assets / "order.jpg"
    image.write_bytes(b"jpeg")
    thumbnail = assets / "thumbnail.jpg"
    thumbnail.write_bytes(b"thumbnail")
    messages = [
        CleanMessage(
            message_id="text-1",
            sequence=1,
            sender="Lan",
            timestamp_text="09:15",
            content="Chốt 2 áo",
            direction="incoming",
            message_type="text",
        ),
        CleanMessage(
            message_id="image-1",
            sequence=2,
            content="[Hình ảnh]",
            message_type="image",
            media=[
                MediaAsset(
                    path="assets/order.jpg",
                    mime_type="image/jpeg",
                    role="message_image",
                    sha256="abc",
                ),
                MediaAsset(
                    path="assets/thumbnail.jpg",
                    mime_type="image/jpeg",
                    role="link_thumbnail",
                    sha256="def",
                ),
            ],
        ),
    ]
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 8, 30),
        messages=messages,
    )

    assert publisher.sheet_name == "30-08-2026"
    assert publisher.folder_name == "30-08-2026_image"
    assert publisher.rows == [
        [
            "30-08-2026",
            "Nhóm A",
            "text-1",
            1,
            "Lan",
            "09:15",
            "incoming",
            "Chốt 2 áo",
            "text",
            "",
        ]
    ]
    assert publisher.uploaded == [("Nhóm A", "image-1", "order.jpg")]
    assert result["sheet"]["rows_added"] == 1
    assert result["image_folder"]["images_uploaded"] == 1


def test_publish_writes_ocr_rows_to_daily_google_sheet(tmp_path: Path) -> None:
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 8, 30),
        messages=[],
        decisions=[
            OrderDecision(
                message_id="m1",
                is_order=True,
                confidence=0.98,
                data_confidence=0.95,
                reason="Ảnh là đơn hàng",
                branch_name="Chi nhánh Phạm Văn Đồng",
                products=["Rau muống"],
                quantities=["2 kg"],
            )
        ],
        ocr_results=[
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
            )
        ],
    )

    assert publisher.sheet_name == "30-08-2026"
    assert publisher.ocr_sheet_requested is True
    assert publisher.ocr_rows == [
        [
            "30-08-2026",
            "Nhóm A",
            "Chi nhánh Phạm Văn Đồng",
            "m1",
            "S6",
            "Quán Rau",
            "Rau muống",
            "kg",
            2,
        ]
    ]
    assert result["ocr_sheet"]["rows_added"] == 1
    assert result["ocr_sheet"]["tab_name"] == "Đơn hàng OCR"
    assert result["ocr_sheet"]["url"].endswith("#gid=42")


def test_publish_writes_text_order_items_to_order_sheet(tmp_path: Path) -> None:
    publisher = RecordingPublisher()
    messages = [
        CleanMessage(
            message_id="m1",
            sequence=1,
            content="S6 thêm\n0,5kg cần tàu\n0,5kg chuối chát",
            message_type="text",
        ),
        CleanMessage(
            message_id="m2",
            sequence=2,
            content="Tân Phú thêm 0.5 me cục",
            message_type="text",
        ),
    ]
    decisions = [
        OrderDecision(
            message_id="m1",
            is_order=True,
            confidence=0.95,
            data_confidence=0.95,
            reason="Đơn bổ sung",
            branch_name="Chi nhánh Phạm Văn Đồng",
            products=["cần tàu", "chuối chát"],
            quantities=["0,5kg", "0,5 kg"],
        ),
        OrderDecision(
            message_id="m2",
            is_order=True,
            confidence=0.95,
            data_confidence=0.95,
            reason="Đơn bổ sung",
            branch_name="Chi nhánh Tân Phú",
            products=["me cục"],
            quantities=["0.5"],
        ),
    ]

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 9, 1),
        messages=messages,
        decisions=decisions,
    )

    assert publisher.ocr_rows == [
        [
            "01-09-2026",
            "Nhóm A",
            "Chi nhánh Phạm Văn Đồng",
            "m1",
            "",
            "",
            "cần tàu",
            "kg",
            0.5,
        ],
        [
            "01-09-2026",
            "Nhóm A",
            "Chi nhánh Phạm Văn Đồng",
            "m1",
            "",
            "",
            "chuối chát",
            "kg",
            0.5,
        ],
        [
            "01-09-2026",
            "Nhóm A",
            "Chi nhánh Tân Phú",
            "m2",
            "",
            "",
            "me cục",
            "",
            0.5,
        ],
    ]
    assert result["ocr_sheet"]["rows_added"] == 3
    assert publisher.ocr_review_sheet_requested is False


def test_publish_routes_text_order_needing_review_to_adjacent_tab(
    tmp_path: Path,
) -> None:
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 9, 1),
        messages=[
            CleanMessage(
                message_id="m1",
                sequence=1,
                content="Thêm 2 rau",
                message_type="text",
            )
        ],
        decisions=[
            OrderDecision(
                message_id="m1",
                is_order=True,
                confidence=0.95,
                data_confidence=0.8,
                needs_review=True,
                reason="Không xác định được chi nhánh.",
                products=["rau"],
                quantities=["2 kg"],
            )
        ],
    )

    assert publisher.ocr_sheet_requested is True
    assert publisher.ocr_rows == []
    assert publisher.ocr_review_sheet_requested is True
    assert publisher.ocr_review_rows == [
        [
            "01-09-2026",
            "Nhóm A",
            "",
            "m1",
            "",
            "",
            (
                "Đơn tin nhắn cần đối chiếu (phân loại 95%, dữ liệu 80%). "
                "Không xác định được chi nhánh."
            ),
            "",
            "",
            "",
            "rau",
            "kg",
            2,
            "Cần kiểm tra",
            "",
        ]
    ]
    assert result["ocr_sheet"]["rows_added"] == 0
    assert result["ocr_review_sheet"]["orders_for_review"] == 1
    assert result["ocr_review_sheet"]["rows_added"] == 1
    assert publisher.positioned_ocr_tabs == (42, 84)


def test_publish_skips_ocr_sheet_when_no_applicable_results(tmp_path: Path) -> None:
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 8, 30),
        messages=[],
        ocr_results=[
            ImageOcrResult(
                message_id="m1", media_path="assets/m1.jpg", applicable=False
            )
        ],
    )

    assert publisher.ocr_sheet_requested is False
    assert "ocr_sheet" not in result


def test_publish_routes_low_quality_order_to_review_sheet(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    image = assets / "m1.jpg"
    image.write_bytes(b"jpeg")
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 8, 30),
        messages=[
            CleanMessage(
                message_id="m1",
                sequence=1,
                content="[Hình ảnh]",
                message_type="image",
                media=[
                    MediaAsset(
                        path="assets/m1.jpg",
                        mime_type="image/jpeg",
                        role="message_image",
                    )
                ],
            )
        ],
        decisions=[
            OrderDecision(
                message_id="m1",
                is_order=True,
                confidence=0.98,
                data_confidence=0.8,
                reason="Ảnh là đơn hàng",
                branch_name="Chi nhánh Thảo Điền",
            )
        ],
        ocr_results=[
            ImageOcrResult(
                message_id="m1",
                media_path="assets/m1.jpg",
                applicable=True,
                image_quality_score=0.72,
                image_quality_affects_output=True,
                image_quality_reason="Ảnh nghiêng, chữ viết tay mờ",
                needs_review=True,
                items=[
                    OcrLineItem(
                        customer_name="Sườn Thảo Điền",
                        product_name="Rau muống",
                        quantity=2,
                    )
                ],
            )
        ],
    )

    assert publisher.ocr_sheet_requested is True
    assert publisher.ocr_rows == []
    assert publisher.ocr_review_sheet_requested is True
    assert publisher.ocr_review_rows == [
        [
            "30-08-2026",
            "Nhóm A",
            "Chi nhánh Thảo Điền",
            "m1",
            72.0,
            "Có",
            "Ảnh nghiêng, chữ viết tay mờ",
            "https://drive.google.com/file/d/m1/view",
            "",
            "Sườn Thảo Điền",
            "Rau muống",
            "",
            2,
            "Cần kiểm tra",
            "",
        ]
    ]
    assert result["ocr_review_sheet"]["rows_added"] == 1
    assert result["ocr_review_sheet"]["orders_for_review"] == 1
    assert result["ocr_review_sheet"]["rows_removed_from_ocr"] == 1
    assert result["ocr_review_sheet"]["url"].endswith("#gid=84")
    assert result["ocr_sheet"]["rows_added"] == 0
    assert publisher.positioned_ocr_tabs == (42, 84)


def test_publish_routes_classifier_review_even_when_image_quality_is_high(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    image = assets / "m1.jpg"
    image.write_bytes(b"jpeg")
    publisher = RecordingPublisher()

    result = publisher.publish(
        run_dir=tmp_path,
        group_name="Nhóm A",
        target_date=date(2026, 9, 1),
        messages=[
            CleanMessage(
                message_id="m1",
                sequence=1,
                content="[Hình ảnh]",
                message_type="image",
                media=[
                    MediaAsset(
                        path="assets/m1.jpg",
                        mime_type="image/jpeg",
                        role="message_image",
                    )
                ],
            )
        ],
        decisions=[
            OrderDecision(
                message_id="m1",
                is_order=True,
                confidence=0.95,
                data_confidence=0.8,
                needs_review=True,
                reason="Sản phẩm và số lượng chưa khớp.",
                branch_name="Chi nhánh Thảo Điền",
            )
        ],
        ocr_results=[
            ImageOcrResult(
                message_id="m1",
                media_path="assets/m1.jpg",
                applicable=True,
                image_quality_score=0.9,
                image_quality_affects_output=False,
                image_quality_reason="Ảnh rõ",
                items=[OcrLineItem(product_name="Rau muống", quantity=2)],
            )
        ],
    )

    assert publisher.ocr_sheet_requested is True
    assert publisher.ocr_rows == []
    assert publisher.ocr_review_sheet_requested is True
    assert publisher.ocr_review_rows[0][4:7] == [
        90.0,
        "Không",
        (
            "Độ tin cậy thông tin đơn ở bước phân loại chỉ đạt 80%; "
            "cần đối chiếu lại tên hàng, số lượng và chi nhánh."
        ),
    ]
    assert result["ocr_review_sheet"]["orders_for_review"] == 1
    assert result["ocr_review_sheet"]["rows_removed_from_ocr"] == 1
    assert result["ocr_sheet"]["rows_added"] == 0
    assert publisher.positioned_ocr_tabs == (42, 84)


def test_remove_reviewed_orders_from_ocr_sheet_deletes_matching_rows_bottom_up(
) -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    batches: list[list[dict[str, Any]]] = []
    publisher._sheet_metadata = lambda *_: [  # type: ignore[method-assign]
        (0, "Tin nhắn", 1000),
        (42, "Đơn hàng OCR", 6),
    ]
    publisher._get_values = lambda *_: [  # type: ignore[method-assign]
        ["Nhóm A", "Chi nhánh A", "m1"],
        ["Nhóm A", "Chi nhánh A", "m1"],
        ["Nhóm A", "Chi nhánh A", "m2"],
        ["Nhóm A", "Chi nhánh A", "m1"],
    ]
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, requests: batches.append(requests)
    )

    removed = publisher._remove_reviewed_orders_from_ocr_sheet(
        "sheet-id", "Nhóm A", {"m1"}
    )

    assert removed == 3
    assert batches == [
        [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": 42,
                        "dimension": "ROWS",
                        "startIndex": 4,
                        "endIndex": 5,
                    }
                }
            },
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": 42,
                        "dimension": "ROWS",
                        "startIndex": 1,
                        "endIndex": 3,
                    }
                }
            },
        ]
    ]


def test_remove_pending_ocr_review_rows_keeps_completed_human_review() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    batches: list[list[dict[str, Any]]] = []
    publisher._sheet_metadata = lambda *_: [  # type: ignore[method-assign]
        (84, "OCR cần kiểm tra", 5),
    ]
    publisher._get_values = lambda *_: [  # type: ignore[method-assign]
        ["Nhóm A", "Chi nhánh A", "m1", 80, "Có", "Ảnh mờ", "url", "", "", "Rau muống", "", 2, "Cần kiểm tra", ""],
        ["Nhóm A", "Chi nhánh A", "m1", 80, "Có", "Ảnh mờ", "url", "", "", "Cải ngọt", "", 1, "Đã kiểm tra", "Đúng"],
        ["Nhóm B", "Chi nhánh A", "m1", 80, "Có", "Ảnh mờ", "url", "", "", "Bầu", "", 1, "Cần kiểm tra", ""],
    ]
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, requests: batches.append(requests)
    )

    removed = publisher._remove_pending_ocr_review_rows(
        "sheet-id", "Nhóm A", {"m1"}
    )

    assert removed == 1
    assert batches == [
        [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": 84,
                        "dimension": "ROWS",
                        "startIndex": 1,
                        "endIndex": 2,
                    }
                }
            }
        ]
    ]


def test_ocr_rows_skip_non_applicable_and_blank_product_name() -> None:
    ocr_results = [
        ImageOcrResult(
            message_id="m1",
            media_path="assets/m1.jpg",
            applicable=True,
            items=[
                OcrLineItem(product_name="Rau muống", unit="kg", quantity=2),
                OcrLineItem(product_name="   "),
            ],
        ),
        ImageOcrResult(message_id="m2", media_path="assets/m2.jpg", applicable=False),
    ]

    rows = GoogleDriveOutputPublisher._ocr_rows("Nhóm A", date(2026, 8, 30), ocr_results)

    assert rows == [
        ["30-08-2026", "Nhóm A", "", "m1", "", "", "Rau muống", "kg", 2]
    ]


def test_ocr_rows_exclude_results_that_need_quality_review() -> None:
    rows = GoogleDriveOutputPublisher._ocr_rows(
        "Nhóm A",
        date(2026, 8, 30),
        [
            ImageOcrResult(
                message_id="m1",
                media_path="assets/m1.jpg",
                applicable=True,
                image_quality_score=0.7,
                image_quality_affects_output=True,
                needs_review=True,
                items=[OcrLineItem(product_name="Rau muống", quantity=2)],
            )
        ],
    )

    assert rows == []


def test_ocr_review_rows_keep_order_even_when_no_items_were_read() -> None:
    rows = GoogleDriveOutputPublisher._ocr_review_rows(
        "Nhóm A",
        date(2026, 8, 30),
        [
            ImageOcrResult(
                message_id="m1",
                media_path="assets/m1.jpg",
                applicable=False,
                skip_reason="Không đọc được phiếu",
                image_quality_score=0.4,
                image_quality_affects_output=True,
                image_quality_reason="Ảnh bị cắt mất cột số lượng",
                needs_review=True,
            )
        ],
        image_urls={
            ("m1", "assets/m1.jpg"): "https://drive.google.com/file/d/m1/view"
        },
    )

    assert len(rows) == 1
    assert rows[0][3:8] == [
        "m1",
        40.0,
        "Có",
        "Ảnh bị cắt mất cột số lượng",
        "https://drive.google.com/file/d/m1/view",
    ]
    assert rows[0][10:13] == ["", "", ""]
    assert rows[0][13] == "Cần kiểm tra"


def test_append_unique_ocr_rows_skips_existing_group_message_product_key() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    requests: list[dict[str, Any]] = []
    publisher._get_values = (  # type: ignore[method-assign]
        lambda *_: [
            ["Nhóm A", "Chi nhánh A", "m1", "S6", "Quán Rau", "Rau muống"]
        ]
    )
    publisher._request_json = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: requests.append(
            {"method": method, "url": url, **kwargs}
        )
        or {}
    )
    rows = [
        [
            "30-08-2026",
            "Nhóm A",
            "Chi nhánh A",
            "m1",
            "S6",
            "Quán Rau",
            "Rau muống",
            "kg",
            2,
        ],
        [
            "30-08-2026",
            "Nhóm A",
            "Chi nhánh A",
            "m1",
            "S6",
            "Quán Rau",
            "Cải ngọt",
            "kg",
            1,
        ],
    ]

    added = publisher._append_unique_ocr_rows(
        "sheet-id", "Đơn hàng OCR", 1000, rows
    )

    assert added == 1
    assert requests[0]["json"]["values"] == [rows[1]]
    assert "%27%C4%90%C6%A1n%20h%C3%A0ng%20OCR%27%21A%3AI:append" in requests[0]["url"]


def test_ensure_ocr_sheet_adds_tab_then_rereads_metadata() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    metadata = iter(
        [
            [(0, "Tin nhắn", 1000)],
            [(0, "Tin nhắn", 1000), (42, "Đơn hàng OCR", 1000)],
        ]
    )
    batches: list[list[dict[str, Any]]] = []
    headers: list[tuple[str, int, str]] = []
    publisher._sheet_metadata = lambda *_: next(metadata)  # type: ignore[method-assign]
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, requests: batches.append(requests)
    )
    publisher._ensure_ocr_sheet_header = (  # type: ignore[method-assign]
        lambda spreadsheet_id, sheet_id, sheet_title: headers.append(
            (spreadsheet_id, sheet_id, sheet_title)
        )
    )

    result = publisher._ensure_ocr_sheet("sheet-id")

    assert result == (42, "Đơn hàng OCR", 1000)
    assert batches == [
        [
            {
                "addSheet": {
                    "properties": {"title": "Đơn hàng OCR", "index": 1}
                }
            }
        ]
    ]
    assert headers == [("sheet-id", 42, "Đơn hàng OCR")]


def test_ensure_ocr_sheet_header_writes_nine_columns() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    requests: list[dict[str, Any]] = []
    batches: list[list[dict[str, Any]]] = []
    publisher._get_values = lambda *_: []  # type: ignore[method-assign]
    publisher._request_json = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: requests.append(
            {"method": method, "url": url, **kwargs}
        )
        or {}
    )
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, batch: batches.append(batch)
    )

    publisher._ensure_ocr_sheet_header("sheet-id", 42, "Đơn hàng OCR")

    assert requests[0]["json"]["values"][0][2] == "Chi nhánh"
    assert len(requests[0]["json"]["values"][0]) == 9
    assert "%21A1%3AI1" in requests[0]["url"]
    assert batches[0][0]["updateSheetProperties"]["properties"]["sheetId"] == 42


def test_ensure_ocr_review_sheet_adds_tab_then_rereads_metadata() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    metadata = iter(
        [
            [(0, "Tin nhắn", 1000)],
            [(0, "Tin nhắn", 1000), (84, "OCR cần kiểm tra", 1000)],
        ]
    )
    batches: list[list[dict[str, Any]]] = []
    headers: list[tuple[str, int, str]] = []
    publisher._sheet_metadata = lambda *_: next(metadata)  # type: ignore[method-assign]
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, requests: batches.append(requests)
    )
    publisher._ensure_ocr_review_sheet_header = (  # type: ignore[method-assign]
        lambda spreadsheet_id, sheet_id, sheet_title: headers.append(
            (spreadsheet_id, sheet_id, sheet_title)
        )
    )

    result = publisher._ensure_ocr_review_sheet("sheet-id")

    assert result == (84, "OCR cần kiểm tra", 1000)
    assert batches == [
        [
            {
                "addSheet": {
                    "properties": {"title": "OCR cần kiểm tra", "index": 2}
                }
            }
        ]
    ]
    assert headers == [("sheet-id", 84, "OCR cần kiểm tra")]


def test_position_ocr_tabs_places_review_immediately_after_order_tab() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    batches: list[list[dict[str, Any]]] = []
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, requests: batches.append(requests)
    )

    publisher._position_ocr_tabs("sheet-id", 42, 84)

    assert batches == [
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": 42, "index": 1},
                    "fields": "index",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": 84, "index": 2},
                    "fields": "index",
                }
            },
        ]
    ]


def test_request_json_retries_safe_get_after_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FlakyReadSession(failures=2)
    publisher = GoogleDriveOutputPublisher(session, "folder123")
    waits: list[int] = []
    monkeypatch.setattr(
        "zalo_order_crawler.drive_output.time.sleep",
        lambda seconds: waits.append(seconds),
    )

    result = publisher._request_json("GET", "https://sheets.googleapis.com/test")

    assert result == {"ok": True}
    assert session.calls == 3
    assert waits == [1, 2]


def test_request_json_does_not_retry_write_after_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FlakyReadSession(failures=1)
    publisher = GoogleDriveOutputPublisher(session, "folder123")
    waits: list[int] = []
    monkeypatch.setattr(
        "zalo_order_crawler.drive_output.time.sleep",
        lambda seconds: waits.append(seconds),
    )

    with pytest.raises(GoogleDriveOutputError, match="sau 1 lần"):
        publisher._request_json("POST", "https://sheets.googleapis.com/test")

    assert session.calls == 1
    assert waits == []


def test_ensure_ocr_review_sheet_header_writes_fifteen_columns() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    requests: list[dict[str, Any]] = []
    batches: list[list[dict[str, Any]]] = []
    publisher._get_values = lambda *_: []  # type: ignore[method-assign]
    publisher._request_json = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: requests.append(
            {"method": method, "url": url, **kwargs}
        )
        or {}
    )
    publisher._sheets_batch_update = (  # type: ignore[method-assign]
        lambda _spreadsheet_id, batch: batches.append(batch)
    )

    publisher._ensure_ocr_review_sheet_header(
        "sheet-id", 84, "OCR cần kiểm tra"
    )

    headers = requests[0]["json"]["values"][0]
    assert len(headers) == 15
    assert headers[4] == "Chất lượng ảnh (%)"
    assert headers[7] == "Link ảnh"
    assert headers[-1] == "Ghi chú kiểm tra"
    assert "%21A1%3AO1" in requests[0]["url"]
    assert any("addConditionalFormatRule" in request for request in batches[0])


def test_append_unique_ocr_review_rows_skips_existing_image_product_key() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    requests: list[dict[str, Any]] = []
    publisher._get_values = (  # type: ignore[method-assign]
        lambda *_: [
            [
                "Nhóm A",
                "Chi nhánh A",
                "m1",
                70,
                "Có",
                "Ảnh mờ",
                "https://drive.google.com/file/d/m1/view",
                "",
                "Quán Rau",
                "Rau muống",
            ]
        ]
    )
    publisher._request_json = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: requests.append(
            {"method": method, "url": url, **kwargs}
        )
        or {}
    )
    existing = [
        "30-08-2026",
        "Nhóm A",
        "Chi nhánh A",
        "m1",
        70,
        "Có",
        "Ảnh mờ",
        "https://drive.google.com/file/d/m1/view",
        "",
        "Quán Rau",
        "Rau muống",
        "",
        2,
        "Cần kiểm tra",
        "",
    ]
    new = existing.copy()
    new[10] = "Cải ngọt"

    added = publisher._append_unique_ocr_review_rows(
        "sheet-id", "OCR cần kiểm tra", 1000, [existing, new]
    )

    assert added == 1
    assert requests[0]["json"]["values"] == [new]
    assert "%21A%3AO:append" in requests[0]["url"]


def test_append_unique_text_rows_skips_existing_group_message_key() -> None:
    publisher = GoogleDriveOutputPublisher(object(), "folder123")
    requests: list[dict[str, Any]] = []
    publisher._get_values = lambda *_: [["Nhóm A", "m1"]]  # type: ignore[method-assign]
    publisher._request_json = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: requests.append(
            {"method": method, "url": url, **kwargs}
        )
        or {}
    )
    rows = [
        ["30-08-2026", "Nhóm A", "m1", 1, "", "", "incoming", "Cũ", "text", ""],
        ["30-08-2026", "Nhóm B", "m2", 2, "", "", "incoming", "Mới", "text", ""],
    ]

    added = publisher._append_unique_text_rows(
        "sheet-id", "Tin nhắn", 1000, rows
    )

    assert added == 1
    assert requests[0]["json"]["values"] == [rows[1]]


def test_media_path_cannot_escape_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private")

    with pytest.raises(GoogleDriveOutputError, match="ngoài thư mục"):
        GoogleDriveOutputPublisher._resolve_media_path(run_dir, "../outside.jpg")


def test_text_rows_include_branch_only_for_order_decisions() -> None:
    messages = [
        CleanMessage(message_id="m1", sequence=1, content="S6 thêm 2 kg rau"),
        CleanMessage(message_id="m2", sequence=2, content="Dạ"),
    ]
    decisions = [
        OrderDecision(
            message_id="m1",
            is_order=True,
            confidence=0.98,
            data_confidence=0.95,
            reason="Đơn bổ sung",
            branch_name="Chi nhánh Phạm Văn Đồng",
            products=["rau"],
            quantities=["2 kg"],
        ),
        OrderDecision(
            message_id="m2",
            is_order=False,
            confidence=0.99,
            data_confidence=0,
            reason="Xã giao",
            branch_name="Chi nhánh Tân Phú",
        ),
    ]

    rows = GoogleDriveOutputPublisher._text_rows(
        "Rau SMO", date(2026, 8, 30), messages, decisions
    )

    assert rows[0][-1] == "Chi nhánh Phạm Văn Đồng"
    assert rows[1][-1] == ""


def test_parse_branch_mappings_preserves_aliases_and_rejects_conflicts() -> None:
    rows = [
        ["S6", "Chi nhánh Phạm Văn Đồng"],
        ["Tân Phú", "Chi nhánh Tân Phú"],
    ]

    assert GoogleDriveOutputPublisher._parse_branch_mappings(rows) == {
        "S6": "Chi nhánh Phạm Văn Đồng",
        "Tân Phú": "Chi nhánh Tân Phú",
    }

    with pytest.raises(GoogleDriveOutputError, match="nhiều chi nhánh"):
        GoogleDriveOutputPublisher._parse_branch_mappings(
            [["S6", "Chi nhánh A"], [" s6 ", "Chi nhánh B"]]
        )


def test_parse_product_catalog_supports_branch_unit_and_aliases() -> None:
    products = GoogleDriveOutputPublisher._parse_product_catalog(
        [
            ["Chi nhánh Tân Phú", "Ngò gai", "kg", "ngà gai | ngo gai"],
            ["", "Cần tàu", "kg", ""],
        ]
    )

    assert products == (
        ProductCatalogEntry(
            branch_name="Chi nhánh Tân Phú",
            product_name="Ngò gai",
            unit="kg",
            aliases=["ngà gai", "ngo gai"],
        ),
        ProductCatalogEntry(product_name="Cần tàu", unit="kg"),
    )

    with pytest.raises(GoogleDriveOutputError, match="bị lặp"):
        GoogleDriveOutputPublisher._parse_product_catalog(
            [["", "Cần tàu", "kg"], ["", " cần tàu ", "kg"]]
        )


def test_product_catalog_accepts_branch_alias_and_rejects_unknown_branch() -> None:
    products = (
        ProductCatalogEntry(branch_name="S6", product_name="Cần tàu", unit="kg"),
    )
    mappings = {"S6": "Chi nhánh Phạm Văn Đồng"}

    canonical = GoogleDriveOutputPublisher._canonicalise_product_branches(
        products, mappings
    )

    assert canonical[0].branch_name == "Chi nhánh Phạm Văn Đồng"
    with pytest.raises(GoogleDriveOutputError, match="không có trong"):
        GoogleDriveOutputPublisher._canonicalise_product_branches(
            (ProductCatalogEntry(branch_name="S9", product_name="Cần tàu"),),
            mappings,
        )


def test_verify_destination_requires_permission_to_add_files() -> None:
    publisher = GoogleDriveOutputPublisher(
        FakeSession(
            {
                "id": "folder123",
                "name": "Output",
                "mimeType": "application/vnd.google-apps.folder",
                "webViewLink": "https://drive.google.com/drive/folders/folder123",
                "trashed": False,
                "capabilities": {"canAddChildren": False},
            }
        ),
        "folder123",
    )

    with pytest.raises(GoogleDriveOutputError, match="không có quyền"):
        publisher.verify_destination()


def test_web_oauth_redirect_port_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "8766")
    assert GoogleDriveOutputPublisher._oauth_redirect_port() == 8766

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "invalid")
    with pytest.raises(GoogleDriveOutputError, match="số nguyên"):
        GoogleDriveOutputPublisher._oauth_redirect_port()
