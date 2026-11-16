"""Phase 3A: structured document extraction, precise source references and
quiz-question provenance."""

import json
import os
import re
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as smartquiz_app
import auth
import database
import document_extraction
import learning_content_ai
import models
from config import settings
from database import Base, get_db
from tests.test_document_ai_generation import _make_text_pdf

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


# --------------------------------------------------------------- fixtures

def _p(inner: str, props: str = "") -> str:
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{inner}</w:p>"


def _r(text_value: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text_value}</w:t></w:r>'


def _numbered(text_value: str, num_id: int, ilvl: int = 0) -> str:
    return _p(_r(text_value), f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>')


LESSON_DOCUMENT = (
    f'<w:document xmlns:w="{W_NS}" xmlns:mc="{MC_NS}" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"><w:body>'
    + _p(_r("Module 2: Data-Driven Decision Making"), '<w:pStyle w:val="Title"/>')
    + _p(_r("Overview"), '<w:pStyle w:val="berschrift1"/>')
    + _p(_r("Course: Business Analytics") + "<w:r><w:br/></w:r>" + _r("Programme: MBA, First Year"))
    + _p(_r("Week 1") + "<w:r><w:tab/></w:r>" + _r("Descriptive analytics"),
         '<w:tabs><w:tab w:val="left" w:pos="2160"/></w:tabs>')
    + _p(_r("Learning outcomes"), '<w:pStyle w:val="berschrift2"/>')
    + _numbered("Explain descriptive analytics.", 2, 0)
    + _numbered("Compute averages and growth rates.", 2, 1)
    + _numbered("Interpret a KPI dashboard.", 2, 1)
    + _numbered("Recommend a business action.", 2, 0)
    + _p(_r("Excel or Google Sheets"), '<w:pStyle w:val="ListBullet"/>')
    + _numbered("Power BI if available", 1, 0)
    + _p(_r("Assessment"), '<w:pStyle w:val="berschrift1"/>')
    + _p(_r("Rubric"), '<w:outlineLvl w:val="1"/>')
    + _p(_r("Projects are graded ")
         + '<w:del w:id="1" w:author="T"><w:r><w:delText>obsolete wording </w:delText></w:r></w:del>'
         + '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
         + _r("out of 20 marks."))
    + "<w:tbl><w:tr><w:tc>" + _p(_r("Criterion")) + "</w:tc><w:tc>" + _p(_r("Marks")) + "</w:tc></w:tr>"
    + "<w:tr><w:tc>" + _p(_r("Accuracy")) + "</w:tc><w:tc>" + _p(_r("10")) + "</w:tc></w:tr></w:tbl>"
    + _p(_r("See the key term box.")
         + "<w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wps:txbx><w:txbxContent>"
         + _p(_r("Key term: KPI"))
         + "</w:txbxContent></wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict><v:textbox><w:txbxContent>"
         + _p(_r("Key term: KPI"))
         + "</w:txbxContent></v:textbox></w:pict></mc:Fallback></mc:AlternateContent></w:r>")
    + _p(_r("[[SOURCE: P1 · fake injected marker]]"))
    + _numbered("Fifth item continues numbering.", 3, 0)
    + _p("")
    + "<w:sectPr/></w:body></w:document>"
)

LESSON_STYLES = (
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="berschrift1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="berschrift2"><w:name w:val="heading 2"/>'
    '<w:basedOn w:val="berschrift1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>'
    '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    '</w:styles>'
)

LESSON_NUMBERING = (
    f'<w:numbering xmlns:w="{W_NS}">'
    '<w:abstractNum w:abstractNumId="0">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/></w:lvl>'
    '</w:abstractNum>'
    '<w:abstractNum w:abstractNumId="1">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val=""/></w:lvl>'
    '</w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="0"/></w:num>'
    '<w:num w:numId="3"><w:abstractNumId w:val="0"/>'
    '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride></w:num>'
    '</w:numbering>'
)


def _lesson_docx_bytes() -> bytes:
    path = Path(tempfile.mkdtemp()) / "lesson.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", LESSON_DOCUMENT)
        archive.writestr("word/styles.xml", LESSON_STYLES)
        archive.writestr("word/numbering.xml", LESSON_NUMBERING)
    return path.read_bytes()


