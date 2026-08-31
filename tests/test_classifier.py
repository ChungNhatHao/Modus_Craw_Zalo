from pathlib import Path

import pytest

from zalo_order_crawler.classifier import GeminiOrderClassifier
from zalo_order_crawler.models import CleanMessage, MediaAsset, OrderDecision


def make_classifier(tmp_path: Path) -> GeminiOrderClassifier:
    return GeminiOrderClassifier(
        api_key="test-key",
        model="test-model",
        batch_size=10,
        cache_dir=tmp_path / "cache",
        media_base_dir=tmp_path,
    )


def test_build_contents_attaches_only_message_images(tmp_path: Path) -> None:
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "order.png").write_bytes(image_bytes)
    (assets / "thumbnail.png").write_bytes(image_bytes)
    message = CleanMessage(
        message_id="m1",
        sequence=0,
        content="[Hình ảnh]",
        message_type="image",
        media=[
            MediaAsset(
                path="assets/order.png",
                mime_type="image/png",
                role="message_image",
            ),
            MediaAsset(
                path="assets/thumbnail.png",
                mime_type="image/png",
                role="link_thumbnail",
            ),
        ],
    )

    parts, signature = make_classifier(tmp_path)._build_contents("prompt", [message])

    assert len(parts) == 3
    assert parts[0].text == "prompt"
    assert '"message_id": "m1"' in (parts[1].text or "")
    assert parts[2].inline_data is not None
    assert parts[2].inline_data.data == image_bytes
    assert parts[2].inline_data.mime_type == "image/png"
    assert "m1:message_image:image/png" in signature


def test_media_path_cannot_escape_result_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"image")
    classifier = make_classifier(tmp_path)

    with pytest.raises(ValueError, match="nằm ngoài"):
        classifier._resolve_media_path(str(outside))


def test_review_policy_accepts_high_confidence_complete_order() -> None:
    decision = OrderDecision(
        message_id="m1",
        is_order=True,
        confidence=0.96,
        data_confidence=0.94,
        reason="Có sản phẩm và số lượng rõ ràng.",
        products=["Cà chua"],
        quantities=["2 kg"],
    )

    result = GeminiOrderClassifier._apply_review_policy(decision)

    assert result.data_confidence == 0.94
    assert result.needs_review is False


def test_review_policy_caps_incomplete_order_and_requires_review() -> None:
    decision = OrderDecision(
        message_id="m2",
        is_order=True,
        confidence=0.99,
        data_confidence=0.98,
        reason="Có sản phẩm nhưng thiếu số lượng.",
        products=["Cà chua"],
        quantities=[],
    )

    result = GeminiOrderClassifier._apply_review_policy(decision)

    assert result.data_confidence == 0.75
    assert result.needs_review is True


def test_branch_name_is_normalised_from_google_sheet_config(tmp_path: Path) -> None:
    classifier = GeminiOrderClassifier(
        api_key="test-key",
        model="test-model",
        batch_size=10,
        cache_dir=tmp_path / "cache",
        branch_mappings={"S6": "Chi nhánh Phạm Văn Đồng"},
    )
    decision = OrderDecision(
        message_id="m1",
        is_order=True,
        confidence=0.98,
        data_confidence=0.95,
        reason="Có đơn",
        branch_name="s6",
        products=["rau"],
        quantities=["2 kg"],
    )

    results = classifier._validate_batch(
        [decision], [CleanMessage(message_id="m1", sequence=1, content="S6 thêm rau")]
    )

    assert results[0].branch_name == "Chi nhánh Phạm Văn Đồng"
    assert results[0].needs_review is False


def test_missing_configured_branch_requires_review() -> None:
    decision = OrderDecision(
        message_id="m1",
        is_order=True,
        confidence=0.98,
        data_confidence=0.95,
        reason="Có đơn",
        products=["rau"],
        quantities=["2 kg"],
    )

    result = GeminiOrderClassifier._apply_review_policy(
        decision, require_branch=True
    )

    assert result.needs_review is True
