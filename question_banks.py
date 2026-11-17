"""Phase 4: teacher-authored question banks.

This module holds the pure logic (no web or database code):

* reading Word (.docx) and PowerPoint (.pptx) question documents into
  ordered source blocks that keep table rows together,
* parsing numbered questions, labelled options and a separate instructor
  answer key into structured questions,
* validating a question for use in a quiz,
* server-side random selection, option permutation and quiz timing.

Source fidelity is a design rule, not a best effort. Extraction is
deterministic: every stored question text, option, answer and
explanation is a verbatim slice of the document (only the leading
"1." / "A." numbering is split off and kept separately). Nothing is
translated, paraphrased, corrected or generated. Whenever the structure
is uncertain, the question is flagged for the teacher instead of guessed.
The supplied answer key is authoritative.

Passage IDs match ``document_extraction`` exactly (P<n> for each
non-empty DOCX paragraph, S<n> for a PPTX slide) so the teacher sees the
same citations everywhere in SmartQuiz.
"""

from __future__ import annotations

import json
import re
import secrets
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from document_extraction import (
    A,
    MC_FALLBACK,
    W,
    _Numbering,
    _docx_styles,
    _heading_level,
    _paragraph_text,
    _read_xml,
    _style_chain,
    _textboxes,
    _val,
)

SUPPORTED_EXTENSIONS = {".docx", ".pptx"}
QUESTION_TYPES = ("mcq_single", "mcq_multiple", "true_false", "short_answer")
MAX_OPTIONS = 8
MAX_TEXT_CHARACTERS = 4000
MAX_OPTION_CHARACTERS = 1000
MAX_QUIZ_TOTAL_SECONDS = 600          # 10 minutes for a whole bank-assembled quiz
QUESTION_MIN_SECONDS = 15             # same per-question bounds as the manual builder
QUESTION_MAX_SECONDS = 1200

# "1. text", "1) text", "Q1. text", "Q1: text", "Question 1 - text", "Question 1"
_QUESTION_RE = re.compile(
    r"^\s*(?:(?:Q|Question)\s*(\d{1,3})\s*(?:[.):\]]|\s[-–—])?\s*|(\d{1,3})\s*[.)\]]\s+)(.*)$",
    re.IGNORECASE | re.DOTALL,
)
# "A. text", "a) text", "(B) text", "C: text"
_OPTION_RE = re.compile(r"^\s*\(?([A-Ha-h])\s*[.)\]:]\s+(\S.*)$", re.DOTALL)
_INLINE_ANSWER_RE = re.compile(r"^\s*(?:correct\s+)?answer\s*[:\-–]\s*(.+)$", re.IGNORECASE | re.DOTALL)
_INLINE_EXPLANATION_RE = re.compile(
    r"^\s*(?:explanation|rationale|reason|feedback)\s*[:\-–]\s*(.+)$", re.IGNORECASE | re.DOTALL
)
_GUIDANCE_RE = re.compile(r"^\s*(?:marking\s+guidance|marking\s+notes?|rubric)\s*[:\-–]\s*(.+)$",
                          re.IGNORECASE | re.DOTALL)
