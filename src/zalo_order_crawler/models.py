from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


OCR_IMAGE_QUALITY_REVIEW_THRESHOLD = 0.85


class MediaAsset(BaseModel):
    path: str
    mime_type: str
    role: str = "message_image"
    size_bytes: int = 0
    sha256: str = ""


class RawMessage(BaseModel):
    message_id: str
    sequence: int = 0
    html: str
    text_hint: str = ""
    date_label: str | None = None
    media: list[MediaAsset] = Field(default_factory=list)
    captured_at: datetime


class CleanMessage(BaseModel):
    message_id: str
    sequence: int
    sender: str | None = None
    timestamp_text: str | None = None
    content: str
    direction: str = "unknown"
    message_type: str = "text"
    media: list[MediaAsset] = Field(default_factory=list)
    raw_html: str | None = Field(default=None, exclude=True)


class OrderDecision(BaseModel):
    message_id: str
    is_order: bool
    confidence: float = Field(ge=0, le=1)
    data_confidence: float = Field(ge=0, le=1)
    needs_review: bool = False
    reason: str
    branch_name: str | None = None
    customer_name: str | None = None
    phone: str | None = None
    address: str | None = None
    products: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    notes: str | None = None


class OrderDecisionBatch(BaseModel):
    decisions: list[OrderDecision]


class ProductCatalogEntry(BaseModel):
    branch_name: str = ""
    product_name: str
    unit: str = ""
    aliases: list[str] = Field(default_factory=list)


class OcrLineItem(BaseModel):
    customer_code: str = ""
    customer_name: str = ""
    product_name: str
    unit: str = ""
    quantity: float | None = None


class ImageOcrResult(BaseModel):
    message_id: str
    media_path: str
    applicable: bool
    skip_reason: str | None = None
    image_quality_score: float = Field(default=1.0, ge=0, le=1)
    image_quality_affects_output: bool = False
    image_quality_reason: str | None = None
    needs_review: bool = False
    review_reason: str | None = None
    items: list[OcrLineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def apply_image_quality_review_policy(self) -> "ImageOcrResult":
        quality_requires_review = (
            self.image_quality_score < OCR_IMAGE_QUALITY_REVIEW_THRESHOLD
            and self.image_quality_affects_output
        )
        self.needs_review = self.needs_review or quality_requires_review
        if quality_requires_review and not self.review_reason:
            self.review_reason = (
                self.image_quality_reason
                or "Chất lượng ảnh có thể làm sai hoặc thiếu kết quả OCR."
            )
        return self

    def apply_order_review_policy(
        self, decision: OrderDecision
    ) -> "ImageOcrResult":
        if not decision.needs_review:
            return self

        data_percent = round(decision.data_confidence * 100, 1)
        decision_reason = (
            "Độ tin cậy thông tin đơn ở bước phân loại chỉ đạt "
            f"{data_percent:g}%; cần đối chiếu lại tên hàng, số lượng và chi nhánh."
        )
        reasons = [reason for reason in (self.review_reason, decision_reason) if reason]
        return self.model_copy(
            update={
                "needs_review": True,
                "review_reason": " ".join(dict.fromkeys(reasons)),
            }
        )


class CrawlManifest(BaseModel):
    group_name: str
    target_date: date
    started_at: datetime
    finished_at: datetime
    browser_mode: str
    raw_message_count: int
    clean_message_count: int
    classified_message_count: int
    order_count: int
    media_count: int = 0
    message_image_count: int = 0
    ocr_image_count: int = 0
    ocr_item_count: int = 0
    ocr_review_image_count: int = 0
    ocr_review_item_count: int = 0
    files: dict[str, str]
    google_drive: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
