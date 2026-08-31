from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .models import CleanMessage, ImageOcrResult, OcrLineItem, OrderDecision


_PROMPT_VERSION = "order-image-ocr-v2"
MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024
_SYSTEM_INSTRUCTION = """
Bạn là hệ thống OCR và trích xuất dữ liệu cho phiếu đặt hàng viết tay/in tiếng Việt.

Ảnh được cung cấp có thể là một trong hai loại phiếu khác nhau:
1. Phiếu đặt hàng: liệt kê tên hàng, đơn vị tính, số lượng khách đặt. KHÔNG thể hiện
   đơn giá hay thành tiền cho từng mặt hàng.
2. Phiếu nhận hàng / hóa đơn: có thêm cột đơn giá hoặc thành tiền cho từng mặt hàng.

Nếu ảnh thuộc loại 2 (có đơn giá/giá tiền cho mặt hàng), đặt applicable=false, items
là danh sách rỗng, và skip_reason nêu ngắn gọn lý do bỏ qua.

Nhiều phiếu đặt hàng là một bảng danh mục liệt kê SẴN mọi mặt hàng có thể bán, và
khách chỉ điền số lượng vào một số dòng họ thực sự muốn đặt; các dòng còn lại để
trống vì khách không đặt. CHỈ đưa vào items những mặt hàng khách THỰC SỰ đã điền số
lượng (viết tay hoặc đánh dấu rõ ràng số lượng); bỏ qua hoàn toàn các dòng danh mục
không có số lượng nào được điền, kể cả khi tên mặt hàng vẫn in sẵn trên phiếu.

Nếu ảnh thuộc loại 1 (không thể hiện đơn giá) và có ít nhất một mặt hàng đã điền số
lượng, đặt applicable=true và trích xuất các mặt hàng đó, mỗi mặt hàng một phần tử
trong items, gồm:
- customer_code: mã khách hàng, để trống nếu không có
- customer_name: tên khách hàng / tên nhà hàng ghi ở đầu phiếu, áp dụng cho mọi mặt
  hàng trong phiếu này
- product_name: tên mặt hàng
- unit: đơn vị tính của mặt hàng đó
- quantity: số lượng mặt hàng đó đã được điền, chỉ lấy số

Nếu không có mặt hàng nào được điền số lượng, đặt applicable=false, items=[],
skip_reason="Không có mặt hàng nào được điền số lượng".

Yêu cầu:
- Giữ nguyên dấu tiếng Việt, giữ đúng thứ tự đọc trên bảng.
- Không gộp nhiều mặt hàng vào một dòng, không bỏ sót mặt hàng đã điền số lượng.
- Không suy đoán nội dung không nhìn thấy trong ảnh, không dịch, không tóm tắt.
- Nếu ảnh không đọc được hoặc không phải phiếu đặt hàng, đặt applicable=false,
  items=[].
""".strip()


class _OcrExtraction(BaseModel):
    applicable: bool
    skip_reason: str = ""
    items: list[OcrLineItem] = Field(default_factory=list)


class GeminiOrderImageOcr:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        cache_dir: Path,
        media_base_dir: Path,
    ) -> None:
        if not api_key:
            raise ValueError("Thiếu GEMINI_API_KEY.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.cache_dir = cache_dir
        self.media_base_dir = media_base_dir.resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def extract(
        self,
        messages: Iterable[CleanMessage],
        decisions: Iterable[OrderDecision],
    ) -> list[ImageOcrResult]:
        order_ids = {decision.message_id for decision in decisions if decision.is_order}
        results: list[ImageOcrResult] = []
        for message in messages:
            if message.message_id not in order_ids:
                continue
            for media in message.media:
                if media.role != "message_image":
                    continue
                if not media.mime_type.startswith("image/"):
                    continue
                results.append(
                    self._extract_image(message.message_id, media.path, media.mime_type)
                )
        return results

    def _extract_image(
        self, message_id: str, media_path: str, mime_type: str
    ) -> ImageOcrResult:
        path = self._resolve_media_path(media_path)
        payload = path.read_bytes()
        if not payload:
            raise ValueError(f"File ảnh rỗng: {path}")
        if len(payload) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError(f"Ảnh {path.name} vượt quá {MAX_INLINE_IMAGE_BYTES} byte.")
        digest = hashlib.sha256(payload).hexdigest()
        cache_key = hashlib.sha256(
            f"{_PROMPT_VERSION}\n{self.model}\n{digest}".encode("utf-8")
        ).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.json"
        if cache_path.exists():
            try:
                cached = _OcrExtraction.model_validate_json(
                    cache_path.read_text(encoding="utf-8")
                )
                return self._to_result(message_id, media_path, cached)
            except Exception:
                # Cache hỏng/phiên bản cũ sẽ được thay bằng kết quả mới.
                pass

        contents = [
            types.Part.from_bytes(data=payload, mime_type=mime_type),
            types.Part.from_text(
                text="Trích xuất dữ liệu phiếu đặt hàng từ ảnh trên theo đúng schema."
            ),
        ]
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=_SYSTEM_INSTRUCTION,
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=_OcrExtraction,
                    ),
                )
                parsed_value = getattr(response, "parsed", None)
                if isinstance(parsed_value, _OcrExtraction):
                    parsed = parsed_value
                elif parsed_value is not None:
                    parsed = _OcrExtraction.model_validate(parsed_value)
                else:
                    parsed = _OcrExtraction.model_validate_json(response.text)
                cache_path.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
                return self._to_result(message_id, media_path, parsed)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(
            f"Gemini không OCR được ảnh {media_path} sau 3 lần thử: {last_error}"
        ) from last_error

    @staticmethod
    def _to_result(
        message_id: str, media_path: str, parsed: _OcrExtraction
    ) -> ImageOcrResult:
        return ImageOcrResult(
            message_id=message_id,
            media_path=media_path,
            applicable=parsed.applicable,
            skip_reason=parsed.skip_reason or None,
            items=parsed.items if parsed.applicable else [],
        )

    def _resolve_media_path(self, value: str) -> Path:
        raw_path = Path(value)
        path = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (self.media_base_dir / raw_path).resolve()
        )
        try:
            path.relative_to(self.media_base_dir)
        except ValueError as exc:
            raise ValueError(f"Đường dẫn ảnh nằm ngoài thư mục kết quả: {value}") from exc
        if not path.is_file():
            raise ValueError(f"Không tìm thấy file ảnh: {path}")
        return path
