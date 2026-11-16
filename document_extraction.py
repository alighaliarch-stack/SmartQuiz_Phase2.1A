"""Structured text extraction for teaching documents.

Every supported format is split into *passages* - the smallest unit the
format genuinely provides - and each passage gets a stable identifier:

    P<n>   paragraph n (DOCX: every non-empty paragraph, including headings,
           list items and table cells; TXT: blank-line separated blocks)
    PG<n>  PDF page n
    S<n>   PPTX slide n

The extracted text keeps the existing ``[[SOURCE: <label>]]`` marker format
(one marker per passage) so the AI generation pipeline and documents
extracted before this module existed stay compatible. Labels always start
with the passage ID, e.g. ``P12 · Paragraph 12 · Section: Tools``.

Nothing is invented: page numbers only come from PDFs, slide numbers only
from presentations, and headings only from real heading styles or outline
levels in the document.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
MAX_LABEL_CHARACTERS = 120
_MARKER_LINE = re.compile(r"(?m)^(\[\[SOURCE:)")
_HEADING_STYLE_NAME = re.compile(r"^heading\s*(\d)$")
_HEADING_STYLE_ID = re.compile(r"(?i)^heading(\d)$")


@dataclass
class Passage:
    id: str
    kind: str  # paragraph | heading | list_item | table_cell | page | slide
    text: str
    section_path: list[str] = field(default_factory=list)
    paragraph: int | None = None
    page: int | None = None
    slide: int | None = None
    heading_level: int | None = None
    list_level: int | None = None
    list_marker: str | None = None

    @property
    def label(self) -> str:
        if self.page is not None:
            location = f"Page {self.page}"
        elif self.slide is not None:
            location = f"Slide {self.slide}"
        else:
            location = f"Paragraph {self.paragraph}"
        label = f"{self.id} · {location}"
        if self.section_path:
            label += f" · Section: {self.section_path[-1]}"
        label = " ".join(label.replace("]]", ")").split())
        return label[:MAX_LABEL_CHARACTERS]

    def metadata(self) -> dict[str, Any]:
        """Location metadata stored on the document version (no passage text)."""
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "section_path": self.section_path,
            "paragraph": self.paragraph,
            "page": self.page,
            "slide": self.slide,
            "heading_level": self.heading_level,
            "list_level": self.list_level,
            "list_marker": self.list_marker,
        }


@dataclass
class ExtractionResult:
    text: str
    passages: list[Passage]
    status: str
    error: str | None


def extract_document(path: Path) -> ExtractionResult:
    suffix = path.suffix.lower()
    try:
        if suffix == ".txt":
            passages = _extract_txt(path)
        elif suffix == ".pdf":
            if path.read_bytes()[:5] != b"%PDF-":
                return ExtractionResult("", [], "ERROR", "Invalid PDF signature.")
            passages = _extract_pdf(path)
        elif suffix == ".docx":
            passages = _extract_docx(path)
        elif suffix == ".pptx":
            passages = _extract_pptx(path)
        else:
            return ExtractionResult("", [], "ERROR", f"Unsupported document extension: {suffix or '(none)'}.")
    except Exception as exc:
        return ExtractionResult(
            "", [], "ERROR", f"Could not extract text from the source document ({type(exc).__name__})."
        )

    passages = [p for p in passages if p.text.strip()]
    text = "\n\n".join(f"[[SOURCE: {p.label}]]\n{_neutralize_markers(p.text)}" for p in passages)
    if not text.strip():
        return ExtractionResult(
            "", [], "NO_TEXT", "No extractable text was found. Scanned PDFs require OCR before AI generation."
        )
    return ExtractionResult(text, passages, "EXTRACTED", None)


def _neutralize_markers(text: str) -> str:
    """A document line that looks like a source marker must not become one."""
    return _MARKER_LINE.sub(r" \1", text)


def _clean_block(text: str) -> str:
    """Normalise line endings and trailing spaces, keep line breaks and tabs."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n").strip(" ")