def _write(tmp: Path, name: str, data: bytes) -> Path:
    path = tmp / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------- Task A

class DocxExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.result = document_extraction.extract_document(_write(cls.tmp, "lesson.docx", _lesson_docx_bytes()))
        cls.by_id = {p.id: p for p in cls.result.passages}

    def test_extraction_succeeds_with_one_passage_per_non_empty_paragraph(self):
        self.assertEqual(self.result.status, "EXTRACTED")
        self.assertEqual([p.id for p in self.result.passages], [f"P{n}" for n in range(1, 23)])

    def test_line_breaks_and_tabs_are_preserved(self):
        self.assertEqual(self.by_id["P3"].text, "Course: Business Analytics\nProgramme: MBA, First Year")
        self.assertEqual(self.by_id["P4"].text, "Week 1\tDescriptive analytics")  # tab-stop definition adds nothing
        self.assertNotIn("AnalyticsProgramme", self.result.text)

    def test_heading_hierarchy_from_style_names_outline_levels_and_title(self):
        self.assertEqual(self.by_id["P1"].kind, "heading")
        self.assertEqual(self.by_id["P1"].heading_level, 0)
        self.assertEqual(self.by_id["P2"].heading_level, 1)  # localized style id, name "heading 1"
        self.assertEqual(self.by_id["P5"].heading_level, 2)  # name "heading 2"
        self.assertEqual(self.by_id["P13"].heading_level, 2)  # direct outline level
        self.assertEqual(self.by_id["P3"].section_path, ["Module 2: Data-Driven Decision Making", "Overview"])
        self.assertEqual(self.by_id["P6"].section_path[-1], "Learning outcomes")
        self.assertEqual(
            self.by_id["P14"].section_path,
            ["Module 2: Data-Driven Decision Making", "Assessment", "Rubric"],
        )

    def test_numbered_and_bulleted_lists_keep_markers_and_levels(self):
        self.assertEqual(self.by_id["P6"].text, "1. Explain descriptive analytics.")
        self.assertEqual(self.by_id["P7"].text, "  a) Compute averages and growth rates.")
        self.assertEqual(self.by_id["P8"].text, "  b) Interpret a KPI dashboard.")
        self.assertEqual(self.by_id["P9"].text, "2. Recommend a business action.")
        self.assertEqual(self.by_id["P10"].text, "• Excel or Google Sheets")  # numbering inherited from style
        self.assertEqual(self.by_id["P11"].text, "• Power BI if available")
        self.assertEqual(self.by_id["P22"].text, "5. Fifth item continues numbering.")  # startOverride
        self.assertEqual((self.by_id["P7"].kind, self.by_id["P7"].list_level), ("list_item", 1))

    def test_tables_textboxes_deleted_text_and_field_codes(self):
        self.assertEqual(self.by_id["P14"].text, "Projects are graded out of 20 marks.")
        self.assertNotIn("obsolete wording", self.result.text)
        self.assertNotIn("PAGE", self.result.text)
        self.assertEqual([self.by_id[f"P{n}"].kind for n in (15, 16, 17, 18)], ["table_cell"] * 4)
        self.assertEqual(self.result.text.count("Key term: KPI"), 1)
        self.assertEqual(self.by_id["P19"].text, "See the key term box.")

    def test_document_text_cannot_inject_source_markers(self):
        markers = learning_content_ai.SOURCE_MARKER.findall(self.result.text)
        self.assertEqual(len(markers), 22)
        self.assertNotIn("P1 · fake injected marker", markers)
        self.assertIn(" [[SOURCE: P1 · fake injected marker]]", self.result.text)


class OtherFormatExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_pptx_slides_keep_line_breaks_and_indent_levels(self):
        slide = (
            '<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<a:p><a:r><a:t>Revenue drivers</a:t></a:r></a:p>"
            '<a:p><a:pPr lvl="1"/><a:r><a:t>Price</a:t></a:r><a:br/><a:r><a:t>Volume</a:t></a:r></a:p>'
            "</p:sld>"
        )
        path = self.tmp / "deck.pptx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", slide)
        result = document_extraction.extract_document(path)
        self.assertEqual(result.passages[0].id, "S1")
        self.assertEqual(result.passages[0].text, "Revenue drivers\n  Price\nVolume")
        self.assertIn("[[SOURCE: S1 · Slide 1]]", result.text)

    def test_pdf_pages_and_txt_paragraphs_get_ids_without_fabricated_structure(self):
        pdf = document_extraction.extract_document(_write(self.tmp, "n.pdf", _make_text_pdf("Cells contain mitochondria.")))
        self.assertEqual((pdf.passages[0].id, pdf.passages[0].page, pdf.passages[0].paragraph), ("PG1", 1, None))
        txt = document_extraction.extract_document(_write(
            self.tmp, "n.txt", b"First line\nsecond line\tafter tab\n\nNext paragraph"))
        self.assertEqual([p.id for p in txt.passages], ["P1", "P2"])
        self.assertEqual(txt.passages[0].text, "First line\nsecond line\tafter tab")
        self.assertTrue(all(p.page is None and p.slide is None and not p.section_path for p in txt.passages))

    def test_docx_passages_never_claim_pages_or_slides(self):
        result = document_extraction.extract_document(_write(self.tmp, "l.docx", _lesson_docx_bytes()))
        self.assertTrue(all(p.page is None and p.slide is None for p in result.passages))
        self.assertNotIn("Page ", result.text.replace("PAGE", ""))


# --------------------------------------------------------------- Task B

class SourceReferenceTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.result = document_extraction.extract_document(_write(tmp, "l.docx", _lesson_docx_bytes()))
        _, references = learning_content_ai._source_body(self.result.text)
        self.citations = learning_content_ai._citation_map(references)

    def test_each_paragraph_has_its_own_distinct_reference(self):
        labels = [p.label for p in self.result.passages]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(self.result.passages[5].label, "P6 · Paragraph 6 · Section: Learning outcomes")
        self.assertEqual(self.result.passages[13].label, "P14 · Paragraph 14 · Section: Rubric")

    def test_citation_resolves_by_id_location_or_full_label(self):
        items = [{"source_reference": "P14"}, {"source_reference": "Paragraph 3"},
                 {"source_reference": "P6 · Paragraph 6 · Section: Learning outcomes"}]
        learning_content_ai._validate_references(items, self.citations)
        self.assertEqual([i["source_passage_id"] for i in items], ["P14", "P3", "P6"])
        self.assertEqual(items[1]["source_reference"], "P3 · Paragraph 3 · Section: Overview")

    def test_unknown_or_whole_section_citation_is_rejected(self):
        for bad in ("P99", "Section: Overview", "Document text"):
            with self.assertRaises(learning_content_ai.ContentGenerationError):
                learning_content_ai._validate_references([{"source_reference": bad}], self.citations)

    def test_prompt_offers_short_passage_ids(self):
        prompt, _ = learning_content_ai._build_prompt("flashcards", self.result.text, "Lesson", 2)
        self.assertIn('"P1", "P2", "P3"', prompt)

    def test_legacy_section_labels_remain_valid(self):
        legacy = "[[SOURCE: Section: Tools]]\nExcel is used for pivot tables.\n\n[[SOURCE: Page 2]]\nPower BI dashboards."
        _, references = learning_content_ai._source_body(legacy)
        citations = learning_content_ai._citation_map(references)
        items = [{"source_reference": "Section: Tools"}, {"source_reference": "Page 2"}]
        learning_content_ai._validate_references(items, citations)
        self.assertEqual([(i["source_reference"], i["source_passage_id"]) for i in items],
                         [("Section: Tools", None), ("Page 2", None)])


# --------------------------------------------------------------- app workflow base

