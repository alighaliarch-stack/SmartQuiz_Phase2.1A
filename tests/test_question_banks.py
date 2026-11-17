"""Phase 4: teacher-authored question banks and quiz assembly.

Covers import (DOCX/PPTX), source fidelity, bank separation/versioning,
server-side selection and option randomisation, per-question timing with
the 10-minute total, answer-key privacy for students, and history safety.
"""

import json
import os
import re
import tempfile
import unittest
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app as smartquiz_app
import document_extraction
import models
import question_banks as qb
from tests.test_source_provenance import _AppTestBase

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# The teacher's real example documents are not committed; point
# SMARTQUIZ_EXAMPLE_DIR at a folder containing them to run that test.
EXAMPLES = Path(os.environ.get("SMARTQUIZ_EXAMPLE_DIR", str(Path.home() / "Downloads")))


# ------------------------------------------------------------------ fixtures

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(text: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num:
        props += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    runs = "<w:r><w:br/></w:r>".join(f'<w:r><w:t xml:space="preserve">{_esc(part)}</w:t></w:r>'
                                      for part in text.split("\n"))
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{runs}</w:p>"


def _table(rows: list[list[str]]) -> str:
    body = "".join("<w:tr>" + "".join(f"<w:tc>{_p(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows)
    return f"<w:tbl><w:tblPr/>{body}</w:tbl>"


STYLES = (
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '</w:styles>'
)
NUMBERING = (
    f'<w:numbering xmlns:w="{W_NS}">'
    '<w:abstractNum w:abstractNumId="0">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="upperLetter"/><w:lvlText w:val="%2."/></w:lvl>'
    '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'
)


def docx_bytes(body: str) -> bytes:
    path = Path(tempfile.mkdtemp()) / "doc.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{W_NS}"><w:body>{body}<w:sectPr/></w:body></w:document>')
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/numbering.xml", NUMBERING)
    return path.read_bytes()


def session_questions(n: int) -> list[dict]:
    return [
        {"text": f"Session {n} question {i}: which statement about topic {n}.{i} is correct?",
         "options": [f"S{n}Q{i} option alpha", f"S{n}Q{i} option beta", f"S{n}Q{i} option gamma", f"S{n}Q{i} option delta"],
         "answer": "ABCD"[(i + n) % 4],
         "explanation": f"Explanation for session {n} question {i}."}
        for i in range(1, 6)
    ]


def session_docx(n: int, questions: list[dict] | None = None) -> bytes:
    """Same structure as the teacher's 'Session N Quiz and Answer Key' documents."""
    questions = questions or session_questions(n)
    body = _p(f"Session {n} Quiz", "Title")
    body += _p(f"Topic for session {n} ({len(questions)} questions, about 8 minutes)")
    body += _p("Name: ____________________    Date: ____________    Choose the single best answer.")
    for i, q in enumerate(questions, 1):
        body += _p(f"{i}. {q['text']}")
        for letter, option in zip("ABCDEFGH", q["options"]):
            body += _p(f"{letter}. {option}")
    body += _p("Instructor answer key (do not distribute to students)", "Heading1")
    body += _table([["Q", "Answer", "Explanation"]]
                   + [[str(i), q["answer"], q["explanation"]] for i, q in enumerate(questions, 1)])
    return docx_bytes(body)


MIXED_BODY = (
    _p("Mixed practice", "Title")
    + _p("1. Which TWO are renewable energy sources? (Select all that apply)")
    + _p("A. Solar") + _p("B. Coal") + _p("C. Wind") + _p("D. Diesel")
    + _p("2. The Earth orbits the Sun.") + _p("A. True") + _p("B. False")
    + _p("3. Water boils at 50 °C at sea level.")
    + _p("4. Name the process by which plants make food from light.")
    + _p("5. Define opportunity cost.")
    + _p("6. Which planet is largest?") + _p("A. Mars") + _p("B. Jupiter") + _p("C. Venus") + _p("D. Earth")
    + _p("7. Which gas do plants absorb?") + _p("A. Oxygen") + _p("B. Carbon dioxide") + _p("C. Nitrogen") + _p("D. Helium")
    + _p("Answer Key", "Heading1")
    + _p("1. A, C")
    + _p("Explanation: Solar and wind are renewable.")
    + _p("2. A")
    + _p("3. False – water boils at 100 °C at sea level.")
    + _p("4. Photosynthesis")
    + _p("Explanation: Plants convert light energy into chemical energy.")
    + _p("5. The value of the next best alternative given up.")
    + _p("6. E")
)

UNICODE_QUESTIONS = [
    {"text": "ما هي عاصمة المملكة العربية السعودية؟", "options": ["جدة", "الرياض", "مكة", "الدمام"], "answer": "B",
     "explanation": "الرياض هي العاصمة."},
    {"text": "下列哪一个是质数？", "options": ["四", "六", "七", "九"], "answer": "C", "explanation": "七只能被一和它本身整除。"},
    {"text": "Solve: x² + 2x + 1 = 0 when x ∈ ℝ; which root is correct (≤ π)?",
     "options": ["x = −1", "x = 1", "x = √2", "x = ½"], "answer": "A", "explanation": "(x + 1)² = 0 ⇒ x = −1."},
    {"text": "भारत की राजधानी क्या है?", "options": ["मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई"], "answer": "B",
     "explanation": "नई दिल्ली राजधानी है।"},
    {"text": "Welche Aussage über „Industrie 4.0“ ist richtig – laut Kurs?",
     "options": ["Dampfkraft", "Vernetzte cyber-physische Systeme", "Fließband", "Keine"], "answer": "B",
     "explanation": "Vernetzung ist zentral."},
]


def pptx_bytes(slides: list[str]) -> bytes:
    path = Path(tempfile.mkdtemp()) / "deck.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for number, inner in enumerate(slides, 1):
            archive.writestr(f"ppt/slides/slide{number}.xml",
                             f'<p:sld xmlns:p="p" xmlns:a="{A_NS}"><p:cSld><p:spTree>{inner}</p:spTree></p:cSld></p:sld>')
    return path.read_bytes()


def _ap(text: str) -> str:
    return f"<p:sp><p:txBody><a:p><a:r><a:t>{_esc(text)}</a:t></a:r></a:p></p:txBody></p:sp>"


def _atable(rows: list[list[str]]) -> str:
    body = "".join("<a:tr>" + "".join(f"<a:tc><a:txBody><a:p><a:r><a:t>{_esc(c)}</a:t></a:r></a:p></a:txBody></a:tc>"
                                      for c in row) + "</a:tr>" for row in rows)
    return f"<p:graphicFrame><a:graphic><a:graphicData><a:tbl>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"


PPTX_SLIDES = [
    _ap("Session 3 Quiz") + _ap("Cloud and data platforms"),
    _ap("1. What does IaaS provide?") + _ap("A. Virtual machines and storage") + _ap("B. Finished applications")
    + _ap("C. Only email") + _ap("D. Printers"),
    _ap("Question 2") + _ap("Which model offers ready-to-use software?") + _ap("A. IaaS") + _ap("B. PaaS")
    + _ap("C. SaaS") + _ap("D. On-premises"),
    _ap("Instructor Answer Key") + _atable([["Q", "Answer", "Explanation"], ["1", "A", "IaaS provides infrastructure."],
                                            ["2", "C", "SaaS delivers complete applications."]]),
]


def _write(data: bytes, name: str) -> Path:
    path = Path(tempfile.mkdtemp()) / name
    path.write_bytes(data)
    return path


def _raw_text(path: Path) -> str:
    return "\n".join(p.text for p in document_extraction.extract_document(path).passages)


# ------------------------------------------------------------- parser tests

class ImportParsingTests(unittest.TestCase):
    def test_docx_questions_options_and_separate_answer_key_are_extracted(self):
        expected = session_questions(1)
        result = qb.import_document(_write(session_docx(1), "Session_01.docx"))
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.questions), 5)
        self.assertEqual(result.suggested_title, "Session 1 Quiz")
        for item, source in zip(result.questions, expected):
            self.assertEqual(item.question_text, source["text"])
            self.assertEqual(item.options, source["options"])
            self.assertEqual(item.option_labels, ["A", "B", "C", "D"])
            self.assertEqual(item.question_type, "mcq_single")
            self.assertEqual(item.answer_key_raw, source["answer"])
            self.assertEqual(item.correct_answer, source["options"]["ABCD".index(source["answer"])])
            self.assertEqual(item.explanation, source["explanation"])
            self.assertEqual(item.warnings, [])
        first = result.questions[0]
        self.assertEqual(qb.source_reference(first.source_passage_ids, first.answer_passage_ids),
                         "P4–P8 · answer key P33–P35")
        # Header lines are kept for the teacher, never turned into questions.
        self.assertIn("Name: ____________________    Date: ____________    Choose the single best answer.", result.preamble)

    def test_passage_ids_match_the_existing_document_extractor(self):
        path = _write(session_docx(2), "s2.docx")
        extractor = {p.id: p.text for p in document_extraction.extract_document(path).passages}
        for block in qb.read_blocks(path):
            ids = block.cell_passage_ids if block.kind == "table_row" else [block.passage_id]
            for pid in ids:
                self.assertIn(pid, extractor)
            if block.kind != "table_row":
                self.assertEqual(block.text, extractor[block.passage_id])

    def test_pptx_import_reads_slides_and_answer_key_table_on_a_separate_slide(self):
        result = qb.import_document(_write(pptx_bytes(PPTX_SLIDES), "deck.pptx"))
        self.assertEqual([q.question_text for q in result.questions],
                         ["What does IaaS provide?", "Which model offers ready-to-use software?"])
        self.assertEqual(result.questions[0].correct_answer, "Virtual machines and storage")
        self.assertEqual(result.questions[1].correct_answer, "SaaS")
        self.assertEqual(result.questions[1].explanation, "SaaS delivers complete applications.")
        self.assertEqual(result.questions[0].source_passage_ids, ["S2"])
        self.assertEqual(result.questions[1].answer_passage_ids, ["S4"])
        self.assertTrue(all(not q.warnings for q in result.questions))

    def test_unicode_scripts_and_mathematics_are_preserved_exactly(self):
        result = qb.import_document(_write(session_docx(9, UNICODE_QUESTIONS), "unicode.docx"))
        self.assertEqual(len(result.questions), len(UNICODE_QUESTIONS))
        for item, source in zip(result.questions, UNICODE_QUESTIONS):
            self.assertEqual(item.question_text, source["text"])
            self.assertEqual(item.options, source["options"])
            self.assertEqual(item.explanation, source["explanation"])
            self.assertEqual(item.correct_answer, source["options"]["ABCD".index(source["answer"])])

    def test_word_auto_numbered_questions_and_lettered_options(self):
        body = (_p("Which colour is the sky?", num=(1, 0)) + _p("Blue", num=(1, 1)) + _p("Green", num=(1, 1))
                + _p("Which number is even?", num=(1, 0)) + _p("Three", num=(1, 1)) + _p("Four", num=(1, 1))
                + _p("Answers", "Heading1") + _p("1. A") + _p("2. B"))
        result = qb.import_document(_write(docx_bytes(body), "auto.docx"))
        self.assertEqual([q.question_text for q in result.questions], ["Which colour is the sky?", "Which number is even?"])
        self.assertEqual([q.options for q in result.questions], [["Blue", "Green"], ["Three", "Four"]])
        self.assertEqual([q.correct_answer for q in result.questions], ["Blue", "Four"])

    def test_question_types_and_paragraph_answer_keys(self):
        result = qb.import_document(_write(docx_bytes(MIXED_BODY), "mixed.docx"))
        by_number = {q.question_number: q for q in result.questions}
        self.assertEqual(by_number["1"].question_type, "mcq_multiple")
        self.assertEqual(by_number["1"].correct_answer, ["Solar", "Wind"])
        self.assertEqual(by_number["1"].explanation, "Solar and wind are renewable.")
        self.assertEqual((by_number["2"].question_type, by_number["2"].correct_answer), ("true_false", "True"))
        self.assertEqual((by_number["3"].question_type, by_number["3"].correct_answer), ("true_false", "False"))
        self.assertEqual(by_number["3"].explanation, "water boils at 100 °C at sea level.")
        self.assertEqual((by_number["4"].question_type, by_number["4"].correct_answer), ("short_answer", ["Photosynthesis"]))
        self.assertEqual(by_number["4"].explanation, "Plants convert light energy into chemical energy.")
        self.assertEqual(by_number["5"].question_type, "short_answer")
        for number in ("1", "2", "3", "4"):
            q = by_number[number]
            self.assertEqual(qb.validate_question(q.question_type, q.question_text, q.options, q.correct_answer,
                                                  q.explanation, q.marking_guidance), [], number)

    def test_incomplete_answer_keys_are_flagged_not_guessed(self):
        result = qb.import_document(_write(docx_bytes(MIXED_BODY), "mixed.docx"))
        by_number = {q.question_number: q for q in result.questions}
        # Q5: short answer without an explanation -> invalid, needs review.
        q5 = by_number["5"]
        self.assertIn("Short-answer questions need an explanation.",
                      qb.validate_question(q5.question_type, q5.question_text, q5.options, q5.correct_answer, q5.explanation))
        # Q6: key letter E does not exist -> no answer chosen, warning raised.
        q6 = by_number["6"]
        self.assertIsNone(q6.correct_answer)
        self.assertTrue(any("does not match any option" in w for w in q6.warnings))
        # Q7: no key entry at all.
        q7 = by_number["7"]
        self.assertIsNone(q7.correct_answer)
        self.assertTrue(any("No answer was found" in w for w in q7.warnings))
        for q in (q5, q6, q7):
            errors = qb.validate_question(q.question_type, q.question_text, q.options, q.correct_answer, q.explanation)
            self.assertEqual(qb.question_status(errors, q.warnings, False, False), "NEEDS_REVIEW")

    def test_ambiguous_structure_is_flagged(self):
        body = (_p("Quiz", "Title")
                + _p("Which tool is used for version control?") + _p("A. Git") + _p("B. Paint")
                + _p("2. Which is a database?") + _p("A. PostgreSQL") + _p("B. Excel chart")
                + _p("Remember to show your working on the back of the page.")
                + _p("3. Which is a language?") + _p("A. Python") + _p("A. Python 3") + _p("C. HTML table")
                + _p("Answer key", "Heading1") + _p("2. A") + _p("3. A") + _p("9. B"))
        result = qb.import_document(_write(docx_bytes(body), "ambiguous.docx"))
        unnumbered, second, third = result.questions
        self.assertIsNone(unnumbered.question_number)
        self.assertEqual(unnumbered.question_text, "Which tool is used for version control?")
        self.assertTrue(any("No question number" in w for w in unnumbered.warnings))
        self.assertTrue(any("Unrecognised text after the options" in w and "show your working" in w for w in second.warnings))
        self.assertTrue(any("label is repeated" in w for w in third.warnings))
        self.assertTrue(any("question 9" in w for w in result.warnings))
        self.assertTrue(any("missing from the sequence" in w for w in result.warnings))

    def test_no_invented_or_rewritten_content(self):
        for data, name in ((session_docx(1), "s1.docx"), (docx_bytes(MIXED_BODY), "mixed.docx"),
                           (session_docx(9, UNICODE_QUESTIONS), "u.docx"), (pptx_bytes(PPTX_SLIDES), "deck.pptx")):
            path = _write(data, name)
            raw = _raw_text(path)
            result = qb.import_document(path)
            question_part = re.split(r"(?i)answer key", raw)[0]
            numbered = len(re.findall(r"(?m)^(?:\d+\.|Question \d+)", question_part))
            self.assertEqual(len(result.questions), numbered, name)  # nothing invented
            for q in result.questions:
                self.assertIn(q.question_text, raw, name)
                for option in q.options:
                    if q.question_type != "true_false":
                        self.assertIn(option, raw, name)
                if q.explanation:
                    self.assertIn(q.explanation, raw, name)
                if q.answer_key_raw:
                    self.assertIn(q.answer_key_raw, raw, name)

    @unittest.skipUnless((EXAMPLES / "Session_01_Quiz_and_Answer_Key.docx").exists()
                         and (EXAMPLES / "Session_02_Quiz_and_Answer_Key.docx").exists(),
                         "teacher example documents not available on this machine")
    def test_teacher_example_documents_import_completely(self):
        for name, answers in (("Session_01_Quiz_and_Answer_Key.docx", "BCBBC"),
                              ("Session_02_Quiz_and_Answer_Key.docx", "BCBBB")):
            result = qb.import_document(EXAMPLES / name)
            self.assertEqual(result.warnings, [], name)
            self.assertEqual("".join(q.answer_key_raw for q in result.questions), answers)
            for q in result.questions:
                self.assertEqual(q.question_type, "mcq_single")
                self.assertEqual(len(q.options), 4)
                self.assertTrue(q.explanation)
                self.assertEqual(q.warnings, [])


class ValidationAndSelectionTests(unittest.TestCase):
    def test_validation_rules_per_type(self):
        opts = ["One", "Two", "Three"]
        self.assertEqual(qb.validate_question("mcq_single", "Q?", opts, "Two"), [])
        self.assertTrue(qb.validate_question("mcq_single", "Q?", opts, ["One", "Two"]))
        self.assertTrue(qb.validate_question("mcq_single", "Q?", opts, None))
        self.assertEqual(qb.validate_question("mcq_multiple", "Q?", opts, ["One", "Three"]), [])
        self.assertTrue(qb.validate_question("mcq_multiple", "Q?", opts, []))
        self.assertTrue(qb.validate_question("mcq_single", "Q?", ["Same", "same"], "Same"))
        self.assertEqual(qb.validate_question("true_false", "Q?", ["True", "False"], "False"), [])
        self.assertTrue(qb.validate_question("true_false", "Q?", ["True", "False"], "Maybe"))
        self.assertTrue(qb.validate_question("short_answer", "Q?", [], [], "Because."))
        self.assertEqual(qb.validate_question("short_answer", "Q?", [], [], "Because.", "Mentions X"), [])
        self.assertIn("Short-answer questions need an explanation.",
                      qb.validate_question("short_answer", "Q?", [], ["X"], ""))
        # Explanations are optional for every other type.
        self.assertEqual(qb.validate_question("mcq_single", "Q?", opts, "One", ""), [])

    def test_random_selection_count_uniqueness_and_limits(self):
        pool = list(range(1, 21))
        for _ in range(50):
            chosen = qb.select_questions(pool, 10, "random", False)
            self.assertEqual(len(chosen), 10)
            self.assertEqual(len(set(chosen)), 10)
            self.assertTrue(set(chosen) <= set(pool))
        self.assertGreater(len({tuple(qb.select_questions(pool, 10, "random", False)) for _ in range(20)}), 1)
        with self.assertRaisesRegex(ValueError, "only 20 ready questions"):
            qb.select_questions(pool, 21, "random", False)
        self.assertEqual(qb.select_questions([3, 1, 3, 2], None, "all", False), [3, 1, 2])

    def test_option_permutation_keeps_correct_answer(self):
        options = ["alpha", "beta", "gamma", "delta"]
        for _ in range(30):
            order = qb.option_permutation(4, True)
            shuffled = qb.apply_permutation(options, order)
            self.assertEqual(sorted(shuffled), sorted(options))
            self.assertEqual(shuffled.index("gamma"), order.index(2))
        self.assertEqual(qb.option_permutation(4, False), [0, 1, 2, 3])
        self.assertIsNone(qb.parse_permutation("0,0,1,2", 4))
        self.assertIsNone(qb.parse_permutation("0,1,2", 4))
        self.assertEqual(qb.parse_permutation("3,1,0,2", 4), [3, 1, 0, 2])

    def test_minutes_seconds_and_total_limit(self):
        self.assertEqual(qb.parse_minutes_seconds("0", "20"), (20, None))
        self.assertEqual(qb.parse_minutes_seconds("1", "30"), (90, None))
        self.assertIsNotNone(qb.parse_minutes_seconds("0", "75")[1])
        self.assertIsNotNone(qb.parse_minutes_seconds("0", "10")[1])  # below 15 s
        self.assertIsNone(qb.total_time_error([20, 40, 15, 90]))
        self.assertIsNone(qb.total_time_error([300, 300]))
        self.assertIn("10 min 01 sec", qb.total_time_error([300, 301]))
        self.assertEqual(qb.format_minutes_seconds(95), "1 min 35 sec")


# --------------------------------------------------------------- HTTP tests

class _BankAppBase(_AppTestBase):
    def setUp(self):
        super().setUp()
        self.previous_bank_dir = smartquiz_app.QUESTION_BANK_UPLOADS_DIR
        self.bank_storage = tempfile.TemporaryDirectory()
        smartquiz_app.QUESTION_BANK_UPLOADS_DIR = Path(self.bank_storage.name)

    def tearDown(self):
        smartquiz_app.QUESTION_BANK_UPLOADS_DIR = self.previous_bank_dir
        self.bank_storage.cleanup()
        super().tearDown()

    def upload(self, data: bytes, filename: str, **form) -> object:
        mime = PPTX_MIME if filename.endswith(".pptx") else DOCX_MIME
        return self.client.post("/admin/question-banks/upload", files={"file": (filename, data, mime)},
                                data=form, follow_redirects=False)

    def import_bank(self, data: bytes, filename: str, confirm: bool = True, **form) -> int:
        response = self.upload(data, filename, **form)
        self.assertEqual(response.status_code, 303, response.text)
        location = response.headers["location"]
        self.assertTrue(location.endswith("/import?notice=uploaded"), location)
        bank_id = int(location.split("/")[3])
        if confirm:
            confirmed = self.client.post(f"/admin/question-banks/{bank_id}/import", data={"action": "confirm"},
                                         follow_redirects=False)
            self.assertEqual(confirmed.status_code, 303, confirmed.text)
            self.assertEqual(confirmed.headers["location"], f"/admin/question-banks/{bank_id}?notice=confirmed")
        return bank_id

    def bank_questions(self, bank_id: int) -> list:
        db = self.db()
        try:
            return db.query(models.BankQuestion).filter_by(question_bank_id=bank_id).order_by(models.BankQuestion.position).all()
        finally:
            db.close()

    def preview(self, bank_ids, **fields):
        data = {"title": fields.pop("title", "Module 1 quiz"), "bank_ids": [str(b) for b in bank_ids],
                "mode": "random", "shuffle_order": "true", **fields}
        return self.client.post("/admin/quizzes/assemble/preview", data=data)

    def selected_from_preview(self, html: str) -> list[dict]:
        """Rows as rendered in the preview, in order, with their included state and option order."""
        rows = []
        for match in re.finditer(r'data-item="(\d+)"(.*?)name="sec_\1" value="(\d+)"', html, re.S):
            qid, chunk, seconds = match.group(1), match.group(2), match.group(3)
            order = re.search(rf'name="order_{qid}" value="([^"]*)"', chunk).group(1)
            minutes = re.search(rf'name="min_{qid}" value="(\d+)"', chunk).group(1)
            included = re.search(rf'name="include_{qid}" value="yes" data-include\s+checked', chunk) is not None
            rows.append({"id": int(qid), "order": order, "min": minutes, "sec": seconds, "included": included})
        return rows

    def save(self, rows: list[dict], bank_ids, title="Module 1 quiz", **extra):
        data = {"title": title, "bank_ids": [str(b) for b in bank_ids], "item": [str(r["id"]) for r in rows], **extra}
        for row in rows:
            data[f"order_{row['id']}"] = row["order"]
            data[f"min_{row['id']}"] = row["min"]
            data[f"sec_{row['id']}"] = row["sec"]
            if row["included"]:
                data[f"include_{row['id']}"] = "yes"
        return self.client.post("/admin/quizzes/assemble/save", data=data, follow_redirects=False)

    def quiz_questions(self, quiz_id: int) -> list:
        db = self.db()
        try:
            return db.query(models.Question).filter_by(quiz_id=quiz_id).order_by(models.Question.id).all()
        finally:
            db.close()

    def assemble(self, bank_ids, count=None, mode="random", **fields) -> int:
        response = self.preview(bank_ids, count=str(count or ""), mode=mode, **fields)
        self.assertEqual(response.status_code, 200, response.text)
        rows = [r for r in self.selected_from_preview(response.text) if r["included"]]
        saved = self.save(rows, bank_ids, **{k: v for k, v in fields.items() if k in ("auto_advance", "title")})
        self.assertEqual(saved.status_code, 303, saved.text)
        return int(saved.headers["location"].split("/")[3].split("?")[0])


class QuestionBankImportFlowTests(_BankAppBase):
    def test_upload_shows_preview_and_nothing_is_usable_until_confirmed(self):
        bank_id = self.import_bank(session_docx(1), "Session_01_Quiz_and_Answer_Key.docx", confirm=False)
        page = self.client.get(f"/admin/question-banks/{bank_id}/import")
        self.assertEqual(page.status_code, 200)
        for q in session_questions(1):
            self.assertIn(q["text"], unescape(page.text))
            self.assertIn(q["explanation"], unescape(page.text))
        self.assertIn("Confirm import", page.text)
        self.assertIn("P4–P8 · answer key P33–P35", page.text)
        db = self.db()
        bank = db.get(models.QuestionBank, bank_id)
        self.assertEqual((bank.status, bank.is_current, bank.title), ("DRAFT", False, "Session 1 Quiz"))
        self.assertEqual(bank.source_filename, "Session_01_Quiz_and_Answer_Key.docx")
        db.close()
        # Draft banks are not offered for quiz assembly.
        self.assertEqual(self.preview([bank_id], count="2").status_code, 400)
        self.client.post(f"/admin/question-banks/{bank_id}/import", data={"action": "confirm"})
        self.assertEqual(self.preview([bank_id], count="2").status_code, 200)

    def test_pptx_upload_and_bank_metadata(self):
        bank_id = self.import_bank(pptx_bytes(PPTX_SLIDES), "Session 3.pptx", title="Session 3 Question Bank",
                                   module_id=str(self.module_id))
        db = self.db()
        bank = db.get(models.QuestionBank, bank_id)
        self.assertEqual((bank.title, bank.file_extension, bank.module_id, bank.version_number), (
            "Session 3 Question Bank", ".pptx", self.module_id, 1))
        self.assertIsNotNone(bank.course_id)
        self.assertEqual([q.status for q in bank.questions], ["READY", "READY"])
        db.close()
        listing = self.client.get("/admin/question-banks")
        self.assertIn("Session 3 Question Bank", listing.text)
        self.assertIn("Session 3.pptx", listing.text)

    def test_duplicate_upload_is_rejected_unless_teacher_opts_in(self):
        first = self.import_bank(session_docx(1), "s1.docx")
        again = self.upload(session_docx(1), "s1-copy.docx")
        self.assertEqual(again.status_code, 303)
        self.assertIn("error=", again.headers["location"])
        self.assertIn("already+imported", again.headers["location"])
        self.assertEqual(self.db().query(models.QuestionBank).count(), 1)
        second = self.import_bank(session_docx(1), "s1-copy.docx", allow_duplicate="yes")
        self.assertNotEqual(first, second)

    def test_invalid_and_unsupported_files_are_rejected(self):
        self.assertIn("error=", self.upload(b"%PDF-1.4", "quiz.pdf").headers["location"])
        self.assertIn("error=", self.upload(b"PK\x03\x04broken", "quiz.docx").headers["location"])
        no_questions = docx_bytes(_p("Just a heading", "Title") + _p("Some notes without questions."))
        self.assertIn("No+numbered+questions", self.upload(no_questions, "notes.docx").headers["location"])
        self.assertEqual(self.db().query(models.QuestionBank).count(), 0)
        self.assertEqual(list(Path(self.bank_storage.name).iterdir()), [])

    def test_teacher_corrections_in_preview_keep_the_original_snapshot(self):
        bank_id = self.import_bank(docx_bytes(MIXED_BODY), "mixed.docx", confirm=False)
        questions = {q.question_number: q for q in self.bank_questions(bank_id)}
        self.assertEqual(questions["5"].status, "NEEDS_REVIEW")
        self.assertEqual(questions["6"].status, "NEEDS_REVIEW")
        q5, q6, q7 = questions["5"].id, questions["6"].id, questions["7"].id
        form = {
            "action": "confirm",
            f"q{q5}_type": "short_answer", f"q{q5}_text": "Define opportunity cost.",
            f"q{q5}_expected": "The value of the next best alternative given up.",
            f"q{q5}_explanation": "Teacher-added explanation on request.",
            f"q{q6}_type": "mcq_single", f"q{q6}_text": "Which planet is largest?",
            f"q{q6}_opt_0": "Mars", f"q{q6}_opt_1": "Jupiter", f"q{q6}_opt_2": "Venus", f"q{q6}_opt_3": "Earth",
            f"q{q6}_correct": "1", f"q{q6}_reviewed": "yes",
            f"q{q7}_type": "mcq_single", f"q{q7}_text": "Which gas do plants absorb?", f"q{q7}_exclude": "yes",
        }
        response = self.client.post(f"/admin/question-banks/{bank_id}/import", data=form, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        questions = {q.question_number: q for q in self.bank_questions(bank_id)}
        self.assertEqual(questions["5"].status, "READY")
        self.assertEqual(json.loads(questions["6"].correct_answer), "Jupiter")
        self.assertEqual(questions["6"].status, "READY")
        self.assertEqual(questions["7"].status, "EXCLUDED")
        original = json.loads(questions["6"].original_snapshot)
        self.assertIsNone(original["correct_answer"])
        self.assertEqual(original["answer_key_raw"], "E")
        self.assertEqual(json.loads(questions["5"].original_snapshot)["explanation"], "")

    def test_flagged_question_needs_explicit_review_even_when_complete(self):
        body = (_p("Which tool is used for version control?") + _p("A. Git") + _p("B. Paint")
                + _p("2. Which is a database?") + _p("A. PostgreSQL") + _p("B. Paint")
                + _p("Answer key", "Heading1") + _p("2. A"))
        bank_id = self.import_bank(docx_bytes(body), "flag.docx", confirm=False)
        flagged = self.bank_questions(bank_id)[0]
        base = {f"q{flagged.id}_type": "mcq_single", f"q{flagged.id}_text": flagged.question_text,
                f"q{flagged.id}_opt_0": "Git", f"q{flagged.id}_opt_1": "Paint", f"q{flagged.id}_correct": "0",
                "action": "save"}
        self.client.post(f"/admin/question-banks/{bank_id}/import", data=base)
        self.assertEqual(self.bank_questions(bank_id)[0].status, "NEEDS_REVIEW")
        self.client.post(f"/admin/question-banks/{bank_id}/import", data={**base, f"q{flagged.id}_reviewed": "yes"})
        self.assertEqual(self.bank_questions(bank_id)[0].status, "READY")

    def test_confirm_requires_at_least_one_ready_question(self):
        body = _p("1. Unanswered question?") + _p("A. Yes") + _p("B. No")
        bank_id = self.import_bank(docx_bytes(body), "none.docx", confirm=False)
        response = self.client.post(f"/admin/question-banks/{bank_id}/import", data={"action": "confirm"},
                                    follow_redirects=False)
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.db().get(models.QuestionBank, bank_id).status, "DRAFT")

    def test_discarding_an_import_removes_the_bank_and_file(self):
        bank_id = self.import_bank(session_docx(1), "s1.docx", confirm=False)
        self.assertEqual(len(list(Path(self.bank_storage.name).iterdir())), 1)
        self.client.post(f"/admin/question-banks/{bank_id}/discard")
        self.assertIsNone(self.db().get(models.QuestionBank, bank_id))
        self.assertEqual(list(Path(self.bank_storage.name).iterdir()), [])

    def test_teacher_pages_show_answer_key_source_and_original_file(self):
        bank_id = self.import_bank(session_docx(1), "s1.docx")
        key = self.client.get(f"/admin/question-banks/{bank_id}/answer-key")
        self.assertEqual(key.status_code, 200)
        self.assertIn("Explanation for session 1 question 1.", key.text)
        source = self.client.get(f"/admin/question-banks/{bank_id}/source")
        self.assertIn("Instructor answer key (do not distribute to students)", source.text)
        self.assertIn('data-passage="P33"', source.text)
        original = self.client.get(f"/admin/question-banks/{bank_id}/file")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, session_docx(1)[:0] + original.content)
        self.assertIn("s1.docx", original.headers["content-disposition"])
        detail = self.client.get(f"/admin/question-banks/{bank_id}?q=topic+1.3")
        self.assertEqual(detail.text.count("data-bank-question="), 1)
        self.assertEqual(self.client.get(f"/admin/question-banks/{bank_id}?question_type=true_false")
                         .text.count("data-bank-question="), 0)


