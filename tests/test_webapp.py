from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zalo_order_crawler.config import Settings
from zalo_order_crawler.webapp import AppManager, normalise_groups, validate_iso_date


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        project_dir=tmp_path,
        group_name="Modus-test bot v2",
        browser_mode="persistent",
        cdp_url="http://127.0.0.1:9222",
        browser_profile_dir=tmp_path / ".browser-profile",
        browser_channel="chrome",
        slow_mo_ms=0,
        gemini_api_key="test",
        gemini_model="test-model",
        gemini_batch_size=10,
        timezone=ZoneInfo("Asia/Ho_Chi_Minh"),
        output_dir=tmp_path / "output",
        selectors_file=tmp_path / "selectors.json",
        max_scrolls=10,
        scroll_pause_ms=10,
        allow_zalo_history_sync=False,
    )


def test_normalise_groups_deduplicates_and_removes_blank_lines() -> None:
    assert normalise_groups([" Nhóm A ", "", "Nhóm   B", "Nhóm A"]) == [
        "Nhóm A",
        "Nhóm B",
    ]


def test_normalise_groups_requires_at_least_one_group() -> None:
    with pytest.raises(ValueError, match="ít nhất một"):
        normalise_groups(["", "  "])


def test_validate_iso_date() -> None:
    assert validate_iso_date("2026-08-28") == "2026-08-28"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        validate_iso_date("28/08/2026")


def test_bootstrap_uses_timezone_today_and_default_group(tmp_path: Path) -> None:
    manager = AppManager(make_settings(tmp_path))
    bootstrap = manager.bootstrap()

    assert bootstrap["today"] == datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
    assert bootstrap["default_groups"] == ["Modus-test bot v2"]
    assert bootstrap["timezone"] == "Asia/Ho_Chi_Minh"


def test_crawl_worker_runs_groups_sequentially(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = AppManager(make_settings(tmp_path))
    commands: list[list[str]] = []

    class FakeProcess:
        returncode = 0

        def communicate(self) -> tuple[str, None]:
            return "Hoàn tất: output/test\n", None

    def fake_popen(command: list[str], **_: object) -> FakeProcess:
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr("zalo_order_crawler.webapp.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        manager,
        "_latest_output_dir",
        lambda group, target: str(
            manager.settings.output_dir / group / target / "test-run"
        ),
    )

    manager._crawl_worker(["Nhóm A", "Nhóm B"], "2026-08-28")
    status = manager.status()["crawl"]

    assert [command[command.index("--group") + 1] for command in commands] == ["Nhóm A", "Nhóm B"]
    assert status["state"] == "completed"
    assert status["completed"] == 2
    assert [item["group"] for item in status["results"]] == ["Nhóm A", "Nhóm B"]
    assert [item["run_id"] for item in status["results"]] == [
        "Nhóm A/2026-08-28/test-run",
        "Nhóm B/2026-08-28/test-run",
    ]


def test_last_log_line_prefers_the_actual_error() -> None:
    output = """
    Đang cuộn và thu thập tin nhắn...
    Lỗi: Không xác định được thanh cuộn chứa tin nhắn.
    Đã lưu chẩn đoán tại debug/zalo-selector-error.png.
    """

    assert AppManager._last_log_line(output) == (
        "Lỗi: Không xác định được thanh cuộn chứa tin nhắn."
    )


def test_run_result_returns_ai_decision_and_safe_asset_url(tmp_path: Path) -> None:
    manager = AppManager(make_settings(tmp_path))
    run_id = "Modus-test-bot-v2/2026-08-25/155556"
    run_dir = manager.settings.output_dir / run_id
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True)
    image_path = assets_dir / "order.jpg"
    image_path.write_bytes(b"jpeg-data")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "group_name": "Modus-test bot v2",
                "target_date": "2026-08-25",
                "raw_message_count": 1,
                "clean_message_count": 1,
                "classified_message_count": 1,
                "order_count": 1,
                "media_count": 1,
                "message_image_count": 1,
                "google_drive": {
                    "sheet": {
                        "id": "sheet-id",
                        "name": "25-08-2026",
                        "url": "https://docs.google.com/spreadsheets/d/sheet-id/edit",
                    }
                },
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "clean_messages.jsonl").write_text(
        json.dumps(
            {
                "message_id": "m1",
                "sequence": 0,
                "sender": "Lan",
                "timestamp_text": "09:15",
                "content": "[Hình ảnh]",
                "direction": "incoming",
                "message_type": "image",
                "media": [
                    {
                        "path": "assets/order.jpg",
                        "mime_type": "image/jpeg",
                        "role": "message_image",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "classifications.jsonl").write_text(
        json.dumps(
            {
                "message_id": "m1",
                "is_order": True,
                "confidence": 0.98,
                "data_confidence": 0.93,
                "needs_review": False,
                "reason": "Ảnh chứa phiếu đặt hàng.",
                "products": ["Cải thìa"],
                "quantities": ["2 kg"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = manager.run_result(run_id)
    asset, mime_type = manager.run_asset(run_id, "assets/order.jpg")

    assert result["summary"]["orders"] == 1
    assert result["messages"][0]["decision"]["products"] == ["Cải thìa"]
    assert result["messages"][0]["decision"]["data_confidence"] == 0.93
    assert result["messages"][0]["media"][0]["url"].startswith("/api/run-asset?")
    assert result["google_drive"]["sheet"]["id"] == "sheet-id"
    assert asset == image_path
    assert mime_type == "image/jpeg"


def test_run_result_blocks_paths_outside_output(tmp_path: Path) -> None:
    manager = AppManager(make_settings(tmp_path))

    with pytest.raises(ValueError, match="ngoài thư mục output"):
        manager._resolve_run_dir("../private")


def test_run_asset_must_be_listed_in_clean_messages(tmp_path: Path) -> None:
    manager = AppManager(make_settings(tmp_path))
    run_id = "group/2026-08-25/120000"
    run_dir = manager.settings.output_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "clean_messages.jsonl").write_text("", encoding="utf-8")
    secret = run_dir / "secret.txt"
    secret.write_text("private", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="không thuộc"):
        manager.run_asset(run_id, "secret.txt")
