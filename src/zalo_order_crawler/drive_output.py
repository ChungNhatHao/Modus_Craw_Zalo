from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import quote

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from .models import (
    CleanMessage,
    ImageOcrResult,
    MediaAsset,
    OrderDecision,
    ProductCatalogEntry,
)
from .storage import safe_slug


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SHEETS_API = "https://sheets.googleapis.com/v4"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)
GOOGLE_API_GET_MAX_ATTEMPTS = 3
GOOGLE_API_CONNECT_TIMEOUT_SECONDS = 15
GOOGLE_API_READ_TIMEOUT_SECONDS = 60
GOOGLE_API_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
SHEET_MARKER_KEY = "zaloOrderCrawler"
SHEET_MARKER_VALUE = "daily-text-v1"
BRANCH_CONFIG_MARKER_VALUE = "branch-config-v1"
BRANCH_CONFIG_NAME = "Cấu hình chi nhánh"
BRANCH_CONFIG_TAB = "Chi nhánh"
BRANCH_CONFIG_HEADERS = ("Tên nhận diện", "Chi nhánh chuẩn")
PRODUCT_CONFIG_TAB = "Sản phẩm"
PRODUCT_CONFIG_HEADERS = (
    "Chi nhánh",
    "Tên sản phẩm chuẩn",
    "Đơn vị",
    "Tên thay thế (phân cách bằng |)",
)
DEFAULT_BRANCH_MAPPINGS = (
    ("S6", "Chi nhánh Phạm Văn Đồng"),
    ("Tân Phú", "Chi nhánh Tân Phú"),
    ("Sườn Thảo Điền", "Chi nhánh Thảo Điền"),
)
IMAGE_MARKER_KEY = "zaloCrawlerImageKey"
MESSAGE_SHEET_TITLE = "Tin nhắn"
OCR_SHEET_TITLE = "Đơn hàng OCR"
OCR_HEADERS = (
    "Ngày",
    "Nhóm",
    "Chi nhánh",
    "Mã tin nhắn",
    "Mã khách hàng",
    "Tên khách hàng",
    "Tên hàng",
    "Đơn vị",
    "Số lượng",
)
OCR_REVIEW_SHEET_TITLE = "OCR cần kiểm tra"
OCR_REVIEW_HEADERS = (
    "Ngày",
    "Nhóm",
    "Chi nhánh",
    "Mã tin nhắn",
    "Chất lượng ảnh (%)",
    "Ảnh hưởng kết quả",
    "Lý do cần kiểm tra",
    "Link ảnh",
    "Mã khách hàng",
    "Tên khách hàng",
    "Tên hàng OCR",
    "Đơn vị OCR",
    "Số lượng OCR",
    "Trạng thái",
    "Ghi chú kiểm tra",
)
LEGACY_SHEET_HEADERS = (
    "Ngày",
    "Nhóm",
    "Mã tin nhắn",
    "STT",
    "Người gửi",
    "Thời gian",
    "Chiều tin nhắn",
    "Nội dung",
    "Loại tin",
)
SHEET_HEADERS = (*LEGACY_SHEET_HEADERS, "Chi nhánh")
_MEDIA_PLACEHOLDERS = {"[hình ảnh]", "[image]"}
_DRIVE_ID_PATTERN = re.compile(r"^[0-9A-Za-z_-]+$")
_TEXT_QUANTITY_PATTERN = re.compile(
    r"^\s*(?P<number>[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\s*(?P<unit>.*?)\s*$"
)


class GoogleDriveOutputError(RuntimeError):
    """Raised when daily output cannot be written safely to Google Drive."""


@dataclass(frozen=True)
class DriveResource:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class ImageUploadResult:
    resource: DriveResource
    created: bool


@dataclass(frozen=True)
class BranchConfig:
    resource: DriveResource
    mappings: dict[str, str]
    products: tuple[ProductCatalogEntry, ...] = ()


