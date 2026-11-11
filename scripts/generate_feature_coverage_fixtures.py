#!/usr/bin/env python3
"""生成功能覆盖演示集所需的 10 个模拟附件（仅用标准库）。"""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "feature_coverage" / "attachments"
# 超大附件仍落到 data/（gitignore），避免把 8MB+ 文件提交进仓库
OVERSIZE_OUT = ROOT / "data" / "demo_coverage" / "attachments"


def _pdf(pages: list[str]) -> bytes:
    """生成可被 pypdf 抽取的简易多页 PDF。"""

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    )

    for page in pages:
        content_idx = len(objects) + 2  # 1-based later
        # page + content stream will be appended as next two objects
        page_obj_num = len(objects) + 1
        content_obj_num = page_obj_num + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_obj_num} 0 R /Resources << /Font << "
            f"/F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>".encode()
        )
        # Escape content for PDF literal string lines
        lines = []
        y = 740
        for raw in page.split("\n"):
            text = raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            # Helvetica 无法稳定显示中文；中文事实同时用 ASCII 关键字落地，便于抽文本验收
            lines.append(f"BT /F1 11 Tf 48 {y} Td ({text}) Tj ET")
            y -= 16
        stream = "\n".join(lines).encode("latin-1", errors="replace")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _docx(paragraphs: list[str]) -> bytes:
    """生成最小 OOXML .docx。"""

    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    body = []
    for p in paragraphs:
        body.append(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            f"{esc(p)}</w:t></w:r></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    from io import BytesIO

    bio = BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return bio.getvalue()


def _text_png(lines: list[str], *, bg=(248, 250, 252), fg=(15, 23, 42)) -> bytes:
    """生成可读截图式 PNG，并写入 MokoaText 明文层供无 OCR 环境抽取。"""

    plaintext = "\n".join(lines)
    try:
        from PIL import Image, ImageDraw, ImageFont
        from PIL.PngImagePlugin import PngInfo
    except ImportError:
        return _text_png_bitmap(lines, bg=bg, fg=fg, plaintext=plaintext)

    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 20)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    pad = 24
    line_h = 28
    width = 900
    height = pad * 2 + line_h * len(lines) + 16
    image = Image.new("RGB", (width, height), color=bg)
    draw = ImageDraw.Draw(image)
    y = pad
    for line in lines:
        draw.text((pad, y), line[:110], fill=fg, font=font)
        y += line_h

    meta = PngInfo()
    meta.add_text("MokoaText", plaintext)
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG", pnginfo=meta)
    return buffer.getvalue()


