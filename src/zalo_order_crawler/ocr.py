from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .image_processing import prepare_ocr_tiles
from .models import (
    CleanMessage,
    ImageOcrResult,
    OcrLineItem,
    OrderDecision,
    ProductCatalogEntry,
)


_PROMPT_VERSION = "order-image-ocr-v5-oriented-focused-verification"
MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024
_FUZZY_PRODUCT_MATCH_THRESHOLD = 0.90
_FUZZY_PRODUCT_MATCH_MARGIN = 0.06
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

Trước khi trích xuất, đánh giá chất lượng ảnh đối với chính nhiệm vụ đọc phiếu:
- image_quality_score: điểm từ 0 đến 1, phản ánh khả năng đọc chính xác tên hàng,
  đơn vị và số lượng. Xem xét độ phân giải, độ nét, độ tương phản, góc xoay, méo phối
  cảnh, chói sáng, che khuất, cắt mất nội dung và độ rõ của chữ viết tay.
- image_quality_affects_output=true khi chất lượng ảnh có thể trực tiếp làm sai hoặc
  bỏ sót ít nhất một mặt hàng, đơn vị hay số lượng; ngược lại đặt false.
- image_quality_reason: nêu ngắn gọn bằng tiếng Việt vấn đề quan sát được. Nếu ảnh
  đủ rõ và không ảnh hưởng kết quả, vẫn nêu ngắn gọn rằng ảnh đọc được.

Điểm chất lượng phải phản ánh khả năng trích xuất thực tế, không dựa vào số lượng
items đã trả về và không tự nâng điểm chỉ để tránh yêu cầu kiểm tra.

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
    image_quality_score: float = Field(ge=0, le=1)
    image_quality_affects_output: bool
    image_quality_reason: str
    items: list[OcrLineItem] = Field(default_factory=list)