_ANSWER_KEY_HEADING_RE = re.compile(
    r"^\W*(?:(?:instructor|teacher|tutor)(?:'s|s)?\s+)?(?:answer\s*key|answers|marking\s+(?:scheme|guide)"
    r"|memo(?:randum)?|solutions?)\s*(?:[:(\-–—].*)?$",
    re.IGNORECASE | re.DOTALL,
)
_KEY_LINE_RE = re.compile(
    r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,3})\s*(?:[.):\-–—]\s*|\s+)(?:(?:correct\s+)?answer\s*[:\-–]?\s*)?(\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
# Upper-case option letters ("B", "A, C", "B and D", "(C)") optionally
# followed by an explanation. A lone lower-case letter ("b", "c.") is also
# accepted, but never a lower-case word such as "a computer".
_LETTER_ANSWER_RE = re.compile(
    r"^\(?([A-H])\)?(?:\s*(?:,|/|&|and)\s*\(?([A-H])\)?)*(?=$|[\s.:;)\-–—])"
)
_LOWER_LETTER_ANSWER_RE = re.compile(r"^\(?([a-h])\)?\.?$")
_SEPARATOR_PREFIX_RE = re.compile(r"^[\s.:;)\-–—]+")
_TRUE_FALSE_RE = re.compile(r"^(true|false)\b", re.IGNORECASE)
# A whole key entry that is True/False, optionally followed by a separator
# and an explanation. "True love is ..." is NOT matched (no separator).
_TRUE_FALSE_KEY_RE = re.compile(r"^(true|false)\s*(?:$|[.:;)\-–—]\s*(.*)$)", re.IGNORECASE | re.DOTALL)
_MULTI_SELECT_HINT_RE = re.compile(
    r"select\s+all|choose\s+(?:all|two|three|\d)|all\s+that\s+apply|more\s+than\s+one", re.IGNORECASE
)
_HEADER_NUMBER = {"q", "q.", "no", "no.", "#", "number", "question", "question no", "question no.", "qn", "item"}
_HEADER_ANSWER = {"answer", "answers", "correct answer", "correct", "key", "answer key", "correct option",
                  "solution", "expected answer", "model answer"}
_HEADER_EXPLANATION = {"explanation", "explanations", "rationale", "reason", "feedback", "why",
                       "notes", "comment", "comments"}
_HEADER_GUIDANCE = {"marking guidance", "marking notes", "marking", "rubric", "guidance"}


# --------------------------------------------------------------------- blocks

@dataclass
class SourceBlock:
    """One readable unit of the document, in reading order."""

    passage_id: str            # P<n> (DOCX paragraph) or S<n> (PPTX slide)
    text: str
    kind: str                  # heading | paragraph | list_item | table_row | slide_line
    cells: list[str] | None = None          # table_row only
    cell_passage_ids: list[str] | None = None
    table: int | None = None
    slide: int | None = None
    heading_level: int | None = None

    @property
    def label(self) -> str:
        if self.slide is not None:
            return f"{self.passage_id} · Slide {self.slide}"
        return f"{self.passage_id} · Paragraph {self.passage_id[1:]}"


def read_blocks(path: Path) -> list[SourceBlock]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _docx_blocks(path)
    if suffix == ".pptx":
        return _pptx_blocks(path)
    raise ValueError(f"Unsupported question document type: {suffix or '(none)'}")


def _docx_blocks(path: Path) -> list[SourceBlock]:
    """Same paragraph walk and numbering as document_extraction._extract_docx,
    but a table row is kept together as one block (needed for answer keys)."""
    with zipfile.ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        styles = _docx_styles(_read_xml(archive, "word/styles.xml"))
        numbering = _Numbering(_read_xml(archive, "word/numbering.xml"))

    blocks: list[SourceBlock] = []
    counter = {"p": 0, "table": 0}

    def paragraph_block(paragraph: ET.Element, in_table: bool) -> tuple[str, str, str, int | None] | None:
        text = _paragraph_text(paragraph)
        if not text:
            return None
        counter["p"] += 1
        pid = f"P{counter['p']}"
        props = paragraph.find(f"{W}pPr")
        style_id = _val(props.find(f"{W}pStyle")) if props is not None else None
        level = None if in_table else _heading_level(props, style_id, styles)
        if level is not None:
            return pid, "heading", text, level
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
            # Word auto-numbering: the visible "1." / "A." is part of what the
            # teacher wrote, so it is restored here exactly as displayed.
            if marker and marker != "•":
                return pid, "list_item", f"{marker} {text}", None
            return pid, "list_item", text, None
        return pid, ("table_cell" if in_table else "paragraph"), text, None

    def walk(element: ET.Element, in_table: bool, sink: list | None) -> None:
        for child in element:
            if child.tag == f"{W}p":
                block = paragraph_block(child, in_table)
                if block is not None:
                    if sink is not None:
                        sink.append(block)
                    else:
                        pid, kind, text, level = block
                        blocks.append(SourceBlock(pid, text, kind, heading_level=level))
                for box in _textboxes(child):
                    walk(box, in_table, sink)
            elif child.tag == f"{W}tbl":
                walk_table(child)
            elif child.tag in (f"{W}sectPr", MC_FALLBACK):
                continue
            else:
                walk(child, in_table, sink)

    def walk_table(table: ET.Element) -> None:
        counter["table"] += 1
        table_number = counter["table"]
        for row in _table_rows(table):
            cells: list[str] = []
            cell_ids: list[str] = []
            for cell in row.findall(f"{W}tc"):
                collected: list = []
                walk(cell, True, collected)
                cells.append("\n".join(item[2] for item in collected))
                cell_ids.append(collected[0][0] if collected else "")
            if any(cell.strip() for cell in cells):
                first = next(pid for pid in cell_ids if pid)
                blocks.append(SourceBlock(first, " | ".join(cells), "table_row", cells=cells,
                                          cell_passage_ids=cell_ids, table=table_number))

    body = document.find(f"{W}body")
    walk(body if body is not None else document, False, None)
    return blocks


def _table_rows(table: ET.Element) -> Iterable[ET.Element]:
    for child in table:
        if child.tag == f"{W}tr":
            yield child
        elif child.tag not in (f"{W}tblPr", f"{W}tblGrid"):
            yield from _table_rows(child)  # rows wrapped in content controls


def _pptx_blocks(path: Path) -> list[SourceBlock]:
    slide_pattern = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
    blocks: list[SourceBlock] = []
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (int(match.group(1)), name)
            for name in archive.namelist()
            if (match := slide_pattern.match(name))
        )
        table_number = 0
        for slide_number, name in slides:
            root = ET.fromstring(archive.read(name))
            consumed: set[int] = set()
            pid = f"S{slide_number}"
            for element in root.iter():
                if element.tag == f"{A}tbl":
                    table_number += 1
                elif element.tag == f"{A}tr":
                    cells = []
                    for cell in element.findall(f"{A}tc"):
                        lines = []
                        for paragraph in cell.iter(f"{A}p"):
                            consumed.add(id(paragraph))
                            line = _pptx_paragraph_text(paragraph)
                            if line:
                                lines.append(line)
                        cells.append("\n".join(lines))
                    if any(cells):
                        blocks.append(SourceBlock(pid, " | ".join(cells), "table_row", cells=cells,
                                                  cell_passage_ids=[pid] * len(cells), table=table_number,
                                                  slide=slide_number))
                elif element.tag == f"{A}p" and id(element) not in consumed:
                    line = _pptx_paragraph_text(element)
                    if line:
                        blocks.append(SourceBlock(pid, line, "slide_line", slide=slide_number))
    return blocks


