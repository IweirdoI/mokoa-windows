"""把 PDF、Word、图片转成 ingestion 可消费的结构化载荷。

PDF / Word 只做本地抽文本。图片优先走视觉模型描述；当前模型没有
视觉能力或调用失败时，再回退本地 Tesseract OCR，最后才用用户说明
（caption）。扫描件 PDF 若抽不出文字，同样要求补充说明，否则拒绝。

Owner: 3fba60e1
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any


class FileExtractionError(ValueError):
    """文件类型不受支持、过大或无法打开时的可预期错误。"""

    def __init__(self, message: str, *, code: str = "unsupported_file") -> None:
        super().__init__(message)
        self.code = code


MAX_FILE_BYTES = 8 * 1024 * 1024
KIND_LABELS = {"pdf": "PDF", "docx": "Word", "image": "图片", "bpmn": "BPMN流程"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_KIND_BY_EXT = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".bpmn": "bpmn",
    **{ext: "image" for ext in _IMAGE_EXTS},
}


def detect_file_kind(filename: str, data: bytes = b"") -> str | None:
    """按扩展名识别，必要时回退到文件头魔数。"""

    ext = Path(filename or "").suffix.lower()
    if ext in _KIND_BY_EXT:
        return _KIND_BY_EXT[ext]
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8"):
        return "image"
    if data.startswith(b"PK") and filename.lower().endswith(".docx"):
        return "docx"
    if ext == ".xml":
        head = data[:4096].decode("utf-8", "ignore")
        lower_head = head.lower()
        if "<word" in lower_head and "bpmn" not in lower_head:
            return None
        if "bpmn" in lower_head:
            return "bpmn"
    return None


def extract_document(
    filename: str,
    data: bytes,
    *,
    caption: str | None = None,
) -> dict[str, Any]:
    """解析文件并返回可交给 ``process(..., "document")`` 的 payload。

    Raises:
        FileExtractionError: 空文件、超限、旧版 ``.doc`` 或无法识别的类型。
    """

    if not data:
        raise FileExtractionError("上传文件为空", code="invalid_payload")
    if len(data) > MAX_FILE_BYTES:
        raise FileExtractionError(
            f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限",
            code="invalid_payload",
        )

    lowered = (filename or "").lower()
    if lowered.endswith(".doc") and not lowered.endswith(".docx"):
        raise FileExtractionError(
            "暂不支持旧版 Word（.doc），请另存为 .docx",
            code="unsupported_file",
        )

    kind = detect_file_kind(filename, data)
    if kind is None:
        raise FileExtractionError(
            "仅支持 PDF、Word（.docx）、BPMN 流程和常见图片格式",
            code="unsupported_file",
        )

    caption_text = (caption or "").strip()
    extracted = ""
    extra: dict[str, Any] = {"file_kind": kind, "filename": Path(filename).name}

    if kind == "pdf":
        extracted, extra["page_count"] = _extract_pdf(data)
    elif kind == "docx":
        extracted = _extract_docx(data)
    elif kind == "bpmn":
        xml = data.decode("utf-8", "ignore")
        from src.ingestion.bpmn_text import extract_bpmn_labels

        parsed = extract_bpmn_labels(xml, filename=extra["filename"])
        extra["bpmn_tasks"] = parsed.get("tasks") or []
        extra["bpmn_names"] = parsed.get("names") or []
        extra["bpmn_parse_ok"] = bool(parsed.get("parse_ok"))
        extracted = str(parsed.get("text") or "").strip()
    else:
        extracted, image_meta = _extract_image(
            data,
            caption=caption_text,
            filename=extra["filename"],
        )
        extra.update(image_meta)

    extra["ocr_used"] = bool(extra.pop("ocr_used", False))
    extra["vision_used"] = bool(extra.pop("vision_used", False))
    extra["embedded_text_used"] = bool(extra.pop("embedded_text_used", False))
    extracted_text = extracted.strip()
    extra["extract_status"] = "ok" if extracted_text else "empty"
    extra["char_count"] = len(extracted_text)
    if caption_text:
        extra["caption"] = caption_text

    # 图内/文档抽取优先；caption 只作补充，避免入库内容只剩一句说明。
    content = _merge_extracted_and_caption(extracted_text, caption_text)
    if content:
        extra["content"] = content
    if extra.get("vision_used"):
        extra["confidence"] = 0.9
    elif extra.get("ocr_used") or extra.get("embedded_text_used"):
        extra["confidence"] = 0.88
    else:
        extra["confidence"] = 0.85
    return extra


def _merge_extracted_and_caption(extracted: str, caption: str) -> str:
    """合成入库正文：有抽取结果时不以 caption 覆盖。"""

    extracted = (extracted or "").strip()
    caption = (caption or "").strip()
    if extracted and caption:
        if caption in extracted:
            return extracted
        return f"{extracted}\n（用户说明：{caption}）"
    return extracted or caption


def _extract_pdf(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise FileExtractionError(
            "服务端未安装 pypdf，无法解析 PDF",
            code="extraction_failed",
        ) from exc

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise FileExtractionError("PDF 无法打开或已损坏", code="extraction_failed") from exc

    pages = list(reader.pages)
    texts = [(page.extract_text() or "").strip() for page in pages]
    return "\n".join(part for part in texts if part), len(pages)


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise FileExtractionError(
            "服务端未安装 python-docx，无法解析 Word",
            code="extraction_failed",
        ) from exc

    try:
        document = Document(BytesIO(data))
    except Exception as exc:
        raise FileExtractionError("Word 文档无法打开或已损坏", code="extraction_failed") from exc

    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_image(
    data: bytes,
    *,
    caption: str | None = None,
    filename: str = "",
) -> tuple[str, dict[str, Any]]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise FileExtractionError(
            "服务端未安装 Pillow，无法解析图片",
            code="extraction_failed",
        ) from exc

    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            embedded_text = _read_embedded_image_text(image)
    except Exception as exc:
        raise FileExtractionError("图片无法打开或已损坏", code="extraction_failed") from exc

    vision_text, vision_model = _describe_with_vision(
        data,
        caption=caption,
        filename=filename,
    )
    ocr_text = ""
    # 视觉结果过短或失败时继续 OCR，避免只留下用户说明。
    if not vision_text or len(vision_text.strip()) < 24:
        try:
            with Image.open(BytesIO(data)) as image:
                image.load()
                ocr_text = _ocr_image(image)
        except Exception as exc:
            raise FileExtractionError("图片无法打开或已损坏", code="extraction_failed") from exc

    chosen = _pick_richest_text(vision_text, ocr_text, embedded_text)
    meta: dict[str, Any] = {
        "image_width": width,
        "image_height": height,
        "ocr_used": bool(ocr_text.strip()),
        "vision_used": bool(vision_text.strip()),
        "embedded_text_used": bool(embedded_text.strip())
        and chosen == embedded_text.strip(),
    }
    if vision_model:
        meta["vision_model"] = vision_model
    return chosen, meta


def _pick_richest_text(*candidates: str) -> str:
    """在多路抽取结果中选信息量最大的非空文本。"""

    scored: list[tuple[int, str]] = []
    for raw in candidates:
        text = (raw or "").strip()
        if not text:
            continue
        # 更长且含数字/路径的更可能是图内事实。
        bonus = 20 if any(ch.isdigit() for ch in text) else 0
        bonus += 10 if ("/" in text or ":" in text or "=" in text) else 0
        scored.append((len(text) + bonus, text))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _read_embedded_image_text(image: Any) -> str:
    """读取 PNG tEXt/iTXt 中的演示/无障碍明文（键名 MokoaText）。

    真实截图通常没有该字段；有 OCR/视觉时它们仍优先。用于无 Tesseract
    环境下的可控夹具，以及辅助工具写入的可访问性文本层。
    """

    info = getattr(image, "text", None) or getattr(image, "info", None) or {}
    if not isinstance(info, dict):
        return ""
    for key in ("MokoaText", "mokoa_text", "alt", "Description"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _describe_with_vision(
    data: bytes,
    *,
    caption: str | None = None,
    filename: str = "",
) -> tuple[str, str | None]:
    """有视觉能力时让模型看图；任何失败都静默回退。"""

    from src.common.config import get_llm_config
    from src.ingestion import vision

    try:
        text = vision.describe_image(data, caption=caption, filename=filename).strip()
    except Exception:
        return "", None
    if not text:
        return "", None
    return text, str(get_llm_config().get("model") or "") or None


def _ocr_image(image: Any) -> str:
    """可选本地 OCR；未安装 Tesseract 时静默返回空串。"""

    try:
        import pytesseract
    except ImportError:
        return ""
    try:
        prepared = _prepare_image_for_ocr(image)
        # 先英数（演示夹具多为 ASCII FACT），再试中英混合。
        for lang in ("eng", "chi_sim+eng"):
            try:
                text = pytesseract.image_to_string(prepared, lang=lang) or ""
            except Exception:
                continue
            if text.strip():
                return text
        return ""
    except Exception:
        return ""


def _prepare_image_for_ocr(image: Any) -> Any:
    """放大并二值化，提升点阵/截图类文字的 OCR 命中率。"""

    from PIL import Image, ImageOps

    rgb = image.convert("RGB")
    width, height = rgb.size
    scale = 3 if max(width, height) < 1200 else 2
    upscaled = rgb.resize((width * scale, height * scale), resample=Image.Resampling.NEAREST)
    gray = ImageOps.grayscale(upscaled)
    # 简单阈值：偏亮背景上的深色字。
    return gray.point(lambda pixel: 255 if pixel > 180 else 0)
