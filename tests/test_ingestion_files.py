"""PDF / Word / 图片接入测试。"""

from __future__ import annotations

from io import BytesIO

from src.ingestion.file_extract import FileExtractionError, extract_document
from src.ingestion.models import RejectCode
from src.ingestion.pipeline import IngestionPipeline


def _pdf_bytes(text: str) -> bytes:
    safe = "".join(char if 32 <= ord(char) < 127 else "?" for char in text)
    stream = f"BT /F1 12 Tf 20 100 Td ({safe}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
            b"/Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = bytearray(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        xref += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{len(output)}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output + xref + trailer)


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (64, 24), color=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_extract_pdf_and_docx_text() -> None:
    pdf = extract_document("note.pdf", _pdf_bytes("Kylin OS Agent memory"))
    docx = extract_document("note.docx", _docx_bytes("麒麟办公助手会议纪要"))

    assert pdf["file_kind"] == "pdf"
    assert "Kylin" in pdf["content"]
    assert docx["file_kind"] == "docx"
    assert "会议纪要" in docx["content"]


def test_image_without_caption_is_rejected_but_caption_is_accepted() -> None:
    pipeline = IngestionPipeline()
    empty = pipeline.process_file("shot.png", _png_bytes(), user_id="u001")
    captioned = pipeline.process_file(
        "shot.png",
        _png_bytes(),
        user_id="u001",
        caption="桌面截图：终端里 pip 安装失败后切换了清华镜像",
    )

    assert empty.accepted is False
    assert empty.reject_code in {RejectCode.NORMALIZATION_FAILED, RejectCode.EMPTY_CONTENT}
    assert captioned.accepted is True
    assert captioned.record is not None
    assert captioned.record.type == "document"
    assert captioned.record.metadata["file_kind"] == "image"
    assert "清华镜像" in captioned.record.content


def test_process_file_accepts_pdf_as_document_knowledge() -> None:
    result = IngestionPipeline().process_file(
        "guide.pdf",
        _pdf_bytes("Install python3-pip on Kylin desktop first"),
        user_id="u001",
        scene_id="os_operation",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.source.value == "document"
    assert result.record.metadata["filename"] == "guide.pdf"
    assert result.record.metadata["record_key"].endswith(":guide.pdf")


def test_old_doc_and_unknown_type_are_rejected() -> None:
    pipeline = IngestionPipeline()
    old = pipeline.process_file("legacy.doc", b"OLE", user_id="u001")
    unknown = pipeline.process_file("notes.txt", b"plain text", user_id="u001")

    assert old.reject_code == RejectCode.INVALID_SOURCE
    assert unknown.reject_code == RejectCode.INVALID_SOURCE


def test_extract_rejects_oversized_file() -> None:
    try:
        extract_document("huge.pdf", b"%PDF" + b"x" * (8 * 1024 * 1024 + 1))
    except FileExtractionError as exc:
        assert exc.code == "invalid_payload"
    else:
        raise AssertionError("oversized file should fail")


def test_vision_model_name_detection() -> None:
    from src.ingestion.vision import model_name_suggests_vision, model_supports_vision

    assert model_name_suggests_vision("qwen2.5-vl-7b") is True
    assert model_name_suggests_vision("gpt-4o") is True
    assert model_name_suggests_vision("llava-v1.6") is True
    assert model_name_suggests_vision("qwen3.5-0.8b") is False
    assert model_name_suggests_vision("phi-3.1-mini-128k-instruct") is False
    assert model_supports_vision() is False


def test_describe_image_sends_data_url(monkeypatch) -> None:
    from src.ingestion import vision

    monkeypatch.setattr(vision, "model_supports_vision", lambda: True)
    monkeypatch.setattr(
        vision,
        "get_llm_config",
        lambda: {
            "base_url": "http://127.0.0.1:1234",
            "api_key": "sk-test",
            "model": "qwen2.5-vl-7b",
            "timeout_seconds": 5,
            "max_tokens": 256,
        },
    )

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "<think>x</think>看到了桌面终端"}}]}

    def fake_post(url, json, headers, timeout, **kwargs):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(vision.httpx, "post", fake_post)
    text = vision.describe_image(_png_bytes(), caption="看一下")

    assert text == "看到了桌面终端"
    assert captured["url"].endswith("/v1/chat/completions")
    parts = captured["json"]["messages"][0]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_uses_vision_description_when_available(monkeypatch) -> None:
    from src.ingestion import vision

    monkeypatch.setattr(
        vision,
        "describe_image",
        lambda data, caption=None, filename="": "终端报错 pip 超时后改用了清华镜像",
    )

    payload = extract_document("shot.png", _png_bytes())
    result = IngestionPipeline().process_file("shot.png", _png_bytes(), user_id="u001")

    assert payload["vision_used"] is True
    assert "清华镜像" in payload["content"]
    assert result.accepted is True
    assert result.record is not None
    assert result.record.metadata["vision_used"] is True
    assert "清华镜像" in result.record.content