def _pptx_paragraph_text(paragraph: ET.Element) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{A}t":
            parts.append(node.text or "")
        elif node.tag == f"{A}br":
            parts.append("\n")
    return "".join(parts).strip()


# ------------------------------------------------------------------- parsing

@dataclass
class ParsedQuestion:
    number: str | None
    text_lines: list[str] = field(default_factory=list)
    options: list[tuple[str, str]] = field(default_factory=list)   # (label, text)
    passage_ids: list[str] = field(default_factory=list)
    slide: int | None = None
    inline_answer: str | None = None
    inline_explanation: str | None = None
    inline_guidance: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.text_lines).strip()


@dataclass
class KeyEntry:
    number: str
    answer: str
    explanation: str = ""
    guidance: str = ""
    passage_ids: list[str] = field(default_factory=list)


@dataclass
class ImportedQuestion:
    """A parsed question in the shape stored as a BankQuestion."""

    position: int
    question_number: str | None
    question_text: str
    question_type: str
    options: list[str]
    option_labels: list[str]
    correct_answer: Any
    answer_key_raw: str | None
    explanation: str
    marking_guidance: str
    source_passage_ids: list[str]
    answer_passage_ids: list[str]
    warnings: list[str]

    def original_snapshot(self) -> dict:
        return {
            "question_number": self.question_number,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "options": self.options,
            "option_labels": self.option_labels,
            "correct_answer": self.correct_answer,
            "answer_key_raw": self.answer_key_raw,
            "explanation": self.explanation,
            "marking_guidance": self.marking_guidance,
        }


@dataclass
class ImportResult:
    questions: list[ImportedQuestion]
    warnings: list[str]
    preamble: list[str]
    suggested_title: str
    passages: list[dict]


def _is_answer_key_start(block: SourceBlock, questions_seen: int) -> bool:
    if questions_seen == 0:
        return False
    text = " ".join(block.text.split())
    if block.kind == "table_row":
        return _header_columns(block.cells or []) is not None
    if _QUESTION_RE.match(text) or _OPTION_RE.match(text):
        return False
    if block.kind != "heading" and len(text) > 80:
        return False
    return bool(_ANSWER_KEY_HEADING_RE.match(text))


def _normalise_header(cell: str) -> str:
    return " ".join(cell.strip().lower().rstrip(":").split())


