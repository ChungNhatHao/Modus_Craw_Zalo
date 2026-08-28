from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Tag

from .models import CleanMessage, MediaAsset, RawMessage


_SYSTEM_ONLY_PATTERNS = (
    re.compile(r"^tin nhắn đã được thu hồi[.!]?$", re.IGNORECASE),
    re.compile(r"^bạn đã thu hồi một tin nhắn[.!]?$", re.IGNORECASE),
    re.compile(r"^(đã |vừa )?(tham gia|rời khỏi) nhóm[.!]?$", re.IGNORECASE),
)


def _first_text(soup: BeautifulSoup | Tag, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        try:
            node = soup.select_one(selector)
        except Exception:
            continue
        if node:
            text = _normalise_text(node.get_text("\n", strip=True))
            if text:
                return text
    return None


def _normalise_text(value: str) -> str:
    lines: list[str] = []
    for raw_line in value.replace("\xa0", " ").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines).strip()


def _message_type(soup: BeautifulSoup, media: Iterable[MediaAsset]) -> str:
    root = soup.find(True)
    classes = " ".join(root.get("class", [])) if isinstance(root, Tag) else ""
    class_text = classes.lower()
    roles = {item.role for item in media}
    if soup.find("video") or "video" in class_text:
        return "video"
    if soup.find("audio") or "voice" in class_text or "audio" in class_text:
        return "audio"
    if "file" in class_text or soup.select_one("[class*='file-message']"):
        return "file"
    if "sticker" in roles or "sticker" in class_text:
        return "sticker"
    if "message_image" in roles or "picture" in class_text:
        return "image"
    if soup.select_one(
        "[data-id*='Msg_Photo'] img, [data-id*='Msg_Image'] img, "
        "[class*='photo-message'] img, [class*='img-msg-v2'] img, "
        "img[alt], img[title]"
    ):
        return "image"
    if soup.find("a", href=True):
        return "link"
    return "text"


def _direction(soup: BeautifulSoup) -> str:
    root = soup.find(True)
    root_classes = set(root.get("class", [])) if isinstance(root, Tag) else set()
    if "me" in root_classes or soup.select_one(".card.me, [class*='message'].me"):
        return "outgoing"
    if "you" in root_classes or "friend" in root_classes:
        return "incoming"
    return "unknown"


def _content_text(
    soup: BeautifulSoup,
    content_selectors: Iterable[str],
    sender_selectors: Iterable[str],
    timestamp_selectors: Iterable[str],
    noise_selectors: Iterable[str],
) -> str:
    working = BeautifulSoup(str(soup), "html.parser")

    for selector in [*noise_selectors, *sender_selectors, *timestamp_selectors]:
        try:
            for node in working.select(selector):
                node.decompose()
        except Exception:
            continue

    for selector in content_selectors:
        try:
            candidates = working.select(selector)
        except Exception:
            continue
        texts = [_normalise_text(node.get_text("\n", strip=True)) for node in candidates]
        texts = [text for text in texts if text]
        if texts:
            # Chọn node có nội dung dài nhất để tránh lấy một span con bị cắt cụt.
            return max(texts, key=len)

    return _normalise_text(working.get_text("\n", strip=True))


def parse_message(raw: RawMessage, selectors: dict[str, list[str]]) -> CleanMessage | None:
    soup = BeautifulSoup(raw.html, "html.parser")
    sender = _first_text(soup, selectors.get("sender", []))
    timestamp = _first_text(soup, selectors.get("timestamp", []))
    content = _content_text(
        soup,
        selectors.get("content", []),
        selectors.get("sender", []),
        selectors.get("timestamp", []),
        selectors.get("noise", []),
    )

    if not content:
        # Ảnh/file không có caption vẫn được giữ dưới dạng mô tả nếu DOM có alt/title.
        media_descriptions = [
            node.get("alt") or node.get("title")
            for node in soup.select("img[alt], img[title], a[title]")
        ]
        content = _normalise_text("\n".join(value for value in media_descriptions if value))

    if not content and any(item.role == "message_image" for item in raw.media):
        content = "[Hình ảnh]"

    if not content or any(pattern.fullmatch(content) for pattern in _SYSTEM_ONLY_PATTERNS):
        return None

    return CleanMessage(
        message_id=raw.message_id,
        sequence=raw.sequence,
        sender=sender,
        timestamp_text=timestamp,
        content=content,
        direction=_direction(soup),
        message_type=_message_type(soup, raw.media),
        media=raw.media,
        raw_html=raw.html,
    )


def clean_messages(
    raw_messages: Iterable[RawMessage], selectors: dict[str, list[str]]
) -> list[CleanMessage]:
    result: list[CleanMessage] = []
    seen_ids: set[str] = set()
    for raw in sorted(raw_messages, key=lambda item: item.sequence):
        if raw.message_id in seen_ids:
            continue
        parsed = parse_message(raw, selectors)
        if parsed is not None:
            result.append(parsed)
            seen_ids.add(raw.message_id)
    return result
