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
from .models import CleanMessage, CrawlManifest
from .parser import clean_messages
from .storage import (
    safe_slug,
    write_html_fragment,
    write_json,
    write_jsonl,
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

    raw_jsonl = run_dir / "raw_messages.jsonl"
    raw_html = run_dir / "raw_messages.html"
    view_html = run_dir / "message_view.html"
    styles_json = run_dir / "stylesheets.json"
    clean_jsonl = run_dir / "clean_messages.jsonl"
    classifications_jsonl = run_dir / "classifications.jsonl"
    orders_csv = run_dir / "orders.csv"
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
        )
        decisions = classifier.classify(cleaned)
        write_jsonl(classifications_jsonl, decisions)
        write_orders_csv(orders_csv, cleaned, [item for item in decisions if item.is_order])

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
    if assets_dir.exists():
        files["assets_dir"] = str(assets_dir)
    media_count = sum(len(message.media) for message in cleaned)
    message_image_count = sum(
        item.role == "message_image"
        for message in cleaned
        for item in message.media
    )
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
        files=files,
        warnings=artifacts.warnings,
    )
    write_json(manifest_json, manifest)

    print(f"Hoàn tất: {run_dir}")
    if with_ai:
        print(f"Đơn hàng AI nhận diện: {manifest.order_count}; CSV: {orders_csv}")
    for warning in artifacts.warnings:
        print(f"Cảnh báo: {warning}")
    return 0


def settings_selectors(selectors: dict[str, list[str]], key: str) -> list[str]:
    values = selectors.get(key, [])
    if not values:
        raise ValueError(f"Thiếu nhóm selector bắt buộc: {key}")
    return values


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
    classifier = GeminiOrderClassifier(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        batch_size=settings.gemini_batch_size,
        cache_dir=settings.project_dir / ".cache" / "gemini",
        media_base_dir=input_path.parent,
    )
    decisions = classifier.classify(messages)
    output_dir = input_path.parent
    write_jsonl(output_dir / "classifications.jsonl", decisions)
    write_orders_csv(
        output_dir / "orders.csv",
        messages,
        [item for item in decisions if item.is_order],
    )
    print(f"Đã phân loại {len(messages)} tin; nhận diện {sum(item.is_order for item in decisions)} đơn.")
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