def _header_columns(cells: list[str]) -> dict[str, int] | None:
    """Column roles for an answer-key table header row, or None if not a header."""
    roles: dict[str, int] = {}
    for index, cell in enumerate(cells):
        name = _normalise_header(cell)
        if name in _HEADER_NUMBER and "number" not in roles:
            roles["number"] = index
        elif name in _HEADER_ANSWER and "answer" not in roles:
            roles["answer"] = index
        elif name in _HEADER_EXPLANATION and "explanation" not in roles:
            roles["explanation"] = index
        elif name in _HEADER_GUIDANCE and "guidance" not in roles:
            roles["guidance"] = index
    return roles if "answer" in roles else None


def parse_blocks(blocks: list[SourceBlock], filename: str = "") -> ImportResult:
    questions: list[ParsedQuestion] = []
    preamble: list[str] = []
    bank_warnings: list[str] = []
    key_entries: list[KeyEntry] = []
    key_mode = False
    key_columns: dict[str, int] | None = None
    current: ParsedQuestion | None = None
    pending: list[tuple[str, str, int | None]] = []   # non-question lines (pid, text, slide)
    title_candidates: list[str] = []

    def close_current() -> None:
        nonlocal current
        if current is not None:
            questions.append(current)
        current = None

    def flush_pending_to_preamble() -> None:
        for _, text, _ in pending:
            preamble.append(text)
        pending.clear()

    for block in blocks:
        if block.kind == "heading" and not questions and current is None:
            title_candidates.append(" ".join(block.text.split()))

        if not key_mode and _is_answer_key_start(block, len(questions) + (1 if current else 0)):
            close_current()
            flush_pending_to_preamble()
            key_mode = True
            if block.kind == "table_row":
                key_columns = _header_columns(block.cells or [])
            continue

        if key_mode:
            if block.kind == "table_row":
                cells = block.cells or []
                header = _header_columns(cells)
                if header is not None:
                    key_columns = header
                    continue
                entry = _key_entry_from_row(cells, block.cell_passage_ids or [], key_columns)
                if entry is not None:
                    key_entries.append(entry)
                elif any(cell.strip() for cell in cells):
                    bank_warnings.append(f"Answer-key row not understood ({block.passage_id}): {block.text[:160]}")
                continue
            for line in _lines(block.text):
                explanation = _INLINE_EXPLANATION_RE.match(line)
                if explanation and key_entries:
                    key_entries[-1].explanation = _join(key_entries[-1].explanation, explanation.group(1).strip())
                    key_entries[-1].passage_ids.append(block.passage_id)
                    continue
                guidance = _GUIDANCE_RE.match(line)
                if guidance and key_entries:
                    key_entries[-1].guidance = _join(key_entries[-1].guidance, guidance.group(1).strip())
                    key_entries[-1].passage_ids.append(block.passage_id)
                    continue
                match = _KEY_LINE_RE.match(line)
                if match:
                    key_entries.append(KeyEntry(str(int(match.group(1))), match.group(2).strip(),
                                                passage_ids=[block.passage_id]))
                elif _ANSWER_KEY_HEADING_RE.match(line) or not line.strip():
                    continue
                else:
                    bank_warnings.append(f"Answer-key text not understood ({block.passage_id}): {line[:160]}")
            continue

        # ------------------------------------------------- question section
        if block.slide is not None and current is not None and current.slide is not None \
                and block.slide != current.slide and current.options:
            close_current()  # a new slide starts a new question once options were seen
        if block.kind == "table_row":
            lines = [cell for cell in (block.cells or []) if cell.strip()]
        else:
            lines = _lines(block.text)
        for line in lines:
            option = _OPTION_RE.match(line)
            question = _QUESTION_RE.match(line)
            if question:
                number = question.group(1) or question.group(2)
                close_current()
                flush_pending_to_preamble()
                current = ParsedQuestion(str(int(number)), slide=block.slide)
                body = question.group(3).strip()
                if body:
                    current.text_lines.append(body)
                current.passage_ids.append(block.passage_id)
                continue
            if option:
                label, text = option.group(1).upper(), option.group(2).strip()
                if current is None or (current.options and block.slide is not None
                                       and current.slide is not None and block.slide != current.slide):
                    close_current()
                    current = _unnumbered_question(pending, block, filename)
                    if current is None:
                        preamble.append(line)
                        continue
                current.options.append((label, text))
                _add_pid(current, block.passage_id)
                continue
            if current is not None:
                answer = _INLINE_ANSWER_RE.match(line)
                if answer:
                    current.inline_answer = answer.group(1).strip()
                    _add_pid(current, block.passage_id)
                    continue
                explanation = _INLINE_EXPLANATION_RE.match(line)
                if explanation:
                    current.inline_explanation = _join(current.inline_explanation or "",
                                                       explanation.group(1).strip())
                    _add_pid(current, block.passage_id)
                    continue
                guidance = _GUIDANCE_RE.match(line)
                if guidance:
                    current.inline_guidance = _join(current.inline_guidance or "", guidance.group(1).strip())
                    _add_pid(current, block.passage_id)
                    continue
                if not current.options and current.inline_answer is None:
                    current.text_lines.append(line)   # question continues on the next line
                    _add_pid(current, block.passage_id)
                    continue
                if block.slide is not None and block.slide != current.slide:
                    close_current()
                    pending.append((block.passage_id, line, block.slide))
                    continue
                # Text after the options that is neither an option nor an
                # answer: never dropped silently and never guessed into place.
                current.warnings.append(
                    f"Unrecognised text after the options ({block.passage_id}): \"{line[:200]}\". "
                    "Check the original document."
                )
                _add_pid(current, block.passage_id)
                continue
            pending.append((block.passage_id, line, block.slide))

    close_current()
    flush_pending_to_preamble()
    imported, merge_warnings = _merge(questions, key_entries)
    bank_warnings.extend(merge_warnings)
    if not imported:
        bank_warnings.append("No numbered questions were recognised in this document.")
    title = title_candidates[0] if title_candidates else (Path(filename).stem.replace("_", " ") if filename else "")
    passages = _passages(blocks)
    return ImportResult(imported, bank_warnings, preamble, title[:200], passages)


