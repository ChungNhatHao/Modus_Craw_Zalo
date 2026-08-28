from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _as_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} phải là số nguyên, nhận được: {raw!r}") from exc


def _as_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} phải là true/false, nhận được: {raw!r}")


@dataclass(frozen=True)
class Settings:
    project_dir: Path
    group_name: str
    browser_mode: str
    cdp_url: str
    browser_profile_dir: Path
    browser_channel: str | None
    slow_mo_ms: int
    gemini_api_key: str | None
    gemini_model: str
    gemini_batch_size: int
    timezone: ZoneInfo
    output_dir: Path
    selectors_file: Path
    max_scrolls: int
    scroll_pause_ms: int
    allow_zalo_history_sync: bool

    @classmethod
    def from_env(
        cls,
        project_dir: Path,
        *,
        group_name: str | None = None,
        browser_mode: str | None = None,
    ) -> "Settings":
        project_dir = project_dir.resolve()
        load_dotenv(project_dir / ".env")
        values = os.environ

        mode = (browser_mode or values.get("BROWSER_MODE", "persistent")).strip().lower()
        if mode not in {"persistent", "cdp"}:
            raise ValueError("BROWSER_MODE chỉ nhận 'persistent' hoặc 'cdp'.")

        timezone_name = values.get("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError(f"Múi giờ không hợp lệ: {timezone_name!r}") from exc

        def resolve_path(name: str, default: str) -> Path:
            path = Path(values.get(name, default).strip())
            return path if path.is_absolute() else project_dir / path

        configured_group = (group_name or values.get("ZALO_GROUP_NAME", "")).strip()
        # Ưu tiên Chrome hệ thống; đặt biến thành chuỗi rỗng để dùng Chromium
        # do `playwright install chromium` tải về.
        channel = values.get("BROWSER_CHANNEL", "chrome").strip() or None
        api_key = values.get("GEMINI_API_KEY", "").strip() or None

        return cls(
            project_dir=project_dir,
            group_name=configured_group,
            browser_mode=mode,
            cdp_url=values.get("CDP_URL", "http://127.0.0.1:9222").strip(),
            browser_profile_dir=resolve_path("BROWSER_PROFILE_DIR", ".browser-profile"),
            browser_channel=channel,
            slow_mo_ms=_as_int(values, "SLOW_MO_MS", 80),
            gemini_api_key=api_key,
            gemini_model=values.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip(),
            gemini_batch_size=_as_int(values, "GEMINI_BATCH_SIZE", 25),
            timezone=timezone,
            output_dir=resolve_path("OUTPUT_DIR", "output"),
            selectors_file=resolve_path("SELECTORS_FILE", "config/selectors.json"),
            max_scrolls=_as_int(values, "MAX_SCROLLS", 300),
            scroll_pause_ms=_as_int(values, "SCROLL_PAUSE_MS", 650),
            allow_zalo_history_sync=_as_bool(values, "ALLOW_ZALO_HISTORY_SYNC", False),
        )


def load_selectors(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Không tìm thấy file selector: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"File selector không phải JSON hợp lệ: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("File selector phải là một JSON object.")

    selectors: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            raise ValueError(f"Selector {key!r} phải là một danh sách.")
        cleaned = [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]
        selectors[key] = cleaned
    return selectors