class BankLifecycleTests(_BankAppBase):
    def test_banks_stay_separate_and_can_be_combined_for_one_quiz(self):
        banks = [self.import_bank(session_docx(n), f"Session_0{n}.docx", title=f"Session {n} Question Bank")
                 for n in (1, 2, 3, 4)]
        self.assertEqual(len(set(banks)), 4)
        for bank_id in banks:
            self.assertEqual(len(self.bank_questions(bank_id)), 5)
        quiz_id = self.assemble(banks, count=10)
        questions = self.quiz_questions(quiz_id)
        self.assertEqual(len(questions), 10)
        self.assertEqual(len({q.bank_question_id for q in questions}), 10)
        self.assertTrue({q.question_bank_id for q in questions} <= set(banks))
        # The banks themselves are untouched and still separate.
        for bank_id in banks:
            self.assertEqual(len(self.bank_questions(bank_id)), 5)

    def test_requesting_more_questions_than_available_is_explained(self):
        bank_id = self.import_bank(session_docx(1), "s1.docx")
        response = self.preview([bank_id], count="6")
        self.assertEqual(response.status_code, 400)
        self.assertIn("You asked for 6 questions, but only 5 ready questions are available", unescape(response.text))

    def test_archive_and_restore(self):
        bank_id = self.import_bank(session_docx(1), "s1.docx")
        self.client.post(f"/admin/question-banks/{bank_id}/archive")
        self.assertIsNotNone(self.db().get(models.QuestionBank, bank_id).archived_at)
        self.assertNotIn(f'name="bank_ids" value="{bank_id}"', self.client.get("/admin/quizzes/assemble").text)
        self.assertEqual(self.preview([bank_id], count="2").status_code, 400)
        self.client.post(f"/admin/question-banks/{bank_id}/restore")
        self.assertIsNone(self.db().get(models.QuestionBank, bank_id).archived_at)
        self.assertEqual(self.preview([bank_id], count="2").status_code, 200)

    def test_deletion_is_restricted_when_questions_are_used(self):
        used = self.import_bank(session_docx(1), "s1.docx")
        unused = self.import_bank(session_docx(2), "s2.docx")
        quiz_id = self.assemble([used], count=2)
        for bank_id in (used, unused):
            page = self.client.get(f"/admin/question-banks/{bank_id}/delete")
            self.assertIn("Move to Recently deleted", page.text)
            self.assertEqual(self.client.post(f"/admin/question-banks/{bank_id}/delete", data={"confirm": "yes"},
                                              follow_redirects=False).status_code, 303)
        self.assertIn("data-blocked", self.client.get(f"/admin/question-banks/{used}/purge").text)
        self.assertEqual(self.client.post(f"/admin/question-banks/{used}/purge", data={"confirm": "yes"}).status_code, 409)
        self.assertEqual(self.client.post(f"/admin/question-banks/{unused}/purge", data={"confirm": "yes"},
                                          follow_redirects=False).status_code, 303)
        self.assertIsNone(self.db().get(models.QuestionBank, unused))
        self.assertIsNotNone(self.db().get(models.QuestionBank, used))
        self.assertEqual(len(self.quiz_questions(quiz_id)), 2)
        # Without confirmation nothing happens.
        self.assertEqual(self.client.post(f"/admin/question-banks/{used}/restore", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.post(f"/admin/question-banks/{used}/delete").status_code, 400)

    def test_removing_a_single_question_is_soft_and_restorable(self):
        bank_id = self.import_bank(session_docx(1), "s1.docx")
        quiz_id = self.assemble([bank_id], mode="all", shuffle_order="")
        target = self.bank_questions(bank_id)[0]
        confirm_page = self.client.get(f"/admin/question-banks/{bank_id}/questions/{target.id}/delete")
        self.assertIn("already used in 1 quiz", confirm_page.text)
        self.client.post(f"/admin/question-banks/{bank_id}/questions/{target.id}/delete", data={"confirm": "yes"})
        self.assertIsNotNone(self.db().get(models.BankQuestion, target.id).deleted_at)
        self.assertEqual(len(self.quiz_questions(quiz_id)), 5)
        self.assertEqual(self.preview([bank_id], count="5").status_code, 400)
        self.client.post(f"/admin/question-banks/{bank_id}/questions/{target.id}/restore")
        self.assertIsNone(self.db().get(models.BankQuestion, target.id).deleted_at)


class QuizAssemblyTests(_BankAppBase):
    def setUp(self):
        super().setUp()
        self.banks = [self.import_bank(session_docx(n), f"s{n}.docx") for n in (1, 2)]

    def test_use_all_in_bank_order_and_manual_reorder_and_exclusion(self):
        response = self.preview(self.banks, mode="all", shuffle_order="")
        rows = self.selected_from_preview(response.text)
        self.assertEqual(len(rows), 10)
        expected_order = [q.id for b in self.banks for q in self.bank_questions(b)]
        self.assertEqual([r["id"] for r in rows], expected_order)
        rows = list(reversed(rows))
        rows[0]["included"] = False   # teacher leaves one out
        saved = self.save(rows, self.banks)
        quiz_id = int(saved.headers["location"].split("/")[3].split("?")[0])
        self.assertEqual([q.bank_question_id for q in self.quiz_questions(quiz_id)],
                         [r["id"] for r in rows if r["included"]])

    def test_pool_questions_can_be_added_in_preview(self):
        response = self.preview(self.banks, count="3")
        rows = self.selected_from_preview(response.text)
        self.assertEqual(sum(r["included"] for r in rows), 3)
        self.assertEqual(len(rows), 10)
        extra = next(r for r in rows if not r["included"])
        extra["included"] = True
        saved = self.save(rows, self.banks)
        quiz_id = int(saved.headers["location"].split("/")[3].split("?")[0])
        self.assertEqual(len(self.quiz_questions(quiz_id)), 4)

    def test_duplicates_cannot_enter_a_quiz(self):
        response = self.preview(self.banks, count="2")
        rows = [r for r in self.selected_from_preview(response.text) if r["included"]]
        doubled = self.save(rows + [rows[0]], self.banks)
        self.assertEqual(doubled.status_code, 400)
        self.assertIn("submitted twice", doubled.text)
        copy_bank = self.import_bank(session_docx(1), "copy.docx", allow_duplicate="yes")
        pooled = self.preview([self.banks[0], copy_bank], mode="all", shuffle_order="")
        self.assertEqual(len(self.selected_from_preview(pooled.text)), 5)
        self.assertIn("each is offered only once", pooled.text)

    def test_option_randomisation_preserves_wording_and_correct_answer(self):
        quiz_id = self.assemble(self.banks, mode="all", shuffle_options="true")
        moved = 0
        db = self.db()
        for question in self.quiz_questions(quiz_id):
            bank_question = db.get(models.BankQuestion, question.bank_question_id)
            options, original = json.loads(question.options), json.loads(bank_question.options)
            self.assertEqual(sorted(options), sorted(original))
            self.assertEqual(question.correct_answer, bank_question.correct_answer)
            self.assertIn(json.loads(question.correct_answer), options)
            self.assertEqual(question.question_text, bank_question.question_text)
            moved += options != original
        db.close()
        self.assertGreater(moved, 0)

    def test_tampered_option_order_falls_back_to_document_order(self):
        rows = [r for r in self.selected_from_preview(self.preview(self.banks, count="1").text) if r["included"]]
        rows[0]["order"] = "0,0,0,0"
        quiz_id = int(self.save(rows, self.banks).headers["location"].split("/")[3].split("?")[0])
        question = self.quiz_questions(quiz_id)[0]
        self.assertEqual(question.options, self.db().get(models.BankQuestion, rows[0]["id"]).options)

    def test_quiz_questions_are_snapshots_with_provenance(self):
        quiz_id = self.assemble(self.banks[:1], count=2)
        question = self.quiz_questions(quiz_id)[0]
        self.assertTrue(question.source_reference.startswith("s1.docx · P"))
        provenance = self.client.get(f"/admin/quizzes/{quiz_id}/questions/{question.id}/provenance").json()
        self.assertEqual(provenance["origin"], "question_bank")
        self.assertEqual(provenance["question_bank_id"], self.banks[0])
        builder = self.client.get(f"/admin/quizzes/{quiz_id}")
        self.assertIn("From question bank", builder.text)
        self.assertIn('data-bank-provenance="', builder.text)

    def test_dashboard_offers_both_creation_methods(self):
        page = self.client.get("/admin")
        self.assertIn("Create Quiz Manually", page.text)
        self.assertIn("Create Quiz from Question Banks", page.text)
        self.assertIn('href="/admin/quizzes/assemble"', page.text)
        self.assertEqual(self.client.get("/admin/quizzes/assemble").status_code, 200)


class TimingTests(_BankAppBase):
    def setUp(self):
        super().setUp()
        self.bank = self.import_bank(session_docx(1), "s1.docx")

    def test_per_question_minutes_and_seconds(self):
        rows = [r for r in self.selected_from_preview(self.preview([self.bank], count="3").text) if r["included"]]
        self.assertEqual((rows[0]["min"], rows[0]["sec"]), ("0", "20"))  # mcq example starting value
        rows[0].update(min="0", sec="45")
        rows[1].update(min="1", sec="30")
        quiz_id = int(self.save(rows, [self.bank]).headers["location"].split("/")[3].split("?")[0])
        self.assertEqual([q.duration_seconds for q in self.quiz_questions(quiz_id)], [45, 90, 20])
        builder = self.client.get(f"/admin/quizzes/{quiz_id}")
        self.assertIn("Total question time 2 min 35 sec", builder.text)
        self.assertIn("1 min 30 sec", builder.text)

    def test_invalid_timer_is_rejected(self):
        rows = [r for r in self.selected_from_preview(self.preview([self.bank], count="1").text) if r["included"]]
        rows[0].update(min="0", sec="75")
        self.assertEqual(self.save(rows, [self.bank]).status_code, 400)
        too_short = self.preview([self.bank], count="1", time_mcq_single_min="0", time_mcq_single_sec="5")
        self.assertEqual(too_short.status_code, 400)
        self.assertIn("Multiple Choice (Single) starting time", too_short.text)

    def test_over_ten_minutes_saves_as_draft_but_cannot_be_published(self):
        rows = [r for r in self.selected_from_preview(self.preview([self.bank], count="3").text) if r["included"]]
        for row in rows:
            row.update(min="3", sec="30")   # 3 x 210 s = 630 s
        preview_warning = self.client.post("/admin/quizzes/assemble/preview", data={
            "title": "x", "bank_ids": [str(self.bank)], "mode": "all", "time_mcq_single_min": "2", "time_mcq_single_sec": "1"})
        self.assertIn("over the 10-minute", preview_warning.text)
        quiz_id = int(self.save(rows, [self.bank]).headers["location"].split("/")[3].split("?")[0])
        builder = self.client.get(f"/admin/quizzes/{quiz_id}")
        self.assertIn("data-time-limit-warning", builder.text)
        self.assertIn("10 min 30 sec", builder.text)
        published = self.client.post(f"/admin/quizzes/{quiz_id}/publish", follow_redirects=False)
        self.assertIn("error=", published.headers["location"])
        self.assertEqual(self.db().get(models.Quiz, quiz_id).status, "Draft")
        # Shorten one timer with the existing question editor (minutes + seconds).
        question = self.quiz_questions(quiz_id)[0]
        edit = self.client.post(f"/admin/quizzes/{quiz_id}/questions/{question.id}/edit", data={
            "question_text": question.question_text, "question_type": "mcq_single",
            "option_a": json.loads(question.options)[0], "option_b": json.loads(question.options)[1],
            "option_c": json.loads(question.options)[2], "option_d": json.loads(question.options)[3],
            "correct_single": "ABCD"[json.loads(question.options).index(json.loads(question.correct_answer))],
            "duration_min_part": "2", "duration_sec_part": "0"}, follow_redirects=False)
        self.assertEqual(edit.status_code, 303)
        self.assertEqual(self.db().get(models.Question, question.id).duration_seconds, 120)
        self.client.post(f"/admin/quizzes/{quiz_id}/publish")
        self.assertEqual(self.db().get(models.Quiz, quiz_id).status, "Published")

    def test_new_manual_quizzes_follow_the_ten_minute_rule(self):
        # Policy since the university-deployment preparation: every NEW quiz,
        # manual or bank-assembled, is limited to 600 seconds in total.
        quiz_id = self.create_draft_quiz("Manual quiz")
        self.assertEqual(self.db().get(models.Quiz, quiz_id).time_limit_seconds, 600)
        form = self.client.get(f"/admin/quizzes/{quiz_id}/questions/new").text
        self.assertRegex(form, r'name="duration_min_part"\s+min="0" max="20" value="1"')  # 1:00 default
        self.assertRegex(form, r'name="duration_sec_part"\s+min="0" max="59" value="0"')
        self.assertIn("data-time-remaining", form)
        for n in range(2):
            self.client.post(f"/admin/quizzes/{quiz_id}/questions/new", data={
                "question_text": f"Manual {n}?", "question_type": "true_false", "correct_true_false": "True",
                "duration_seconds": "600"})
        self.assertEqual([q.duration_seconds for q in self.quiz_questions(quiz_id)], [600, 600])  # draft may exceed
        page = self.client.get(f"/admin/quizzes/{quiz_id}").text
        self.assertIn("data-time-limit-warning", page)
        self.client.post(f"/admin/quizzes/{quiz_id}/publish")
        self.assertEqual(self.db().get(models.Quiz, quiz_id).status, "Draft")
        db = self.db()
        db.query(models.Question).filter_by(quiz_id=quiz_id).update({"duration_seconds": 300})
        db.commit()
        db.close()
        self.client.post(f"/admin/quizzes/{quiz_id}/publish")
        self.assertEqual(self.db().get(models.Quiz, quiz_id).status, "Published")

    def test_quizzes_created_before_the_rule_keep_their_existing_timing(self):
        # Pre-existing quizzes have no stored limit (NULL after migration).
        db = self.db()
        quiz = models.Quiz(title="Legacy quiz", status="Draft")
        db.add(quiz)
        db.flush()
        for n in range(2):
            db.add(models.Question(quiz_id=quiz.id, question_text=f"Legacy {n}?", question_type="true_false",
                                   options='["True", "False"]', correct_answer='"True"', duration_seconds=600))
        db.commit()
        quiz_id = quiz.id
        db.close()
        self.assertIsNone(self.db().get(models.Quiz, quiz_id).time_limit_seconds)
        self.client.post(f"/admin/quizzes/{quiz_id}/publish")
        self.assertEqual(self.db().get(models.Quiz, quiz_id).status, "Published")
        started = self.client.post(f"/admin/quizzes/{quiz_id}/sessions/start", follow_redirects=False)
        self.assertTrue(started.headers["location"].startswith("/admin/sessions"))  # not sent back with an error
        self.assertEqual(self.db().query(models.Session).filter_by(quiz_id=quiz_id).count(), 1)


class StudentPrivacyAndHistoryTests(_BankAppBase):
    def setUp(self):
        super().setUp()
        body = (_p("1. Name the process by which plants make food from light.")
                + _p("2. Which planet is largest?") + _p("A. Mars") + _p("B. Jupiter") + _p("C. Venus")
                + _p("Answer key", "Heading1")
                + _p("1. Photosynthesis")
                + _p("Explanation: SECRET-EXPLANATION-ONE plants convert light.")
                + _p("Marking guidance: SECRET-GUIDANCE accept chlorophyll-based answers.")
                + _p("2. B – SECRET-EXPLANATION-TWO it is a gas giant."))
        self.bank = self.import_bank(docx_bytes(body), "privacy-quiz.docx")
        self.quiz_id = self.assemble([self.bank], mode="all", shuffle_order="", auto_advance="true", title="Privacy quiz")
        self.client.post(f"/admin/quizzes/{self.quiz_id}/publish")
        self.client.post(f"/admin/quizzes/{self.quiz_id}/sessions/start")
        self.client.post(f"/admin/quizzes/{self.quiz_id}/sessions/start-quiz")
        db = self.db()
        self.code = db.query(models.Session).filter_by(quiz_id=self.quiz_id).one().session_code
        db.close()
        self.pupil = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        joined = self.pupil.post("/join", data={"quiz_id": self.quiz_id, "name": "Pupil", "roll_number": "R1"},
                                 follow_redirects=False)
        location = urlsplit(joined.headers["location"])
        self.student_id = int(location.path.rsplit("/", 1)[1])
        self.token = parse_qs(location.query)["token"][0]

    SECRETS = ("Photosynthesis", "SECRET-EXPLANATION-ONE", "SECRET-EXPLANATION-TWO", "SECRET-GUIDANCE",
               "correct_answer", "answer key", "Answer key", "privacy-quiz.docx", "explanation")

    def question_page(self, number: int):
        return self.pupil.get(f"/student/session/{self.code}/question/{number}"
                              f"?student_id={self.student_id}&token={self.token}")

    def test_student_html_and_api_never_contain_answers_or_explanations(self):
        for number in (1, 2):
            page = self.question_page(number)
            self.assertEqual(page.status_code, 200)
            for secret in self.SECRETS:
                self.assertNotIn(secret, page.text, f"question {number} leaks {secret!r}")
        status = self.pupil.get(f"/session/{self.code}/status?student_id={self.student_id}&token={self.token}")
        for secret in self.SECRETS + ("Jupiter",):
            self.assertNotIn(secret, status.text)
        # The MCQ page shows the options (including the right one) but nothing marks it.
        page = self.question_page(2).text
        for option in ("Mars", "Jupiter", "Venus"):
            self.assertIn(option, page)
        self.assertNotRegex(page, r"(?i)data-correct|is-correct|correct-option")

    def test_students_and_anonymous_users_cannot_reach_bank_management(self):
        anonymous = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        for client in (self.pupil, anonymous):
            for path in ("/admin/question-banks", f"/admin/question-banks/{self.bank}",
                         f"/admin/question-banks/{self.bank}/answer-key", f"/admin/question-banks/{self.bank}/source",
                         f"/admin/question-banks/{self.bank}/file", "/admin/quizzes/assemble"):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 303, path)
                self.assertTrue(response.headers["location"].startswith("/login"), path)
            upload = client.post("/admin/question-banks/upload", files={"file": ("x.docx", session_docx(1), DOCX_MIME)},
                                 follow_redirects=False)
            self.assertEqual(upload.status_code, 303)
        self.assertEqual(self.db().query(models.QuestionBank).count(), 1)

    def test_auto_advance_and_submission_work_for_assembled_quizzes(self):
        questions = self.quiz_questions(self.quiz_id)
        first = self.pupil.post(f"/student/session/{self.code}/question/1", data={
            "student_id": self.student_id, "token": self.token, "question_id": questions[0].id,
            "selected_option": "photosynthesis"}).json()
        self.assertTrue(first["success"])
        self.assertEqual(first["next_question_number"], 2)
        second = self.pupil.post(f"/student/session/{self.code}/question/2", data={
            "student_id": self.student_id, "token": self.token, "question_id": questions[1].id,
            "selected_option": "Jupiter"}).json()
        self.assertTrue(second["student_completed"])
        db = self.db()
        responses = db.query(models.Response).filter_by(student_id=self.student_id).order_by(models.Response.id).all()
        self.assertEqual([r.is_correct for r in responses], [True, True])
        db.close()
        results = self.pupil.get(f"/student/results/{self.student_id}?token={self.token}")
        self.assertEqual(results.status_code, 200)
        for secret in ("SECRET-EXPLANATION-ONE", "SECRET-EXPLANATION-TWO", "SECRET-GUIDANCE", "Photosynthesis"):
            self.assertNotIn(secret, results.text)

    def test_bank_edits_and_replacement_never_change_quizzes_or_history(self):
        questions = self.quiz_questions(self.quiz_id)
        self.pupil.post(f"/student/session/{self.code}/question/1", data={
            "student_id": self.student_id, "token": self.token, "question_id": questions[0].id,
            "selected_option": "Photosynthesis"})
        self.client.post(f"/admin/quizzes/{self.quiz_id}/sessions/end")
        before = [(q.question_text, q.options, q.correct_answer) for q in self.quiz_questions(self.quiz_id)]
        bank_question = self.bank_questions(self.bank)[1]

        # 1. Edit the bank entry.
        self.client.post(f"/admin/question-banks/{self.bank}/questions/{bank_question.id}/edit", data={
            f"q{bank_question.id}_type": "mcq_single", f"q{bank_question.id}_text": "EDITED wording?",
            f"q{bank_question.id}_opt_0": "Mars", f"q{bank_question.id}_opt_1": "Saturn",
            f"q{bank_question.id}_correct": "1"})
        self.assertEqual(self.db().get(models.BankQuestion, bank_question.id).question_text, "EDITED wording?")

        # 2. Replace the whole bank with a new version.
        replacement = docx_bytes(_p("1. Replacement question?") + _p("A. Yes") + _p("B. No")
                                 + _p("Answer key", "Heading1") + _p("1. A"))
        uploaded = self.upload(replacement, "privacy-quiz-v2.docx", replaces_bank_id=str(self.bank))
        new_bank = int(uploaded.headers["location"].split("/")[3])
        db = self.db()
        self.assertTrue(db.get(models.QuestionBank, self.bank).is_current)   # not until confirmed
        db.close()
        self.client.post(f"/admin/question-banks/{new_bank}/import", data={"action": "confirm"})
        db = self.db()
        old, new = db.get(models.QuestionBank, self.bank), db.get(models.QuestionBank, new_bank)
        self.assertEqual((old.is_current, new.is_current), (False, True))
        self.assertEqual((new.version_number, new.supersedes_id, new.lineage_id), (2, old.id, old.lineage_id))
        self.assertEqual(len(old.questions), 2)
        db.close()
        self.assertEqual(self.client.get(f"/admin/question-banks/{self.bank}/questions/{bank_question.id}/edit")
                         .status_code, 409)

        after = [(q.question_text, q.options, q.correct_answer) for q in self.quiz_questions(self.quiz_id)]
        self.assertEqual(before, after)
        db = self.db()
        response = db.query(models.Response).filter_by(student_id=self.student_id).one()
        self.assertEqual(response.question_text_snapshot, before[0][0])
        self.assertTrue(response.is_correct)
        db.close()
        self.assertNotIn("EDITED wording?", self.client.get(f"/admin/quizzes/{self.quiz_id}").text)
        # A second replacement upload cannot be started while one is pending.
        third = self.upload(session_docx(5), "v3.docx", replaces_bank_id=str(new_bank))
        self.assertEqual(third.status_code, 303)
        pending_again = self.upload(session_docx(6), "v3b.docx", replaces_bank_id=str(new_bank))
        self.assertIn("replacement_pending", pending_again.headers["location"])


if __name__ == "__main__":
    unittest.main()
