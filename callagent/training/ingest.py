"""Read uploaded training files and cut them into passages.

Supported: PDF, Word (.docx), PowerPoint (.pptx), HTML, CSV, plain text and Markdown.
Every passage keeps a label saying where it came from ("pricing.pdf, page 3"), so an
answer on a call can always be traced back to its source.
"""

from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# A Word or PowerPoint file is an archive. What it unpacks to is bounded too, so a small
# upload cannot expand into gigabytes in memory.
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000
MAX_PDF_PAGES = 600
EXTRACT_SECONDS = 300  # reading one PDF may take this long, checked page by page
MAX_CSV_ROWS = 2000
FILE_TYPES = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".csv": "csv",
    ".txt": "txt",
    ".md": "md",
}


class IngestError(ValueError):
    """The file cannot be used for training. The message is written for the person who uploaded it."""


@dataclass
class Section:
    """A run of text from one place in a document."""

    label: str  # "page 3", "slide 2", "Pricing", "" for the whole file
    text: str


@dataclass
class Passage:
    source_label: str
    text: str


def file_type_of(filename: str) -> str:
    """The supported type for this filename, or an IngestError naming what is accepted."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in FILE_TYPES:
        accepted = ", ".join(sorted(FILE_TYPES))
        raise IngestError(f"'{suffix or filename}' files are not supported. Upload one of: {accepted}")
    return FILE_TYPES[suffix]


def validate_upload(filename: str, data: bytes) -> str:
    """Check an upload before it is stored. Returns the file type."""
    file_type = file_type_of(filename)
    if not data:
        raise IngestError("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise IngestError(f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    # The name says one thing; make sure the bytes agree, so a renamed executable is refused.
    if file_type == "pdf" and not data.lstrip()[:5].startswith(b"%PDF-"):
        raise IngestError("this file is named .pdf but is not a PDF")
    if file_type in ("docx", "pptx"):
        if not data.startswith(b"PK"):
            raise IngestError(f"this file is named .{file_type} but is not an Office document")
        try:
            entries = zipfile.ZipFile(io.BytesIO(data)).infolist()
        except zipfile.BadZipFile:
            raise IngestError(f"this .{file_type} file is damaged and cannot be opened") from None
        names = [entry.filename for entry in entries]
        # The archive states how large each part becomes; reading stops at that size, so the sum is a true bound.
        if len(entries) > MAX_ARCHIVE_ENTRIES or sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
            raise IngestError(f"this .{file_type} file unpacks to more than {MAX_UNPACKED_BYTES // (1024 * 1024)} MB and was refused")
        marker = "word/" if file_type == "docx" else "ppt/"
        if not any(n.startswith(marker) for n in names):
            raise IngestError(f"this file is named .{file_type} but is a different kind of Office document")
    if file_type in ("txt", "md", "csv", "html") and b"\x00" in data[:4096]:
        raise IngestError("this looks like a binary file, not text")
    return file_type


# ----------------------------------------------------------------------------- extraction


def _decode(data: bytes) -> str:
    # UTF-16 only when the file says so with a byte order mark. Without one, any file of
    # even length "decodes" as UTF-16, and a French or Arabic Windows file became
    # unreadable text that was stored and searched as if it were content.
    utf16 = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ()
    for encoding in ("utf-8-sig", *utf16, "cp1256", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _page_ranges(numbers: list[int]) -> str:
    """[3, 4, 5, 9] -> "3-5, 9"."""
    ranges, start, previous = [], numbers[0], numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == previous + 1:
            previous = n
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        if n is not None:
            start = previous = n
    return ", ".join(ranges)


def _from_pdf(data: bytes, warnings: list[str] | None = None) -> list[Section]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    deadline = time.monotonic() + EXTRACT_SECONDS
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise IngestError("this PDF is password-protected; upload an unlocked copy")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise IngestError(f"this PDF has {len(reader.pages)} pages; the limit is {MAX_PDF_PAGES}. Split it and upload the parts")
        pages = []
        for i, page in enumerate(reader.pages):
            if time.monotonic() > deadline:
                raise IngestError(f"reading this PDF took longer than {EXTRACT_SECONDS // 60} minutes and was stopped at page {i + 1}; upload a simpler or smaller copy")
            pages.append((i + 1, _clean(page.extract_text() or "")))
    except PdfReadError as e:
        raise IngestError(f"this PDF could not be read ({e})") from None
    sections = [Section(f"page {n}", text) for n, text in pages if text]
    if pages and sum(len(s.text) for s in sections) < 20:
        raise IngestError(
            "no selectable text was found: this looks like a scanned PDF. Scanned documents are not "
            "supported yet; upload the original file or a text version"
        )
    blank = [n for n, text in pages if not text]
    if blank and warnings is not None:
        # Most often scanned pages inside an otherwise digital file. The agent will not know what they say.
        warnings.append(f"no text could be read from page{'s' if len(blank) > 1 else ''} {_page_ranges(blank)} of {len(pages)} (scanned or image-only); the agent does not know their content")
    return sections


def _from_docx(data: bytes) -> list[Section]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    sections: list[Section] = []
    heading, lines = "", []

    def flush() -> None:
        nonlocal lines
        text = _clean("\n".join(lines))
        if text:
            sections.append(Section(heading, text))
        lines = []

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            if (block.style.name or "").lower().startswith(("heading", "title")):
                flush()
                heading = text
            else:
                lines.append(text)
        elif isinstance(block, Table):
            for row in block.rows:
                cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
                if cells:
                    lines.append(" | ".join(cells))
    flush()
    return sections


def _from_pptx(data: bytes) -> list[Section]:
    from pptx import Presentation

    sections: list[Section] = []
    for number, slide in enumerate(Presentation(io.BytesIO(data)).slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Speaker notes: {notes}")
        text = _clean("\n".join(lines))
        if text:
            sections.append(Section(f"slide {number}", text))
    return sections


class _HTMLText(HTMLParser):
    """Visible text of a page, split at headings. Scripts, styles, navigation and footers are dropped."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe", "template", "aside"}
    BLOCK = {"p", "div", "li", "br", "tr", "section", "article", "table", "ul", "ol", "blockquote", "dd", "dt"}
    HEADINGS = {"h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sections: list[Section] = []
        self.links: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_heading = False
        self._heading = ""
        self._heading_buf: list[str] = []
        self._buf: list[str] = []

    def _flush(self) -> None:
        text = _clean(re.sub(r"[ \t]*\|[ \t]*(?=\n|$)", "", "".join(self._buf)))
        if text:
            self.sections.append(Section(self._heading, text))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.HEADINGS and not self._skip:
            self._flush()
            self._in_heading = True
            self._heading_buf = []
        elif tag in self.BLOCK:
            self._buf.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS and self._in_heading:
            self._in_heading = False
            self._heading = _clean("".join(self._heading_buf))
        elif tag in self.BLOCK or tag in ("td", "th"):
            self._buf.append("\n" if tag in self.BLOCK else " | ")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip:
            return
        elif self._in_heading:
            self._heading_buf.append(data)
        else:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_html(html: str) -> tuple[str, list[Section], list[str]]:
    """(title, sections, links) of an HTML page."""
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return _clean(parser.title), parser.sections, parser.links


def _from_csv(data: bytes) -> list[Section]:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    header, body = [h.strip() for h in rows[0]], rows[1 : MAX_CSV_ROWS + 1]
    lines = []
    for row in body:
        pairs = [f"{header[i] if i < len(header) and header[i] else f'column {i + 1}'}: {v.strip()}" for i, v in enumerate(row) if v.strip()]
        if pairs:
            lines.append("; ".join(pairs))
    return [Section("", "\n".join(lines))] if lines else []


def extract_sections(file_type: str, data: bytes, warnings: list[str] | None = None) -> list[Section]:
    """All readable text of a file, in reading order. Raises IngestError when there is none.
    What was left out of an otherwise readable file is added to ``warnings``."""
    if file_type == "pdf":
        sections = _from_pdf(data, warnings)
    elif file_type == "docx":
        sections = _from_docx(data)
    elif file_type == "pptx":
        sections = _from_pptx(data)
    elif file_type == "html":
        _title, sections, _links = parse_html(_decode(data))
    elif file_type == "csv":
        sections = _from_csv(data)
    elif file_type in ("txt", "md"):
        sections = [Section("", _clean(_decode(data)))]
    else:
        raise IngestError(f"unsupported file type: {file_type}")
    sections = [s for s in sections if s.text.strip()]
    if not sections:
        raise IngestError("no text could be read from this file")
    return sections


# ----------------------------------------------------------------------------- chunking

_SENTENCE_END = re.compile(r"(?<=[.!?؟。])\s+")


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break one over-long paragraph at sentence ends, then at spaces as a last resort."""
    pieces, current = [], ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append((current + " " + sentence[:cut]).strip() if current else sentence[:cut].strip())
            current, sentence = "", sentence[cut:].strip()
        if len(current) + len(sentence) + 1 > limit and current:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_sections(sections: list[Section], document_name: str, target_chars: int = 900, overlap_chars: int = 120) -> list[Passage]:
    """Pack each section's paragraphs into passages of about ``target_chars``.

    Passages never span two sections (a page, a slide, a heading), so the source label
    stays exact. Consecutive passages of one section share a short overlap so a fact
    split across the boundary is still found whole in one of them.
    """
    passages: list[Passage] = []
    for section in sections:
        label = f"{document_name}, {section.label}" if section.label else document_name
        paragraphs: list[str] = []
        for paragraph in re.split(r"\n\s*\n", section.text):
            paragraph = paragraph.strip()
            if paragraph:
                paragraphs.extend(_split_long(paragraph, target_chars) if len(paragraph) > target_chars else [paragraph])
        current = ""
        for paragraph in paragraphs:
            if current and len(current) + len(paragraph) + 2 > target_chars:
                passages.append(Passage(label, current))
                tail = current[-overlap_chars:]
                tail = tail[tail.find(" ") + 1 :] if " " in tail else ""
                current = f"{tail}\n\n{paragraph}" if tail else paragraph
            else:
                current = f"{current}\n\n{paragraph}" if current else paragraph
        if current:
            passages.append(Passage(label, current))
    return passages
