from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from .config import Settings


def ensure_headed_browser_environment(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Give Linux server deployments an actionable error before Chrome starts."""
    current_platform = platform or sys.platform
    values = environ if environ is not None else os.environ
    if not current_platform.startswith("linux"):
        return
    if values.get("DISPLAY") or values.get("WAYLAND_DISPLAY"):
        return
    raise RuntimeError(
        "Server Linux không có DISPLAY để mở Chrome có giao diện. "
        "Hãy khởi động web tool bằng scripts/run-server-ui.sh "
        "(Xvfb + noVNC), hoặc cấu hình BROWSER_MODE=cdp để kết nối "
        "tới một Chrome đã chạy."
    )


class BrowserSession:
    """Mở hồ sơ Playwright riêng hoặc kết nối Chrome qua CDP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "BrowserSession":
        if self.settings.browser_mode == "persistent":
            ensure_headed_browser_environment()
        self._playwright = sync_playwright().start()
        chromium = self._playwright.chromium

        if self.settings.browser_mode == "cdp":
            try:
                self._browser = chromium.connect_over_cdp(
                    self.settings.cdp_url,
                    slow_mo=self.settings.slow_mo_ms,
                    timeout=30_000,
                )
            except Exception as exc:
                raise RuntimeError(
                    "Không kết nối được trình duyệt qua CDP. Hãy khởi động Chrome/Edge "
                    "với --remote-debugging-port=9222 rồi thử lại."
                ) from exc
            if not self._browser.contexts:
                raise RuntimeError("Trình duyệt CDP không có context mặc định.")
            self._context = self._browser.contexts[0]
        else:
            self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, object] = {
                "user_data_dir": str(self.settings.browser_profile_dir),
                "headless": False,
                "slow_mo": self.settings.slow_mo_ms,
                "viewport": None,
                "args": ["--start-maximized"],
            }
            if self.settings.browser_channel:
                kwargs["channel"] = self.settings.browser_channel
            self._context = chromium.launch_persistent_context(**kwargs)

        self.page = self._choose_zalo_page(self._context)
        self.page.set_default_timeout(12_000)
        return self

    @staticmethod
    def _choose_zalo_page(context: BrowserContext) -> Page:
        for page in context.pages:
            if "chat.zalo.me" in page.url:
                return page
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://chat.zalo.me/", wait_until="domcontentloaded", timeout=60_000)
        return page

    def wait_until_logged_in(
        self,
        ready_selectors: list[str],
        *,
        timeout_seconds: int = 180,
    ) -> None:
        if self.page is None:
            raise RuntimeError("BrowserSession chưa được mở.")
        print("Đang chờ Zalo Web sẵn sàng. Nếu được yêu cầu, hãy đăng nhập trong cửa sổ vừa mở...")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for selector in ready_selectors:
                try:
                    if self.page.locator(selector).first.is_visible(timeout=400):
                        return
                except Exception:
                    continue
            time.sleep(1)
        raise RuntimeError(
            f"Zalo Web chưa vào màn hình trò chuyện sau {timeout_seconds} giây. "
            "Hãy kiểm tra đăng nhập/đồng bộ tin nhắn rồi chạy lại."
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            # Không gọi browser.close() ở CDP vì có thể đóng trình duyệt người dùng.
            if self.settings.browser_mode == "persistent" and self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()


def save_diagnostics(page: Page, directory: Path, label: str) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(char if char.isalnum() or char in "-_" else "-" for char in label)
    screenshot_path = directory / f"{safe_label}.png"
    html_path = directory / f"{safe_label}.html"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path.write_text("Không chụp được ảnh màn hình.", encoding="utf-8")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        html_path.write_text("Không lấy được HTML.", encoding="utf-8")
    return screenshot_path, html_path