# ----------------------------------------------------------------------- TXT

def _extract_txt(path: Path) -> list[Passage]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="strict")
    blocks = [_clean_block(part) for part in re.split(r"\r?\n[ \t]*\r?\n", text)]
    blocks = [block for block in blocks if block.strip()]
    return [Passage(id=f"P{n}", kind="paragraph", text=block, paragraph=n) for n, block in enumerate(blocks, 1)]


# ----------------------------------------------------------------------- PDF

def _extract_pdf(path: Path) -> list[Passage]:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    passages = []
    for page_number, page in enumerate(reader.pages, 1):
        page_text = _clean_block(page.extract_text() or "")
        if page_text:
            passages.append(Passage(id=f"PG{page_number}", kind="page", text=page_text, page=page_number))
    return passages


# ---------------------------------------------------------------------- PPTX

def _extract_pptx(path: Path) -> list[Passage]:
    slide_pattern = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
    passages = []
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (int(match.group(1)), name)
            for name in archive.namelist()
            if (match := slide_pattern.match(name))
        )
        for slide_number, name in slides:
            root = ET.fromstring(archive.read(name))
            lines = []
            for paragraph in root.iter(f"{A}p"):
                parts = []
                for node in paragraph.iter():
                    if node.tag == f"{A}t":
                        parts.append(node.text or "")
                    elif node.tag == f"{A}br":
                        parts.append("\n")
                line = "".join(parts).strip()
                if not line:
                    continue
                properties = paragraph.find(f"{A}pPr")
                level = int(properties.get("lvl", "0")) if properties is not None else 0
                lines.append("  " * level + line)
            in_paragraphs = sum(1 for paragraph in root.iter(f"{A}p") for _ in paragraph.iter(f"{A}t"))
            if in_paragraphs < sum(1 for _ in root.iter(f"{A}t")):
                # Text runs outside any a:p (non-standard producers): keep every run, as before.
                lines = [" ".join((node.text or "").strip() for node in root.iter(f"{A}t") if (node.text or "").strip())]
            slide_text = _clean_block("\n".join(lines))
            if slide_text:
                passages.append(Passage(id=f"S{slide_number}", kind="slide", text=slide_text, slide=slide_number))
    return passages


# ---------------------------------------------------------------------- DOCX

def _val(element: ET.Element | None, default: str | None = None) -> str | None:
    if element is None:
        return default
    return element.get(f"{W}val", default)


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(archive.read(name))
    except KeyError:
        return None


def _docx_styles(root: ET.Element | None) -> dict[str, dict[str, Any]]:
    styles: dict[str, dict[str, Any]] = {}
    if root is None:
        return styles
    for style in root.iter(f"{W}style"):
        style_id = style.get(f"{W}styleId")
        if not style_id:
            continue
        outline = _val(style.find(f"{W}pPr/{W}outlineLvl"))
        num_pr = style.find(f"{W}pPr/{W}numPr")
        styles[style_id] = {
            "name": (_val(style.find(f"{W}name"), "") or "").strip().lower(),
            "based_on": _val(style.find(f"{W}basedOn")),
            "outline": int(outline) if outline is not None and outline.isdigit() else None,
            "num_id": _val(num_pr.find(f"{W}numId")) if num_pr is not None else None,
            "ilvl": _val(num_pr.find(f"{W}ilvl")) if num_pr is not None else None,
        }
    return styles


def _style_chain(styles: dict[str, dict[str, Any]], style_id: str | None) -> Iterator[tuple[str, dict[str, Any]]]:
    seen = set()
    while style_id and style_id not in seen and len(seen) < 20:
        seen.add(style_id)
        style = styles.get(style_id)
        if style is None:
            yield style_id, {}
            return
        yield style_id, style
        style_id = style.get("based_on")


