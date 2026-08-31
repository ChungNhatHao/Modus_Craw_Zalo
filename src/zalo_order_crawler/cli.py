from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from .browser import BrowserSession, save_diagnostics
from .classifier import GeminiOrderClassifier
from .config import Settings, load_selectors
from .crawler import ZaloCrawler
from .drive_output import GoogleDriveOutputPublisher
from .models import CleanMessage, CrawlManifest, ImageOcrResult
from .ocr import GeminiOrderImageOcr
from .parser import clean_messages
from .storage import (
    safe_slug,
    write_html_fragment,
    write_json,
    write_jsonl,
    write_order_ocr_csv,
    write_orders_csv,
    write_raw_html,
)


def _parse_date(value: str, settings: Settings) -> date:
    raw = value.strip().lower()
    if raw in {"today", "hom-nay", "hôm-nay", "hôm nay"}:
        return datetime.now(settings.timezone).date()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("Ngày phải là 'today', YYYY-MM-DD hoặc DD/MM/YYYY.")


def _project_dir() -> Path:
    return Path.cwd()


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_env(
        _project_dir(),
        group_name=getattr(args, "group", None),
        browser_mode=getattr(args, "browser_mode", None),
    )


def _run_crawl(args: argparse.Namespace, *, with_ai: bool) -> int:
    settings = _settings(args)
    selectors = load_selectors(settings.selectors_file)
    target_date = _parse_date(args.date, settings)
    if not settings.group_name:
        raise ValueError("Thiếu tên nhóm: truyền --group hoặc đặt ZALO_GROUP_NAME trong .env.")
    if with_ai and not settings.gemini_api_key:
        raise ValueError("Thiếu GEMINI_API_KEY trong .env. Dùng lệnh 'crawl' nếu chưa muốn chạy AI.")

    drive_publisher = None
    branch_config = None
    if settings.google_drive_upload_enabled:
        print("Đang kiểm tra quyền ghi Google Drive...")
        drive_publisher = GoogleDriveOutputPublisher.from_default_credentials(
            settings.google_drive_parent_folder_id,
            project_dir=settings.project_dir,
        )
        destination = drive_publisher.verify_destination()
        print(f"Google Drive output: {destination.name}")
        branch_config = drive_publisher.ensure_branch_config()
        print(
            "Cấu hình chi nhánh: "
            f"{len(branch_config.mappings)} ánh xạ; {branch_config.resource.url}"
        )

    started_at = datetime.now(settings.timezone)
    run_name = started_at.strftime("%H%M%S")
    run_dir = (
        settings.output_dir
        / safe_slug(settings.group_name)
        / target_date.isoformat()
        / run_name
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = run_dir / "assets"
    debug_dir = settings.project_dir / "debug" / started_at.strftime("%Y%m%d-%H%M%S")

    print(f"Nhóm: {settings.group_name}")
    print(f"Ngày: {target_date.strftime('%d/%m/%Y')}")
    print(f"Chế độ trình duyệt: {settings.browser_mode}")

    with BrowserSession(settings) as browser:
        assert browser.page is not None
        browser.wait_until_logged_in(settings_selectors(selectors, "ready"))
        crawler = ZaloCrawler(browser.page, settings, selectors)
        try:
            print("Đang mở nhóm...")
            crawler.open_group(settings.group_name)
            if crawler.ensure_message_history(allow_sync=settings.allow_zalo_history_sync):
                print("Đồng bộ hoàn tất; đang mở lại nhóm...")
                crawler.open_group(settings.group_name)
            print("Đang chọn bộ lọc ngày và mở kết quả...")
            if crawler.search_messages_for_date(target_date):
                print("Đang cuộn và thu thập tin nhắn...")
                artifacts = crawler.crawl_day(target_date, assets_dir=assets_dir)
            else:
                print("Không có tin nhắn trong ngày đã chọn.")
                artifacts = crawler.empty_day_artifacts(target_date)
        except Exception as exc:
            screenshot, html = save_diagnostics(browser.page, debug_dir, "zalo-selector-error")
            raise RuntimeError(
                f"{exc}\nĐã lưu chẩn đoán tại {screenshot} và {html}."
            ) from exc

    daily_name = target_date.strftime("%d-%m-%Y")
    raw_jsonl = run_dir / "raw_messages.jsonl"
    raw_html = run_dir / "raw_messages.html"
    view_html = run_dir / "message_view.html"
    styles_json = run_dir / "stylesheets.json"
    clean_jsonl = run_dir / "clean_messages.jsonl"
    classifications_jsonl = run_dir / "classifications.jsonl"
    orders_csv = run_dir / f"{daily_name}.csv"
    manifest_json = run_dir / "manifest.json"

    write_jsonl(raw_jsonl, artifacts.messages)
    write_raw_html(raw_html, artifacts.messages)
    write_html_fragment(view_html, artifacts.page_html, "Zalo message view snapshot")
    write_json(styles_json, artifacts.stylesheets)

    cleaned = clean_messages(artifacts.messages, selectors)
    write_jsonl(clean_jsonl, cleaned)
    print(f"Đã lấy {len(artifacts.messages)} node, còn {len(cleaned)} tin sau làm sạch.")

    decisions = []
    if with_ai:
        print(f"Đang phân loại bằng Gemini ({settings.gemini_model})...")
        classifier = GeminiOrderClassifier(
            api_key=settings.gemini_api_key or "",
            model=settings.gemini_model,
            batch_size=settings.gemini_batch_size,
            cache_dir=settings.project_dir / ".cache" / "gemini",
            media_base_dir=run_dir,
            branch_mappings=(branch_config.mappings if branch_config else {}),
        )
        decisions = classifier.classify(cleaned)
        write_jsonl(classifications_jsonl, decisions)
        write_orders_csv(orders_csv, cleaned, [item for item in decisions if item.is_order])

    order_ocr_jsonl = run_dir / "order_ocr.jsonl"
    order_ocr_csv = run_dir / f"{daily_name}-ocr.csv"
    ocr_results: list[ImageOcrResult] = []
    if with_ai:
        order_ids = {item.message_id for item in decisions if item.is_order}
        has_order_images = any(
            media.role == "message_image"
            for message in cleaned
            if message.message_id in order_ids
            for media in message.media
        )
        if has_order_images:
            print("Đang OCR ảnh phiếu đặt hàng bằng Gemini...")
            ocr_engine = GeminiOrderImageOcr(
                api_key=settings.gemini_api_key or "",
                model=settings.gemini_model,
                cache_dir=settings.project_dir / ".cache" / "gemini-ocr",
                media_base_dir=run_dir,
            )
            ocr_results = ocr_engine.extract(cleaned, decisions)
            write_jsonl(order_ocr_jsonl, ocr_results)
            write_order_ocr_csv(order_ocr_csv, settings.group_name, target_date, ocr_results)

    files = {
        "raw_jsonl": str(raw_jsonl),
        "raw_html": str(raw_html),
        "message_view_html": str(view_html),
        "stylesheets": str(styles_json),
        "clean_jsonl": str(clean_jsonl),
    }
    if with_ai:
        files.update(
            {
                "classifications_jsonl": str(classifications_jsonl),
                "orders_csv": str(orders_csv),
            }
        )
    if ocr_results:
        files.update(
            {
                "order_ocr_jsonl": str(order_ocr_jsonl),
                "order_ocr_csv": str(order_ocr_csv),
            }
        )
    if assets_dir.exists():
        files["assets_dir"] = str(assets_dir)
    media_count = sum(len(message.media) for message in cleaned)
    message_image_count = sum(
        item.role == "message_image"
        for message in cleaned
        for item in message.media
    )
    ocr_item_count = sum(
        len(result.items) for result in ocr_results if result.applicable
    )
    google_drive: dict[str, object] = {}
    if branch_config is not None:
        google_drive["branch_config"] = {
            "id": branch_config.resource.id,
            "name": branch_config.resource.name,
            "url": branch_config.resource.url,
            "mappings_loaded": len(branch_config.mappings),
        }
    drive_error: Exception | None = None
    if drive_publisher is not None:
        print("Đang lưu tin nhắn và hình ảnh lên Google Drive...")
        try:
            google_drive.update(
                drive_publisher.publish(
                    run_dir=run_dir,
                    group_name=settings.group_name,
                    target_date=target_date,
                    messages=cleaned,
                    decisions=decisions,
                    ocr_results=ocr_results,
                )
            )
        except Exception as exc:
            drive_error = exc
            google_drive["error"] = str(exc)

    warnings = list(artifacts.warnings)
    if drive_error is not None:
        warnings.append(f"Không lưu được Google Drive: {drive_error}")
    manifest = CrawlManifest(
        group_name=settings.group_name,
        target_date=target_date,
        started_at=started_at,
        finished_at=datetime.now(settings.timezone),
        browser_mode=settings.browser_mode,
        raw_message_count=len(artifacts.messages),
        clean_message_count=len(cleaned),
        classified_message_count=len(decisions),
        order_count=sum(item.is_order for item in decisions),
        media_count=media_count,
        message_image_count=message_image_count,
        ocr_image_count=len(ocr_results),
        ocr_item_count=ocr_item_count,
        files=files,
        google_drive=google_drive,
        warnings=warnings,
    )
    write_json(manifest_json, manifest)

    if drive_error is not None:
        raise RuntimeError(f"Không lưu được output lên Google Drive: {drive_error}")

    print(f"Hoàn tất: {run_dir}")
    if with_ai:
        print(f"Đơn hàng AI nhận diện: {manifest.order_count}; CSV: {orders_csv}")
        if ocr_results:
            print(
                f"Ảnh OCR phiếu đặt hàng: {manifest.ocr_image_count}; "
                f"mặt hàng trích xuất: {manifest.ocr_item_count}"
            )
    sheet = google_drive.get("sheet")
    if isinstance(sheet, dict):
        print(f"Google Sheet: {sheet.get('url', '')}")
    image_folder = google_drive.get("image_folder")
    if isinstance(image_folder, dict):
        print(f"Thư mục ảnh Google Drive: {image_folder.get('url', '')}")
    ocr_workbook = google_drive.get("ocr_workbook")
    if isinstance(ocr_workbook, dict):
        print(f"File Excel OCR đơn đặt hàng: {ocr_workbook.get('url', '')}")
    branch_config_output = google_drive.get("branch_config")
    if isinstance(branch_config_output, dict):
        print(f"Cấu hình chi nhánh: {branch_config_output.get('url', '')}")
    for warning in warnings:
        print(f"Cảnh báo: {warning}")
    print("Hoàn tất và đã cập nhật output Google Drive.")
    return 0


def settings_selectors(selectors: dict[str, list[str]], key: str) -> list[str]:
    values = selectors.get(key, [])
    if not values:
        raise ValueError(f"Thiếu nhóm selector bắt buộc: {key}")
    return values


def _run_dir_date(output_dir: Path) -> date | None:
    try:
        return date.fromisoformat(output_dir.parent.name)
    except ValueError:
        return None


def _read_clean_messages(path: Path) -> list[CleanMessage]:
    if not path.exists():
        raise ValueError(f"Không tìm thấy file: {path}")
    messages: list[CleanMessage] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            messages.append(CleanMessage.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"JSONL lỗi tại dòng {line_number}: {exc}") from exc
    return messages


def _run_classify(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if not settings.gemini_api_key:
        raise ValueError("Thiếu GEMINI_API_KEY trong .env.")
    input_path = Path(args.input).resolve()
    messages = _read_clean_messages(input_path)
    branch_mappings: dict[str, str] = {}
    if settings.google_drive_upload_enabled:
        publisher = GoogleDriveOutputPublisher.from_default_credentials(
            settings.google_drive_parent_folder_id,
            project_dir=settings.project_dir,
        )
        publisher.verify_destination()
        branch_mappings = publisher.ensure_branch_config().mappings
    classifier = GeminiOrderClassifier(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        batch_size=settings.gemini_batch_size,
        cache_dir=settings.project_dir / ".cache" / "gemini",
        media_base_dir=input_path.parent,
        branch_mappings=branch_mappings,
    )
    decisions = classifier.classify(messages)
    output_dir = input_path.parent
    write_jsonl(output_dir / "classifications.jsonl", decisions)
    run_date = _run_dir_date(output_dir)
    orders_name = f"{run_date.strftime('%d-%m-%Y')}.csv" if run_date else "orders.csv"
    write_orders_csv(
        output_dir / orders_name,
        messages,
        [item for item in decisions if item.is_order],
    )
    print(f"Đã phân loại {len(messages)} tin; nhận diện {sum(item.is_order for item in decisions)} đơn.")
    return 0


def _run_branch_config(args: argparse.Namespace) -> int:
    settings = _settings(args)
    publisher = GoogleDriveOutputPublisher.from_default_credentials(
        settings.google_drive_parent_folder_id,
        project_dir=settings.project_dir,
    )
    destination = publisher.verify_destination()
    config = publisher.ensure_branch_config()
    print(f"Google Drive output: {destination.name}")
    print(f"Google Sheet cấu hình: {config.resource.url}")
    for alias, branch_name in config.mappings.items():
        print(f"- {alias} = {branch_name}")
    return 0


def _run_ui(args: argparse.Namespace) -> int:
    from .webapp import serve_ui

    settings = _settings(args)
    return serve_ui(
        settings,
        port=args.port,
        open_browser=not args.no_open,
    )


def _run_auth_session_command(args: argparse.Namespace) -> int:
    from .auth_session import run_auth_session

    settings = _settings(args)
    return run_auth_session(
        settings,
        status_file=Path(args.status_file).resolve(),
        complete_signal=Path(args.complete_signal).resolve(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zalo-order-crawler",
        description="Crawl tin nhắn Zalo Web theo ngày và nhận diện đơn hàng.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("run", "Crawl, làm sạch và phân loại bằng Gemini."),
        ("crawl", "Chỉ crawl và làm sạch, không gọi Gemini."),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        sub.add_argument("--group", help="Tên chính xác của nhóm Zalo.")
        sub.add_argument("--date", default="today", help="today, YYYY-MM-DD hoặc DD/MM/YYYY.")
        sub.add_argument(
            "--browser-mode",
            choices=("persistent", "cdp"),
            help="Ghi đè BROWSER_MODE trong .env.",
        )

    classify = subparsers.add_parser(
        "classify", help="Chạy lại Gemini từ một file clean_messages.jsonl."
    )
    classify.add_argument("--input", required=True, help="Đường dẫn clean_messages.jsonl.")

    subparsers.add_parser(
        "branch-config",
        help="Tạo hoặc kiểm tra Google Sheet cấu hình chi nhánh.",
    )

    ui = subparsers.add_parser("ui", help="Mở giao diện web cục bộ.")
    ui.add_argument("--port", type=int, default=8765, help="Cổng local, mặc định 8765.")
    ui.add_argument(
        "--no-open",
        action="store_true",
        help="Không tự mở giao diện trong trình duyệt.",
    )

    auth = subparsers.add_parser("_auth-session", help="Tác vụ nội bộ của giao diện UI.")
    auth.add_argument("--status-file", required=True)
    auth.add_argument("--complete-signal", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run_crawl(args, with_ai=True)
        if args.command == "crawl":
            return _run_crawl(args, with_ai=False)
        if args.command == "classify":
            return _run_classify(args)
        if args.command == "branch-config":
            return _run_branch_config(args)
        if args.command == "ui":
            return _run_ui(args)
        if args.command == "_auth-session":
            return _run_auth_session_command(args)
        parser.error(f"Lệnh không hỗ trợ: {args.command}")
    except KeyboardInterrupt:
        print("Đã dừng theo yêu cầu người dùng.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1
    return 0