def _unnumbered_question(pending: list, block: SourceBlock, filename: str) -> ParsedQuestion | None:
    """Options appeared without a numbered question line.

    On a slide, the slide's earlier text is the question; in Word, the
    immediately preceding line is. Always flagged for teacher review."""
    if not pending:
        return None
    if block.slide is not None:
        lines = [item for item in pending if item[2] == block.slide]
    else:
        lines = pending[-1:]
    if not lines:
        return None
    for item in lines:
        pending.remove(item)
    question = ParsedQuestion(None, slide=block.slide)
    question.text_lines = [text for _, text, _ in lines]
    question.passage_ids = list(dict.fromkeys(pid for pid, _, _ in lines))
    question.warnings.append(
        "No question number was found, so the question wording was taken from the text just before the "
        "options. Confirm the wording against the original."
    )
    return question


def _key_entry_from_row(cells: list[str], cell_ids: list[str], columns: dict[str, int] | None) -> KeyEntry | None:
    def cell(role: str, fallback: int | None) -> str:
        index = columns.get(role) if columns else fallback
        if index is None or index >= len(cells):
            return ""
        return cells[index].strip()

    number_raw = cell("number", 0)
    match = re.match(r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,3})\s*[.)]?\s*$", number_raw, re.IGNORECASE)
    if not match:
        return None
    answer = cell("answer", 1)
    explanation = cell("explanation", 2)
    guidance = cell("guidance", None) if columns else ""
    ids = [pid for pid in cell_ids if pid]
    return KeyEntry(str(int(match.group(1))), answer, explanation, guidance, list(dict.fromkeys(ids)))


def _merge(questions: list[ParsedQuestion], key_entries: list[KeyEntry]) -> tuple[list[ImportedQuestion], list[str]]:
    warnings: list[str] = []
    keys: dict[str, KeyEntry] = {}
    for entry in key_entries:
        if entry.number in keys:
            warnings.append(f"The answer key lists question {entry.number} more than once; the first entry is used.")
            continue
        keys[entry.number] = entry

    numbers = [q.number for q in questions if q.number is not None]
    duplicates = {n for n in numbers if numbers.count(n) > 1}
    for number in sorted(duplicates, key=int):
        warnings.append(f"Question number {number} appears more than once in the document.")
    if numbers:
        expected = set(range(1, max(int(n) for n in numbers) + 1))
        missing = sorted(expected - {int(n) for n in numbers})
        if missing:
            warnings.append("Question numbers missing from the sequence: " + ", ".join(map(str, missing)) + ".")
    question_numbers = set(numbers)
    for number in keys:
        if number not in question_numbers:
            warnings.append(f"The answer key has an entry for question {number}, but no question {number} was found.")

    imported = []
    for position, parsed in enumerate(questions, 1):
        entry = keys.get(parsed.number) if parsed.number is not None and parsed.number not in duplicates else None
        imported.append(_build_question(position, parsed, entry, parsed.number in duplicates))
    return imported, warnings