def _text_png_bitmap(
    lines: list[str],
    *,
    bg=(248, 250, 252),
    fg=(15, 23, 42),
    plaintext: str = "",
) -> bytes:
    """无 Pillow 时的点阵回退，并手工写入 PNG tEXt。"""

    # Minimal 5x7 font for A-Z 0-9 and common symbols
    FONT = {
        " ": [0, 0, 0, 0, 0, 0, 0],
        "-": [0, 0, 0, 31, 0, 0, 0],
        ".": [0, 0, 0, 0, 0, 0, 4],
        ":": [0, 4, 0, 0, 4, 0, 0],
        "/": [1, 2, 4, 8, 16, 0, 0],
        "0": [14, 17, 19, 21, 25, 17, 14],
        "1": [4, 12, 4, 4, 4, 4, 14],
        "2": [14, 17, 1, 2, 4, 8, 31],
        "3": [30, 1, 1, 14, 1, 1, 30],
        "4": [2, 6, 10, 18, 31, 2, 2],
        "5": [31, 16, 30, 1, 1, 17, 14],
        "6": [14, 16, 16, 30, 17, 17, 14],
        "7": [31, 1, 2, 4, 8, 8, 8],
        "8": [14, 17, 17, 14, 17, 17, 14],
        "9": [14, 17, 17, 15, 1, 1, 14],
        "A": [14, 17, 17, 31, 17, 17, 17],
        "B": [30, 17, 17, 30, 17, 17, 30],
        "C": [14, 17, 16, 16, 16, 17, 14],
        "D": [30, 17, 17, 17, 17, 17, 30],
        "E": [31, 16, 16, 30, 16, 16, 31],
        "F": [31, 16, 16, 30, 16, 16, 16],
        "G": [14, 17, 16, 19, 17, 17, 14],
        "H": [17, 17, 17, 31, 17, 17, 17],
        "I": [14, 4, 4, 4, 4, 4, 14],
        "J": [15, 2, 2, 2, 2, 18, 12],
        "K": [17, 18, 20, 24, 20, 18, 17],
        "L": [16, 16, 16, 16, 16, 16, 31],
        "M": [17, 27, 21, 21, 17, 17, 17],
        "N": [17, 25, 21, 19, 17, 17, 17],
        "O": [14, 17, 17, 17, 17, 17, 14],
        "P": [30, 17, 17, 30, 16, 16, 16],
        "Q": [14, 17, 17, 17, 21, 18, 13],
        "R": [30, 17, 17, 30, 20, 18, 17],
        "S": [15, 16, 16, 14, 1, 1, 30],
        "T": [31, 4, 4, 4, 4, 4, 4],
        "U": [17, 17, 17, 17, 17, 17, 14],
        "V": [17, 17, 17, 17, 17, 10, 4],
        "W": [17, 17, 17, 21, 21, 21, 10],
        "X": [17, 17, 10, 4, 10, 17, 17],
        "Y": [17, 17, 10, 4, 4, 4, 4],
        "Z": [31, 1, 2, 4, 8, 16, 31],
        "_": [0, 0, 0, 0, 0, 0, 31],
        "=": [0, 0, 31, 0, 31, 0, 0],
        "[": [14, 8, 8, 8, 8, 8, 14],
        "]": [14, 2, 2, 2, 2, 2, 14],
        "(": [4, 8, 8, 8, 8, 8, 4],
        ")": [4, 2, 2, 2, 2, 2, 4],
        ",": [0, 0, 0, 0, 4, 4, 8],
        "'": [4, 4, 0, 0, 0, 0, 0],
        "+": [0, 4, 4, 31, 4, 4, 0],
        "*": [0, 21, 14, 31, 14, 21, 0],
        "!": [4, 4, 4, 4, 4, 0, 4],
        "?": [14, 17, 1, 2, 4, 0, 4],
        "%": [25, 26, 2, 4, 8, 11, 19],
        "@": [14, 17, 19, 21, 19, 16, 14],
        "~": [0, 0, 8, 21, 2, 0, 0],
    }
    for c in "abcdefghijklmnopqrstuvwxyz":
        FONT[c] = FONT[c.upper()]

    scale = 2
    pad = 16
    line_h = 7 * scale + 8
    width = 720
    height = pad * 2 + line_h * len(lines) + 20
    pixels = [[bg for _ in range(width)] for _ in range(height)]

    def put_char(cx: int, cy: int, ch: str) -> None:
        glyph = FONT.get(ch, FONT["?"])
        for row, bits in enumerate(glyph):
            for col in range(5):
                if bits & (16 >> col):
                    for dy in range(scale):
                        for dx in range(scale):
                            x = cx + col * scale + dx
                            y = cy + row * scale + dy
                            if 0 <= x < width and 0 <= y < height:
                                pixels[y][x] = fg

    for i, line in enumerate(lines):
        x = pad
        y = pad + i * line_h
        for ch in line[:90]:
            put_char(x, y, ch)
            x += 6 * scale

    def paint(x: int, y: int):
        return pixels[y][x]

    png = _png(width, height, paint)
    return _png_with_text_chunk(png, "MokoaText", plaintext or "\n".join(lines))


def _png_with_text_chunk(png: bytes, key: str, value: str) -> bytes:
    """在 IHDR 后插入 tEXt 块。"""

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return png
    payload = key.encode("latin-1", errors="replace") + b"\x00" + value.encode("latin-1", errors="replace")
    chunk = (
        struct.pack(">I", len(payload))
        + b"tEXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF)
    )
    # 签名(8) + IHDR chunk = 8 + 4+4+13+4 = 33
    insert_at = 8 + (4 + 4 + 13 + 4)
    return png[:insert_at] + chunk + png[insert_at:]