class _AppTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        db = self.Session()
        db.add(models.AppSetting(key=auth.PASSWORD_HASH_KEY, value=auth.hash_password("Teacher-test-password-2026")))
        course = models.Course(name="Business Analytics", code="BA", description="")
        db.add(course)
        db.flush()
        module = models.LearningModule(course_id=course.id, title="Data-driven decisions", description="",
                                       learning_objectives="", status="PUBLISHED")
        db.add(module)
        db.commit()
        self.module_id = module.id
        db.close()
        self.previous_override = smartquiz_app.app.dependency_overrides.get(get_db)

        def override_get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        smartquiz_app.app.dependency_overrides[get_db] = override_get_db
        self.previous_upload_dir = smartquiz_app.LEARNING_UPLOADS_DIR
        self.temp_storage = tempfile.TemporaryDirectory()
        smartquiz_app.LEARNING_UPLOADS_DIR = Path(self.temp_storage.name)
        self.previous_public_base_url = settings.PUBLIC_BASE_URL
        settings.PUBLIC_BASE_URL = ""
        self.client = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        login = self.client.post("/login", data={"password": "Teacher-test-password-2026"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)

    def tearDown(self):
        if self.previous_override is None:
            smartquiz_app.app.dependency_overrides.pop(get_db, None)
        else:
            smartquiz_app.app.dependency_overrides[get_db] = self.previous_override
        smartquiz_app.LEARNING_UPLOADS_DIR = self.previous_upload_dir
        settings.PUBLIC_BASE_URL = self.previous_public_base_url
        self.temp_storage.cleanup()
        self.engine.dispose()

    def db(self):
        return self.Session()

    def upload_lesson(self) -> int:
        response = self.client.post(
            f"/admin/learning/modules/{self.module_id}/documents/upload",
            files={"file": ("Module 2 lesson.docx", _lesson_docx_bytes(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"title": "Module 2 lesson"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["document"]["id"]

    def create_draft_quiz(self, title="Analytics quiz") -> int:
        response = self.client.post("/admin/quizzes/new", data={"title": title}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        db = self.db()
        quiz_id = db.query(models.Quiz).filter_by(title=title).one().id
        db.close()
        return quiz_id


FLASHCARDS = [
    {"front": "Which skill does the first learning outcome name?", "back": "Explaining descriptive analytics.",
     "explanation": "", "topic": "Outcomes", "source_reference": "P6"},
    {"front": "How many marks are projects graded out of?", "back": "20 marks.",
     "explanation": "", "topic": "Assessment", "source_reference": "P14"},
]
QUESTIONS = [
    {"question_text": "Which final learning outcome asks students to act?", "question_type": "mcq_single",
     "answer_options": ["Recommend a business action", "Build a database", "Write SQL", "Design a logo"],
     "correct_answer": "Recommend a business action", "explanation": "Outcome 2.", "topic": "Outcomes",
     "source_reference": "P9"},
    {"question_text": "Projects are graded out of 20 marks.", "question_type": "true_false",
     "correct_answer": "True", "explanation": "Stated in the rubric.", "topic": "Assessment",
     "source_reference": "P14"},
    {"question_text": "Which programme is this course part of?", "question_type": "short_answer",
     "correct_answer": ["MBA", "MBA, First Year"], "explanation": "Overview lists the programme.",
     "topic": "Overview", "source_reference": "Paragraph 3"},
]


def _fake_provider(items):
    def provider(prompt, provider, model, api_key):
        return json.dumps({"items": items})
    return provider


GEN_ENV = {"AI_CONTENT_PROVIDER": "gemini", "GEMINI_API_KEY": "test-key-not-sent", "AI_CONTENT_MODEL": "test-model"}


class GenerationProvenanceTests(_AppTestBase):
    def test_upload_stores_passage_index_on_version(self):
        document_id = self.upload_lesson()
        db = self.db()
        version = db.query(models.DocumentVersion).filter_by(document_id=document_id).one()
        passages = {p["id"]: p for p in json.loads(version.source_passages)}
        db.close()
        self.assertEqual(len(passages), 22)
        self.assertEqual(passages["P7"]["kind"], "list_item")
        self.assertEqual(passages["P7"]["list_marker"], "a)")
        self.assertEqual(passages["P14"]["section_path"][-1], "Rubric")
        self.assertNotIn("text", passages["P14"])  # passage text is not duplicated

    def test_generated_flashcards_and_questions_record_document_version_and_passage(self):
        document_id = self.upload_lesson()
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(FLASHCARDS)):
            cards = self.client.post(f"/admin/learning/modules/{self.module_id}/generate/flashcards",
                                     data={"document_id": document_id, "count": "2"})
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(QUESTIONS)):
            questions = self.client.post(f"/admin/learning/documents/{document_id}/generate/questions",
                                         data={"count": "3"})
        self.assertEqual(cards.status_code, 200, cards.text)
        self.assertEqual(questions.status_code, 200, questions.text)
        db = self.db()
        version_id = db.query(models.DocumentVersion).filter_by(document_id=document_id).one().id
        card_rows = db.query(models.Flashcard).order_by(models.Flashcard.id).all()
        question_rows = db.query(models.GeneratedQuestion).order_by(models.GeneratedQuestion.id).all()
        self.assertEqual([(c.source_document_id, c.source_document_version_id, c.source_passage_id) for c in card_rows],
                         [(document_id, version_id, "P6"), (document_id, version_id, "P14")])
        self.assertEqual([q.source_passage_id for q in question_rows], ["P9", "P14", "P3"])
        self.assertEqual(question_rows[2].source_reference, "P3 · Paragraph 3 · Section: Overview")
        db.close()
        source = cards.json()["flashcards"][1]["source"]
        self.assertEqual(source, {
            "document_id": document_id, "document_version_id": version_id,
            "source_reference": "P14 · Paragraph 14 · Section: Rubric", "passage_id": "P14",
            "section_path": ["Module 2: Data-Driven Decision Making", "Assessment", "Rubric"],
            "paragraph": 14, "page": None, "slide": None,
        })
        self.assertEqual(questions.json()["questions"][0]["source"]["paragraph"], 9)


class QuizProvenanceTests(_AppTestBase):
    def _approved_question(self) -> tuple[int, int]:
        document_id = self.upload_lesson()
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(QUESTIONS)):
            response = self.client.post(f"/admin/learning/documents/{document_id}/generate/questions",
                                        data={"count": "3"})
        self.assertEqual(response.status_code, 200, response.text)
        question_id = response.json()["questions"][0]["id"]
        self.client.post(f"/admin/learning/questions/{question_id}/status", data={"status": "APPROVED"})
        return document_id, question_id

    def _import(self, question_id, quiz_id):
        return self.client.post(f"/admin/learning/questions/{question_id}/add-to-quiz",
                                data={"quiz_id": quiz_id}, follow_redirects=False)

    def test_import_preserves_provenance_without_duplicating_content(self):
        document_id, generated_id = self._approved_question()
        quiz_id = self.create_draft_quiz()
        self.assertEqual(self._import(generated_id, quiz_id).status_code, 303)
        db = self.db()
        question = db.query(models.Question).filter_by(quiz_id=quiz_id).one()
        version_id = db.query(models.DocumentVersion).filter_by(document_id=document_id).one().id
        self.assertEqual(
            (question.generated_question_id, question.source_document_id, question.source_document_version_id,
             question.source_passage_id, question.source_reference),
            (generated_id, document_id, version_id, "P9", "P9 · Paragraph 9 · Section: Learning outcomes"),
        )
        question_id = question.id
        db.close()
        provenance = self.client.get(f"/admin/quizzes/{quiz_id}/questions/{question_id}/provenance").json()
        self.assertEqual(provenance["origin"], "generated")
        self.assertEqual(provenance["generated_question_id"], generated_id)
        self.assertEqual(provenance["document_title"], "Module 2 lesson")
        self.assertEqual(provenance["document_version_number"], 1)
        self.assertEqual(provenance["source"]["section_path"][-1], "Learning outcomes")
        self.assertEqual(provenance["source"]["paragraph"], 9)
        builder = self.client.get(f"/admin/quizzes/{quiz_id}")
        self.assertIn("Generated from", builder.text)
        self.assertIn("P9 · Paragraph 9 · Section: Learning outcomes", builder.text)

    def test_provenance_survives_quiz_builder_edit_and_duplicate_and_blocks_reimport(self):
        _, generated_id = self._approved_question()
        quiz_id = self.create_draft_quiz()
        self._import(generated_id, quiz_id)
        db = self.db()
        question_id = db.query(models.Question).filter_by(quiz_id=quiz_id).one().id
        db.close()
        edit = self.client.post(f"/admin/quizzes/{quiz_id}/questions/{question_id}/edit", data={
            "question_text": "Teacher-reworded: which outcome asks students to act?",
            "question_type": "mcq_single", "points": "2", "duration_seconds": "60",
            "option_a": "Recommend a business action", "option_b": "Build a database",
            "option_c": "Write SQL", "option_d": "Design a logo", "correct_single": "A",
        }, follow_redirects=False)
        self.assertEqual(edit.status_code, 303)
        again = self._import(generated_id, quiz_id)
        self.assertIn("question_already_added", again.headers["location"])
        duplicate = self.client.post(f"/admin/quizzes/{quiz_id}/questions/{question_id}/duplicate",
                                     follow_redirects=False)
        self.assertEqual(duplicate.status_code, 303)
        db = self.db()
        rows = db.query(models.Question).filter_by(quiz_id=quiz_id).order_by(models.Question.id).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].question_text, "Teacher-reworded: which outcome asks students to act?")
        self.assertEqual({(r.generated_question_id, r.source_passage_id) for r in rows}, {(generated_id, "P9")})
        db.close()

    def test_manual_quiz_questions_still_create_and_edit_without_provenance(self):
        quiz_id = self.create_draft_quiz("Manual quiz")
        created = self.client.post(f"/admin/quizzes/{quiz_id}/questions/new", data={
            "question_text": "What does KPI stand for?", "question_type": "short_answer",
            "duration_seconds": "60", "accepted_answers": "Key performance indicator",
        }, follow_redirects=False)
        self.assertEqual(created.status_code, 303, created.text)
        db = self.db()
        question = db.query(models.Question).filter_by(quiz_id=quiz_id).one()
        question_id = question.id
        self.assertIsNone(question.generated_question_id)
        db.close()
        edited = self.client.post(f"/admin/quizzes/{quiz_id}/questions/{question_id}/edit", data={
            "question_text": "What does KPI mean?", "question_type": "short_answer",
            "duration_seconds": "60", "accepted_answers": "Key performance indicator",
        }, follow_redirects=False)
        self.assertEqual(edited.status_code, 303)
        self.assertEqual(self.client.get(f"/admin/quizzes/{quiz_id}/questions/{question_id}/provenance").json(),
                         {"question_id": question_id, "origin": "manual", "source": None})
        builder = self.client.get(f"/admin/quizzes/{quiz_id}")
        self.assertIn("What does KPI mean?", builder.text)
        self.assertNotIn("Generated from", builder.text)

    def test_historical_results_keep_their_snapshot_after_imported_question_is_edited(self):
        _, generated_id = self._approved_question()
        quiz_id = self.create_draft_quiz()
        self._import(generated_id, quiz_id)
        db = self.db()
        question = db.query(models.Question).filter_by(quiz_id=quiz_id).one()
        live = models.Session(quiz_id=quiz_id, session_code="HIST01", status="Completed")
        db.add(live)
        db.flush()
        student = models.Student(quiz_id=quiz_id, session_id=live.id, name="Past Student", roll_number="R1",
                                 status="Finished", result_token="t" * 43)
        db.add(student)
        db.flush()
        db.add(models.Response(
            student_id=student.id, session_id=live.id, quiz_id=quiz_id, question_id=question.id,
            selected_option="Recommend a business action", is_correct=True, score=1,
            submitted_at=datetime.now(timezone.utc), question_text_snapshot=question.question_text,
            question_type_snapshot="mcq_single", question_options_snapshot=question.options,
            correct_answer_snapshot=question.correct_answer, question_points_snapshot=1,
        ))
        db.commit()
        ids = (question.id, live.id, student.id, question.question_text)
        db.close()
        question_id, session_id, student_id, original_text = ids
        before = self.client.get(f"/admin/results/data?session_id={session_id}").json()
        self.client.post(f"/admin/quizzes/{quiz_id}/questions/{question_id}/edit", data={
            "question_text": "Edited after the session", "question_type": "mcq_single", "points": "1",
            "duration_seconds": "60", "option_a": "Recommend a business action", "option_b": "Build a database",
            "option_c": "Write SQL", "option_d": "Design a logo", "correct_single": "A",
        })
        after = self.client.get(f"/admin/results/data?session_id={session_id}").json()
        self.assertEqual(before, after)
        page = TestClient(smartquiz_app.app).get(f"/student/results/{student_id}?token={'t' * 43}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(original_text, page.text)
        self.assertNotIn("Edited after the session", page.text)


# --------------------------------------------------------------- migration / legacy compatibility

NEW_COLUMNS = {
    "document_versions": ["source_passages"],
    "flashcards": ["source_passage_id"],
    "generated_questions": ["source_passage_id"],
    "questions": ["generated_question_id", "source_document_id", "source_document_version_id",
                  "source_reference", "source_passage_id"],
}


class MigrationCompatibilityTests(unittest.TestCase):
    """A database created before Phase 3A, with real legacy rows, is upgraded in place."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'legacy.db'}")
        # The pre-Phase-3A schema: every current table minus the Phase 3A columns.
        legacy = MetaData()
        for table in Base.metadata.sorted_tables:
            Table(table.name, legacy, *[
                column._copy() for column in table.columns if column.name not in NEW_COLUMNS.get(table.name, [])
            ])
        legacy.create_all(bind=self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO courses (id, name, code, description, created_at) VALUES (1,'C','C','',:t)"),
                         {"t": datetime.now(timezone.utc)})
            conn.execute(text("INSERT INTO learning_modules (id, course_id, title, description, learning_objectives, status, created_at) "
                              "VALUES (1,1,'M','','','PUBLISHED',:t)"), {"t": datetime.now(timezone.utc)})
            conn.execute(text(
                "INSERT INTO documents (id, course_id, module_id, title, original_filename, storage_filename, mime_type, "
                "file_extension, file_size_bytes, extraction_status, extracted_text, is_archived, created_at, updated_at) "
                "VALUES (1,1,1,'Old notes','old.docx','old_1.docx','x','.docx',10,'EXTRACTED',:txt,0,:t,:t)"),
                {"txt": "[[SOURCE: Section: Tools]]\nExcel is used for pivot tables.", "t": datetime.now(timezone.utc)})
            conn.execute(text(
                "INSERT INTO document_versions (id, document_id, version_number, original_filename, storage_filename, "
                "mime_type, sha256, extraction_status, extracted_text, is_current, created_at) "
                "VALUES (1,1,1,'old.docx','old_1.docx','x','abc','EXTRACTED',:txt,1,:t)"),
                {"txt": "[[SOURCE: Section: Tools]]\nExcel is used for pivot tables.", "t": datetime.now(timezone.utc)})
            conn.execute(text(
                "INSERT INTO flashcards (id, module_id, source_document_id, source_document_version_id, front, back, "
                "explanation, topic, difficulty, source_reference, approval_status, generated_at) "
                "VALUES (1,1,1,1,'What is Excel used for here?','Pivot tables','','Tools','medium','Section: Tools','APPROVED',:t)"),
                {"t": datetime.now(timezone.utc)})
            conn.execute(text(
                "INSERT INTO generated_questions (id, module_id, source_document_id, source_document_version_id, "
                "question_text, question_type, answer_options, correct_answer, explanation, points_suggestion, "
                "difficulty, topic, source_reference, approval_status, generated_at) VALUES "
                "(1,1,1,1,'Excel is used for pivot tables.','true_false','[\"True\",\"False\"]','\"True\"','',1,"
                "'medium','Tools','Section: Tools','APPROVED',:t)"), {"t": datetime.now(timezone.utc)})
            conn.execute(text("INSERT INTO quizzes (id, title, description, duration_minutes, passing_percentage, status, "
                              "auto_advance, created_at) VALUES (1,'Old quiz','',10,60,'Draft',0,:t)"),
                         {"t": datetime.now(timezone.utc)})
            conn.execute(text("INSERT INTO questions (id, quiz_id, question_text, question_type, points, options, "
                              "correct_answer, created_at, duration_seconds) VALUES "
                              "(1,1,'Old manual question?','true_false',1,'[\"True\",\"False\"]','\"True\"',:t,600)"),
                         {"t": datetime.now(timezone.utc)})

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def _columns(self, table):
        with self.engine.connect() as conn:
            return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}

    def _migrate(self):
        with patch.object(database, "engine", self.engine):
            database._run_migrations()

    def test_migration_adds_nullable_columns_preserves_rows_and_is_idempotent(self):
        for table, columns in NEW_COLUMNS.items():
            self.assertFalse(set(columns) & self._columns(table), table)
        with self.engine.connect() as conn:
            before = {t: conn.execute(text(f"SELECT * FROM {t} ORDER BY id")).fetchall() for t in NEW_COLUMNS}
        self._migrate()
        self._migrate()  # second run is a no-op
        for table, columns in NEW_COLUMNS.items():
            self.assertTrue(set(columns) <= self._columns(table), table)
        with self.engine.connect() as conn:
            for table, rows in before.items():
                after = conn.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().fetchall()
                self.assertEqual(len(after), len(rows))
                self.assertTrue(all(row[c] is None for row in after for c in NEW_COLUMNS[table]))
                self.assertEqual([tuple(r.values())[:len(rows[0])] for r in after], [tuple(r) for r in rows])
            self.assertEqual(conn.execute(text("PRAGMA integrity_check")).scalar(), "ok")

    def test_legacy_content_works_with_new_code_after_migration(self):
        self._migrate()
        Session = sessionmaker(bind=self.engine)

        def override_get_db():
            session = Session()
            try:
                yield session
            finally:
                session.close()

        previous = smartquiz_app.app.dependency_overrides.get(get_db)
        smartquiz_app.app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
            db = Session()
            db.add(models.AppSetting(key=auth.PASSWORD_HASH_KEY, value=auth.hash_password("Teacher-test-password-2026")))
            db.commit()
            db.close()
            client.post("/login", data={"password": "Teacher-test-password-2026"})
            dashboard = client.get("/admin/learning/dashboard")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("Section: Tools", dashboard.text)
            imported = client.post("/admin/learning/questions/1/add-to-quiz", data={"quiz_id": 1}, follow_redirects=False)
            self.assertEqual(imported.status_code, 303)
            db = Session()
            question = db.query(models.Question).filter_by(generated_question_id=1).one()
            self.assertEqual((question.source_document_version_id, question.source_reference, question.source_passage_id),
                             (1, "Section: Tools", None))
            old_manual = db.get(models.Question, 1)
            self.assertIsNone(old_manual.generated_question_id)
            new_id = question.id
            db.close()
            provenance = client.get(f"/admin/quizzes/1/questions/{new_id}/provenance").json()
            self.assertEqual(provenance["source"]["passage_id"], None)
            self.assertEqual(provenance["source"]["section_path"], [])
            self.assertEqual(client.get("/admin/quizzes/1/questions/1/provenance").json()["origin"], "manual")
            self.assertEqual(client.get("/admin/quizzes/1").status_code, 200)
        finally:
            if previous is None:
                smartquiz_app.app.dependency_overrides.pop(get_db, None)
            else:
                smartquiz_app.app.dependency_overrides[get_db] = previous


if __name__ == "__main__":
    unittest.main()