def _build_question(position: int, parsed: ParsedQuestion, entry: KeyEntry | None, duplicate_number: bool) -> ImportedQuestion:
    warnings = list(parsed.warnings)
    labels = [label for label, _ in parsed.options]
    options = [text for _, text in parsed.options]
    raw_answer = entry.answer if entry and entry.answer else parsed.inline_answer
    explanation = (entry.explanation if entry else "") or (parsed.inline_explanation or "")
    guidance = (entry.guidance if entry else "") or (parsed.inline_guidance or "")
    answer_pids = list(entry.passage_ids) if entry else []
    if entry and parsed.inline_answer and entry.answer and entry.answer.strip() != parsed.inline_answer.strip():
        warnings.append(
            f"The answer key says \"{entry.answer}\" but the answer under the question says "
            f"\"{parsed.inline_answer}\". The answer key was used; confirm which is right."
        )
    if duplicate_number:
        warnings.append("This question number is used more than once, so its answer could not be matched safely.")

    if not parsed.text:
        warnings.append("The question wording is empty.")
    if len(labels) != len(set(labels)):
        warnings.append("An option label is repeated; two questions may have been merged. Check the original.")
    elif labels and labels != [chr(ord("A") + i) for i in range(len(labels))]:
        warnings.append("Option labels are not consecutive (" + ", ".join(labels) + "); an option may be missing.")
    if len(options) > MAX_OPTIONS:
        warnings.append(f"More than {MAX_OPTIONS} options were found.")
    if options and len({o.casefold() for o in options}) != len(options):
        warnings.append("Two options have identical wording.")

    correct: Any = None
    question_type = "mcq_single"
    if options and {o.strip().casefold() for o in options} == {"true", "false"} and len(options) == 2:
        question_type = "true_false"
    elif not options:
        question_type = "short_answer"

    mapped, rest = (None, "")
    if question_type in ("true_false", "mcq_single"):
        mapped, rest = _resolve_letters_or_text(raw_answer, labels, options)
        if rest and not explanation:
            explanation = rest   # "B – because ..." in a paragraph-style key (verbatim)
    if question_type == "true_false":
        if mapped is not None and len(mapped) == 1:
            correct = options[mapped[0]].strip().capitalize()
        elif raw_answer and _TRUE_FALSE_RE.match(raw_answer.strip()):
            tf = _TRUE_FALSE_RE.match(raw_answer.strip())
            correct = tf.group(1).capitalize()
            tail = _SEPARATOR_PREFIX_RE.sub("", raw_answer.strip()[tf.end():])
            if tail and not explanation:
                explanation = tail
        options = ["True", "False"]
    elif question_type == "short_answer":
        tf_key = _TRUE_FALSE_KEY_RE.match(raw_answer.strip()) if raw_answer else None
        if tf_key:
            # "False" or "False – because ..." for a statement with no options.
            question_type = "true_false"
            options = ["True", "False"]
            correct = tf_key.group(1).capitalize()
            if tf_key.group(2) and not explanation:
                explanation = tf_key.group(2).strip()
        elif raw_answer:
            correct = [raw_answer.strip()]
        if question_type == "short_answer" and not parsed.text_lines:
            warnings.append("No options and no question wording were found.")
    else:
        if mapped and len(mapped) > 1:
            question_type = "mcq_multiple"
            correct = [options[i] for i in mapped]
        elif mapped:
            correct = options[mapped[0]]
            if _MULTI_SELECT_HINT_RE.search(parsed.text):
                question_type = "mcq_multiple"
                correct = [options[mapped[0]]]
                warnings.append("The wording suggests several answers may be correct, but the key gives one. "
                                "Confirm the question type.")
        elif _MULTI_SELECT_HINT_RE.search(parsed.text):
            question_type = "mcq_multiple"
        if raw_answer and mapped is None:
            warnings.append(
                f"The answer key value \"{raw_answer}\" does not match any option label or option wording."
            )

    if raw_answer is None:
        warnings.append("No answer was found in the answer key for this question.")

    ids = list(dict.fromkeys(parsed.passage_ids))
    return ImportedQuestion(
        position=position,
        question_number=parsed.number,
        question_text=parsed.text,
        question_type=question_type,
        options=options,
        option_labels=labels if question_type in ("mcq_single", "mcq_multiple") else (labels or []),
        correct_answer=correct,
        answer_key_raw=raw_answer,
        explanation=explanation,
        marking_guidance=guidance,
        source_passage_ids=ids,
        answer_passage_ids=answer_pids,
        warnings=warnings,
    )


