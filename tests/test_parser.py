from datetime import datetime, timezone
from pathlib import Path

from zalo_order_crawler.config import load_selectors
from zalo_order_crawler.models import MediaAsset, RawMessage
from zalo_order_crawler.parser import clean_messages, parse_message


SELECTORS = {
    "sender": [".card-sender-name"],
    "timestamp": [".card-send-time"],
    "content": [".chat-message-content", ".card"],
    "noise": ["button", ".reacts-list", ".chat-message__actions"],
}


def raw(message_id: str, html: str, sequence: int = 0) -> RawMessage:
    return RawMessage(
        message_id=message_id,
        sequence=sequence,
        html=html,
        captured_at=datetime.now(timezone.utc),
    )


def test_parse_message_removes_ui_noise_and_extracts_metadata() -> None:
    message = raw(
        "m-1",
        """
        <div class="chat-message">
          <div class="card">
            <span class="card-sender-name">Lan</span>
            <div class="chat-message-content">Chốt 2 áo xanh, giao 12 Lê Lợi</div>
            <span class="card-send-time">09:15</span>
            <div class="reacts-list">❤️ 3</div>
            <button>Trả lời</button>
          </div>
        </div>
        """,
    )

    parsed = parse_message(message, SELECTORS)

    assert parsed is not None
    assert parsed.sender == "Lan"
    assert parsed.timestamp_text == "09:15"
    assert parsed.content == "Chốt 2 áo xanh, giao 12 Lê Lợi"
    assert "Trả lời" not in parsed.content
    assert "❤️" not in parsed.content


def test_parse_message_detects_outgoing_and_media() -> None:
    message = raw(
        "m-2",
        '<div class="chat-message"><div class="card me card--picture">'
        '<img alt="Ảnh mẫu áo đỏ"><span class="card-send-time">10:00</span></div></div>',
    )

    parsed = parse_message(message, SELECTORS)

    assert parsed is not None
    assert parsed.direction == "outgoing"
    assert parsed.message_type == "image"
    assert parsed.content == "Ảnh mẫu áo đỏ"


def test_system_recall_message_is_filtered() -> None:
    message = raw(
        "m-3",
        '<div class="chat-message"><div class="chat-message-content">Tin nhắn đã được thu hồi</div></div>',
    )
    assert parse_message(message, SELECTORS) is None


def test_clean_messages_deduplicates_and_keeps_sequence_order() -> None:
    first = raw(
        "m-1",
        '<div class="chat-message-content">Đặt 1 hộp</div>',
        sequence=2,
    )
    second = raw(
        "m-2",
        '<div class="chat-message-content">Giao chiều nay</div>',
        sequence=1,
    )
    duplicate = first.model_copy(update={"sequence": 3})

    result = clean_messages([first, duplicate, second], SELECTORS)

    assert [item.message_id for item in result] == ["m-2", "m-1"]


def test_parse_current_zalo_reaction_class_does_not_remove_whole_message() -> None:
    selectors = load_selectors(
        Path(__file__).resolve().parents[1] / "config" / "selectors.json"
    )
    message = raw(
        "m-current-zalo",
        """
        <div class="chat-message chat-message-v2 me -send-time -reaction">
          <div class="card message-frame">
            <div data-component="message-text-content">
              <span>@Bot Cty Phạm Huy Đặt 2 kg cà chua</span>
            </div>
            <span class="card-send-time">21:33</span>
            <div class="message-reaction-container">/-heart</div>
          </div>
          <div class="message-reaction-v2-space"></div>
        </div>
        """,
    )

    parsed = parse_message(message, selectors)

    assert parsed is not None
    assert parsed.content == "@Bot Cty Phạm Huy Đặt 2 kg cà chua"
    assert parsed.timestamp_text == "21:33"
    assert parsed.message_type == "text"


def test_image_message_without_caption_is_kept_from_media_metadata() -> None:
    message = raw(
        "m-photo",
        '<div class="chat-message"><div class="photo-message-v2"><img></div></div>',
    ).model_copy(
        update={
            "media": [
                MediaAsset(
                    path="assets/m-photo.jpg",
                    mime_type="image/jpeg",
                    role="message_image",
                    size_bytes=123,
                    sha256="abc",
                )
            ]
        }
    )

    parsed = parse_message(message, SELECTORS)

    assert parsed is not None
    assert parsed.content == "[Hình ảnh]"
    assert parsed.message_type == "image"
    assert parsed.media == message.media


def test_reaction_icon_does_not_turn_text_message_into_image() -> None:
    message = raw(
        "m-reaction-icon",
        """
        <div class="chat-message -send-time -reaction">
          <div class="chat-message-content">Đặt 3 kg khoai</div>
          <div class="message-reaction-container"><img src="like.png"></div>
        </div>
        """,
    )

    parsed = parse_message(message, SELECTORS)

    assert parsed is not None
    assert parsed.message_type == "text"
