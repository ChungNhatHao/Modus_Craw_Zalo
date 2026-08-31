from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .models import CleanMessage, OrderDecision, RawMessage


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-zÀ-ỹ_-]+", "-", value, flags=re.UNICODE).strip("-")
    return slug[:80] or "zalo-group"


def _serialise(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def write_json(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
    else:
        payload = value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, values: Iterable[BaseModel | dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_serialise(value), ensure_ascii=False) + "\n")


def write_raw_html(path: Path, messages: Iterable[RawMessage]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        "<!doctype html>",
        '<html lang="vi"><head><meta charset="utf-8">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self' data: https:; style-src 'unsafe-inline'\">",
        "<title>Zalo message snapshot</title>",
        "<style>body{font-family:system-ui;margin:24px}.captured-message{padding:12px;border-bottom:1px solid #ddd}</style>",
        "</head><body>",
    ]
    for message in messages:
        blocks.append(
            f'<section class="captured-message" data-crawler-id="{message.message_id}">{message.html}</section>'
        )
    blocks.append("</body></html>")
    path.write_text("\n".join(blocks), encoding="utf-8")


def write_html_fragment(path: Path, fragment: str, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = "\n".join(
        [
            "<!doctype html>",
            '<html lang="vi"><head><meta charset="utf-8">',
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self' data: https:; style-src 'unsafe-inline'\">",
            f"<title>{title}</title></head><body>",
            fragment,
            "</body></html>",
        ]
    )
    path.write_text(document, encoding="utf-8")


def write_orders_csv(
    path: Path,
    messages: Iterable[CleanMessage],
    decisions: Iterable[OrderDecision],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    message_by_id = {message.message_id: message for message in messages}
    fieldnames = [
        "message_id",
        "is_order",
        "confidence",
        "order_confidence_percent",
        "data_confidence",
        "data_confidence_percent",
        "needs_review",
        "branch_name",
        "sender",
        "time",
        "content",
        "media_paths",
        "reason",
        "customer_name",
        "phone",
        "address",
        "products",
        "quantities",
        "notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for decision in decisions:
            message = message_by_id.get(decision.message_id)
            writer.writerow(
                {
                    "message_id": decision.message_id,
                    "is_order": decision.is_order,
                    "confidence": decision.confidence,
                    "order_confidence_percent": round(decision.confidence * 100, 1),
                    "data_confidence": decision.data_confidence,
                    "data_confidence_percent": round(decision.data_confidence * 100, 1),
                    "needs_review": decision.needs_review,
                    "branch_name": decision.branch_name or "",
                    "sender": message.sender if message else "",
                    "time": message.timestamp_text if message else "",
                    "content": message.content if message else "",
                    "media_paths": (
                        "; ".join(item.path for item in message.media)
                        if message
                        else ""
                    ),
                    "reason": decision.reason,
                    "customer_name": decision.customer_name or "",
                    "phone": decision.phone or "",
                    "address": decision.address or "",
                    "products": "; ".join(decision.products),
                    "quantities": "; ".join(decision.quantities),
                    "notes": decision.notes or "",
                }
            )