def _resolve_letters_or_text(raw: str | None, labels: list[str], options: list[str]) -> tuple[list[int] | None, str]:
    """Map the key's answer to option indexes.

    Returns (indexes, remaining_text): indexes is None when the answer
    cannot be matched to the options; remaining_text is any explanation
    written after the letters, verbatim."""
    if not raw:
        return None, ""
    value = raw.strip()
    lower = _LOWER_LETTER_ANSWER_RE.match(value)
    match = _LETTER_ANSWER_RE.match(value)
    if match or lower:
        letters = [lower.group(1).upper()] if lower else re.findall(r"[A-H]", match.group(0))
        rest = "" if lower else _SEPARATOR_PREFIX_RE.sub("", value[match.end():])
        indexes = []
        for letter in letters:
            if letter not in labels:
                return None, ""
            indexes.append(labels.index(letter))
        return list(dict.fromkeys(indexes)), rest
    folded = value.casefold().rstrip(".")
    for index, option in enumerate(options):
        if option.strip().casefold().rstrip(".") == folded:
            return [index], ""
    return None, ""


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def _join(existing: str, addition: str) -> str:
    return f"{existing}\n{addition}".strip() if existing else addition


def _add_pid(question: ParsedQuestion, pid: str) -> None:
    if pid not in question.passage_ids:
        question.passage_ids.append(pid)


def _passages(blocks: list[SourceBlock]) -> list[dict]:
    """Teacher-only evidence for comparing with the original, in reading order."""
    passages: list[dict] = []
    for block in blocks:
        if block.kind == "table_row":
            passages.append({"id": block.passage_id, "label": block.label, "kind": "table_row",
                             "text": block.text, "cells": block.cells,
                             "cell_ids": [pid for pid in (block.cell_passage_ids or []) if pid]})
        elif passages and block.slide is not None and passages[-1]["id"] == block.passage_id \
                and passages[-1]["kind"] == "slide":
            passages[-1]["text"] += "\n" + block.text
        else:
            passages.append({"id": block.passage_id, "label": block.label,
                             "kind": "slide" if block.slide is not None else block.kind, "text": block.text})
    return passages


def import_document(path: Path, filename: str = "") -> ImportResult:
    return parse_blocks(read_blocks(path), filename or path.name)


def describe_passage_ids(ids: list[str]) -> str:
    """'P4–P8' for a contiguous run, otherwise a comma list."""
    if not ids:
        return ""
    prefix = re.match(r"^[A-Z]+", ids[0])
    if prefix and all(re.match(rf"^{prefix.group(0)}\d+$", pid) for pid in ids):
        numbers = [int(pid[len(prefix.group(0)):]) for pid in ids]
        if numbers == list(range(numbers[0], numbers[0] + len(numbers))) and len(numbers) > 1:
            return f"{ids[0]}–{ids[-1]}"
    return ", ".join(ids)


def source_reference(question_ids: list[str], answer_ids: list[str]) -> str:
    reference = describe_passage_ids(question_ids)
    if answer_ids:
        reference += f" · answer key {describe_passage_ids(answer_ids)}"
    return reference[:255]


# ---------------------------------------------------------------- validation

