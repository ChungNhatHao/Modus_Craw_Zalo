from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .config import Settings, load_selectors


def write_runtime_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        **payload,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_auth_session(
    settings: Settings,
    *,
    status_file: Path,
    complete_signal: Path,
    session_timeout_seconds: int = 1_800,
) -> int:
    selectors = load_selectors(settings.selectors_file)
    complete_signal.parent.mkdir(parents=True, exist_ok=True)
    complete_signal.unlink(missing_ok=True)
    write_runtime_status(
        status_file,
        {
            "state": "starting",
            "message": "Đang mở Zalo Web...",
        },
    )

    try:
        with BrowserSession(settings) as browser:
            assert browser.page is not None
            write_runtime_status(
                status_file,
                {
                    "state": "waiting_login",
                    "message": "Hãy đăng nhập Zalo trong cửa sổ vừa mở.",
                },
            )
            browser.wait_until_logged_in(selectors.get("ready", []), timeout_seconds=300)
            write_runtime_status(
                status_file,
                {
                    "state": "ready",
                    "message": (
                        "Zalo đã đăng nhập. Hãy tự đồng bộ tin nhắn cần thiết, sau đó "
                        "quay lại giao diện và bấm xác nhận hoàn tất."
                    ),
                },
            )

            deadline = time.monotonic() + session_timeout_seconds
            while time.monotonic() < deadline:
                if complete_signal.exists():
                    write_runtime_status(
                        status_file,
                        {
                            "state": "closing",
                            "message": "Đang lưu phiên đăng nhập và đóng cửa sổ Zalo...",
                        },
                    )
                    browser.page.wait_for_timeout(500)
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    f"Phiên xác thực đã hết hạn sau {session_timeout_seconds // 60} phút."
                )

        write_runtime_status(
            status_file,
            {
                "state": "completed",
                "message": "Đã lưu phiên đăng nhập và đồng bộ.",
            },
        )
        return 0
    except KeyboardInterrupt:
        write_runtime_status(
            status_file,
            {"state": "cancelled", "message": "Phiên xác thực đã bị hủy."},
        )
        return 130
    except Exception as exc:
        write_runtime_status(
            status_file,
            {"state": "error", "message": str(exc)},
        )
        return 1
    finally:
        complete_signal.unlink(missing_ok=True)