class GoogleDriveOutputPublisher:
    def __init__(self, session: Any, parent_folder_id: str) -> None:
        if not _DRIVE_ID_PATTERN.fullmatch(parent_folder_id):
            raise ValueError("GOOGLE_DRIVE_PARENT_FOLDER_ID không hợp lệ.")
        self.session = session
        self.parent_folder_id = parent_folder_id

    @classmethod
    def from_default_credentials(
        cls, parent_folder_id: str, *, project_dir: Path | None = None
    ) -> "GoogleDriveOutputPublisher":
        try:
            import google.auth
            from google.auth.transport.requests import Request
            from google.auth.transport.requests import AuthorizedSession

            client_secret_value = os.environ.get(
                "GOOGLE_OAUTH_CLIENT_SECRET_FILE", ""
            ).strip()
            if client_secret_value:
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow

                base_dir = (project_dir or Path.cwd()).resolve()
                client_secret = cls._configured_path(client_secret_value, base_dir)
                token_value = os.environ.get(
                    "GOOGLE_OAUTH_TOKEN_FILE", ".google-drive-token.json"
                ).strip()
                token_file = cls._configured_path(token_value, base_dir)
                credentials = None
                if token_file.is_file():
                    try:
                        credentials = Credentials.from_authorized_user_file(
                            str(token_file), GOOGLE_SCOPES
                        )
                    except (OSError, ValueError):
                        credentials = None
                if credentials and credentials.expired and credentials.refresh_token:
                    try:
                        credentials.refresh(Request())
                    except Exception:
                        credentials = None
                if not credentials or not credentials.valid:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(client_secret), GOOGLE_SCOPES
                    )
                    client_config = json.loads(
                        client_secret.read_text(encoding="utf-8")
                    )
                    if "web" in client_config:
                        redirect_port = cls._oauth_redirect_port()
                        credentials = flow.run_local_server(
                            host="127.0.0.1",
                            bind_addr="127.0.0.1",
                            port=redirect_port,
                            timeout_seconds=300,
                            authorization_prompt_message=(
                                "Hãy mở URL này để cấp quyền Google Drive: {url}"
                            ),
                            success_message=(
                                "Đã xác thực Google thành công. Bạn có thể đóng tab này."
                            ),
                        )
                    else:
                        credentials = flow.run_local_server(
                            port=0,
                            timeout_seconds=300,
                            success_message=(
                                "Đã xác thực Google thành công. Bạn có thể đóng tab này."
                            ),
                        )
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text(credentials.to_json(), encoding="utf-8")
                try:
                    token_file.chmod(0o600)
                except OSError:
                    pass
            else:
                credentials, _ = google.auth.default(scopes=GOOGLE_SCOPES)
        except Exception as exc:
            raise GoogleDriveOutputError(
                "Chưa xác thực được Google. Hãy cấu hình "
                "GOOGLE_OAUTH_CLIENT_SECRET_FILE để đăng nhập tài khoản Google, "
                "hoặc GOOGLE_APPLICATION_CREDENTIALS/Application Default Credentials."
            ) from exc
        return cls(AuthorizedSession(credentials), parent_folder_id)

    def verify_destination(self) -> DriveResource:
        payload = self._request_json(
            "GET",
            f"{DRIVE_API}/files/{quote(self.parent_folder_id, safe='')}",
            params={
                "supportsAllDrives": "true",
                "fields": (
                    "id,name,mimeType,webViewLink,trashed,"
                    "capabilities(canAddChildren)"
                ),
            },
        )
        if payload.get("mimeType") != GOOGLE_FOLDER_MIME or payload.get("trashed"):
            raise GoogleDriveOutputError(
                "GOOGLE_DRIVE_PARENT_FOLDER_ID không trỏ tới một folder đang hoạt động."
            )
        capabilities = payload.get("capabilities") or {}
        if capabilities.get("canAddChildren") is not True:
            raise GoogleDriveOutputError(
                "Tài khoản Google hiện tại không có quyền tạo file trong folder đích."
            )
        return self._resource(payload)

    def publish(
        self,
        *,
        run_dir: Path,
        group_name: str,
        target_date: date,
        messages: Iterable[CleanMessage],
        decisions: Iterable[OrderDecision] = (),
        ocr_results: Iterable[ImageOcrResult] = (),
    ) -> dict[str, Any]:
        run_dir = run_dir.resolve()
        message_list = list(messages)
        decision_list = list(decisions)
        ocr_result_list = list(ocr_results)
        decisions_by_message_id = {
            decision.message_id: decision
            for decision in decision_list
            if decision.is_order
        }
        ocr_result_list = [
            result.apply_order_review_policy(decision)
            if (decision := decisions_by_message_id.get(result.message_id)) is not None
            else result
            for result in ocr_result_list
        ]
        text_rows = self._text_rows(
            group_name,
            target_date,
            message_list,
            decision_list,
        )
        image_assets = self._message_images(run_dir, message_list)
        image_message_ids = {
            message.message_id
            for message in message_list
            if any(
                media.role == "message_image"
                and media.mime_type.startswith("image/")
                for media in message.media
            )
        }
        image_message_ids.update(result.message_id for result in ocr_result_list)
        text_order_decisions = [
            decision
            for decision in decision_list
            if decision.is_order and decision.message_id not in image_message_ids
        ]
        text_order_rows = self._text_order_rows(
            group_name,
            target_date,
            text_order_decisions,
        )
        ocr_rows = self._ocr_rows(
            group_name,
            target_date,
            ocr_result_list,
            decision_list,
        )
        order_rows = [*text_order_rows, *ocr_rows]
        message_order = {
            message.message_id: index for index, message in enumerate(message_list)
        }
        order_rows.sort(
            key=lambda row: message_order.get(str(row[3]), len(message_order))
        )
        review_results = [result for result in ocr_result_list if result.needs_review]
        text_review_decisions = [
            decision for decision in text_order_decisions if decision.needs_review
        ]
        review_message_ids = {
            result.message_id for result in review_results
        } | {decision.message_id for decision in text_review_decisions}
        processed_order_message_ids = {
            decision.message_id
            for decision in decision_list
            if decision.is_order
        }
        daily_name = target_date.strftime("%d-%m-%Y")
        output: dict[str, Any] = {}
        daily_sheet: tuple[DriveResource, str, int] | None = None

        if text_rows or order_rows or review_message_ids:
            daily_sheet = self._ensure_daily_sheet(daily_name)

        if text_rows and daily_sheet is not None:
            sheet, sheet_title, row_count = daily_sheet
            rows_added = self._append_unique_text_rows(
                sheet.id, sheet_title, row_count, text_rows
            )
            output["sheet"] = {
                "id": sheet.id,
                "name": sheet.name,
                "url": sheet.url,
                "rows_added": rows_added,
            }

        if image_assets:
            folder = self._ensure_image_folder(f"{daily_name}_image")
            uploaded = 0
            existing = 0
            image_urls: dict[tuple[str, str], str] = {}
            for message_id, media, media_path in image_assets:
                upload = self._upload_unique_image(
                    folder.id,
                    group_name=group_name,
                    message_id=message_id,
                    media=media,
                    media_path=media_path,
                )
                uploaded += int(upload.created)
                existing += int(not upload.created)
                image_urls[(message_id, media.path)] = upload.resource.url
            output["image_folder"] = {
                "id": folder.id,
                "name": folder.name,
                "url": folder.url,
                "images_uploaded": uploaded,
                "images_existing": existing,
            }
        else:
            image_urls = {}

        ocr_sheet_info: tuple[int, str, int] | None = None
        if (order_rows or review_message_ids) and daily_sheet is not None:
            sheet, _, _ = daily_sheet
            ocr_sheet_info = self._ensure_ocr_sheet(sheet.id)
            ocr_sheet_id, ocr_sheet_title, ocr_row_count = ocr_sheet_info
            rows_removed_from_ocr = self._remove_reviewed_orders_from_ocr_sheet(
                sheet.id,
                group_name,
                processed_order_message_ids,
            )
            ocr_row_count = max(1, ocr_row_count - rows_removed_from_ocr)
            rows_added = 0
            if order_rows:
                rows_added = self._append_unique_ocr_rows(
                    sheet.id,
                    ocr_sheet_title,
                    ocr_row_count,
                    order_rows,
                )
            output["ocr_sheet"] = {
                "id": sheet.id,
                "name": f"{sheet.name} · {ocr_sheet_title}",
                "url": self._sheet_tab_url(sheet.url, ocr_sheet_id),
                "tab_name": ocr_sheet_title,
                "rows_added": rows_added,
                "rows_replaced": rows_removed_from_ocr,
            }

        if review_message_ids and daily_sheet is not None:
            sheet, _, _ = daily_sheet
            image_review_rows = self._ocr_review_rows(
                group_name,
                target_date,
                review_results,
                decision_list,
                image_urls,
            )
            text_review_rows = self._text_order_review_rows(
                group_name,
                target_date,
                text_review_decisions,
            )
            review_rows = [*text_review_rows, *image_review_rows]
            review_rows.sort(
                key=lambda row: message_order.get(str(row[3]), len(message_order))
            )
            review_sheet_id, review_sheet_title, review_row_count = (
                self._ensure_ocr_review_sheet(sheet.id)
            )
            rows_replaced_in_review = self._remove_pending_ocr_review_rows(
                sheet.id,
                group_name,
                processed_order_message_ids,
            )
            review_row_count = max(1, review_row_count - rows_replaced_in_review)
            if ocr_sheet_info is None:
                raise GoogleDriveOutputError(
                    "Thiếu tab Đơn hàng OCR trước khi tạo tab cần kiểm tra."
                )
            self._position_ocr_tabs(
                sheet.id,
                ocr_sheet_info[0],
                review_sheet_id,
            )
            rows_added = self._append_unique_ocr_review_rows(
                sheet.id,
                review_sheet_title,
                review_row_count,
                review_rows,
            )
            output["ocr_review_sheet"] = {
                "id": sheet.id,
                "name": f"{sheet.name} · {review_sheet_title}",
                "url": self._sheet_tab_url(sheet.url, review_sheet_id),
                "tab_name": review_sheet_title,
                "rows_added": rows_added,
                "orders_for_review": len(review_message_ids),
                "rows_removed_from_ocr": rows_removed_from_ocr,
                "rows_replaced": rows_replaced_in_review,
            }
        elif ocr_sheet_info is not None and daily_sheet is not None:
            self._remove_pending_ocr_review_rows(
                daily_sheet[0].id,
                group_name,
                processed_order_message_ids,
            )
            self._position_ocr_tabs(daily_sheet[0].id, ocr_sheet_info[0])

        return output

    @staticmethod
    def _text_rows(
        group_name: str,
        target_date: date,
        messages: Iterable[CleanMessage],
        decisions: Iterable[OrderDecision] = (),
    ) -> list[list[Any]]:
        display_date = target_date.strftime("%d-%m-%Y")
        branch_by_message_id = {
            decision.message_id: decision.branch_name or ""
            for decision in decisions
            if decision.is_order
        }
        rows: list[list[Any]] = []
        for message in messages:
            content = message.content.strip()
            if not content or content.casefold() in _MEDIA_PLACEHOLDERS:
                continue
            rows.append(
                [
                    display_date,
                    group_name,
                    message.message_id,
                    message.sequence,
                    message.sender or "",
                    message.timestamp_text or "",
                    message.direction,
                    content,
                    message.message_type,
                    branch_by_message_id.get(message.message_id, ""),
                ]
            )
        return rows

    @classmethod
    def _text_order_rows(
        cls,
        group_name: str,
        target_date: date,
        decisions: Iterable[OrderDecision],
    ) -> list[list[Any]]:
        display_date = target_date.strftime("%d-%m-%Y")
        rows: list[list[Any]] = []
        for decision in decisions:
            if not decision.is_order or decision.needs_review:
                continue
            for product, raw_quantity in zip_longest(
                decision.products,
                decision.quantities,
                fillvalue="",
            ):
                product_name = product.strip()
                if not product_name:
                    continue
                quantity, unit = cls._text_quantity_parts(raw_quantity)
                rows.append(
                    [
                        display_date,
                        group_name,
                        decision.branch_name or "",
                        decision.message_id,
                        "",
                        decision.customer_name or "",
                        product_name,
                        unit,
                        quantity,
                    ]
                )
        return rows

    @classmethod
    def _text_order_review_rows(
        cls,
        group_name: str,
        target_date: date,
        decisions: Iterable[OrderDecision],
    ) -> list[list[Any]]:
        display_date = target_date.strftime("%d-%m-%Y")
        rows: list[list[Any]] = []
        for decision in decisions:
            if not decision.is_order or not decision.needs_review:
                continue
            products_and_quantities = list(
                zip_longest(decision.products, decision.quantities, fillvalue="")
            ) or [("", "")]
            reason = (
                "Đơn tin nhắn cần đối chiếu "
                f"(phân loại {decision.confidence * 100:g}%, "
                f"dữ liệu {decision.data_confidence * 100:g}%)."
            )
            if decision.reason.strip():
                reason = f"{reason} {decision.reason.strip()}"
            for product, raw_quantity in products_and_quantities:
                quantity, unit = cls._text_quantity_parts(raw_quantity)
                rows.append(
                    [
                        display_date,
                        group_name,
                        decision.branch_name or "",
                        decision.message_id,
                        "",
                        "",
                        reason,
                        "",
                        "",
                        decision.customer_name or "",
                        product.strip(),
                        unit,
                        quantity,
                        "Cần kiểm tra",
                        "",
                    ]
                )
        return rows

    @staticmethod
    def _text_quantity_parts(raw_quantity: str) -> tuple[Any, str]:
        value = raw_quantity.strip()
        if not value:
            return "", ""
        match = _TEXT_QUANTITY_PATTERN.fullmatch(value)
        if match is None:
            return value, ""
        numeric_value = float(match.group("number").replace(",", "."))
        quantity: int | float = (
            int(numeric_value) if numeric_value.is_integer() else numeric_value
        )
        return quantity, match.group("unit").strip()

    @classmethod
    def _message_images(
        cls,
        run_dir: Path,
        messages: Iterable[CleanMessage],
    ) -> list[tuple[str, MediaAsset, Path]]:
        result: list[tuple[str, MediaAsset, Path]] = []
        seen: set[tuple[str, str]] = set()
        for message in messages:
            for media in message.media:
                if media.role != "message_image" or not media.mime_type.startswith("image/"):
                    continue
                media_path = cls._resolve_media_path(run_dir, media.path)
                identity = (media.sha256, str(media_path))
                if identity in seen:
                    continue
                seen.add(identity)
                result.append((message.message_id, media, media_path))
        return result

    @staticmethod
    def _resolve_media_path(run_dir: Path, media_path: str) -> Path:
        raw = Path(media_path)
        if not media_path or raw.is_absolute():
            raise GoogleDriveOutputError("Đường dẫn ảnh output không hợp lệ.")
        candidate = (run_dir / raw).resolve()
        try:
            candidate.relative_to(run_dir)
        except ValueError as exc:
            raise GoogleDriveOutputError(
                "Ảnh output nằm ngoài thư mục của lượt crawl."
            ) from exc
        if not candidate.is_file():
            raise GoogleDriveOutputError(f"Không tìm thấy ảnh output: {media_path}")
        return candidate

    @staticmethod
    def _ocr_rows(
        group_name: str,
        target_date: date,
        ocr_results: Iterable[ImageOcrResult],
        decisions: Iterable[OrderDecision] = (),
    ) -> list[list[Any]]:
        display_date = target_date.strftime("%d-%m-%Y")
        branch_by_message_id = {
            decision.message_id: decision.branch_name or ""
            for decision in decisions
            if decision.is_order
        }
        rows: list[list[Any]] = []
        for result in ocr_results:
            if not result.applicable or result.needs_review:
                continue
            for item in result.items:
                if not item.product_name.strip():
                    continue
                rows.append(
                    [
                        display_date,
                        group_name,
                        branch_by_message_id.get(result.message_id, ""),
                        result.message_id,
                        item.customer_code,
                        item.customer_name,
                        item.product_name,
                        item.unit,
                        item.quantity if item.quantity is not None else "",
                    ]
                )
        return rows

    @staticmethod
    def _ocr_review_rows(
        group_name: str,
        target_date: date,
        ocr_results: Iterable[ImageOcrResult],
        decisions: Iterable[OrderDecision] = (),
        image_urls: dict[tuple[str, str], str] | None = None,
    ) -> list[list[Any]]:
        display_date = target_date.strftime("%d-%m-%Y")
        branch_by_message_id = {
            decision.message_id: decision.branch_name or ""
            for decision in decisions
            if decision.is_order
        }
        urls = image_urls or {}
        rows: list[list[Any]] = []
        for result in ocr_results:
            if not result.needs_review:
                continue
            items = [item for item in result.items if item.product_name.strip()]
            review_items = items or [None]
            for item in review_items:
                rows.append(
                    [
                        display_date,
                        group_name,
                        branch_by_message_id.get(result.message_id, ""),
                        result.message_id,
                        round(result.image_quality_score * 100, 1),
                        "Có" if result.image_quality_affects_output else "Không",
                        result.review_reason
                        or result.image_quality_reason
                        or result.skip_reason
                        or "",
                        urls.get((result.message_id, result.media_path), ""),
                        item.customer_code if item else "",
                        item.customer_name if item else "",
                        item.product_name if item else "",
                        item.unit if item else "",
                        (
                            item.quantity
                            if item is not None and item.quantity is not None
                            else ""
                        ),
                        "Cần kiểm tra",
                        "",
                    ]
                )
        return rows

    def _remove_reviewed_orders_from_ocr_sheet(
        self,
        spreadsheet_id: str,
        group_name: str,
        message_ids: set[str],
    ) -> int:
        if not message_ids:
            return 0

        match = next(
            (
                item
                for item in self._sheet_metadata(spreadsheet_id)
                if item[1] == OCR_SHEET_TITLE
            ),
            None,
        )
        if match is None:
            return 0

        sheet_id, sheet_title, row_count = match
        matching_rows: list[int] = []
        chunk_size = 10_000
        for start_row in range(2, row_count + 1, chunk_size):
            end_row = min(row_count, start_row + chunk_size - 1)
            key_range = f"{self._a1_sheet(sheet_title)}!B{start_row}:D{end_row}"
            values = self._get_values(spreadsheet_id, key_range)
            matching_rows.extend(
                start_row + offset
                for offset, row in enumerate(values)
                if len(row) >= 3
                and str(row[0]) == group_name
                and str(row[2]) in message_ids
            )

        if not matching_rows:
            return 0

        contiguous_ranges: list[tuple[int, int]] = []
        range_start = matching_rows[0]
        range_end = range_start
        for row_number in matching_rows[1:]:
            if row_number == range_end + 1:
                range_end = row_number
                continue
            contiguous_ranges.append((range_start, range_end))
            range_start = range_end = row_number
        contiguous_ranges.append((range_start, range_end))

        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_row - 1,
                            "endIndex": end_row,
                        }
                    }
                }
                for start_row, end_row in reversed(contiguous_ranges)
            ],
        )
        return len(matching_rows)

    def _remove_pending_ocr_review_rows(
        self,
        spreadsheet_id: str,
        group_name: str,
        message_ids: set[str],
    ) -> int:
        """Remove stale pending OCR rows while retaining completed human reviews."""
        if not message_ids:
            return 0

        match = next(
            (
                item
                for item in self._sheet_metadata(spreadsheet_id)
                if item[1] == OCR_REVIEW_SHEET_TITLE
            ),
            None,
        )
        if match is None:
            return 0

        sheet_id, sheet_title, row_count = match
        matching_rows: list[int] = []
        chunk_size = 3_000
        for start_row in range(2, row_count + 1, chunk_size):
            end_row = min(row_count, start_row + chunk_size - 1)
            key_range = f"{self._a1_sheet(sheet_title)}!B{start_row}:O{end_row}"
            values = self._get_values(spreadsheet_id, key_range)
            for offset, row in enumerate(values):
                status = self._row_value(row, 12).strip().casefold()
                note = self._row_value(row, 13).strip()
                still_pending = status in {"", "cần kiểm tra"} and not note
                if (
                    self._row_value(row, 0) == group_name
                    and self._row_value(row, 2) in message_ids
                    and still_pending
                ):
                    matching_rows.append(start_row + offset)

        if not matching_rows:
            return 0

        contiguous_ranges: list[tuple[int, int]] = []
        range_start = matching_rows[0]
        range_end = range_start
        for row_number in matching_rows[1:]:
            if row_number == range_end + 1:
                range_end = row_number
                continue
            contiguous_ranges.append((range_start, range_end))
            range_start = range_end = row_number
        contiguous_ranges.append((range_start, range_end))

        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_row - 1,
                            "endIndex": end_row,
                        }
                    }
                }
                for start_row, end_row in reversed(contiguous_ranges)
            ],
        )
        return len(matching_rows)

    def _ensure_ocr_sheet(self, spreadsheet_id: str) -> tuple[int, str, int]:
        sheets = self._sheet_metadata(spreadsheet_id)
        match = next(
            (item for item in sheets if item[1] == OCR_SHEET_TITLE),
            None,
        )
        if match is None:
            self._sheets_batch_update(
                spreadsheet_id,
                [
                    {
                        "addSheet": {
                            "properties": {
                                "title": OCR_SHEET_TITLE,
                                "index": 1,
                            }
                        }
                    }
                ],
            )
            sheets = self._sheet_metadata(spreadsheet_id)
            match = next(
                (item for item in sheets if item[1] == OCR_SHEET_TITLE),
                None,
            )
        if match is None:
            raise GoogleDriveOutputError(
                f"Không tạo được tab {OCR_SHEET_TITLE!r} trong Google Sheet."
            )
        sheet_id, sheet_title, row_count = match
        self._ensure_ocr_sheet_header(spreadsheet_id, sheet_id, sheet_title)
        return sheet_id, sheet_title, row_count

    def _ensure_ocr_sheet_header(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_title: str,
    ) -> None:
        header_range = f"{self._a1_sheet(sheet_title)}!A1:I1"
        values = self._get_values(spreadsheet_id, header_range)
        if values:
            existing = [str(value) for value in values[0]]
            if existing != list(OCR_HEADERS):
                raise GoogleDriveOutputError(
                    f"Tab {sheet_title!r} của Sheet {spreadsheet_id} có header "
                    "không đúng cấu trúc OCR; tool dừng để không ghi đè dữ liệu."
                )
            return

        self._request_json(
            "PUT",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(header_range, safe='')}",
            params={"valueInputOption": "RAW"},
            json={"majorDimension": "ROWS", "values": [list(OCR_HEADERS)]},
        )
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(OCR_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.035,
                                        "green": 0.408,
                                        "blue": 1.0,
                                    }
                                },
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColorStyle": {
                                        "rgbColor": {
                                            "red": 1.0,
                                            "green": 1.0,
                                            "blue": 1.0,
                                        }
                                    },
                                },
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColorStyle,textFormat)"
                        ),
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(OCR_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {"verticalAlignment": "TOP"}
                        },
                        "fields": "userEnteredFormat.verticalAlignment",
                    }
                },
                *self._ocr_column_width_requests(sheet_id),
            ],
        )

    def _append_unique_ocr_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        existing_keys: set[tuple[str, str, str]] = set()
        chunk_size = 8_000
        for start_row in range(2, row_count + 1, chunk_size):
            end_row = min(row_count, start_row + chunk_size - 1)
            key_range = f"{self._a1_sheet(sheet_title)}!B{start_row}:G{end_row}"
            existing_values = self._get_values(spreadsheet_id, key_range)
            existing_keys.update(
                (str(row[0]), str(row[2]), str(row[5]))
                for row in existing_values
                if len(row) >= 6
            )
        pending = [
            row
            for row in rows
            if (str(row[1]), str(row[3]), str(row[6])) not in existing_keys
        ]
        if not pending:
            return 0

        append_range = f"{self._a1_sheet(sheet_title)}!A:I"
        self._request_json(
            "POST",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(append_range, safe='')}:append",
            params={
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"majorDimension": "ROWS", "values": pending},
        )
        return len(pending)

    def _ensure_ocr_review_sheet(self, spreadsheet_id: str) -> tuple[int, str, int]:
        sheets = self._sheet_metadata(spreadsheet_id)
        match = next(
            (item for item in sheets if item[1] == OCR_REVIEW_SHEET_TITLE),
            None,
        )
        if match is None:
            self._sheets_batch_update(
                spreadsheet_id,
                [
                    {
                        "addSheet": {
                            "properties": {
                                "title": OCR_REVIEW_SHEET_TITLE,
                                "index": 2,
                            }
                        }
                    }
                ],
            )
            sheets = self._sheet_metadata(spreadsheet_id)
            match = next(
                (item for item in sheets if item[1] == OCR_REVIEW_SHEET_TITLE),
                None,
            )
        if match is None:
            raise GoogleDriveOutputError(
                f"Không tạo được tab {OCR_REVIEW_SHEET_TITLE!r} trong Google Sheet."
            )
        sheet_id, sheet_title, row_count = match
        self._ensure_ocr_review_sheet_header(
            spreadsheet_id,
            sheet_id,
            sheet_title,
        )
        return sheet_id, sheet_title, row_count

    def _position_ocr_tabs(
        self,
        spreadsheet_id: str,
        ocr_sheet_id: int,
        review_sheet_id: int | None = None,
    ) -> None:
        requests: list[dict[str, Any]] = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ocr_sheet_id, "index": 1},
                    "fields": "index",
                }
            }
        ]
        if review_sheet_id is not None:
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": review_sheet_id, "index": 2},
                        "fields": "index",
                    }
                }
            )
        self._sheets_batch_update(spreadsheet_id, requests)

    def _ensure_ocr_review_sheet_header(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_title: str,
    ) -> None:
        last_column = self._column_letter(len(OCR_REVIEW_HEADERS))
        header_range = f"{self._a1_sheet(sheet_title)}!A1:{last_column}1"
        values = self._get_values(spreadsheet_id, header_range)
        if values:
            existing = [str(value) for value in values[0]]
            if existing != list(OCR_REVIEW_HEADERS):
                raise GoogleDriveOutputError(
                    f"Tab {sheet_title!r} của Sheet {spreadsheet_id} có header "
                    "không đúng cấu trúc kiểm tra OCR; tool dừng để không ghi đè dữ liệu."
                )
            return

        self._request_json(
            "PUT",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(header_range, safe='')}",
            params={"valueInputOption": "RAW"},
            json={"majorDimension": "ROWS", "values": [list(OCR_REVIEW_HEADERS)]},
        )
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(OCR_REVIEW_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.984,
                                        "green": 0.737,
                                        "blue": 0.016,
                                    }
                                },
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColorStyle": {
                                        "rgbColor": {
                                            "red": 0.12,
                                            "green": 0.12,
                                            "blue": 0.12,
                                        }
                                    },
                                },
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColorStyle,textFormat)"
                        ),
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(OCR_REVIEW_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "verticalAlignment": "TOP",
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": (
                            "userEnteredFormat(verticalAlignment,wrapStrategy)"
                        ),
                    }
                },
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "startColumnIndex": 4,
                                    "endColumnIndex": 5,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "NUMBER_LESS",
                                    "values": [{"userEnteredValue": "85"}],
                                },
                                "format": {
                                    "backgroundColorStyle": {
                                        "rgbColor": {
                                            "red": 0.988,
                                            "green": 0.894,
                                            "blue": 0.894,
                                        }
                                    },
                                    "textFormat": {
                                        "foregroundColorStyle": {
                                            "rgbColor": {
                                                "red": 0.60,
                                                "green": 0.07,
                                                "blue": 0.07,
                                            }
                                        }
                                    },
                                },
                            },
                        },
                        "index": 0,
                    }
                },
                *self._ocr_review_column_width_requests(sheet_id),
            ],
        )

    def _append_unique_ocr_review_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        existing_keys: set[tuple[str, str, str, str]] = set()
        chunk_size = 4_000
        for start_row in range(2, row_count + 1, chunk_size):
            end_row = min(row_count, start_row + chunk_size - 1)
            key_range = f"{self._a1_sheet(sheet_title)}!B{start_row}:K{end_row}"
            existing_values = self._get_values(spreadsheet_id, key_range)
            for row in existing_values:
                existing_keys.add(
                    (
                        self._row_value(row, 0),
                        self._row_value(row, 2),
                        self._row_value(row, 6),
                        self._row_value(row, 9),
                    )
                )
        pending = [
            row
            for row in rows
            if (str(row[1]), str(row[3]), str(row[7]), str(row[10]))
            not in existing_keys
        ]
        if not pending:
            return 0

        last_column = self._column_letter(len(OCR_REVIEW_HEADERS))
        append_range = f"{self._a1_sheet(sheet_title)}!A:{last_column}"
        self._request_json(
            "POST",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(append_range, safe='')}:append",
            params={
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"majorDimension": "ROWS", "values": pending},
        )
        return len(pending)

    def ensure_branch_config(self) -> BranchConfig:
        marker_query = (
            f"appProperties has {{ key='{SHEET_MARKER_KEY}' and "
            f"value='{BRANCH_CONFIG_MARKER_VALUE}' }}"
        )
        resource = self._find_resource(
            self.parent_folder_id,
            BRANCH_CONFIG_NAME,
            GOOGLE_SHEET_MIME,
            extra_query=marker_query,
        )
        created = resource is None
        if resource is None:
            resource = self._create_resource(
                BRANCH_CONFIG_NAME,
                GOOGLE_SHEET_MIME,
                self.parent_folder_id,
                app_properties={SHEET_MARKER_KEY: BRANCH_CONFIG_MARKER_VALUE},
            )

        sheet_id, sheet_title, row_count = self._first_sheet(resource.id)
        if created and sheet_title != BRANCH_CONFIG_TAB:
            self._rename_sheet(resource.id, sheet_id, BRANCH_CONFIG_TAB)
            sheet_title = BRANCH_CONFIG_TAB

        header_range = f"{self._a1_sheet(sheet_title)}!A1:B1"
        header_values = self._get_values(resource.id, header_range)
        if not header_values:
            seed_rows = [list(BRANCH_CONFIG_HEADERS), *map(list, DEFAULT_BRANCH_MAPPINGS)]
            seed_range = f"{self._a1_sheet(sheet_title)}!A1:B{len(seed_rows)}"
            self._request_json(
                "PUT",
                f"{SHEETS_API}/spreadsheets/{quote(resource.id, safe='')}/values/"
                f"{quote(seed_range, safe='')}",
                params={"valueInputOption": "RAW"},
                json={"majorDimension": "ROWS", "values": seed_rows},
            )
            self._format_branch_config(resource.id, sheet_id)
        else:
            existing_header = [str(value).strip() for value in header_values[0]]
            if existing_header != list(BRANCH_CONFIG_HEADERS):
                raise GoogleDriveOutputError(
                    f"Sheet cấu hình {resource.id} phải có header "
                    f"{', '.join(BRANCH_CONFIG_HEADERS)}."
                )

        max_row = min(max(row_count, len(DEFAULT_BRANCH_MAPPINGS) + 1), 10_000)
        data_range = f"{self._a1_sheet(sheet_title)}!A2:B{max_row}"
        mappings = self._parse_branch_mappings(
            self._get_values(resource.id, data_range)
        )
        products = self._canonicalise_product_branches(
            self._ensure_product_config(resource.id),
            mappings,
        )
        return BranchConfig(
            resource=resource,
            mappings=mappings,
            products=products,
        )

    def _ensure_product_config(
        self, spreadsheet_id: str
    ) -> tuple[ProductCatalogEntry, ...]:
        sheets = self._sheet_metadata(spreadsheet_id)
        match = next(
            (item for item in sheets if item[1] == PRODUCT_CONFIG_TAB),
            None,
        )
        if match is None:
            self._sheets_batch_update(
                spreadsheet_id,
                [
                    {
                        "addSheet": {
                            "properties": {
                                "title": PRODUCT_CONFIG_TAB,
                                "index": 1,
                            }
                        }
                    }
                ],
            )
            sheets = self._sheet_metadata(spreadsheet_id)
            match = next(
                (item for item in sheets if item[1] == PRODUCT_CONFIG_TAB),
                None,
            )
        if match is None:
            raise GoogleDriveOutputError(
                f"Không tạo được tab cấu hình {PRODUCT_CONFIG_TAB!r}."
            )

        sheet_id, sheet_title, row_count = match
        header_range = f"{self._a1_sheet(sheet_title)}!A1:D1"
        header_values = self._get_values(spreadsheet_id, header_range)
        if not header_values:
            self._request_json(
                "PUT",
                f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
                f"{quote(header_range, safe='')}",
                params={"valueInputOption": "RAW"},
                json={
                    "majorDimension": "ROWS",
                    "values": [list(PRODUCT_CONFIG_HEADERS)],
                },
            )
            self._format_product_config(spreadsheet_id, sheet_id)
        else:
            existing_header = [str(value).strip() for value in header_values[0]]
            if existing_header != list(PRODUCT_CONFIG_HEADERS):
                raise GoogleDriveOutputError(
                    f"Tab {PRODUCT_CONFIG_TAB!r} của Sheet {spreadsheet_id} phải có "
                    f"header {', '.join(PRODUCT_CONFIG_HEADERS)}."
                )

        max_row = min(max(row_count, 1_000), 20_000)
        data_range = f"{self._a1_sheet(sheet_title)}!A2:D{max_row}"
        return self._parse_product_catalog(
            self._get_values(spreadsheet_id, data_range)
        )

    def _format_product_config(
        self, spreadsheet_id: str, sheet_id: int
    ) -> None:
        widths = (220, 260, 100, 320)
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(PRODUCT_CONFIG_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.9,
                                        "green": 0.9,
                                        "blue": 0.9,
                                    }
                                },
                                "textFormat": {"bold": True},
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColorStyle,textFormat)"
                        ),
                    }
                },
                *[
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": index,
                                "endIndex": index + 1,
                            },
                            "properties": {"pixelSize": width},
                            "fields": "pixelSize",
                        }
                    }
                    for index, width in enumerate(widths)
                ],
            ],
        )

    def _format_branch_config(self, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 2,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.9,
                                        "green": 0.9,
                                        "blue": 0.9,
                                    }
                                },
                                "textFormat": {"bold": True},
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColorStyle,textFormat)"
                        ),
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"pixelSize": 180},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 2,
                        },
                        "properties": {"pixelSize": 260},
                        "fields": "pixelSize",
                    }
                },
            ],
        )

    @classmethod
    def _parse_branch_mappings(cls, rows: Iterable[list[Any]]) -> dict[str, str]:
        mappings: dict[str, str] = {}
        canonical_by_key: dict[str, str] = {}
        display_aliases: dict[str, str] = {}
        for row_number, row in enumerate(rows, start=2):
            alias = str(row[0]).strip() if row else ""
            canonical = str(row[1]).strip() if len(row) >= 2 else ""
            if not alias and not canonical:
                continue
            if not alias or not canonical:
                raise GoogleDriveOutputError(
                    f"Cấu hình chi nhánh dòng {row_number} phải có đủ hai cột."
                )
            key = cls._normalise_branch_key(alias)
            if key in canonical_by_key and canonical_by_key[key] != canonical:
                raise GoogleDriveOutputError(
                    f"Tên nhận diện {display_aliases[key]!r} bị ánh xạ tới nhiều "
                    "chi nhánh khác nhau."
                )
            mappings[alias] = canonical
            canonical_by_key[key] = canonical
            display_aliases[key] = alias
        if not mappings:
            raise GoogleDriveOutputError(
                "Google Sheet cấu hình chi nhánh chưa có ánh xạ nào."
            )
        return mappings

    @classmethod
    def _parse_product_catalog(
        cls, rows: Iterable[list[Any]]
    ) -> tuple[ProductCatalogEntry, ...]:
        products: list[ProductCatalogEntry] = []
        seen: set[tuple[str, str]] = set()
        for row_number, row in enumerate(rows, start=2):
            branch_name = str(row[0]).strip() if row else ""
            product_name = str(row[1]).strip() if len(row) >= 2 else ""
            unit = str(row[2]).strip() if len(row) >= 3 else ""
            aliases_value = str(row[3]).strip() if len(row) >= 4 else ""
            if not any((branch_name, product_name, unit, aliases_value)):
                continue
            if not product_name:
                raise GoogleDriveOutputError(
                    f"Cấu hình sản phẩm dòng {row_number} thiếu tên sản phẩm chuẩn."
                )
            key = (
                cls._normalise_branch_key(branch_name),
                cls._normalise_branch_key(product_name),
            )
            if key in seen:
                raise GoogleDriveOutputError(
                    f"Sản phẩm {product_name!r} bị lặp cho chi nhánh "
                    f"{branch_name or 'dùng chung'!r}."
                )
            seen.add(key)
            aliases = [
                alias.strip()
                for alias in aliases_value.split("|")
                if alias.strip()
            ]
            products.append(
                ProductCatalogEntry(
                    branch_name=branch_name,
                    product_name=product_name,
                    unit=unit,
                    aliases=aliases,
                )
            )
        return tuple(products)

    @classmethod
    def _canonicalise_product_branches(
        cls,
        products: Iterable[ProductCatalogEntry],
        branch_mappings: dict[str, str],
    ) -> tuple[ProductCatalogEntry, ...]:
        branch_lookup = {
            cls._normalise_branch_key(alias): canonical
            for alias, canonical in branch_mappings.items()
        }
        branch_lookup.update(
            {
                cls._normalise_branch_key(canonical): canonical
                for canonical in branch_mappings.values()
            }
        )
        result: list[ProductCatalogEntry] = []
        for product in products:
            branch_name = product.branch_name.strip()
            if not branch_name or branch_name == "*":
                result.append(product)
                continue
            canonical = branch_lookup.get(cls._normalise_branch_key(branch_name))
            if canonical is None:
                raise GoogleDriveOutputError(
                    f"Chi nhánh {branch_name!r} trong tab {PRODUCT_CONFIG_TAB!r} "
                    "không có trong cấu hình chi nhánh."
                )
            result.append(product.model_copy(update={"branch_name": canonical}))
        return tuple(result)

    @staticmethod
    def _normalise_branch_key(value: str) -> str:
        return " ".join(value.casefold().split())

    def _ensure_daily_sheet(self, name: str) -> tuple[DriveResource, str, int]:
        marker_query = (
            f"appProperties has {{ key='{SHEET_MARKER_KEY}' and "
            f"value='{SHEET_MARKER_VALUE}' }}"
        )
        resource = self._find_resource(
            self.parent_folder_id,
            name,
            GOOGLE_SHEET_MIME,
            extra_query=marker_query,
        )
        created = resource is None
        if resource is None:
            resource = self._create_resource(
                name,
                GOOGLE_SHEET_MIME,
                self.parent_folder_id,
                app_properties={SHEET_MARKER_KEY: SHEET_MARKER_VALUE},
            )

        sheets = self._sheet_metadata(resource.id)
        if created:
            sheet_id, sheet_title, row_count = sheets[0]
            if sheet_title != MESSAGE_SHEET_TITLE:
                self._rename_sheet(
                    resource.id,
                    sheet_id,
                    MESSAGE_SHEET_TITLE,
                )
                sheet_title = MESSAGE_SHEET_TITLE
        else:
            message_sheet = next(
                (item for item in sheets if item[1] == MESSAGE_SHEET_TITLE),
                None,
            )
            if message_sheet is None:
                raise GoogleDriveOutputError(
                    f"Google Sheet {resource.id} không có tab "
                    f"{MESSAGE_SHEET_TITLE!r}; tool dừng để tránh ghi nhầm tab."
                )
            sheet_id, sheet_title, row_count = message_sheet
        self._ensure_sheet_header(resource.id, sheet_id, sheet_title)
        return resource, sheet_title, row_count

    def _ensure_image_folder(self, name: str) -> DriveResource:
        resource = self._find_resource(
            self.parent_folder_id,
            name,
            GOOGLE_FOLDER_MIME,
        )
        if resource is not None:
            return resource
        return self._create_resource(name, GOOGLE_FOLDER_MIME, self.parent_folder_id)

    def _find_resource(
        self,
        parent_id: str,
        name: str,
        mime_type: str,
        *,
        extra_query: str | None = None,
    ) -> DriveResource | None:
        query = (
            f"'{self._query_literal(parent_id)}' in parents and trashed = false "
            f"and name = '{self._query_literal(name)}' "
            f"and mimeType = '{self._query_literal(mime_type)}'"
        )
        if extra_query:
            query += f" and {extra_query}"
        payload = self._request_json(
            "GET",
            f"{DRIVE_API}/files",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": 10,
                "orderBy": "createdTime desc",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "fields": "files(id,name,mimeType,webViewLink)",
            },
        )
        files = payload.get("files") or []
        if not files:
            return None
        return self._resource(files[0])

    def _create_resource(
        self,
        name: str,
        mime_type: str,
        parent_id: str,
        *,
        app_properties: dict[str, str] | None = None,
    ) -> DriveResource:
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": mime_type,
            "parents": [parent_id],
        }
        if app_properties:
            metadata["appProperties"] = app_properties
        payload = self._request_json(
            "POST",
            f"{DRIVE_API}/files",
            expected=(200, 201),
            params={
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
            json=metadata,
        )
        resource_id = str(payload.get("id") or "")
        if not resource_id:
            raise GoogleDriveOutputError("Google Drive không trả id tài nguyên mới.")
        verified = self._request_json(
            "GET",
            f"{DRIVE_API}/files/{quote(resource_id, safe='')}",
            params={
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,webViewLink",
            },
        )
        return self._resource(verified)

    def _sheet_metadata(self, spreadsheet_id: str) -> list[tuple[int, str, int]]:
        payload = self._request_json(
            "GET",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}",
            params={
                "fields": "sheets(properties(sheetId,title,gridProperties(rowCount)))"
            },
        )
        sheets = payload.get("sheets") or []
        if not sheets:
            raise GoogleDriveOutputError("Google Sheet không có tab để ghi dữ liệu.")
        result: list[tuple[int, str, int]] = []
        for sheet in sheets:
            properties = sheet.get("properties") or {}
            try:
                grid_properties = properties.get("gridProperties") or {}
                result.append(
                    (
                        int(properties["sheetId"]),
                        str(properties["title"]),
                        max(1, int(grid_properties.get("rowCount") or 1000)),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GoogleDriveOutputError(
                    "Metadata tab Google Sheet không hợp lệ."
                ) from exc
        return result

    def _first_sheet(self, spreadsheet_id: str) -> tuple[int, str, int]:
        return self._sheet_metadata(spreadsheet_id)[0]

    def _rename_sheet(
        self, spreadsheet_id: str, sheet_id: int, new_title: str
    ) -> None:
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "title": new_title},
                        "fields": "title",
                    }
                }
            ],
        )

    def _ensure_sheet_header(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_title: str,
    ) -> None:
        header_range = f"{self._a1_sheet(sheet_title)}!A1:J1"
        values = self._get_values(spreadsheet_id, header_range)
        if values:
            existing = [str(value) for value in values[0]]
            if existing == list(SHEET_HEADERS):
                return
            if existing == list(LEGACY_SHEET_HEADERS):
                self._request_json(
                    "PUT",
                    f"{SHEETS_API}/spreadsheets/"
                    f"{quote(spreadsheet_id, safe='')}/values/"
                    f"{quote(f'{self._a1_sheet(sheet_title)}!J1', safe='')}",
                    params={"valueInputOption": "RAW"},
                    json={"majorDimension": "ROWS", "values": [["Chi nhánh"]]},
                )
                self._format_migrated_branch_column(spreadsheet_id, sheet_id)
                return
            if existing != list(SHEET_HEADERS):
                raise GoogleDriveOutputError(
                    f"Sheet {spreadsheet_id} đã có header khác cấu trúc crawler; "
                    "tool dừng để không ghi đè dữ liệu."
                )

        self._request_json(
            "PUT",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(header_range, safe='')}",
            params={"valueInputOption": "RAW"},
            json={"majorDimension": "ROWS", "values": [list(SHEET_HEADERS)]},
        )
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(SHEET_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.035,
                                        "green": 0.408,
                                        "blue": 1.0,
                                    }
                                },
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColorStyle": {
                                        "rgbColor": {
                                            "red": 1.0,
                                            "green": 1.0,
                                            "blue": 1.0,
                                        }
                                    },
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColorStyle,textFormat)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(SHEET_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {"verticalAlignment": "TOP"}
                        },
                        "fields": "userEnteredFormat.verticalAlignment",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": 7,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {"wrapStrategy": "WRAP"}
                        },
                        "fields": "userEnteredFormat.wrapStrategy",
                    }
                },
                *self._column_width_requests(sheet_id),
            ],
        )

    def _append_unique_text_rows(
        self,
        spreadsheet_id: str,
        sheet_title: str,
        row_count: int,
        rows: list[list[Any]],
    ) -> int:
        existing_keys: set[tuple[str, str]] = set()
        chunk_size = 20_000
        for start_row in range(2, row_count + 1, chunk_size):
            end_row = min(row_count, start_row + chunk_size - 1)
            key_range = (
                f"{self._a1_sheet(sheet_title)}!B{start_row}:C{end_row}"
            )
            existing_values = self._get_values(spreadsheet_id, key_range)
            existing_keys.update(
                (str(row[0]), str(row[1]))
                for row in existing_values
                if len(row) >= 2
            )
        pending = [
            row for row in rows if (str(row[1]), str(row[2])) not in existing_keys
        ]
        if not pending:
            return 0

        append_range = f"{self._a1_sheet(sheet_title)}!A:J"
        self._request_json(
            "POST",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(append_range, safe='')}:append",
            params={
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"majorDimension": "ROWS", "values": pending},
        )
        return len(pending)

    def _get_values(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        payload = self._request_json(
            "GET",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(range_name, safe='')}",
            params={"majorDimension": "ROWS"},
        )
        values = payload.get("values") or []
        return [row for row in values if isinstance(row, list)]

    def _sheets_batch_update(
        self, spreadsheet_id: str, requests: list[dict[str, Any]]
    ) -> None:
        if not requests or any(len(request) != 1 for request in requests):
            raise GoogleDriveOutputError(
                "Mỗi Sheets batch request phải có đúng một loại thao tác."
            )
        self._request_json(
            "POST",
            f"{SHEETS_API}/spreadsheets/{quote(spreadsheet_id, safe='')}:batchUpdate",
            json={"requests": requests},
        )

    def _format_migrated_branch_column(
        self, spreadsheet_id: str, sheet_id: int
    ) -> None:
        self._sheets_batch_update(
            spreadsheet_id,
            [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 9,
                            "endColumnIndex": 10,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": {
                                        "red": 0.035,
                                        "green": 0.408,
                                        "blue": 1.0,
                                    }
                                },
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColorStyle": {
                                        "rgbColor": {
                                            "red": 1.0,
                                            "green": 1.0,
                                            "blue": 1.0,
                                        }
                                    },
                                },
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColorStyle,textFormat)"
                        ),
                    }
                },
                self._column_width_requests(sheet_id)[9],
            ],
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
        digest = media.sha256 or hashlib.sha256(media_path.read_bytes()).hexdigest()
        source_key = hashlib.sha256(
            f"{group_name}\0{message_id}\0{digest}".encode("utf-8")
        ).hexdigest()
        filename = f"{safe_slug(group_name)}-{media_path.name}"
        marker_query = (
            f"appProperties has {{ key='{IMAGE_MARKER_KEY}' and "
            f"value='{source_key}' }}"
        )
        existing = self._find_resource(
            folder_id,
            filename,
            media.mime_type,
            extra_query=marker_query,
        )
        if existing is not None:
            return ImageUploadResult(resource=existing, created=False)

        metadata = {
            "name": filename,
            "parents": [folder_id],
            "appProperties": {IMAGE_MARKER_KEY: source_key},
        }
        boundary = f"codex-{uuid.uuid4().hex}"
        metadata_bytes = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        file_bytes = media_path.read_bytes()
        body = b"\r\n".join(
            [
                f"--{boundary}".encode(),
                b"Content-Type: application/json; charset=UTF-8",
                b"",
                metadata_bytes,
                f"--{boundary}".encode(),
                f"Content-Type: {media.mime_type}".encode(),
                b"",
                file_bytes,
                f"--{boundary}--".encode(),
                b"",
            ]
        )
        payload = self._request_json(
            "POST",
            f"{DRIVE_UPLOAD_API}/files",
            expected=(200, 201),
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,name,webViewLink",
            },
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            data=body,
        )
        resource_id = str(payload.get("id") or "")
        name = str(payload.get("name") or filename)
        if not resource_id:
            raise GoogleDriveOutputError("Google Drive không trả id ảnh vừa tải lên.")
        url = str(payload.get("webViewLink") or "")
        if not url:
            url = f"https://drive.google.com/file/d/{resource_id}/view"
        return ImageUploadResult(
            resource=DriveResource(id=resource_id, name=name, url=url),
            created=True,
        )
        return True

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> dict[str, Any]:
        method = method.upper()
        max_attempts = GOOGLE_API_GET_MAX_ATTEMPTS if method == "GET" else 1
        timeout = (
            GOOGLE_API_CONNECT_TIMEOUT_SECONDS,
            GOOGLE_API_READ_TIMEOUT_SECONDS,
        )
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs,
                )
            except (RequestsTimeout, RequestsConnectionError) as exc:
                if attempt < max_attempts:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise GoogleDriveOutputError(
                    "Không kết nối được Google API sau "
                    f"{attempt} lần ({method}): {exc}"
                ) from exc
            except Exception as exc:
                raise GoogleDriveOutputError(
                    f"Không kết nối được Google API ({method}): {exc}"
                ) from exc

            try:
                payload = response.json()
            except Exception:
                payload = {}
            if response.status_code in expected:
                if not isinstance(payload, dict):
                    raise GoogleDriveOutputError("Google API trả dữ liệu không hợp lệ.")
                return payload

            if (
                method == "GET"
                and response.status_code in GOOGLE_API_RETRYABLE_STATUS_CODES
                and attempt < max_attempts
            ):
                time.sleep(2 ** (attempt - 1))
                continue

            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            else:
                detail = str(error or "")
            if not detail:
                detail = str(getattr(response, "text", ""))[:500]
            raise GoogleDriveOutputError(
                f"Google API trả HTTP {response.status_code}: "
                f"{detail or 'không có chi tiết lỗi'}"
            )

        raise GoogleDriveOutputError("Google API không trả kết quả sau khi thử lại.")

    @staticmethod
    def _resource(payload: dict[str, Any]) -> DriveResource:
        resource_id = str(payload.get("id") or "")
        name = str(payload.get("name") or "")
        url = str(payload.get("webViewLink") or "")
        if not resource_id or not name or not url:
            raise GoogleDriveOutputError(
                "Google Drive không trả đủ id, tên và link của tài nguyên."
            )
        return DriveResource(id=resource_id, name=name, url=url)

    @staticmethod
    def _query_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _a1_sheet(title: str) -> str:
        return "'" + title.replace("'", "''") + "'"

    @staticmethod
    def _column_letter(column_number: int) -> str:
        if column_number < 1:
            raise ValueError("Số cột Google Sheet phải lớn hơn 0.")
        result = ""
        value = column_number
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @staticmethod
    def _row_value(row: list[Any], index: int) -> str:
        return str(row[index]) if index < len(row) else ""

    @staticmethod
    def _column_width_requests(sheet_id: int) -> list[dict[str, Any]]:
        widths = (96, 180, 220, 60, 160, 90, 110, 420, 90, 200)
        return [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": pixel_size},
                    "fields": "pixelSize",
                }
            }
            for index, pixel_size in enumerate(widths)
        ]

    @staticmethod
    def _ocr_column_width_requests(sheet_id: int) -> list[dict[str, Any]]:
        widths = (96, 180, 200, 220, 130, 220, 280, 90, 90)
        return [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": pixel_size},
                    "fields": "pixelSize",
                }
            }
            for index, pixel_size in enumerate(widths)
        ]

    @staticmethod
    def _ocr_review_column_width_requests(sheet_id: int) -> list[dict[str, Any]]:
        widths = (
            96,
            180,
            200,
            220,
            130,
            140,
            360,
            300,
            130,
            220,
            280,
            100,
            110,
            130,
            280,
        )
        return [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": pixel_size},
                    "fields": "pixelSize",
                }
            }
            for index, pixel_size in enumerate(widths)
        ]

    @staticmethod
    def _sheet_tab_url(url: str, sheet_id: int) -> str:
        return f"{url.split('#', 1)[0]}#gid={sheet_id}"

    @staticmethod
    def _configured_path(value: str, base_dir: Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else base_dir / path

    @staticmethod
    def _oauth_redirect_port() -> int:
        raw = os.environ.get("GOOGLE_OAUTH_REDIRECT_PORT", "8766").strip()
        try:
            port = int(raw)
        except ValueError as exc:
            raise GoogleDriveOutputError(
                "GOOGLE_OAUTH_REDIRECT_PORT phải là số nguyên."
            ) from exc
        if not 1 <= port <= 65535:
            raise GoogleDriveOutputError(
                "GOOGLE_OAUTH_REDIRECT_PORT phải nằm trong khoảng 1-65535."
            )
        return port
