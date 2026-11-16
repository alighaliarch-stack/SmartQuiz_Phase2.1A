import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as smartquiz_app
import auth
import learning_content_ai
import models
from config import settings
from database import Base, get_db


def _make_text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 14 Tf 50 750 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class DocumentAIGenerationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        db = self.Session()
        db.add(models.AppSetting(
            key=auth.PASSWORD_HASH_KEY,
            value=auth.hash_password("Teacher-test-password-2026"),
        ))
        self.course = models.Course(name="Biology", code="BIO", description="", status="PUBLISHED")
        db.add(self.course)
        db.flush()
        self.module = models.LearningModule(
            course_id=self.course.id,
            title="Cell Biology",
            description="",
            learning_objectives="",
            status="PUBLISHED",
        )
        self.quiz = models.Quiz(
            title="Draft Biology Quiz",
            description="",
            duration_minutes=10,
            passing_percentage=60,
            status="Draft",
        )
        db.add_all([self.module, self.quiz])
        db.commit()
        self.module_id = self.module.id
        self.course_id = self.course.id
        self.quiz_id = self.quiz.id
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
        self.client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self._teacher_login()

    def tearDown(self):
        if self.previous_override is None:
            smartquiz_app.app.dependency_overrides.pop(get_db, None)
        else:
            smartquiz_app.app.dependency_overrides[get_db] = self.previous_override
        smartquiz_app.LEARNING_UPLOADS_DIR = self.previous_upload_dir
        settings.PUBLIC_BASE_URL = self.previous_public_base_url
        self.temp_storage.cleanup()
        self.engine.dispose()

    def _teacher_login(self):
        response = self.client.post(
            "/login",
            data={"password": "Teacher-test-password-2026"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def _upload_text_document(self, text="Mitochondria transform energy in cells."):
        response = self.client.post(
            f"/admin/learning/modules/{self.module_id}/documents/upload",
            files={"file": ("cell-energy.txt", text.encode("utf-8"), "text/plain")},
            data={"title": "Cell energy notes"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        document_data = response.json()["document"]
        self.assertEqual(document_data["extraction_status"], "EXTRACTED")
        self.assertGreater(document_data["extracted_characters"], 0)
        db = self.Session()
        document = db.query(models.Document).filter_by(id=document_data["id"]).one()
        version = db.query(models.DocumentVersion).filter_by(
            document_id=document.id,
            is_current=True,
        ).one()
        self.assertIn(text, document.extracted_text)
        self.assertEqual(version.extracted_text, document.extracted_text)
        db.close()
        return document_data["id"]

    def test_student_portal_link_copy_url_and_host_configuration(self):
        main_dashboard = self.client.get("/admin")
        self.assertIn('href="http://testserver/student/login"', main_dashboard.text)
        self.assertIn("Copy Student Login Link", main_dashboard.text)
        dashboard = self.client.get("/admin/learning/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('href="http://testserver/student/login"', dashboard.text)
        self.assertIn("Copy Student Login Link", dashboard.text)
        accounts = self.client.get("/admin/student-accounts")
        self.assertIn('data-portal-url="http://testserver/student/login"', accounts.text)
        self.assertIn("Copy Student Login Link", accounts.text)

        settings.PUBLIC_BASE_URL = "https://students.example.edu/smartquiz"
        configured = self.client.get("/admin/learning/dashboard")
        self.assertIn(
            'data-portal-url="https://students.example.edu/smartquiz/student/login"',
            configured.text,
        )
        account_page = self.client.get("/admin/student-accounts")
        self.assertIn(
            'href="https://students.example.edu/smartquiz/student/login"',
            account_page.text,
        )

    def test_unconfigured_external_host_is_not_used_for_portal_link(self):
        external_client = TestClient(
            smartquiz_app.app,
            base_url="http://untrusted.example",
            headers={"Origin": "http://untrusted.example"},
        )
        login = external_client.post(
            "/login",
            data={"password": "Teacher-test-password-2026"},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)
        response = external_client.get("/admin/learning/dashboard")
        self.assertEqual(response.status_code, 503)
        self.assertIn("PUBLIC_BASE_URL", response.text)

    def test_txt_docx_pptx_and_pdf_extraction_and_empty_pdf_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            txt_path = root / "notes.txt"
            txt_path.write_text(
                "Cell membranes regulate transport.\n\nMitochondria release usable energy.",
                encoding="utf-8",
            )
            text, status, error = smartquiz_app._extract_document_text(txt_path)
            self.assertEqual(status, "EXTRACTED")
            self.assertIsNone(error)
            self.assertIn("[[SOURCE: P1 · Paragraph 1]]", text)
            self.assertIn("Cell membranes regulate transport.", text)
            self.assertIn("[[SOURCE: P2 · Paragraph 2]]", text)

            docx_path = root / "notes.docx"
            docx_xml = b"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Cell Energy</w:t></w:r></w:p>
            <w:p><w:r><w:t>Mitochondria release usable energy.</w:t></w:r></w:p>
            </w:body></w:document>"""
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", docx_xml)
            text, status, error = smartquiz_app._extract_document_text(docx_path)
            self.assertEqual((status, error), ("EXTRACTED", None))
            self.assertIn("[[SOURCE: P1 · Paragraph 1 · Section: Cell Energy]]", text)
            self.assertIn("[[SOURCE: P2 · Paragraph 2 · Section: Cell Energy]]", text)
            self.assertIn("Mitochondria release usable energy.", text)

            pptx_path = root / "slides.pptx"
            with zipfile.ZipFile(pptx_path, "w") as archive:
                archive.writestr(
                    "ppt/slides/slide2.xml",
                    '<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Cell membrane transport</a:t></p:sld>',
                )
                archive.writestr(
                    "ppt/slides/slide1.xml",
                    '<p:sld xmlns:p="p" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Cell basics</a:t></p:sld>',
                )
            text, status, error = smartquiz_app._extract_document_text(pptx_path)
            self.assertEqual((status, error), ("EXTRACTED", None))
            self.assertLess(text.index("Slide 1"), text.index("Slide 2"))
            self.assertIn("Cell membrane transport", text)

            pdf_path = root / "notes.pdf"
            pdf_path.write_bytes(_make_text_pdf("Cells contain mitochondria."))
            text, status, error = smartquiz_app._extract_document_text(pdf_path)
            self.assertEqual((status, error), ("EXTRACTED", None))
            self.assertIn("[[SOURCE: PG1 · Page 1]]", text)
            self.assertIn("Cells contain mitochondria.", text)

            empty_pdf_path = root / "empty.pdf"
            empty_pdf_path.write_bytes(_make_text_pdf(""))
            text, status, error = smartquiz_app._extract_document_text(empty_pdf_path)
            self.assertEqual(text, "")
            self.assertEqual(status, "NO_TEXT")
            self.assertIsNotNone(error)

    def test_upload_extraction_is_private_inspectable_and_versioned(self):
        document_id = self._upload_text_document()
        inspection = self.client.get(f"/admin/learning/documents/{document_id}")
        self.assertEqual(inspection.status_code, 200)
        self.assertIn("Mitochondria transform energy in cells.", inspection.text)
        downloaded = self.client.get(f"/admin/learning/documents/{document_id}/file")
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn(b"Mitochondria transform energy in cells.", downloaded.content)
        student = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(
            student.get(
                f"/admin/learning/documents/{document_id}",
                follow_redirects=False,
            ).status_code,
            303,
        )

    def test_empty_extraction_blocks_generation_without_calling_provider(self):
        response = self.client.post(
            f"/admin/learning/modules/{self.module_id}/documents/upload",
            files={"file": ("empty.txt", b"", "text/plain")},
            data={"title": "Empty material"},
        )
        self.assertEqual(response.status_code, 200)
        document = response.json()["document"]
        self.assertEqual(document["extraction_status"], "NO_TEXT")
        self.assertEqual(document["extracted_characters"], 0)
        with patch.object(learning_content_ai, "_request_provider") as request_provider:
            generation = self.client.post(
                f"/admin/learning/documents/{document['id']}/generate/flashcards",
                data={"count": "1"},
            )
        self.assertEqual(generation.status_code, 422)
        self.assertIn("No extractable text", generation.json()["detail"])
        request_provider.assert_not_called()

    def test_genuine_generation_adapters_save_source_bound_pending_content_and_review(self):
        source_text = (
            "Mitochondria transform energy stored in nutrients into ATP through cellular respiration. "
            "The inner mitochondrial membrane contains an electron transport chain that pumps protons. "
            "Proton flow through ATP synthase powers the formation of ATP from ADP and phosphate."
        )
        document_id = self._upload_text_document(source_text)
        flashcards = [
            {
                "front": "How do mitochondria convert nutrient energy into ATP?",
                "back": "Cellular respiration transfers energy from nutrients to form ATP.",
                "explanation": "The source describes respiration and ATP production.",
                "topic": "Cell respiration",
                "source_reference": "Paragraph 1",
            },
            {
                "front": "What role does proton flow play in ATP formation?",
                "back": "Proton flow through ATP synthase powers ATP formation from ADP and phosphate.",
                "explanation": "This links the proton gradient to ATP synthesis.",
                "topic": "ATP synthase",
                "source_reference": "Paragraph 1",
            },
        ]
        flashcard_prompt = []

        def flashcard_provider(prompt, provider, model, api_key):
            flashcard_prompt.append(prompt)
            return json.dumps({"items": flashcards})

        with patch.dict(os.environ, {
            "AI_CONTENT_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test-key-not-sent",
            "AI_CONTENT_MODEL": "test-model",
        }), patch.object(
            learning_content_ai,
            "_request_provider",
            side_effect=flashcard_provider,
        ):
            generated_cards = self.client.post(
                f"/admin/learning/documents/{document_id}/generate/flashcards",
                data={"count": "2"},
            )
        self.assertEqual(generated_cards.status_code, 200, generated_cards.text)
        self.assertIn(source_text, flashcard_prompt[0])
        self.assertEqual(generated_cards.json()["provider"], "gemini")
        self.assertEqual(generated_cards.json()["model"], "test-model")
        self.assertNotIn("test-key-not-sent", generated_cards.text)
        card_ids = [item["id"] for item in generated_cards.json()["flashcards"]]

        generated_questions = [
            {
                "question_text": "Which process produces ATP from nutrient energy?",
                "question_type": "mcq_single",
                "answer_options": [
                    "Cellular respiration",
                    "DNA replication",
                    "Protein translation",
                    "Lipid storage",
                ],
                "correct_answer": "Cellular respiration",
                "explanation": "The document identifies cellular respiration as the energy conversion process.",
                "topic": "Cell respiration",
                "source_reference": "Paragraph 1",
            },
            {
                "question_text": "Proton flow through ATP synthase powers ATP formation.",
                "question_type": "true_false",
                "correct_answer": "True",
                "explanation": "The source states that proton flow powers ATP formation.",
                "topic": "ATP synthase",
                "source_reference": "Paragraph 1",
            },
            {
                "question_text": "What molecule is formed from ADP and phosphate?",
                "question_type": "short_answer",
                "correct_answer": ["ATP", "adenosine triphosphate"],
                "explanation": "The source says ATP is formed from ADP and phosphate.",
                "topic": "ATP",
                "source_reference": "Paragraph 1",
            },
        ]
        question_prompt = []

        def question_provider(prompt, provider, model, api_key):
            question_prompt.append(prompt)
            return json.dumps({"items": generated_questions})

        with patch.dict(os.environ, {
            "AI_CONTENT_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test-key-not-sent",
            "AI_CONTENT_MODEL": "test-model",
        }), patch.object(
            learning_content_ai,
            "_request_provider",
            side_effect=question_provider,
        ):
            generated_q = self.client.post(
                f"/admin/learning/documents/{document_id}/generate/questions",
                data={"count": "3"},
            )
        self.assertEqual(generated_q.status_code, 200, generated_q.text)
        self.assertIn(source_text, question_prompt[0])
        self.assertEqual(
            {item["question_type"] for item in generated_q.json()["questions"]},
            {"mcq_single", "true_false", "short_answer"},
        )
        question_ids = [item["id"] for item in generated_q.json()["questions"]]

        db = self.Session()
        saved_cards = db.query(models.Flashcard).filter(
            models.Flashcard.id.in_(card_ids)
        ).order_by(models.Flashcard.id).all()
        saved_questions = db.query(models.GeneratedQuestion).filter(
            models.GeneratedQuestion.id.in_(question_ids)
        ).order_by(models.GeneratedQuestion.id).all()
        # Phase 3B: generated content is saved automatically, unassigned and unpublished.
        self.assertTrue(all(card.course_id is None and not card.is_published for card in saved_cards))
        self.assertTrue(all(card.approval_status == "SAVED" for card in saved_cards))
        self.assertTrue(all(card.source_document_id == document_id for card in saved_cards))
        self.assertTrue(all(card.source_document_version_id is not None for card in saved_cards))
        self.assertTrue(all(card.source_reference == "P1 · Paragraph 1" for card in saved_cards))
        self.assertTrue(all(card.source_passage_id == "P1" for card in saved_cards))
        self.assertTrue(all(card.ai_provider == "gemini" and card.ai_model == "test-model" for card in saved_cards))
        self.assertTrue(all(question.course_id is None and not question.is_published for question in saved_questions))
        self.assertTrue(all(question.source_document_id == document_id for question in saved_questions))
        self.assertTrue(all(question.source_document_version_id is not None for question in saved_questions))
        self.assertTrue(all(question.source_reference == "P1 · Paragraph 1" for question in saved_questions))
        self.assertTrue(all(question.source_passage_id == "P1" for question in saved_questions))
        self.assertTrue(all(question.ai_provider == "gemini" and question.ai_model == "test-model" for question in saved_questions))
        db.close()

        card_edit = self.client.post(
            f"/admin/learning/flashcards/{card_ids[0]}/edit",
            data={
                "front": "How does cellular respiration form ATP?",
                "back": "It transfers nutrient energy to form ATP.",
                "explanation": "Edited explanation",
                "topic": "Reviewed energy",
            },
            follow_redirects=False,
        )
        self.assertEqual(card_edit.status_code, 303)
        db = self.Session()
        edited_card = db.query(models.Flashcard).filter_by(id=card_ids[0]).one()
        self.assertEqual(edited_card.front, "How does cellular respiration form ATP?")
        self.assertIsNone(edited_card.course_id)  # editing does not assign or publish
        db.close()
        assigned = self.client.post(
            f"/admin/content/flashcards/{card_ids[0]}/assign",
            data={"module_id": self.module_id, "publish": "true"},
            follow_redirects=False,
        )
        self.assertEqual(assigned.status_code, 303)
        self.assertEqual(
            self.client.post(f"/admin/content/flashcards/{card_ids[1]}/archive", follow_redirects=False).status_code,
            303,
        )

        edited_question = self.client.post(
            f"/admin/learning/questions/{question_ids[0]}/edit",
            data={
                "question_text": "Which process produces ATP from nutrient energy?",
                "question_type": "mcq_single",
                "answer_options": json.dumps(generated_questions[0]["answer_options"]),
                "correct_answer": json.dumps("Cellular respiration"),
                "explanation": "Teacher-reviewed explanation.",
                "topic": "Reviewed process",
            },
            follow_redirects=False,
        )
        self.assertEqual(edited_question.status_code, 303)
        self.assertEqual(
            self.client.post(f"/admin/content/questions/{question_ids[1]}/archive", follow_redirects=False).status_code,
            303,
        )
        archived_question = self.client.post(
            f"/admin/learning/questions/{question_ids[1]}/add-to-quiz",
            data={"quiz_id": self.quiz_id},
            follow_redirects=False,
        )
        self.assertEqual(archived_question.status_code, 409)
        added = self.client.post(
            f"/admin/learning/questions/{question_ids[0]}/add-to-quiz",
            data={"quiz_id": self.quiz_id},
            follow_redirects=False,
        )
        self.assertEqual(added.status_code, 303)
        db = self.Session()
        quiz_question = db.query(models.Question).filter_by(quiz_id=self.quiz_id).one()
        self.assertEqual(quiz_question.question_type, "mcq_single")
        self.assertEqual(json.loads(quiz_question.correct_answer), "Cellular respiration")
        db.close()

        account_response = self.client.post(
            "/admin/student-accounts",
            data={
                "username": "student.ai",
                "student_code": "AI-001",
                "full_name": "AI Test Student",
            },
        )
        self.assertEqual(account_response.status_code, 201)
        account_id = account_response.json()["account"]["id"]
        assignment = self.client.post(
            f"/admin/student-accounts/{account_id}/enrollments",
            data={"course_id": self.course_id, "module_id": self.module_id},
        )
        self.assertEqual(assignment.status_code, 201)
        student_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        login = student_client.post(
            "/student/login",
            data={
                "username": "student.ai",
                "password": account_response.json()["temporary_password"],
            },
            follow_redirects=False,
        )
        self.assertEqual(login.headers["location"], "/student/change-password")
        changed = student_client.post(
            "/student/change-password",
            data={
                "new_password": "student-ai-test-password-2026",
                "confirm_password": "student-ai-test-password-2026",
            },
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        visible_cards = student_client.get("/student/flashcards?due_only=false").json()["cards"]
        self.assertEqual([item["id"] for item in visible_cards], [card_ids[0]])
        review = student_client.post(
            f"/student/flashcards/{card_ids[0]}/review",
            data={"rating": "Good"},
        )
        self.assertEqual(review.status_code, 200, review.text)
        history = student_client.get("/student/study/history").json()["reviews"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["rating"], "Good")
        self.assertEqual(student_client.get("/student/study/queue").json()["reviewed_today"], 1)
        self.assertEqual(
            student_client.post(
                f"/student/flashcards/{card_ids[1]}/review",
                data={"rating": "Good"},
            ).status_code,
            404,
        )

        teacher_dashboard = self.client.get("/admin/learning/dashboard")
        self.assertIn("1 assigned · 1 live for students", teacher_dashboard.text)
        self.assertNotIn("Approve", teacher_dashboard.text)
        bank = self.client.get("/admin/content?status=all")
        self.assertIn("Live for students", bank.text)
        self.assertIn("Archived", bank.text)
        self.assertIn("Saved · not assigned", bank.text)
        self.assertIn("Cell energy notes", bank.text)  # source document shown
        self.assertIn("Draft Biology Quiz", bank.text)

    def test_ai_generation_fails_explicitly_without_provider_key(self):
        source = "[[SOURCE: Paragraph 1]]\nA sufficiently long source statement for learning."
        with patch.dict(os.environ, {"AI_CONTENT_PROVIDER": "gemini"}, clear=True):
            with self.assertRaises(learning_content_ai.ContentGenerationError) as error:
                learning_content_ai.generate_flashcards(source, "Source", 1)
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("not configured", str(error.exception))

    def test_generation_route_reports_sanitized_provider_http_error(self):
        source_text = "Ribosomes assemble proteins by linking amino acids in the order set by messenger RNA."
        document_id = self._upload_text_document(source_text)
        fake_key = "AIzaFakeKeyForTests_0123456789abcdef"
        failure = genai_errors.ClientError(404, {"error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": f"This model models/old-model is no longer available. key={fake_key}",
        }})
        with patch.dict(os.environ, {
            "AI_CONTENT_PROVIDER": "gemini",
            "GEMINI_API_KEY": fake_key,
            "AI_CONTENT_MODEL": "old-model",
        }), patch("google.genai.Client") as client_class:
            client_class.return_value.models.generate_content.side_effect = failure
            response = self.client.post(
                f"/admin/learning/documents/{document_id}/generate/flashcards",
                data={"count": "1"},
            )
        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("ClientError, HTTP 404 NOT_FOUND, model old-model", detail)
        self.assertIn("no longer available", detail)
        self.assertNotIn(fake_key, response.text)
        self.assertNotIn(source_text, response.text)
        self.assertNotIn("Traceback", response.text)


class GeminiProviderErrorTests(unittest.TestCase):
    """_call_gemini() error reporting, with the SDK client replaced."""

    API_KEY = "AIzaSecretKeyThatMustNeverLeak_0123456789"
    PROMPT = "BEGIN SOURCE TEXT\nConfidential lecture notes about enzyme kinetics and Michaelis constants.\nEND SOURCE TEXT"

    def _call_failing(self, exc):
        with patch("google.genai.Client") as client_class:
            client_class.return_value.models.generate_content.side_effect = exc
            with self.assertRaises(learning_content_ai.ContentGenerationError) as caught:
                learning_content_ai._call_gemini(self.PROMPT, "test-model", self.API_KEY)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(self.API_KEY, str(caught.exception))
        return caught.exception

    def _api_error(self, cls, code, status, message):
        return cls(code, {"error": {"code": code, "status": status, "message": message}})

    def test_client_error_keeps_http_status_and_provider_message(self):
        error = self._call_failing(self._api_error(
            genai_errors.ClientError, 404, "NOT_FOUND", "This model is no longer available to new users."
        ))
        self.assertEqual(error.status_code, 502)
        self.assertEqual(
            str(error),
            "AI provider request failed (ClientError, HTTP 404 NOT_FOUND, model test-model): "
            "This model is no longer available to new users.",
        )

    def test_provider_message_credentials_are_redacted(self):
        error = self._call_failing(self._api_error(
            genai_errors.ClientError, 400, "INVALID_ARGUMENT",
            f"API key not valid: {self.API_KEY}. Request url ?key=other-secret&alt=json "
            "Authorization: Bearer ya29.token-value x-goog-api-key: AIzaAnotherLookingKey_0123456789",
        ))
        message = str(error)
        self.assertIn("HTTP 400 INVALID_ARGUMENT", message)
        self.assertIn("API key not valid", message)
        for secret in ("other-secret", "ya29.token-value", "AIzaAnotherLookingKey_0123456789"):
            self.assertNotIn(secret, message)
        self.assertIn("[redacted]", message)

    def test_provider_message_echoing_document_text_is_withheld(self):
        error = self._call_failing(self._api_error(
            genai_errors.ClientError, 400, "INVALID_ARGUMENT",
            "Could not process: Confidential lecture notes about enzyme kinetics and Michaelis constants.",
        ))
        self.assertIn("HTTP 400 INVALID_ARGUMENT", str(error))
        self.assertIn("withheld", str(error))
        self.assertNotIn("enzyme kinetics", str(error))

    def test_long_provider_message_is_truncated(self):
        error = self._call_failing(self._api_error(
            genai_errors.ServerError, 500, "INTERNAL", "x " * 1000,
        ))
        self.assertEqual(error.status_code, 502)
        self.assertLess(len(str(error)), learning_content_ai.MAX_PROVIDER_MESSAGE_CHARACTERS + 120)

    def test_provider_deadline_exceeded_maps_to_gateway_timeout(self):
        error = self._call_failing(self._api_error(
            genai_errors.ServerError, 504, "DEADLINE_EXCEEDED", "Deadline expired before operation could complete.",
        ))
        self.assertEqual(error.status_code, 504)
        self.assertIn("ServerError, HTTP 504 DEADLINE_EXCEEDED", str(error))

    def test_network_timeout_is_reported_clearly(self):
        error = self._call_failing(httpx.ReadTimeout("read timed out"))
        self.assertEqual(error.status_code, 504)
        self.assertIn("timed out after 45 seconds", str(error))

    def test_connection_failure_is_reported_clearly(self):
        error = self._call_failing(httpx.ConnectError("getaddrinfo failed"))
        self.assertEqual(error.status_code, 502)
        self.assertIn("could not be reached (ConnectError)", str(error))

    def test_unexpected_exception_keeps_generic_message(self):
        error = self._call_failing(RuntimeError(f"internal detail {self.API_KEY} {self.PROMPT}"))
        self.assertEqual(error.status_code, 502)
        self.assertEqual(str(error), "AI provider request failed (RuntimeError).")


class GeminiModelDefaultTests(unittest.TestCase):
    def _model(self, env):
        with patch.dict(os.environ, {"AI_CONTENT_PROVIDER": "gemini", "GEMINI_API_KEY": "k", **env}, clear=True):
            return learning_content_ai._provider_config()[1]

    def test_default_model_is_gemini_3_5_flash(self):
        self.assertEqual(self._model({}), "gemini-3.5-flash")

    def test_explicit_model_overrides_still_win(self):
        self.assertEqual(self._model({"GEMINI_MODEL": "gemini-custom"}), "gemini-custom")
        self.assertEqual(
            self._model({"GEMINI_MODEL": "gemini-custom", "AI_CONTENT_MODEL": "content-model"}),
            "content-model",
        )

    def test_grading_and_config_check_share_the_new_default(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("ai_grading.py", "check_gemini_config.py", "learning_content_ai.py", ".env.example"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("gemini-2.5-flash", text, name)
        self.assertIn('os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")', (root / "ai_grading.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