def _heading_level(paragraph_props: ET.Element | None, style_id: str | None,
                   styles: dict[str, dict[str, Any]]) -> int | None:
    """Heading level from an explicit outline level, a heading style, or a Title style."""
    if paragraph_props is not None:
        outline = _val(paragraph_props.find(f"{W}outlineLvl"))
        if outline is not None and outline.isdigit():
            level = int(outline)
            return level + 1 if level < 9 else None
    for chain_id, style in _style_chain(styles, style_id):
        if style.get("outline") is not None:
            return style["outline"] + 1 if style["outline"] < 9 else None
        name = style.get("name", "")
        match = _HEADING_STYLE_NAME.match(name) or _HEADING_STYLE_ID.match(chain_id)
        if match:
            return int(match.group(1))
        if name == "title" or chain_id.lower() == "title":
            return 0
    return None


class _Numbering:
    """Resolves Word list numbering (numbering.xml) into visible list markers."""

    _ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
              (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]

    def __init__(self, root: ET.Element | None):
        self.levels: dict[str, dict[int, dict[str, Any]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.counters: dict[str, list[int | None]] = {}
        if root is None:
            return
        for abstract in root.iter(f"{W}abstractNum"):
            levels = {}
            for lvl in abstract.findall(f"{W}lvl"):
                ilvl = int(lvl.get(f"{W}ilvl", "0"))
                start = _val(lvl.find(f"{W}start"), "1") or "1"
                levels[ilvl] = {
                    "fmt": _val(lvl.find(f"{W}numFmt"), "decimal") or "decimal",
                    "text": _val(lvl.find(f"{W}lvlText"), "") or "",
                    "start": int(start) if start.isdigit() else 1,
                }
            self.levels[abstract.get(f"{W}abstractNumId", "")] = levels
        for num in root.iter(f"{W}num"):
            overrides = {}
            for override in num.findall(f"{W}lvlOverride"):
                start = _val(override.find(f"{W}startOverride"))
                if start is not None and start.isdigit():
                    overrides[int(override.get(f"{W}ilvl", "0"))] = int(start)
            self.nums[num.get(f"{W}numId", "")] = (_val(num.find(f"{W}abstractNumId"), "") or "", overrides)

    def _format(self, value: int, fmt: str) -> str:
        if fmt in ("lowerLetter", "upperLetter"):
            letters = ""
            while value > 0:
                value, remainder = divmod(value - 1, 26)
                letters = chr(97 + remainder) + letters
            return letters.upper() if fmt == "upperLetter" else letters
        if fmt in ("lowerRoman", "upperRoman"):
            roman, number = "", value
            for amount, numeral in self._ROMAN:
                while number >= amount:
                    roman += numeral
                    number -= amount
            return roman.upper() if fmt == "upperRoman" else roman
        if fmt == "decimalZero":
            return f"{value:02d}"
        return str(value)

    def marker(self, num_id: str, ilvl: int) -> str:
        abstract_id, overrides = self.nums.get(num_id, ("", {}))
        levels = self.levels.get(abstract_id, {})
        level = levels.get(ilvl)
        if level is None:
            return "•"  # list item whose numbering definition is missing
        if level["fmt"] == "bullet":
            return "•"
        if level["fmt"] == "none":
            return ""
        state = self.counters.setdefault(num_id, [None] * 9)
        start = overrides.get(ilvl, level["start"])
        state[ilvl] = start if state[ilvl] is None else state[ilvl] + 1
        for deeper in range(ilvl + 1, 9):
            state[deeper] = None

        def substitute(match: re.Match) -> str:
            referenced = int(match.group(1)) - 1
            ref_level = levels.get(referenced, {"fmt": "decimal", "start": 1})
            value = state[referenced] if state[referenced] is not None else overrides.get(referenced, ref_level["start"])
            return self._format(value, ref_level["fmt"])

        return re.sub(r"%(\d)", substitute, level["text"]) or f"{state[ilvl]}."


_SKIPPED_RUN_CONTENT = {f"{W}pPr", f"{W}rPr", f"{W}del", f"{W}delText", f"{W}instrText",
                        f"{W}txbxContent", f"{W}p", MC_FALLBACK}


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []

    def walk(element: ET.Element) -> None:
        for child in element:
            tag = child.tag
            if tag in _SKIPPED_RUN_CONTENT:
                continue  # properties, deleted text, field codes, nested paragraphs
            if tag == f"{W}t":
                parts.append(child.text or "")
            elif tag == f"{W}tab":
                parts.append("\t")
            elif tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
            elif tag == f"{W}noBreakHyphen":
                parts.append("-")
            else:
                walk(child)

    walk(paragraph)
    return _clean_block("".join(parts))


def _textboxes(paragraph: ET.Element) -> Iterator[ET.Element]:
    queue = list(paragraph)
    while queue:
        element = queue.pop(0)
        if element.tag == MC_FALLBACK:
            continue  # legacy duplicate of the same text box
        if element.tag == f"{W}txbxContent":
            yield element
            continue
        queue.extend(list(element))


def _iter_paragraphs(element: ET.Element, in_table: bool = False) -> Iterator[tuple[ET.Element, bool]]:
    for child in element:
        if child.tag == f"{W}p":
            yield child, in_table
            for box in _textboxes(child):
                yield from _iter_paragraphs(box, in_table)
        elif child.tag == f"{W}tbl":
            yield from _iter_paragraphs(child, True)
        elif child.tag in (f"{W}sectPr", MC_FALLBACK):
            continue
        else:
            yield from _iter_paragraphs(child, in_table)


def _extract_docx(path: Path) -> list[Passage]:
    with zipfile.ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        styles = _docx_styles(_read_xml(archive, "word/styles.xml"))
        numbering = _Numbering(_read_xml(archive, "word/numbering.xml"))

    body = document.find(f"{W}body")
    headings: dict[int, str] = {}
    passages: list[Passage] = []
    for paragraph, in_table in _iter_paragraphs(body if body is not None else document):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        props = paragraph.find(f"{W}pPr")
        style_id = _val(props.find(f"{W}pStyle")) if props is not None else None
        number = len(passages) + 1
        level = None if in_table else _heading_level(props, style_id, styles)

        if level is not None:
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = " ".join(text.split())[:100]
            passages.append(Passage(
                id=f"P{number}", kind="heading", text=text, paragraph=number,
                section_path=[headings[k] for k in sorted(headings)], heading_level=level,
            ))
            continue

        section_path = [headings[k] for k in sorted(headings)]
        num_pr = props.find(f"{W}numPr") if props is not None else None
        num_id = _val(num_pr.find(f"{W}numId")) if num_pr is not None else None
        ilvl_raw = _val(num_pr.find(f"{W}ilvl")) if num_pr is not None else None
        if num_id is None:
            for _, style in _style_chain(styles, style_id):
                if style.get("num_id"):
                    num_id, ilvl_raw = style["num_id"], ilvl_raw or style.get("ilvl")
                    break
        if num_id and num_id != "0":
            ilvl = int(ilvl_raw) if ilvl_raw and ilvl_raw.isdigit() else 0
            marker = numbering.marker(num_id, ilvl)
            indent = "  " * ilvl
            body_text = text.replace("\n", "\n" + indent + "  ")
            passages.append(Passage(
                id=f"P{number}", kind="list_item", text=f"{indent}{marker} {body_text}".rstrip() if marker else indent + body_text,
                paragraph=number, section_path=section_path, list_level=ilvl, list_marker=marker or None,
            ))
            continue

        passages.append(Passage(
            id=f"P{number}", kind="table_cell" if in_table else "paragraph", text=text,
            paragraph=number, section_path=section_path,
        ))
    return passages
