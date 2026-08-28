from __future__ import annotations

import base64
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page

from .config import Settings
from .models import MediaAsset, RawMessage


MAX_MEDIA_BYTES = 20 * 1024 * 1024
_IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass
class CrawlArtifacts:
    messages: list[RawMessage]
    page_html: str
    stylesheets: list[dict[str, str]]
    warnings: list[str] = field(default_factory=list)


def _fold(value: str) -> str:
    normalised = unicodedata.normalize("NFD", value.lower())
    return "".join(char for char in normalised if unicodedata.category(char) != "Mn").strip()


def _first_visible(page: Page, selectors: list[str], *, timeout: int = 700) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 30)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible(timeout=timeout):
                    return candidate
            except Exception:
                continue
    return None


def _click_first_visible(page: Page, selectors: list[str], label: str) -> Locator:
    locator = _first_visible(page, selectors)
    if locator is None:
        raise RuntimeError(f"Không tìm thấy {label}; cần cập nhật selector Zalo Web.")
    locator.click()
    return locator


class ZaloCrawler:
    def __init__(self, page: Page, settings: Settings, selectors: dict[str, list[str]]) -> None:
        self.page = page
        self.settings = settings
        self.selectors = selectors
        self.warnings: list[str] = []
        self._media_replacements: dict[str, str] = {}
        self._media_paths_by_digest: dict[str, str] = {}

    def open_group(self, group_name: str) -> None:
        if not group_name:
            raise ValueError("Thiếu tên nhóm. Dùng --group hoặc ZALO_GROUP_NAME trong .env.")

        search = _first_visible(self.page, self.selectors.get("global_search_input", []))
        if search is None:
            raise RuntimeError("Không tìm thấy ô tìm nhóm Zalo.")
        search.click()
        search.fill(group_name)
        self.page.wait_for_timeout(1_200)

        clicked = False
        for selector in self.selectors.get("conversation_item", []):
            candidates = self.page.locator(selector).filter(has_text=group_name)
            try:
                count = min(candidates.count(), 20)
            except Exception:
                continue
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if candidate.is_visible(timeout=400):
                        candidate.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break

        if not clicked:
            exact_matches = self.page.get_by_text(group_name, exact=True)
            try:
                count = min(exact_matches.count(), 20)
            except Exception:
                count = 0
            for index in range(count):
                match = exact_matches.nth(index)
                try:
                    if match.is_visible(timeout=400):
                        match.click()
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            raise RuntimeError(f"Không tìm thấy nhóm Zalo có tên {group_name!r}.")

        self.page.wait_for_timeout(1_500)
        if not self._header_matches(group_name):
            raise RuntimeError(
                f"Đã bấm kết quả nhưng không xác nhận được tiêu đề nhóm {group_name!r}. "
                "Tool dừng để tránh crawl nhầm nhóm."
            )

    def _header_matches(self, group_name: str) -> bool:
        expected = _fold(group_name)
        for selector in self.selectors.get("conversation_header", []):
            locator = self.page.locator(selector)
            try:
                count = min(locator.count(), 10)
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible(timeout=300):
                        continue
                    values = [candidate.inner_text()]
                    for attribute in ("data-trailer", "placeholder", "aria-label", "title"):
                        values.append(candidate.get_attribute(attribute) or "")
                    if any(expected in _fold(value) for value in values if value):
                        return True
                except Exception:
                    continue
        return False

    def ensure_message_history(
        self,
        *,
        allow_sync: bool = False,
        timeout_seconds: int = 600,
    ) -> bool:
        sync_in_progress = _first_visible(
            self.page,
            self.selectors.get("sync_in_progress", []),
        )
        sync_button = _first_visible(self.page, self.selectors.get("sync_button", []))
        if sync_button is None and sync_in_progress is None:
            return False

        if sync_in_progress is None and not allow_sync:
            raise RuntimeError(
                "Zalo đang yêu cầu 'Đồng bộ ngay'. Thao tác này có thể tải lịch sử "
                "rộng hơn ngày cần crawl nên tool chưa bấm. Chỉ đặt "
                "ALLOW_ZALO_HISTORY_SYNC=true sau khi chủ tài khoản đồng ý."
            )

        if sync_in_progress is not None:
            print("Zalo đang đồng bộ lịch sử; tool sẽ chờ đồng bộ hoàn tất...")
        else:
            print(
                "Zalo yêu cầu đồng bộ lịch sử. Đang bấm 'Đồng bộ ngay'; "
                "hãy xác nhận trên điện thoại/cửa sổ Zalo nếu được hỏi..."
            )
            assert sync_button is not None
            sync_button.click()

        started = time.monotonic()
        observed_progress = sync_in_progress is not None
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current_progress = _first_visible(
                self.page,
                self.selectors.get("sync_in_progress", []),
                timeout=300,
            )
            current_button = _first_visible(
                self.page,
                self.selectors.get("sync_button", []),
                timeout=300,
            )
            if current_progress is not None:
                observed_progress = True
            elapsed = time.monotonic() - started
            if current_progress is None and current_button is None and elapsed >= 5:
                # Kiểm tra lại sau một nhịp để không nhầm chuyển trang ngắn với hoàn tất.
                self.page.wait_for_timeout(2_000)
                confirm_progress = _first_visible(
                    self.page,
                    self.selectors.get("sync_in_progress", []),
                    timeout=300,
                )
                confirm_button = _first_visible(
                    self.page,
                    self.selectors.get("sync_button", []),
                    timeout=300,
                )
                if confirm_progress is None and confirm_button is None:
                    return True
            self.page.wait_for_timeout(1_000)
        raise RuntimeError(
            f"Zalo chưa đồng bộ xong sau {timeout_seconds} giây"
            + (" dù đã thấy trạng thái đang đồng bộ. " if observed_progress else ". ")
            + "Hãy hoàn tất xác nhận đồng bộ rồi chạy lại."
        )

    def search_messages_for_date(self, target_date: date) -> bool:
        _click_first_visible(
            self.page,
            self.selectors.get("message_search_button", []),
            "nút Tìm kiếm tin nhắn",
        )
        self.page.wait_for_timeout(700)

        _click_first_visible(
            self.page,
            self.selectors.get("date_filter_button", []),
            "bộ lọc Ngày gửi",
        )
        self.page.wait_for_timeout(500)

        if not self._choose_date(target_date):
            raise RuntimeError(
                f"Không chọn được ngày {target_date.strftime('%d/%m/%Y')} trong bộ lọc."
            )

        apply_button = _first_visible(self.page, self.selectors.get("apply_filter_button", []))
        if apply_button is not None:
            apply_button.click()
        self.page.wait_for_timeout(1_200)

        empty_state = _first_visible(
            self.page,
            self.selectors.get("search_empty_state", []),
            timeout=500,
        )
        if empty_state is not None:
            return False

        result = _first_visible(
            self.page,
            self.selectors.get("search_result_item", []),
            timeout=1_000,
        )
        if result is None:
            raise RuntimeError(
                f"Không có kết quả tìm kiếm tin nhắn cho ngày {target_date.strftime('%d/%m/%Y')}."
            )
        # Kết quả tìm kiếm thường xếp mới nhất trước. Bấm kết quả đầu rồi cuộn ngược
        # tới ranh giới đầu ngày để không bỏ sót các tin cũ hơn.
        result.click()
        self.page.wait_for_timeout(1_000)
        if not self._header_matches(self.settings.group_name):
            raise RuntimeError(
                "Kết quả tìm kiếm đã rời khỏi nhóm mục tiêu; tool dừng để không "
                "crawl nhầm hội thoại."
            )
        return True

    def empty_day_artifacts(self, target_date: date) -> CrawlArtifacts:
        message_view = _first_visible(self.page, self.selectors.get("message_view", []))
        if message_view is not None:
            try:
                page_html = message_view.evaluate("el => el.outerHTML")
            except Exception:
                page_html = '<div data-crawler-empty="true"></div>'
        else:
            page_html = '<div data-crawler-empty="true"></div>'
        return CrawlArtifacts(
            messages=[],
            page_html=page_html,
            stylesheets=self._stylesheet_snapshot(),
            warnings=[
                f"Không có kết quả tin nhắn trong ngày {target_date.strftime('%d/%m/%Y')}."
            ],
        )

    def _choose_date(self, target_date: date) -> bool:
        date_triggers = self.page.locator(
            ", ".join(self.selectors.get("date_picker_trigger", []))
        ) if self.selectors.get("date_picker_trigger", []) else None
        trigger_count = 0
        if date_triggers is not None:
            try:
                trigger_count = date_triggers.count()
            except Exception:
                trigger_count = 0
        if trigger_count:
            first_trigger = date_triggers.first
            if first_trigger.is_visible(timeout=500):
                first_trigger.click()
                self.page.wait_for_timeout(300)
                # Zalo tự chuyển từ ngày bắt đầu sang ngày kết thúc sau lần chọn đầu.
                for _ in range(2):
                    if not self._select_zalo_calendar_day(target_date):
                        return False
                    self.page.wait_for_timeout(300)
                return True

        inputs: list[Locator] = []
        for selector in self.selectors.get("date_input", []):
            locator = self.page.locator(selector)
            try:
                count = min(locator.count(), 4)
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible(timeout=300):
                        inputs.append(candidate)
                except Exception:
                    continue
            if inputs:
                break

        if inputs:
            for item in inputs:
                input_type = (item.get_attribute("type") or "").lower()
                value = target_date.isoformat() if input_type == "date" else target_date.strftime("%d/%m/%Y")
                item.fill(value)
                item.dispatch_event("input")
                item.dispatch_event("change")
            return True

        today = datetime.now(self.settings.timezone).date()
        if target_date == today:
            today_buttons = self.page.locator(
                "button:has-text('Hôm nay'), [role='button']:has-text('Hôm nay'), "
                "[role='gridcell']:has-text('Hôm nay')"
            )
            try:
                count = min(today_buttons.count(), 10)
            except Exception:
                count = 0
            for index in range(count):
                candidate = today_buttons.nth(index)
                try:
                    if candidate.is_visible(timeout=300):
                        candidate.click()
                        return True
                except Exception:
                    continue

        # Datepicker của các phiên bản Zalo thường hiển thị tháng hiện tại. Vì luồng
        # mặc định lấy hôm nay, chọn ngày trong dialog/popover đang mở là đủ.
        day_text = str(target_date.day)
        calendar_roots = self.page.locator(
            "[role='dialog']:visible, [class*='datepicker']:visible, [class*='calendar']:visible"
        )
        try:
            root_count = min(calendar_roots.count(), 10)
        except Exception:
            root_count = 0
        for root_index in range(root_count):
            root = calendar_roots.nth(root_index)
            candidates = root.get_by_text(day_text, exact=True)
            try:
                count = min(candidates.count(), 15)
            except Exception:
                continue
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if candidate.is_visible(timeout=300):
                        candidate.click()
                        return True
                except Exception:
                    continue
        return False

    def _select_zalo_calendar_day(self, target_date: date) -> bool:
        calendar = self.page.locator("#calendar-v3")
        try:
            if not calendar.is_visible(timeout=1_000):
                return False
        except Exception:
            return False

        today = datetime.now(self.settings.timezone).date()
        if target_date == today:
            today_cell = calendar.locator(".cal-item.is-today").first
            try:
                if today_cell.is_visible(timeout=500):
                    today_cell.click()
                    return True
            except Exception:
                pass

        cells = calendar.locator(".cal-item:not(.out-month)")
        try:
            count = min(cells.count(), 42)
        except Exception:
            return False
        expected = str(target_date.day)
        for index in range(count):
            cell = cells.nth(index)
            try:
                if cell.is_visible(timeout=200) and cell.inner_text().strip() == expected:
                    cell.click()
                    return True
            except Exception:
                continue
        return False

    def crawl_day(self, target_date: date, *, assets_dir: Path | None = None) -> CrawlArtifacts:
        container = self._find_scroll_container()
        found_boundary, saw_target = self._scroll_up_to_day_start(container, target_date)
        if not found_boundary:
            self.warnings.append(
                "Không nhận diện chắc chắn ranh giới đầu ngày; dữ liệu được lấy theo "
                "phạm vi Zalo đã tải sau khi lọc ngày."
            )

        messages = self._collect_downward(
            container,
            target_date,
            start_collecting=(not found_boundary and not saw_target),
            assets_dir=assets_dir,
        )
        if not messages:
            raise RuntimeError("Không lấy được node tin nhắn nào từ khung trò chuyện.")

        # Dựng snapshot chỉ từ các tin thuộc ngày mục tiêu. DOM virtualized đang hiển
        # thị có thể lẫn vài tin của ngày kế tiếp và các blob URL chưa thuộc phạm vi.
        page_html = (
            f'<div id="crawler-message-view" data-target-date="{target_date.isoformat()}">'
            + "\n".join(message.html for message in messages)
            + "</div>"
        )
        stylesheets = self._stylesheet_snapshot()
        return CrawlArtifacts(
            messages=messages,
            page_html=page_html,
            stylesheets=stylesheets,
            warnings=self.warnings.copy(),
        )

    def _find_scroll_container(self) -> Locator:
        best: tuple[float, Locator] | None = None
        for selector in self.selectors.get("scroll_container", []):
            locator = self.page.locator(selector)
            try:
                count = min(locator.count(), 30)
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    metrics = candidate.evaluate(
                        "el => ({visible: !!(el.offsetWidth || el.offsetHeight), "
                        "client: el.clientHeight, scroll: el.scrollHeight})"
                    )
                except Exception:
                    continue
                overflow = float(metrics["scroll"]) - float(metrics["client"])
                if metrics["visible"] and metrics["client"] > 100 and overflow > 20:
                    if best is None or overflow > best[0]:
                        best = (overflow, candidate)
        if best is None:
            raise RuntimeError("Không xác định được thanh cuộn chứa tin nhắn.")
        return best[1]

    def _scroll_up_to_day_start(self, container: Locator, target_date: date) -> tuple[bool, bool]:
        saw_target = False
        stable_count = 0
        previous_state: tuple[Any, ...] | None = None

        for _ in range(self.settings.max_scrolls):
            markers = self._date_markers(container)
            relations = [self._marker_relation(item["text"], target_date) for item in markers]
            if 0 in relations:
                saw_target = True
            if saw_target and -1 in relations:
                return True, True

            state_before = self._scroll_state(container)
            self._scroll(container, direction=-1)
            self.page.wait_for_timeout(self.settings.scroll_pause_ms)
            state_after = self._scroll_state(container)

            current_state = (*state_after, self._visible_message_signature(container))
            if current_state == previous_state or state_after == state_before:
                stable_count += 1
            else:
                stable_count = 0
            previous_state = current_state
            if stable_count >= 4:
                break
        return False, saw_target

    def _collect_downward(
        self,
        container: Locator,
        target_date: date,
        *,
        start_collecting: bool,
        assets_dir: Path | None,
    ) -> list[RawMessage]:
        records: list[RawMessage] = []
        fingerprints: set[str] = set()
        collecting = start_collecting
        stable_count = 0
        previous_state: tuple[Any, ...] | None = None

        for _ in range(self.settings.max_scrolls):
            markers = self._date_markers(container)
            target_markers = [item for item in markers if self._marker_relation(item["text"], target_date) == 0]
            future_markers = [item for item in markers if self._marker_relation(item["text"], target_date) == 1]
            min_top: float | None = None
            if target_markers:
                collecting = True
                min_top = min(float(item["top"]) for item in target_markers)

            if collecting:
                for node in self._visible_message_nodes(container):
                    if min_top is not None and float(node["top"]) <= min_top:
                        continue
                    fingerprint = self._fingerprint(node)
                    if fingerprint in fingerprints:
                        continue
                    fingerprints.add(fingerprint)
                    media = self._save_node_media(node, fingerprint, assets_dir)
                    records.append(
                        RawMessage(
                            message_id=fingerprint,
                            sequence=len(records),
                            html=self._rewrite_media_sources(node["html"]),
                            text_hint=node["text"],
                            date_label=target_date.isoformat(),
                            media=media,
                            captured_at=datetime.now(self.settings.timezone),
                        )
                    )

            if collecting and future_markers:
                break

            state_before = self._scroll_state(container)
            self._scroll(container, direction=1)
            self.page.wait_for_timeout(self.settings.scroll_pause_ms)
            state_after = self._scroll_state(container)
            current_state = (*state_after, self._visible_message_signature(container))
            if current_state == previous_state or state_after == state_before:
                stable_count += 1
            else:
                stable_count = 0
            previous_state = current_state
            if stable_count >= 4:
                break

        return records

    def _save_node_media(
        self,
        node: dict[str, Any],
        message_id: str,
        assets_dir: Path | None,
    ) -> list[MediaAsset]:
        if assets_dir is None:
            return []

        result: list[MediaAsset] = []
        candidates = node.get("media") or []
        for index, candidate in enumerate(candidates):
            source_url = str(candidate.get("src") or "").strip()
            role = str(candidate.get("role") or "message_image").strip()
            if not source_url:
                continue
            try:
                payload, mime_type = self._download_media(source_url)
            except Exception as exc:
                payload = self._screenshot_media_node(node, candidate)
                mime_type = "image/png" if payload else ""
                if not payload:
                    if role == "message_image":
                        self.warnings.append(
                            f"Không lưu được ảnh của tin {message_id}: {exc}"
                        )
                    continue

            digest = hashlib.sha256(payload).hexdigest()
            relative_path = self._media_paths_by_digest.get(digest)
            if relative_path is None:
                extension = _IMAGE_EXTENSIONS.get(mime_type, ".bin")
                safe_id = re.sub(r"[^0-9A-Za-z_-]+", "-", message_id).strip("-")
                filename = f"{safe_id[:80] or 'message'}-{index + 1}-{digest[:10]}{extension}"
                assets_dir.mkdir(parents=True, exist_ok=True)
                destination = assets_dir / filename
                destination.write_bytes(payload)
                relative_path = f"{assets_dir.name}/{filename}"
                self._media_paths_by_digest[digest] = relative_path

            self._media_replacements[source_url] = relative_path
            html_source = str(candidate.get("htmlSrc") or "").strip()
            if html_source:
                self._media_replacements[html_source] = relative_path
            result.append(
                MediaAsset(
                    path=relative_path,
                    mime_type=mime_type or "application/octet-stream",
                    role=role,
                    size_bytes=len(payload),
                    sha256=digest,
                )
            )
        return result

    def _download_media(self, source_url: str) -> tuple[bytes, str]:
        value = self.page.evaluate(
            r"""
            async ({url, maxBytes}) => {
              const response = await fetch(url);
              if (!response.ok && !url.startsWith('blob:') && !url.startsWith('data:')) {
                throw new Error(`HTTP ${response.status}`);
              }
              const blob = await response.blob();
              if (blob.size > maxBytes) throw new Error(`Ảnh vượt quá ${maxBytes} byte`);
              const bytes = new Uint8Array(await blob.arrayBuffer());
              let binary = '';
              const chunkSize = 0x8000;
              for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
              }
              return {base64: btoa(binary), mimeType: blob.type || ''};
            }
            """,
            {"url": source_url, "maxBytes": MAX_MEDIA_BYTES},
        )
        payload = base64.b64decode(value["base64"], validate=True)
        if not payload:
            raise ValueError("Ảnh tải về rỗng.")
        if len(payload) > MAX_MEDIA_BYTES:
            raise ValueError(f"Ảnh vượt quá {MAX_MEDIA_BYTES} byte.")
        mime_type = str(value.get("mimeType") or "").split(";", 1)[0].strip().lower()
        sniffed_mime = self._sniff_image_mime(payload)
        if sniffed_mime:
            mime_type = sniffed_mime
        elif mime_type == "image/jpg":
            mime_type = "image/jpeg"
        if not mime_type:
            raise ValueError("Không xác định được định dạng ảnh.")
        return payload, mime_type

    def _screenshot_media_node(
        self, node: dict[str, Any], candidate: dict[str, Any]
    ) -> bytes | None:
        source_id = str(node.get("sourceId") or "").strip()
        dom_index = candidate.get("domIndex")
        if not source_id or not isinstance(dom_index, int):
            return None
        try:
            image = self.page.locator(f'[id="{source_id}"]').locator("img").nth(dom_index)
            if not image.is_visible(timeout=500):
                return None
            return image.screenshot(type="png")
        except Exception:
            return None

    @staticmethod
    def _sniff_image_mime(payload: bytes) -> str:
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _rewrite_media_sources(self, html: str) -> str:
        for source_url, relative_path in self._media_replacements.items():
            html = html.replace(source_url, relative_path)
        return html

    @staticmethod
    def _fingerprint(node: dict[str, Any]) -> str:
        source_id = str(node.get("sourceId") or "").strip()
        if source_id:
            return source_id
        payload = f"{node.get('html', '')}\n{node.get('text', '')}"
        return "sha256-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _visible_message_nodes(self, container: Locator) -> list[dict[str, Any]]:
        return container.evaluate(
            r"""
            (root, selectors) => {
              const rootRect = root.getBoundingClientRect();
              for (const selector of selectors) {
                let nodes;
                try { nodes = [...root.querySelectorAll(selector)]; } catch (_) { continue; }
                const visible = nodes.filter((node) => {
                  const rect = node.getBoundingClientRect();
                  return rect.height > 0 && rect.bottom >= rootRect.top && rect.top <= rootRect.bottom;
                });
                if (!visible.length) continue;
                return visible.map((node) => {
                  const rect = node.getBoundingClientRect();
                  const media = [...node.querySelectorAll('img')].map((image, domIndex) => {
                    const htmlSrc = image.getAttribute('src') || '';
                    const src = image.currentSrc || htmlSrc;
                    if (!src) return null;
                    let role = '';
                    if (image.closest(
                      "[data-id*='Msg_Photo'], [data-id*='Msg_Image'], " +
                      "[class*='chatImageMessage'], [class*='photo-message'], [class*='img-msg-v2']"
                    )) {
                      role = 'message_image';
                    } else if (image.closest(
                      "[data-id*='Msg_Sticker'], [class*='sticker-message']"
                    )) {
                      role = 'sticker';
                    } else if (image.closest(
                      ".link-message__thumbnail, [class*='preview-link']"
                    )) {
                      role = 'link_thumbnail';
                    }
                    return role ? {src, htmlSrc, role, domIndex} : null;
                  }).filter(Boolean);
                  return {
                    html: node.outerHTML,
                    text: (node.innerText || node.textContent || '').trim(),
                    top: rect.top,
                    media,
                    sourceId: node.getAttribute('data-message-id') ||
                      node.getAttribute('data-msg-id') || node.getAttribute('data-id') || node.id || ''
                  };
                }).sort((a, b) => a.top - b.top);
              }
              return [];
            }
            """,
            self.selectors.get("message", []),
        )

    def _date_markers(self, container: Locator) -> list[dict[str, Any]]:
        return container.evaluate(
            r"""
            (root, selectors) => {
              const rootRect = root.getBoundingClientRect();
              const found = [];
              const seen = new Set();
              const add = (node) => {
                const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                const rect = node.getBoundingClientRect();
                if (!text || text.length > 60 || rect.height <= 0 ||
                    rect.bottom < rootRect.top || rect.top > rootRect.bottom) return;
                const key = `${text}|${Math.round(rect.top)}`;
                if (!seen.has(key)) {
                  seen.add(key);
                  found.push({text, top: rect.top});
                }
              };
              for (const selector of selectors) {
                try { root.querySelectorAll(selector).forEach(add); } catch (_) {}
              }
              if (!found.length) {
                root.querySelectorAll('div, span').forEach((node) => {
                  const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                  if (/^(Hôm nay|Hôm qua|Today|Yesterday)$/i.test(text) ||
                      /^\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?$/.test(text)) add(node);
                });
              }
              return found.sort((a, b) => a.top - b.top);
            }
            """,
            self.selectors.get("date_divider", []),
        )

    def _marker_relation(self, value: str, target_date: date) -> int | None:
        folded = _fold(value)
        today = datetime.now(self.settings.timezone).date()
        if folded in {"hom nay", "today"}:
            marker_date = today
        elif folded in {"hom qua", "yesterday"}:
            marker_date = today - timedelta(days=1)
        else:
            match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", folded)
            if not match:
                match = re.search(r"\b(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam\s+(\d{4}))?\b", folded)
            if not match:
                return None
            day, month, year_raw = match.groups()
            year = int(year_raw) if year_raw else target_date.year
            if year < 100:
                year += 2000
            try:
                marker_date = date(year, int(month), int(day))
            except ValueError:
                return None
        return (marker_date > target_date) - (marker_date < target_date)

    @staticmethod
    def _scroll_state(container: Locator) -> tuple[int, int, int]:
        values = container.evaluate(
            "el => [Math.round(el.scrollTop), Math.round(el.scrollHeight), Math.round(el.clientHeight)]"
        )
        return int(values[0]), int(values[1]), int(values[2])

    @staticmethod
    def _scroll(container: Locator, *, direction: int) -> None:
        container.evaluate(
            """
            (el, direction) => {
              const amount = Math.max(450, Math.floor(el.clientHeight * 0.78)) * direction;
              el.scrollBy({top: amount, left: 0, behavior: 'instant'});
              el.dispatchEvent(new WheelEvent('wheel', {deltaY: amount, bubbles: true}));
            }
            """,
            direction,
        )

    def _visible_message_signature(self, container: Locator) -> str:
        nodes = self._visible_message_nodes(container)
        if not nodes:
            return ""
        first = nodes[0]
        last = nodes[-1]
        value = f"{first.get('sourceId')}|{first.get('text')}|{last.get('sourceId')}|{last.get('text')}"
        return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]

    def _stylesheet_snapshot(self) -> list[dict[str, str]]:
        try:
            return self.page.evaluate(
                r"""
                () => [...document.styleSheets].map((sheet) => {
                  let cssText = '';
                  try { cssText = [...sheet.cssRules].map((rule) => rule.cssText).join('\n'); }
                  catch (_) { cssText = ''; }
                  return {href: sheet.href || '', cssText};
                })
                """
            )
        except Exception:
            self.warnings.append("Không đọc được metadata stylesheet của trang Zalo.")
            return []