class GeminiOrderImageOcr:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        cache_dir: Path,
        media_base_dir: Path,
        product_catalog: Iterable[ProductCatalogEntry] = (),
        enhancement_enabled: bool = True,
        tile_count: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("Thiếu GEMINI_API_KEY.")
        if tile_count < 2 or tile_count > 8:
            raise ValueError("OCR_TILE_COUNT phải nằm trong khoảng 2..8.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.cache_dir = cache_dir
        self.media_base_dir = media_base_dir.resolve()
        self.product_catalog = tuple(product_catalog)
        self.enhancement_enabled = enhancement_enabled
        self.tile_count = tile_count
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def extract(
        self,
        messages: Iterable[CleanMessage],
        decisions: Iterable[OrderDecision],
    ) -> list[ImageOcrResult]:
        order_decisions = {
            decision.message_id: decision
            for decision in decisions
            if decision.is_order
        }
        results: list[ImageOcrResult] = []
        for message in messages:
            decision = order_decisions.get(message.message_id)
            if decision is None:
                continue
            for media in message.media:
                if media.role != "message_image":
                    continue
                if not media.mime_type.startswith("image/"):
                    continue
                result = self._extract_image(
                    message.message_id,
                    media.path,
                    media.mime_type,
                    decision,
                )
                results.append(result.apply_order_review_policy(decision))
        return results

    def _extract_image(
        self,
        message_id: str,
        media_path: str,
        mime_type: str,
        decision: OrderDecision | None = None,
    ) -> ImageOcrResult:
        path = self._resolve_media_path(media_path)
        payload = path.read_bytes()
        if not payload:
            raise ValueError(f"File ảnh rỗng: {path}")
        if len(payload) > MAX_INLINE_IMAGE_BYTES:
            raise ValueError(f"Ảnh {path.name} vượt quá {MAX_INLINE_IMAGE_BYTES} byte.")
        catalog = self._catalog_for_branch(
            decision.branch_name if decision is not None else None
        )
        product_hints = decision.products if decision is not None else []
        prompt_context = self._prompt_context(catalog, product_hints)
        parsed = self._extract_payload(
            payload,
            mime_type,
            cache_scope="full",
            task_prompt=(
                "Trích xuất dữ liệu phiếu đặt hàng từ toàn bộ ảnh theo đúng schema. "
                "Tự xác định chiều chữ trước khi đọc."
                + prompt_context
            ),
        )
        full_result = self._canonicalise_result(
            self._to_result(message_id, media_path, parsed),
            catalog,
            mark_unknown_for_review=False,
        )
        if not self.enhancement_enabled or not full_result.applicable:
            return full_result

        tiles = prepare_ocr_tiles(payload, tile_count=self.tile_count)
        if not tiles:
            return full_result

        tile_parsed = self._extract_payloads(
            [
                (
                    tile.data,
                    tile.mime_type,
                    f"Vùng {tile.position}/{tile.total}",
                )
                for tile in tiles
            ],
            cache_scope=f"tiles-{len(tiles)}",
            task_prompt=(
                "Các ảnh trên lần lượt là những vùng có chồng lấn, đã được cắt, "
                "xoay đúng chiều, làm rõ và phóng lớn từ cùng một phiếu đặt hàng "
                "đã xác nhận. Một vùng có thể không chứa tiêu đề hoặc tên khách. "
                "Hãy gộp kết quả từ mọi vùng. Chỉ lấy một mặt hàng khi ô SỐ LƯỢNG "
                "ngay bên phải tên hàng có số viết tay hoặc dấu gạch viết tay rõ "
                "ràng; tuyệt đối không biến đường kẻ bảng hoặc dòng trống thành số "
                "lượng, không chuyển số lượng sang dòng trên/dưới hay cột kế bên. "
                "Bỏ qua dòng bị cắt ở mép và không lặp mặt hàng do vùng chồng lấn. "
                "Để trống customer_code và customer_name ở lượt đọc vùng."
                + prompt_context
            ),
        )
        tile_results = [
            self._canonicalise_result(
                self._to_result(message_id, media_path, tile_parsed),
                catalog,
                mark_unknown_for_review=False,
            )
        ]
        verification_candidates = self._verification_candidates(
            full_result,
            tile_results,
        )
        verification_result: ImageOcrResult | None = None
        if verification_candidates:
            candidate_payload = [
                {
                    "product_name": candidate["product_name"],
                    "quantity_full_image": candidate["full_quantity"],
                    "quantity_tiled_image": candidate["tile_quantity"],
                }
                for candidate in verification_candidates
            ]
            verification_parsed = self._extract_payloads(
                [
                    (payload, mime_type, "Toàn ảnh cần đối chiếu"),
                    *[
                        (
                            tile.data,
                            tile.mime_type,
                            f"Vùng rõ {tile.position}/{tile.total}",
                        )
                        for tile in tiles
                    ],
                ],
                cache_scope=f"verify-{len(tiles)}",
                task_prompt=(
                    "Đây là lượt xác minh cuối. CHỈ đối chiếu các mặt hàng trong "
                    "JSON bên dưới. Với từng mặt hàng, quan sát đúng dòng tên hàng "
                    "và ô số lượng liền kề trên ảnh; chỉ trả mặt hàng nếu nhìn thấy "
                    "một số hoặc dấu gạch viết tay rõ ràng. Dùng ảnh toàn cảnh để "
                    "xác định đúng cột, dùng ảnh vùng để đọc nét viết. Không trả "
                    "thêm mặt hàng ngoài danh sách, không đoán theo hai giá trị gợi "
                    "ý và để trống customer_code/customer_name. Nếu không đọc chắc "
                    "thì bỏ mặt hàng đó khỏi items. Danh sách cần xác minh:\n"
                    + json.dumps(candidate_payload, ensure_ascii=False)
                    + prompt_context
                ),
            )
            verification_result = self._canonicalise_result(
                self._to_result(message_id, media_path, verification_parsed),
                catalog,
                mark_unknown_for_review=False,
            )
        merged = self._merge_results(
            full_result,
            tile_results,
            catalog,
            verification_result=verification_result,
        )
        return self._canonicalise_result(merged, catalog)

    def _extract_payload(
        self,
        payload: bytes,
        mime_type: str,
        *,
        cache_scope: str,
        task_prompt: str,
    ) -> _OcrExtraction:
        return self._extract_payloads(
            [(payload, mime_type, "Toàn ảnh")],
            cache_scope=cache_scope,
            task_prompt=task_prompt,
        )

    def _extract_payloads(
        self,
        images: Iterable[tuple[bytes, str, str]],
        *,
        cache_scope: str,
        task_prompt: str,
    ) -> _OcrExtraction:
        image_list = list(images)
        if not image_list:
            raise ValueError("Không có ảnh để OCR.")
        digest = hashlib.sha256(
            "\n".join(
                f"{label}:{hashlib.sha256(payload).hexdigest()}"
                for payload, _, label in image_list
            ).encode("utf-8")
        ).hexdigest()
        cache_key = hashlib.sha256(
            (
                f"{_PROMPT_VERSION}\n{self.model}\n{cache_scope}\n"
                f"{task_prompt}\n{digest}"
            ).encode("utf-8")
        ).hexdigest()
        cache_path = self.cache_dir / f"{cache_key}.json"
        if cache_path.exists():
            try:
                return _OcrExtraction.model_validate_json(
                    cache_path.read_text(encoding="utf-8")
                )
            except Exception:
                # Cache hỏng/phiên bản cũ sẽ được thay bằng kết quả mới.
                pass

        contents: list[types.Part] = []
        for payload, mime_type, label in image_list:
            if not payload:
                raise ValueError(f"Ảnh OCR {label!r} bị rỗng.")
            if len(payload) > MAX_INLINE_IMAGE_BYTES:
                raise ValueError(
                    f"Ảnh OCR {label!r} vượt quá {MAX_INLINE_IMAGE_BYTES} byte."
                )
            contents.append(types.Part.from_text(text=label))
            contents.append(types.Part.from_bytes(data=payload, mime_type=mime_type))
        contents.append(types.Part.from_text(text=task_prompt))
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
                return parsed
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(
            f"Gemini không OCR được ảnh sau 3 lần thử: {last_error}"
        ) from last_error

    @staticmethod
    def _prompt_context(
        catalog: tuple[ProductCatalogEntry, ...],
        product_hints: Iterable[str],
    ) -> str:
        hints = list(
            dict.fromkeys(item.strip() for item in product_hints if item.strip())
        )
        sections: list[str] = []
        if catalog:
            catalog_payload = [
                {
                    "product_name": entry.product_name,
                    "unit": entry.unit,
                    "aliases": entry.aliases,
                }
                for entry in catalog
            ]
            sections.append(
                "Nếu tên nhìn thấy khớp một sản phẩm trong danh mục JSON sau, phải "
                "trả đúng product_name và unit chuẩn trong danh mục. Không tự ghép "
                "với tên gần giống khi hình ảnh không đủ bằng chứng:\n"
                + json.dumps(catalog_payload, ensure_ascii=False)
            )
        elif hints:
            sections.append(
                "Danh sách tên hàng tham khảo từ lượt đọc sơ bộ (có thể có lỗi, chỉ "
                "dùng khi phù hợp với chữ nhìn thấy):\n"
                + json.dumps(hints, ensure_ascii=False)
            )
        return "\n\n" + "\n\n".join(sections) if sections else ""

    def _catalog_for_branch(
        self, branch_name: str | None
    ) -> tuple[ProductCatalogEntry, ...]:
        branch_key = self._normalise_product_name(branch_name or "")
        return tuple(
            entry
            for entry in self.product_catalog
            if not entry.branch_name.strip()
            or entry.branch_name.strip() == "*"
            or self._normalise_product_name(entry.branch_name) == branch_key
        )

    @classmethod
    def _canonicalise_result(
        cls,
        result: ImageOcrResult,
        catalog: tuple[ProductCatalogEntry, ...],
        *,
        mark_unknown_for_review: bool = True,
    ) -> ImageOcrResult:
        if not result.applicable or not catalog:
            return result
        items: list[OcrLineItem] = []
        unknown: list[str] = []
        for item in result.items:
            entry = cls._match_catalog_entry(item.product_name, catalog)
            if entry is None:
                items.append(item)
                if item.product_name.strip():
                    unknown.append(item.product_name.strip())
                continue
            items.append(
                item.model_copy(
                    update={
                        "product_name": entry.product_name,
                        "unit": entry.unit or item.unit,
                    }
                )
            )
        if not unknown or not mark_unknown_for_review:
            return result.model_copy(update={"items": items})

        names = ", ".join(dict.fromkeys(unknown[:5]))
        if len(unknown) > 5:
            names += ", ..."
        reason = f"Tên hàng chưa có trong cấu hình sản phẩm: {names}."
        reasons = [value for value in (result.review_reason, reason) if value]
        return result.model_copy(
            update={
                "items": items,
                "needs_review": True,
                "review_reason": " ".join(dict.fromkeys(reasons)),
            }
        )

    @classmethod
    def _match_catalog_entry(
        cls,
        product_name: str,
        catalog: tuple[ProductCatalogEntry, ...],
    ) -> ProductCatalogEntry | None:
        raw_key = cls._normalise_product_name(product_name)
        if not raw_key:
            return None
        scores_by_product: dict[str, tuple[float, ProductCatalogEntry]] = {}
        exact_matches: dict[str, ProductCatalogEntry] = {}
        ambiguous_exact: set[str] = set()
        for entry in catalog:
            names = (entry.product_name, *entry.aliases)
            for name in names:
                candidate_key = cls._normalise_product_name(name)
                if not candidate_key:
                    continue
                previous = exact_matches.get(candidate_key)
                if previous is not None and previous.product_name != entry.product_name:
                    ambiguous_exact.add(candidate_key)
                else:
                    exact_matches[candidate_key] = entry
                score = SequenceMatcher(None, raw_key, candidate_key).ratio()
                product_key = cls._normalise_product_name(entry.product_name)
                if score > scores_by_product.get(product_key, (0.0, entry))[0]:
                    scores_by_product[product_key] = (score, entry)
        if raw_key in exact_matches and raw_key not in ambiguous_exact:
            return exact_matches[raw_key]

        ranked = sorted(scores_by_product.values(), key=lambda value: value[0], reverse=True)
        if not ranked or ranked[0][0] < _FUZZY_PRODUCT_MATCH_THRESHOLD:
            return None
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if ranked[0][0] - second_score < _FUZZY_PRODUCT_MATCH_MARGIN:
            return None
        return ranked[0][1]

    @classmethod
    def _merge_results(
        cls,
        full_result: ImageOcrResult,
        tile_results: Iterable[ImageOcrResult],
        catalog: tuple[ProductCatalogEntry, ...],
        *,
        verification_result: ImageOcrResult | None = None,
    ) -> ImageOcrResult:
        tile_result_list = list(tile_results)
        tile_items = [
            item
            for result in tile_result_list
            if result.applicable
            for item in result.items
            if item.product_name.strip()
        ]
        if not tile_items:
            reasons = [
                value
                for value in (
                    full_result.review_reason,
                    "OCR ảnh chia vùng không xác nhận được mặt hàng nào.",
                )
                if value
            ]
            return full_result.model_copy(
                update={
                    "needs_review": True,
                    "review_reason": " ".join(dict.fromkeys(reasons)),
                }
            )

        common_code = next(
            (item.customer_code for item in full_result.items if item.customer_code),
            "",
        )
        common_name = next(
            (item.customer_name for item in full_result.items if item.customer_name),
            "",
        )
        base_by_key = {
            cls._normalise_product_name(item.product_name): item
            for item in full_result.items
            if item.product_name.strip()
        }
        tiled_by_key: dict[str, OcrLineItem] = {}
        tile_order: list[str] = []
        tile_conflicts: set[str] = set()
        for item in tile_items:
            key = cls._normalise_product_name(item.product_name)
            previous = tiled_by_key.get(key)
            if previous is None:
                tiled_by_key[key] = item
                tile_order.append(key)
            elif not cls._same_item_value(previous, item):
                tile_conflicts.add(key)

        verified_by_key = {
            cls._normalise_product_name(item.product_name): item
            for item in (
                verification_result.items
                if verification_result is not None and verification_result.applicable
                else []
            )
            if item.product_name.strip() and item.quantity is not None
        }
        merged_order = list(base_by_key)
        merged_order.extend(key for key in tile_order if key not in base_by_key)
        merged_by_key: dict[str, OcrLineItem] = {}
        unresolved: list[str] = []
        for key in merged_order:
            base_item = base_by_key.get(key)
            tiled_item = tiled_by_key.get(key)
            readings_agree = (
                base_item is not None
                and tiled_item is not None
                and key not in tile_conflicts
                and cls._same_item_value(base_item, tiled_item)
            )
            if readings_agree:
                selected = base_item
            else:
                selected = verified_by_key.get(key)
                if selected is None:
                    unresolved_item = base_item or tiled_item
                    if unresolved_item is not None:
                        unresolved.append(unresolved_item.product_name)
                    # A product seen only in a crop is exactly the failure mode
                    # that previously inflated review tabs with blank catalogue
                    # rows.  Keep it only when the focused pass confirms it.
                    if base_item is None:
                        continue
                    selected = base_item
            if selected is None:
                continue
            merged_by_key[key] = selected.model_copy(
                update={
                    # A cropped region often invents a customer from nearby text.
                    # Header data from the full image is authoritative.
                    "customer_code": common_code or selected.customer_code,
                    "customer_name": common_name or selected.customer_name,
                }
            )

        reasons = [full_result.review_reason]
        if unresolved:
            names = ", ".join(dict.fromkeys(unresolved[:8]))
            if len(dict.fromkeys(unresolved)) > 8:
                names += ", ..."
            reasons.append(
                "Lượt xác minh cuối chưa đọc chắc tên hàng hoặc số lượng: "
                + names
                + "."
            )

        review_reasons = [reason for reason in reasons if reason]
        needs_review = full_result.needs_review or bool(unresolved)
        return full_result.model_copy(
            update={
                "items": [
                    merged_by_key[key]
                    for key in merged_order
                    if key in merged_by_key
                ],
                # Only the full image can judge the quality of the source photo.
                # Cropped/upscaled regions are diagnostic evidence, not new photos.
                "image_quality_score": full_result.image_quality_score,
                "image_quality_affects_output": (
                    full_result.image_quality_affects_output
                ),
                "needs_review": needs_review,
                "review_reason": (
                    " ".join(dict.fromkeys(review_reasons)) or None
                ),
            }
        )

    @classmethod
    def _verification_candidates(
        cls,
        full_result: ImageOcrResult,
        tile_results: Iterable[ImageOcrResult],
    ) -> list[dict[str, str | float | None]]:
        base_by_key = {
            cls._normalise_product_name(item.product_name): item
            for item in full_result.items
            if item.product_name.strip()
        }
        tiled_by_key: dict[str, OcrLineItem] = {}
        tile_conflicts: set[str] = set()
        for result in tile_results:
            if not result.applicable:
                continue
            for item in result.items:
                if not item.product_name.strip():
                    continue
                key = cls._normalise_product_name(item.product_name)
                previous = tiled_by_key.get(key)
                if previous is None:
                    tiled_by_key[key] = item
                elif not cls._same_item_value(previous, item):
                    tile_conflicts.add(key)

        candidates: list[dict[str, str | float | None]] = []
        ordered_keys = list(base_by_key)
        ordered_keys.extend(key for key in tiled_by_key if key not in base_by_key)
        for key in ordered_keys:
            base_item = base_by_key.get(key)
            tiled_item = tiled_by_key.get(key)
            if (
                base_item is not None
                and tiled_item is not None
                and key not in tile_conflicts
                and cls._same_item_value(base_item, tiled_item)
            ):
                continue
            representative = base_item or tiled_item
            if representative is None:
                continue
            candidates.append(
                {
                    "product_name": representative.product_name,
                    "full_quantity": (
                        base_item.quantity if base_item is not None else None
                    ),
                    "tile_quantity": (
                        tiled_item.quantity if tiled_item is not None else None
                    ),
                }
            )
        return candidates

    @classmethod
    def _same_item_value(cls, first: OcrLineItem, second: OcrLineItem) -> bool:
        quantity_matches = first.quantity == second.quantity
        units_match = (
            not first.unit.strip()
            or not second.unit.strip()
            or cls._normalise_product_name(first.unit)
            == cls._normalise_product_name(second.unit)
        )
        return quantity_matches and units_match

    @staticmethod
    def _normalise_product_name(value: str) -> str:
        ascii_value = "".join(
            character
            for character in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(character)
        ).replace("đ", "d")
        return " ".join(re.sub(r"[^0-9a-z]+", " ", ascii_value).split())

    @staticmethod
    def _to_result(
        message_id: str, media_path: str, parsed: _OcrExtraction
    ) -> ImageOcrResult:
        return ImageOcrResult(
            message_id=message_id,
            media_path=media_path,
            applicable=parsed.applicable,
            skip_reason=parsed.skip_reason or None,
            image_quality_score=parsed.image_quality_score,
            image_quality_affects_output=parsed.image_quality_affects_output,
            image_quality_reason=parsed.image_quality_reason or None,
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