def _png(width: int, height: int, paint) -> bytes:
    """paint(x,y)->(r,g,b) 生成真彩色 PNG。"""

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter none
        for x in range(width):
            raw.extend(paint(x, y))
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1-2 PDF
    (OUT / "01_xinghe_notes_manual.pdf").write_bytes(
        _pdf(
            [
                "XingHe Notes Project Manual v1.0\n"
                "Project: XingHe Notes / XingHeBiji\n"
                "Owner: Zhang San\n"
                "Default HTTP port: 8080\n"
                "Config file: /opt/xinghe/notes/config.yaml\n"
                "Start: systemctl start xinghe-notes\n"
                "Health: curl http://127.0.0.1:8080/health\n"
                "Data dir: /var/lib/xinghe/notes\n"
                "NOTE: This is the OLD manual. Port may be updated later.",
                "Ops tips\n"
                "1. Prefer systemd over nohup\n"
                "2. Log path: /var/log/xinghe/notes.log\n"
                "3. Backup before changing port\n"
                "4. Course Assistant is a DIFFERENT project",
            ]
        )
    )
    (OUT / "02_office_ops_guide.pdf").write_bytes(
        _pdf(
            [
                "Kylin Office Ops Guide\n"
                "Topic: Meeting room booking and printer share\n"
                "Printer share name: KYLIN-PRINT-01\n"
                "Default paper: A4\n"
                "Meeting room WiFi SSID: Kylin-Office-Guest\n"
                "VPN profile: office-vpn.ovpn\n"
                "File share: smb://files.office.local/teams",
                "FAQ\n"
                "Q: How to export PDF from WPS?\n"
                "A: File -> Export -> PDF\n"
                "Q: Where are downloaded meeting minutes?\n"
                "A: Follow habit.download_path preference if set",
            ]
        )
    )

    # 3-4 Word
    (OUT / "03_meeting_minutes.docx").write_bytes(
        _docx(
            [
                "星河笔记周会纪要 / XingHe Notes Weekly Minutes",
                "日期 / Date: 2026-09-05",
                "负责人 / Owner: 李四 / Li Si",
                "决议 1: 演示用户统一为 demo_user_a",
                "决议 2: 文档事实端口仍写 8080（旧手册），待变更通知生效",
                "决议 3: 课程助手项目 CourseAssistant 独立，端口 8080 可并存",
                "FACT owner=LiSi date=2026-09-05 project=XingHeNotes",
            ]
        )
    )
    (OUT / "04_xinghe_config_update.docx").write_bytes(
        _docx(
            [
                "星河笔记配置变更通知 / XingHe Notes Config Update",
                "生效日期 / Effective: 2026-09-08",
                "变更项: HTTP 端口 8080 -> 9090",
                "新健康检查: curl http://127.0.0.1:9090/health",
                "旧端口 8080 仅保留给课程助手 CourseAssistant",
                "负责人仍为 李四 / Owner remains Li Si",
                "FACT project=XingHeNotes port=9090 previous_port=8080",
                "FACT project=CourseAssistant port=8080",
            ]
        )
    )

    # 5-7 images
    (OUT / "05_terminal_error.png").write_bytes(
        _text_png(
            [
                "kylin@host:~/xinghe-notes$",
                "python app.py --port 8080",
                "Traceback (most recent call last):",
                "OSError: [Errno 98] Address already in use",
                "HINT: XingHe Notes should use port 9090 now",
                "CourseAssistant may still bind 8080",
            ]
        )
    )
    (OUT / "06_settings_page.png").write_bytes(
        _text_png(
            [
                "System Settings / Download",
                "Default download folder:",
                "E:/Downloads",
                "Language: zh-CN",
                "Auto-open: OFF",
                "FACT habit.download_path=E:/Downloads",
            ]
        )
    )
    (OUT / "07_ocr_scan_page.png").write_bytes(
        _text_png(
            [
                "SCANNED PAGE - OCR TEST",
                "Project XingHe Notes FAQ",
                "Q: health check URL?",
                "A: http://127.0.0.1:9090/health",
                "Q: who is owner?",
                "A: Li Si",
                "FACT health=9090 owner=LiSi",
            ]
        )
    )

    # 8-10 abnormal
    (OUT / "08_empty.pdf").write_bytes(b"")
    (OUT / "09_unsupported.xyz").write_bytes(b"not a supported document payload\n")
    OVERSIZE_OUT.mkdir(parents=True, exist_ok=True)
    oversized = OVERSIZE_OUT / "10_oversized_dummy.pdf"
    # 超过 8MB 上限；头部伪装 PDF 以便走大小校验
    header = b"%PDF-1.4\n% oversized fixture for reject test\n"
    padding = b"0" * (8 * 1024 * 1024 + 64 - len(header))
    oversized.write_bytes(header + padding)
    # 仓库内放占位说明，真正超大文件在 data/ 生成
    (OUT / "10_oversized_dummy.README.txt").write_text(
        "Run: python3 scripts/generate_feature_coverage_fixtures.py\n"
        f"Then use: {oversized}\n"
        "File is intentionally >8MB and kept out of git (data/).\n",
        encoding="utf-8",
    )

    manifest = OUT / "MANIFEST.txt"
    lines = ["# Feature coverage demo attachments", f"# dir: {OUT}", f"# oversized: {oversized}"]
    for path in sorted(OUT.iterdir()):
        if path.name == "MANIFEST.txt":
            continue
        lines.append(f"{path.name}\t{path.stat().st_size}\tbytes")
    lines.append(f"{oversized.name}\t{oversized.stat().st_size}\tbytes\t(data/, gitignored)")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