def test_vision_failure_falls_back_to_caption(monkeypatch) -> None:
    from src.ingestion import vision

    monkeypatch.setattr(vision, "describe_image", lambda data, caption=None, filename="": "")

    result = IngestionPipeline().process_file(
        "shot.png",
        _png_bytes(),
        user_id="u001",
        caption="桌面截图：只看到登录失败",
    )

    assert result.accepted is True
    assert result.record is not None
    assert result.record.metadata.get("vision_used") is False
    assert "登录失败" in result.record.content


def test_embedded_png_text_used_when_vision_and_ocr_empty(monkeypatch) -> None:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    from src.ingestion import vision

    monkeypatch.setattr(vision, "describe_image", lambda data, caption=None, filename="": "")
    monkeypatch.setattr(
        "src.ingestion.file_extract._ocr_image",
        lambda image: "",
    )

    image = Image.new("RGB", (80, 40), color=(255, 255, 255))
    meta = PngInfo()
    meta.add_text("MokoaText", "FACT habit.download_path=E:/Downloads\nport=9090")
    buffer = BytesIO()
    image.save(buffer, format="PNG", pnginfo=meta)

    payload = extract_document("settings.png", buffer.getvalue(), caption="设置页截图")
    assert payload["embedded_text_used"] is True
    assert "E:/Downloads" in payload["content"]
    assert "9090" in payload["content"]
    assert "用户说明：设置页截图" in payload["content"]

    result = IngestionPipeline().process_file(
        "settings.png",
        buffer.getvalue(),
        user_id="u_embed",
        caption="设置页截图",
    )
    assert result.accepted is True
    assert result.record is not None
    assert "E:/Downloads" in result.record.content


def test_extract_bpmn_uses_task_labels_not_xml_or_word_boxes() -> None:
    xml = (
        b"""<?xml version="1.0" encoding="UTF-8"?>
        <!-- pad so the file stays larger than a stub -->
        <definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
          <process id="Process_1">
            <startEvent id="start" name="Application received"/>
            <task id="t1" name="notify customer by email"/>
            <endEvent id="end" name="done"/>
          </process>
        </definitions>
        """
    )
    payload = extract_document("ex00_writer0001.bpmn", xml)
    assert payload["file_kind"] == "bpmn"
    assert "notify customer by email" in payload["content"]
    assert "file=ex00_writer0001.bpmn" in payload["content"]
    assert "<?xml" not in payload["content"]

    result = IngestionPipeline().process_file(
        "ex00_writer0001.bpmn",
        xml,
        user_id="u_bpmn",
    )
    assert result.accepted is True
    assert result.record is not None
    assert result.record.type == "document"
    assert "notify customer by email" in result.record.content

    try:
        extract_document(
            "ex00_writer0001.xml",
            b"<?xml version='1.0'?><annotation><object><name>letter</name></object></annotation>",
        )
    except FileExtractionError as exc:
        assert exc.code == "unsupported_file"
    else:
        raise AssertionError("word-box XML must not be indexed as BPMN")