def validate_question(question_type: str, question_text: str, options: list[str], correct: Any,
                      explanation: str = "", marking_guidance: str = "") -> list[str]:
    """Errors that stop a question being used in a quiz (empty when valid)."""
    errors: list[str] = []
    if question_type not in QUESTION_TYPES:
        return ["Choose a question type."]
    if not question_text.strip():
        errors.append("The question wording is required.")
    elif len(question_text) > MAX_TEXT_CHARACTERS:
        errors.append(f"The question wording must be at most {MAX_TEXT_CHARACTERS} characters.")
    if question_type in ("mcq_single", "mcq_multiple"):
        if len(options) < 2:
            errors.append("Multiple-choice questions need at least two options.")
        if len(options) > MAX_OPTIONS:
            errors.append(f"At most {MAX_OPTIONS} options are supported.")
        if any(not option.strip() for option in options):
            errors.append("Options cannot be empty.")
        if any(len(option) > MAX_OPTION_CHARACTERS for option in options):
            errors.append(f"Each option must be at most {MAX_OPTION_CHARACTERS} characters.")
        if len({option.strip().casefold() for option in options}) != len(options):
            errors.append("Every option must have different wording.")
        if question_type == "mcq_single":
            if not isinstance(correct, str) or correct not in options:
                errors.append("Single-choice questions need exactly one correct answer.")
        else:
            if (not isinstance(correct, list) or not correct
                    or any(answer not in options for answer in correct)
                    or len(set(correct)) != len(correct)):
                errors.append("Multiple-select questions need at least one correct answer chosen from the options.")
    elif question_type == "true_false":
        if correct not in ("True", "False"):
            errors.append("True/False questions need a correct answer of True or False.")
    elif question_type == "short_answer":
        has_answers = isinstance(correct, list) and any(isinstance(a, str) and a.strip() for a in correct)
        if not has_answers and not marking_guidance.strip():
            errors.append("Short-answer questions need an expected answer or marking guidance.")
        if not explanation.strip():
            errors.append("Short-answer questions need an explanation.")
    return errors


def question_status(errors: list[str], warnings: list[str], reviewed: bool, excluded: bool) -> str:
    if excluded:
        return "EXCLUDED"
    if errors:
        return "NEEDS_REVIEW"
    if warnings and not reviewed:
        return "NEEDS_REVIEW"
    return "READY"


def loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------- selection and timing

_rng = secrets.SystemRandom()


def select_questions(pool: list[int], count: int | None, mode: str, shuffle_order: bool) -> list[int]:
    """Server-side selection from a de-duplicated pool of bank question IDs.

    mode "random": ``count`` distinct questions chosen uniformly at random
    (``random.sample`` semantics: no question twice). mode "all": every
    question, in bank order unless ``shuffle_order``.
    """
    unique = list(dict.fromkeys(pool))
    if mode == "all":
        chosen = list(unique)
        if shuffle_order:
            _rng.shuffle(chosen)
        return chosen
    if count is None or count < 1:
        raise ValueError("Enter how many questions the quiz should have.")
    if count > len(unique):
        raise ValueError(f"You asked for {count} questions, but only {len(unique)} ready questions are available "
                         "in the selected banks.")
    return _rng.sample(unique, count)


def option_permutation(option_count: int, randomize: bool) -> list[int]:
    order = list(range(option_count))
    if randomize and option_count > 1:
        _rng.shuffle(order)
    return order


def parse_permutation(raw: str, option_count: int) -> list[int] | None:
    try:
        order = [int(part) for part in raw.split(",")] if raw.strip() else []
    except ValueError:
        return None
    if sorted(order) != list(range(option_count)):
        return None
    return order


def apply_permutation(options: list[str], order: list[int]) -> list[str]:
    return [options[index] for index in order]


def parse_minutes_seconds(minutes: str | int | None, seconds: str | int | None) -> tuple[int | None, str | None]:
    try:
        m = int(str(minutes).strip() or 0) if minutes is not None else 0
        s = int(str(seconds).strip() or 0) if seconds is not None else 0
    except ValueError:
        return None, "Minutes and seconds must be whole numbers."
    if m < 0 or s < 0 or s > 59:
        return None, "Use 0–59 seconds and a non-negative number of minutes."
    total = m * 60 + s
    if total < QUESTION_MIN_SECONDS or total > QUESTION_MAX_SECONDS:
        return None, (f"Each question needs between {QUESTION_MIN_SECONDS} seconds and "
                      f"{QUESTION_MAX_SECONDS // 60} minutes.")
    return total, None


def format_minutes_seconds(total_seconds: int) -> str:
    minutes, seconds = divmod(int(total_seconds), 60)
    return f"{minutes} min {seconds:02d} sec"


def total_time_error(durations: list[int], limit: int = MAX_QUIZ_TOTAL_SECONDS) -> str | None:
    total = sum(durations)
    if total > limit:
        span = f"{limit // 60}-minute" if limit % 60 == 0 else format_minutes_seconds(limit)
        return (f"Total quiz time is {format_minutes_seconds(total)}, which is over the {span} "
                f"({limit}-second) limit. Shorten individual question timers before publishing.")
    return None


def normalised_identity(text: str, options: list[str]) -> str:
    """Identity used to stop the same question entering one quiz twice from two banks."""
    return " ".join(text.casefold().split()) + "␟" + "␟".join(" ".join(o.casefold().split()) for o in options)
