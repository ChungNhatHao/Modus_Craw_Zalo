from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .auth_session import write_runtime_status
from .config import Settings
from .storage import safe_slug


MAX_GROUPS = 50
MAX_REQUEST_BYTES = 1_000_000


def normalise_groups(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Danh sách nhóm phải là một mảng.")
    groups: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Mỗi tên nhóm phải là chuỗi.")
        name = " ".join(item.split()).strip()
        if not name or name in seen:
            continue
        if len(name) > 200:
            raise ValueError("Tên nhóm không được dài quá 200 ký tự.")
        seen.add(name)
        groups.append(name)
    if not groups:
        raise ValueError("Hãy nhập ít nhất một tên nhóm.")
    if len(groups) > MAX_GROUPS:
        raise ValueError(f"Mỗi lượt chỉ hỗ trợ tối đa {MAX_GROUPS} nhóm.")
    return groups


def validate_iso_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Ngày crawl không hợp lệ.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Ngày crawl phải có định dạng YYYY-MM-DD.") from exc


def _read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Thiếu file kết quả: {path.name}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} lỗi JSON tại dòng {line_number}.") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} dòng {line_number} không phải object.")
        values.append(value)
    return values


class AppManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtime_dir = settings.project_dir / ".runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.auth_status_file = self.runtime_dir / "auth-status.json"
        self.auth_signal_file = self.runtime_dir / "auth-complete.signal"
        self.auth_log_file = self.runtime_dir / "auth.log"
        self.crawl_status_file = self.runtime_dir / "crawl-status.json"
        self._lock = threading.RLock()
        self._auth_process: subprocess.Popen[str] | None = None
        self._crawl_process: subprocess.Popen[str] | None = None
        self._crawl_thread: threading.Thread | None = None

    def bootstrap(self) -> dict[str, Any]:
        return {
            "today": datetime.now(self.settings.timezone).date().isoformat(),
            "default_groups": [self.settings.group_name] if self.settings.group_name else [],
            "timezone": str(self.settings.timezone),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_auth_process()
            auth = _read_json(
                self.auth_status_file,
                {"state": "idle", "message": "Chưa mở phiên xác thực Zalo."},
            )
            crawl = _read_json(
                self.crawl_status_file,
                {"state": "idle", "message": "Chưa có lượt crawl."},
            )
            return {"auth": auth, "crawl": crawl}

    def start_auth(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_auth_process()
            if self._crawl_active():
                raise RuntimeError("Không thể xác thực khi một lượt crawl đang chạy.")
            if self._auth_process is not None and self._auth_process.poll() is None:
                return self.status()["auth"]

            self.auth_signal_file.unlink(missing_ok=True)
            write_runtime_status(
                self.auth_status_file,
                {"state": "starting", "message": "Đang khởi động cửa sổ Zalo..."},
            )
            command = [
                sys.executable,
                "-m",
                "zalo_order_crawler",
                "_auth-session",
                "--status-file",
                str(self.auth_status_file),
                "--complete-signal",
                str(self.auth_signal_file),
            ]
            log_handle = self.auth_log_file.open("a", encoding="utf-8")
            try:
                self._auth_process = subprocess.Popen(
                    command,
                    cwd=self.settings.project_dir,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                log_handle.close()
            return self.status()["auth"]

    def complete_auth(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_auth_process()
            if self._auth_process is None or self._auth_process.poll() is not None:
                raise RuntimeError("Không có phiên xác thực Zalo đang mở.")
            auth = _read_json(self.auth_status_file, {})
            if auth.get("state") != "ready":
                raise RuntimeError("Hãy chờ Zalo đăng nhập xong trước khi xác nhận.")
            self.auth_signal_file.touch()
            return {
                "state": "closing",
                "message": "Đã nhận xác nhận; đang lưu hồ sơ và đóng Zalo...",
            }

    def start_crawl(self, groups_value: Any, date_value: Any) -> dict[str, Any]:
        groups = normalise_groups(groups_value)
        target_date = validate_iso_date(date_value)
        with self._lock:
            self._refresh_auth_process()
            if self._auth_process is not None and self._auth_process.poll() is None:
                raise RuntimeError(
                    "Hãy bấm 'Đã đăng nhập & đồng bộ xong' và chờ cửa sổ Zalo đóng trước."
                )
            if self._crawl_active():
                raise RuntimeError("Một lượt crawl khác đang chạy.")
            initial = {
                "state": "queued",
                "message": "Đã thêm vào hàng đợi.",
                "target_date": target_date,
                "total": len(groups),
                "completed": 0,
                "current_group": None,
                "results": [],
            }
            write_runtime_status(self.crawl_status_file, initial)
            self._crawl_thread = threading.Thread(
                target=self._crawl_worker,
                args=(groups, target_date),
                name="zalo-crawl-worker",
                daemon=True,
            )
            self._crawl_thread.start()
            return initial

    def _crawl_active(self) -> bool:
        return self._crawl_thread is not None and self._crawl_thread.is_alive()

    def _refresh_auth_process(self) -> None:
        if self._auth_process is None:
            return
        return_code = self._auth_process.poll()
        if return_code is None:
            return
        current = _read_json(self.auth_status_file, {})
        if current.get("state") not in {"completed", "error", "cancelled"}:
            write_runtime_status(
                self.auth_status_file,
                {
                    "state": "completed" if return_code == 0 else "error",
                    "message": (
                        "Đã đóng phiên xác thực."
                        if return_code == 0
                        else f"Phiên xác thực kết thúc với mã lỗi {return_code}."
                    ),
                },
            )

    def _crawl_worker(self, groups: list[str], target_date: str) -> None:
        results: list[dict[str, Any]] = []
        total = len(groups)
        for index, group in enumerate(groups):
            write_runtime_status(
                self.crawl_status_file,
                {
                    "state": "running",
                    "message": f"Đang crawl nhóm {index + 1}/{total}.",
                    "target_date": target_date,
                    "total": total,
                    "completed": index,
                    "current_group": group,
                    "results": results,
                },
            )
            command = [
                sys.executable,
                "-m",
                "zalo_order_crawler",
                "run",
                "--group",
                group,
                "--date",
                target_date,
            ]
            try:
                self._crawl_process = subprocess.Popen(
                    command,
                    cwd=self.settings.project_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                output, _ = self._crawl_process.communicate()
                return_code = self._crawl_process.returncode
                output_dir = (
                    self._latest_output_dir(group, target_date)
                    if return_code == 0
                    else None
                )
                result = {
                    "group": group,
                    "ok": return_code == 0,
                    "message": self._last_log_line(output)
                    or ("Hoàn tất." if return_code == 0 else f"Mã lỗi {return_code}."),
                    "output_dir": output_dir,
                    "run_id": self._run_id(output_dir) if output_dir else None,
                    "google_drive": self._google_drive_output(output_dir),
                }
            except Exception as exc:
                result = {
                    "group": group,
                    "ok": False,
                    "message": str(exc),
                    "output_dir": None,
                    "run_id": None,
                    "google_drive": {},
                }
            finally:
                self._crawl_process = None
            results.append(result)

        failed = sum(not item["ok"] for item in results)
        write_runtime_status(
            self.crawl_status_file,
            {
                "state": "completed" if failed == 0 else "completed_with_errors",
                "message": (
                    f"Hoàn tất {total} nhóm."
                    if failed == 0
                    else f"Hoàn tất với {failed}/{total} nhóm bị lỗi."
                ),
                "target_date": target_date,
                "total": total,
                "completed": total,
                "current_group": None,
                "results": results,
            },
        )

    @staticmethod
    def _last_log_line(output: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        error_lines = [line for line in lines if line.startswith("Lỗi:")]
        if error_lines:
            return error_lines[-1][:500]
        return lines[-1][:500] if lines else ""

    def _latest_output_dir(self, group: str, target_date: str) -> str | None:
        parent = self.settings.output_dir / safe_slug(group) / target_date
        try:
            directories = [path for path in parent.iterdir() if path.is_dir()]
        except FileNotFoundError:
            return None
        if not directories:
            return None
        latest = max(directories, key=lambda path: path.stat().st_mtime)
        return str(latest)

    def _run_id(self, output_dir: str | Path) -> str | None:
        try:
            relative = Path(output_dir).resolve().relative_to(
                self.settings.output_dir.resolve()
            )
        except (OSError, ValueError):
            return None
        return relative.as_posix()

    @staticmethod
    def _google_drive_output(output_dir: str | Path | None) -> dict[str, Any]:
        if not output_dir:
            return {}
        manifest = _read_json(Path(output_dir) / "manifest.json", {})
        value = manifest.get("google_drive")
        return value if isinstance(value, dict) else {}

    def _resolve_run_dir(self, run_id: str) -> Path:
        if not run_id or len(run_id) > 500:
            raise ValueError("Mã lượt crawl không hợp lệ.")
        raw = Path(run_id)
        if raw.is_absolute():
            raise ValueError("Mã lượt crawl không hợp lệ.")
        root = self.settings.output_dir.resolve()
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Mã lượt crawl nằm ngoài thư mục output.") from exc
        if not candidate.is_dir():
            raise FileNotFoundError("Không tìm thấy lượt crawl.")
        return candidate

    def run_result(self, run_id: str) -> dict[str, Any]:
        run_dir = self._resolve_run_dir(run_id)
        manifest = _read_json(run_dir / "manifest.json", {})
        if not manifest:
            raise FileNotFoundError("Thiếu manifest của lượt crawl.")
        messages = _read_jsonl(run_dir / "clean_messages.jsonl")
        decisions = {
            item.get("message_id"): item
            for item in _read_jsonl(run_dir / "classifications.jsonl")
            if item.get("message_id")
        }
        ocr_results_by_message: dict[str, list[dict[str, Any]]] = {}
        ocr_path = run_dir / "order_ocr.jsonl"
        ocr_values = _read_jsonl(ocr_path) if ocr_path.is_file() else []
        for item in ocr_values:
            message_id = str(item.get("message_id") or "")
            if message_id:
                ocr_results_by_message.setdefault(message_id, []).append(item)

        rendered_messages: list[dict[str, Any]] = []
        for message in sorted(messages, key=lambda item: int(item.get("sequence", 0))):
            media_items: list[dict[str, Any]] = []
            for media in message.get("media") or []:
                if not isinstance(media, dict):
                    continue
                media_path = str(media.get("path") or "")
                try:
                    self._resolve_run_media(run_dir, media_path)
                except (FileNotFoundError, ValueError):
                    continue
                media_items.append(
                    {
                        "path": media_path,
                        "mime_type": str(media.get("mime_type") or ""),
                        "role": str(media.get("role") or ""),
                        "url": "/api/run-asset?"
                        + urlencode({"id": run_id, "path": media_path}),
                    }
                )
            rendered_messages.append(
                {
                    "message_id": message.get("message_id"),
                    "sequence": message.get("sequence", 0),
                    "sender": message.get("sender"),
                    "time": message.get("timestamp_text"),
                    "content": message.get("content", ""),
                    "direction": message.get("direction", "unknown"),
                    "message_type": message.get("message_type", "text"),
                    "media": media_items,
                    "decision": decisions.get(message.get("message_id")),
                    "ocr_results": ocr_results_by_message.get(
                        str(message.get("message_id") or ""), []
                    ),
                }
            )

        return {
            "run_id": run_id,
            "group_name": manifest.get("group_name", ""),
            "target_date": manifest.get("target_date", ""),
            "summary": {
                "raw_messages": manifest.get("raw_message_count", 0),
                "clean_messages": manifest.get("clean_message_count", 0),
                "classified_messages": manifest.get("classified_message_count", 0),
                "orders": manifest.get("order_count", 0),
                "media": manifest.get("media_count", 0),
                "message_images": manifest.get("message_image_count", 0),
                "ocr_images": manifest.get("ocr_image_count", 0),
                "ocr_items": manifest.get("ocr_item_count", 0),
                "ocr_review_images": manifest.get("ocr_review_image_count", 0),
                "ocr_review_items": manifest.get("ocr_review_item_count", 0),
                "warnings": manifest.get("warnings", []),
            },
            "google_drive": (
                manifest.get("google_drive")
                if isinstance(manifest.get("google_drive"), dict)
                else {}
            ),
            "messages": rendered_messages,
        }

    def run_asset(self, run_id: str, media_path: str) -> tuple[Path, str]:
        run_dir = self._resolve_run_dir(run_id)
        allowed: dict[str, str] = {}
        for message in _read_jsonl(run_dir / "clean_messages.jsonl"):
            for media in message.get("media") or []:
                if isinstance(media, dict) and media.get("path"):
                    allowed[str(media["path"])] = str(
                        media.get("mime_type") or "application/octet-stream"
                    )
        if media_path not in allowed:
            raise FileNotFoundError("Ảnh không thuộc lượt crawl này.")
        return self._resolve_run_media(run_dir, media_path), allowed[media_path]

    @staticmethod
    def _resolve_run_media(run_dir: Path, media_path: str) -> Path:
        if not media_path:
            raise ValueError("Đường dẫn ảnh không hợp lệ.")
        raw = Path(media_path)
        if raw.is_absolute():
            raise ValueError("Đường dẫn ảnh không hợp lệ.")
        candidate = (run_dir / raw).resolve()
        try:
            candidate.relative_to(run_dir.resolve())
        except ValueError as exc:
            raise ValueError("Đường dẫn ảnh nằm ngoài lượt crawl.") from exc
        if not candidate.is_file():
            raise FileNotFoundError("Không tìm thấy ảnh.")
        return candidate


class AppHTTPServer(ThreadingHTTPServer):
    manager: AppManager
    static_dir: Path


class AppRequestHandler(BaseHTTPRequestHandler):
    server: AppHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[UI] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/bootstrap":
                self._json(HTTPStatus.OK, self.server.manager.bootstrap())
            elif path == "/api/status":
                self._json(HTTPStatus.OK, self.server.manager.status())
            elif path == "/api/run-result":
                self._json(
                    HTTPStatus.OK,
                    self.server.manager.run_result(self._query_value(query, "id")),
                )
            elif path == "/api/run-asset":
                asset, content_type = self.server.manager.run_asset(
                    self._query_value(query, "id"),
                    self._query_value(query, "path"),
                )
                self._binary(HTTPStatus.OK, asset.read_bytes(), content_type)
            elif path in {"/", "/index.html"}:
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})
        except FileNotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._request_json()
            if path == "/api/auth/start":
                result = self.server.manager.start_auth()
            elif path == "/api/auth/complete":
                result = self.server.manager.complete_auth()
            elif path == "/api/crawl/start":
                result = self.server.manager.start_crawl(
                    payload.get("groups"), payload.get("date")
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Không tìm thấy đường dẫn."})
                return
            self._json(HTTPStatus.ACCEPTED, result)
        except (ValueError, RuntimeError) as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _request_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length không hợp lệ.") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request quá lớn.")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request JSON không hợp lệ.") from exc
        if not isinstance(value, dict):
            raise ValueError("Request phải là một JSON object.")
        return value

    def _static(self, filename: str, content_type: str) -> None:
        path = self.server.static_dir / filename
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Thiếu tài nguyên giao diện."})
            return
        self.send_response(HTTPStatus.OK)
        self._common_headers(content_type, len(payload))
        self.end_headers()
        self.wfile.write(payload)

    @staticmethod
    def _query_value(query: dict[str, list[str]], key: str) -> str:
        values = query.get(key, [])
        if len(values) != 1 or not values[0]:
            raise ValueError(f"Thiếu query parameter {key!r}.")
        return values[0]

    def _binary(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self._common_headers(content_type, len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def _common_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )


def serve_ui(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("UI chỉ được phép bind vào loopback để bảo vệ dữ liệu Zalo.")
    static_dir = Path(__file__).with_name("web")
    server = AppHTTPServer((host, port), AppRequestHandler)
    server.manager = AppManager(settings)
    server.static_dir = static_dir
    url = f"http://{host}:{port}/"
    print(f"Giao diện đang chạy tại {url}")
    print("Nhấn Ctrl+C để dừng.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("Đang dừng giao diện...")
    finally:
        server.server_close()
    return 0
