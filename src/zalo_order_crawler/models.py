from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


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
    customer_name: str | None = None
    phone: str | None = None
    address: str | None = None
    products: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    notes: str | None = None


class OrderDecisionBatch(BaseModel):
    decisions: list[OrderDecision]


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
    files: dict[str, str]
    google_drive: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
