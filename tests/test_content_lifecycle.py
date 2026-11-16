"""Phase 3B: automatic saving, course assignment/publication, content bank and lifecycle."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.orm import sessionmaker

import app as smartquiz_app
import database
import learning_content_ai
import models
from database import Base
from tests.test_source_provenance import FLASHCARDS, GEN_ENV, QUESTIONS, _AppTestBase, _fake_provider

STUDENT_PASSWORD = "lifecycle-student-password-2026"


class _LifecycleBase(_AppTestBase):
    """Uploads a lesson, generates 2 flashcards + 3 questions, and creates an enrolled student."""

    def setUp(self):
        super().setUp()
        self.document_id = self.upload_lesson()
        self.card_ids = self.generate("flashcards", FLASHCARDS, 2)
        self.question_ids = self.generate("questions", QUESTIONS, 3)
        db = self.db()
        self.course_id = db.get(models.LearningModule, self.module_id).course_id
        other_course = models.Course(name="Other course", code="OTHER", description="", status="PUBLISHED")
        db.add(other_course)
        db.flush()
        other_module = models.LearningModule(course_id=other_course.id, title="Other module", description="",
                                             learning_objectives="", status="PUBLISHED")
        db.add(other_module)
        db.commit()
        self.other_course_id, self.other_module_id = other_course.id, other_module.id
        db.close()
        created = self.client.post("/admin/student-accounts", data={
            "username": "lifecycle.student", "student_code": "LC-1", "full_name": "Lifecycle Student"})
        self.assertEqual(created.status_code, 201)
        self.account_id = created.json()["account"]["id"]
        enrolled = self.client.post(f"/admin/student-accounts/{self.account_id}/enrollments",
                                    data={"course_id": self.course_id})
        self.assertEqual(enrolled.status_code, 201)
        self.student = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        self.student.post("/student/login", data={"username": "lifecycle.student",
                                                  "password": created.json()["temporary_password"]})
        changed = self.student.post("/student/change-password", data={
            "new_password": STUDENT_PASSWORD, "confirm_password": STUDENT_PASSWORD}, follow_redirects=False)
        self.assertEqual(changed.status_code, 303)

    def generate(self, kind, items, count):
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(items)):
            response = self.client.post(f"/admin/learning/documents/{self.document_id}/generate/{kind}",
                                        data={"count": str(count)})
        self.assertEqual(response.status_code, 200, response.text)
        return [item["id"] for item in response.json()["flashcards" if kind == "flashcards" else "questions"]]

    def act(self, kind, item_id, action, **data):
        return self.client.post(f"/admin/content/{kind}/{item_id}/{action}", data=data, follow_redirects=False)

    def assign(self, kind, item_id, module_id=None, publish=True):
        response = self.act(kind, item_id, "assign", module_id=module_id or self.module_id,
                            publish="true" if publish else "false")
        self.assertEqual(response.status_code, 303, response.text)

    def student_card_ids(self):
        response = self.student.get("/student/flashcards?due_only=false")
        self.assertEqual(response.status_code, 200)
        return {card["id"] for card in response.json()["cards"]}

    def student_practice_text(self):
        response = self.student.get("/student/practice")
        self.assertEqual(response.status_code, 200)
        return response.text

    def row(self, model, item_id):
        db = self.db()
        try:
            return db.get(model, item_id)
        finally:
            db.close()


class AutomaticSavingTests(_LifecycleBase):
    def test_generated_flashcards_and_questions_are_saved_unassigned_with_provenance(self):
        db = self.db()
        cards = db.query(models.Flashcard).filter(models.Flashcard.id.in_(self.card_ids)).all()
        questions = db.query(models.GeneratedQuestion).filter(models.GeneratedQuestion.id.in_(self.question_ids)).all()
        self.assertEqual((len(cards), len(questions)), (2, 3))
        for item in cards + questions:
            self.assertIsNone(item.course_id)
            self.assertFalse(item.is_published)
            self.assertIsNone(item.archived_at)
            self.assertEqual(item.source_document_id, self.document_id)
            self.assertIsNotNone(item.source_document_version_id)
            self.assertTrue(item.source_passage_id)
        db.close()
        bank = self.client.get(f"/admin/content?document_id={self.document_id}")
        self.assertEqual(bank.text.count('data-status="unassigned"'), 5)
        self.assertIn("Module 2 lesson", bank.text)

    def test_html_generation_redirects_to_content_bank_with_saved_notice(self):
        fresh = [dict(card, front="Fresh: " + card["front"]) for card in FLASHCARDS]  # avoid the duplicate guard
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(fresh)):
            response = self.client.post(f"/admin/learning/modules/{self.module_id}/generate/flashcards",
                                        data={"document_id": self.document_id, "count": "2"},
                                        headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/admin/content?document_id={self.document_id}&notice=saved")
        page = self.client.get(response.headers["location"])
        self.assertIn("not visible to students", page.text)

    def test_generated_content_is_invisible_until_assigned_and_published(self):
        card, question = self.card_ids[0], self.question_ids[0]
        self.assertFalse(self.student_card_ids())  # saved only
        self.assertNotIn("Which final learning outcome", self.student_practice_text())
        self.assign("flashcards", card, publish=False)
        self.assign("questions", question, publish=False)
        self.assertFalse(self.student_card_ids())  # assigned but not published
        self.assertNotIn("Which final learning outcome", self.student_practice_text())
        self.assertEqual(self.student.post(f"/student/flashcards/{card}/review", data={"rating": "Good"}).status_code, 404)
        self.act("flashcards", card, "publish", publish="true")
        self.act("questions", question, "publish", publish="true")
        self.assertEqual(self.student_card_ids(), {card})
        self.assertIn("Which final learning outcome", self.student_practice_text())
        self.assertEqual(self.student.post(f"/student/flashcards/{card}/review", data={"rating": "Good"}).status_code, 200)

    def test_invalid_generation_output_saves_nothing(self):
        before = (self.db().query(models.Flashcard).count(), self.db().query(models.GeneratedQuestion).count())
        bad_citation = [dict(FLASHCARDS[0], source_reference="P999")]
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(bad_citation)):
            cited = self.client.post(f"/admin/learning/documents/{self.document_id}/generate/flashcards", data={"count": "1"})
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", return_value="not json at all"):
            malformed = self.client.post(f"/admin/learning/documents/{self.document_id}/generate/questions", data={"count": "3"})
        self.assertEqual((cited.status_code, malformed.status_code), (502, 502))
        after = (self.db().query(models.Flashcard).count(), self.db().query(models.GeneratedQuestion).count())
        self.assertEqual(before, after)
        self.assertFalse(self.student_card_ids())


class CourseAssignmentTests(_LifecycleBase):
    def test_assign_publish_and_remove_from_course_keeps_content(self):
        card = self.card_ids[0]
        self.assign("flashcards", card)
        row = self.row(models.Flashcard, card)
        self.assertEqual((row.course_id, row.module_id, row.is_published), (self.course_id, self.module_id, True))
        self.assertEqual(self.student_card_ids(), {card})
        self.assertEqual(self.act("flashcards", card, "unassign").status_code, 303)
        row = self.row(models.Flashcard, card)
        self.assertIsNotNone(row)  # removing from a course never deletes
        self.assertEqual((row.course_id, row.is_published, row.front), (None, False, FLASHCARDS[0]["front"]))
        self.assertEqual(row.source_passage_id, "P6")  # provenance kept
        self.assertFalse(self.student_card_ids())

    def test_publish_requires_assignment_and_live_course(self):
        card = self.card_ids[0]
        self.assertEqual(self.act("flashcards", card, "publish", publish="true").status_code, 409)
        self.assign("flashcards", card)
        self.client.post(f"/admin/learning/courses/{self.course_id}/status", data={"status": "DRAFT"})
        self.assertFalse(self.student_card_ids())  # course unpublished
        bank = self.client.get("/admin/content?status=published")
        self.assertIn("its course is a draft", bank.text)
        self.client.post(f"/admin/learning/courses/{self.course_id}/status", data={"status": "PUBLISHED"})
        self.assertEqual(self.student_card_ids(), {card})

    def test_students_only_see_content_of_their_own_courses(self):
        mine, other = self.card_ids
        self.assign("flashcards", mine)
        self.assign("flashcards", other, module_id=self.other_module_id)
        self.assertEqual(self.row(models.Flashcard, other).course_id, self.other_course_id)
        self.assertEqual(self.student_card_ids(), {mine})
        self.assertEqual(self.student.post(f"/student/flashcards/{other}/review", data={"rating": "Good"}).status_code, 404)
        self.assertEqual(self.student.get(f"/student/practice?module_id={self.other_module_id}").status_code, 404)

    def test_new_courses_start_as_drafts(self):
        self.client.post("/admin/learning/courses", data={"name": "Brand new", "code": "NEW"})
        db = self.db()
        self.assertEqual(db.query(models.Course).filter_by(code="NEW").one().status, "DRAFT")
        db.close()

    def test_students_and_anonymous_users_cannot_manage_content(self):
        card = self.card_ids[0]
        for client in (self.student, TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})):
            for action, data in (("assign", {"module_id": self.module_id, "publish": "true"}),
                                 ("publish", {"publish": "true"}), ("archive", {}), ("delete", {"confirm": "yes"})):
                response = client.post(f"/admin/content/flashcards/{card}/{action}", data=data, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["location"].startswith("/login"))
            self.assertEqual(client.get("/admin/content", follow_redirects=False).status_code, 303)
        row = self.row(models.Flashcard, card)
        self.assertEqual((row.course_id, row.is_published, row.archived_at, row.deleted_at), (None, False, None, None))


class LifecycleTests(_LifecycleBase):
    def test_course_archive_and_restore_keep_content_and_enrollments(self):
        card = self.card_ids[0]
        self.assign("flashcards", card)
        self.client.post(f"/admin/learning/courses/{self.course_id}/archive")
        self.assertFalse(self.student_card_ids())
        self.assertEqual(self.student.get(f"/student/materials/{self.document_id}").status_code, 404)
        db = self.db()
        self.assertEqual(db.get(models.Flashcard, card).course_id, self.course_id)
        self.assertEqual(db.query(models.StudentAccountEnrollment).filter_by(account_id=self.account_id, status="Active").count(), 1)
        self.assertEqual(db.query(models.Document).filter_by(id=self.document_id).count(), 1)
        db.close()
        self.assertEqual(self.client.post(f"/admin/learning/courses/{self.course_id}/status",
                                          data={"status": "PUBLISHED"}).status_code, 409)
        self.client.post(f"/admin/learning/courses/{self.course_id}/restore")
        self.assertEqual(self.student_card_ids(), {card})

    def test_course_delete_is_soft_confirmed_restorable_and_purge_is_guarded(self):
        self.assign("flashcards", self.card_ids[0])
        self.assertEqual(self.client.post(f"/admin/learning/courses/{self.course_id}/delete").status_code, 400)
        confirm_page = self.client.get(f"/admin/learning/courses/{self.course_id}/delete")
        self.assertIn('name="confirm"', confirm_page.text)
        self.client.post(f"/admin/learning/courses/{self.course_id}/delete", data={"confirm": "yes"})
        self.assertFalse(self.student_card_ids())
        dashboard = self.client.get("/admin/learning/dashboard")
        self.assertIn("Recently deleted courses", dashboard.text)
        blocked = self.client.post(f"/admin/learning/courses/{self.course_id}/purge", data={"confirm": "yes"})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("module", blocked.json()["detail"])
        self.assertIn("data-blocked", self.client.get(f"/admin/learning/courses/{self.course_id}/purge").text)
        db = self.db()
        self.assertIsNotNone(db.get(models.Course, self.course_id))
        self.assertIsNotNone(db.get(models.Flashcard, self.card_ids[0]))
        self.assertEqual(db.query(models.Document).filter_by(id=self.document_id).count(), 1)
        db.close()
        self.client.post(f"/admin/learning/courses/{self.course_id}/restore")
        self.assertEqual(self.student_card_ids(), {self.card_ids[0]})

    def test_empty_deleted_course_can_be_purged(self):
        self.client.post("/admin/learning/courses", data={"name": "Empty", "code": "EMPTY"})
        db = self.db()
        course_id = db.query(models.Course).filter_by(code="EMPTY").one().id
        db.close()
        self.assertEqual(self.client.post(f"/admin/learning/courses/{course_id}/purge", data={"confirm": "yes"}).status_code, 409)
        self.client.post(f"/admin/learning/courses/{course_id}/delete", data={"confirm": "yes"})
        self.assertEqual(self.client.post(f"/admin/learning/courses/{course_id}/purge", data={"confirm": "yes"},
                                          follow_redirects=False).status_code, 303)
        db = self.db()
        self.assertIsNone(db.get(models.Course, course_id))
        db.close()

    def test_flashcard_and_question_archive_and_restore(self):
        card, question = self.card_ids[0], self.question_ids[0]
        self.assign("flashcards", card)
        self.assign("questions", question)
        self.act("flashcards", card, "archive")
        self.act("questions", question, "archive")
        self.assertFalse(self.student_card_ids())
        self.assertNotIn("Which final learning outcome", self.student_practice_text())
        quiz_id = self.create_draft_quiz()
        self.assertEqual(self.client.post(f"/admin/learning/questions/{question}/add-to-quiz",
                                          data={"quiz_id": quiz_id}).status_code, 409)
        self.assertEqual(self.act("flashcards", card, "publish", publish="true").status_code, 409)
        self.act("flashcards", card, "restore")
        self.act("questions", question, "restore")
        self.assertEqual(self.student_card_ids(), {card})  # restored to its previous assignment
        self.assertIn("Which final learning outcome", self.student_practice_text())

    def test_content_delete_needs_confirmation_and_is_restorable(self):
        card = self.card_ids[0]
        self.assign("flashcards", card)
        self.assertEqual(self.act("flashcards", card, "delete").status_code, 400)
        self.assertIsNone(self.row(models.Flashcard, card).deleted_at)
        self.assertEqual(self.act("flashcards", card, "delete", confirm="yes").status_code, 303)
        self.assertFalse(self.student_card_ids())
        deleted_view = self.client.get("/admin/content?status=deleted")
        self.assertIn(f'data-content-row="flashcards-{card}"', deleted_view.text)
        self.act("flashcards", card, "restore")
        self.assertEqual(self.student_card_ids(), {card})

    def test_permanent_delete_is_blocked_by_review_history_and_quiz_provenance(self):
        card, spare_card = self.card_ids
        question = self.question_ids[0]
        self.assign("flashcards", card)
        self.assertEqual(self.student.post(f"/student/flashcards/{card}/review", data={"rating": "Good"}).status_code, 200)
        quiz_id = self.create_draft_quiz()
        self.client.post(f"/admin/learning/questions/{question}/add-to-quiz", data={"quiz_id": quiz_id})
        for kind, item_id in (("flashcards", card), ("questions", question), ("flashcards", spare_card)):
            self.assertEqual(self.act(kind, item_id, "purge", confirm="yes").status_code, 409)  # not yet deleted
            self.act(kind, item_id, "delete", confirm="yes")
        self.assertEqual(self.act("flashcards", card, "purge", confirm="yes").status_code, 409)
        self.assertEqual(self.act("questions", question, "purge", confirm="yes").status_code, 409)
        self.assertEqual(self.act("flashcards", spare_card, "purge").status_code, 400)  # confirmation required
        self.assertEqual(self.act("flashcards", spare_card, "purge", confirm="yes").status_code, 303)
        db = self.db()
        self.assertIsNotNone(db.get(models.Flashcard, card))
        self.assertIsNotNone(db.get(models.GeneratedQuestion, question))
        self.assertIsNone(db.get(models.Flashcard, spare_card))
        self.assertEqual(db.query(models.StudentAccountFlashcardReview).filter_by(flashcard_id=card).count(), 1)
        self.assertEqual(db.query(models.Question).filter_by(generated_question_id=question).count(), 1)
        db.close()

    def test_review_history_survives_archive_and_returns_on_restore(self):
        card = self.card_ids[0]
        self.assign("flashcards", card)
        self.student.post(f"/student/flashcards/{card}/review", data={"rating": "Good"})
        self.assertEqual(len(self.student.get("/student/study/history").json()["reviews"]), 1)
        self.act("flashcards", card, "archive")
        self.assertEqual(self.student.get("/student/study/history").json()["reviews"], [])
        db = self.db()
        self.assertEqual(db.query(models.StudentAccountFlashcardReview).filter_by(flashcard_id=card).count(), 1)
        self.assertEqual(db.query(models.StudentAccountFlashcardProgress).filter_by(flashcard_id=card).count(), 1)
        db.close()
        self.act("flashcards", card, "restore")
        self.assertEqual(len(self.student.get("/student/study/history").json()["reviews"]), 1)

    def test_module_and_document_archive(self):
        card = self.card_ids[0]
        self.assign("flashcards", card)
        self.client.post(f"/admin/learning/modules/{self.module_id}/archive")
        self.assertFalse(self.student_card_ids())
        self.client.post(f"/admin/learning/modules/{self.module_id}/restore")
        self.assertEqual(self.student_card_ids(), {card})
        self.client.post(f"/admin/learning/documents/{self.document_id}/archive")
        self.assertEqual(self.student.get(f"/student/materials/{self.document_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/admin/learning/documents/{self.document_id}").status_code, 200)
        with patch.dict(os.environ, GEN_ENV), patch.object(learning_content_ai, "_request_provider") as provider:
            blocked = self.client.post(f"/admin/learning/documents/{self.document_id}/generate/flashcards", data={"count": "1"})
        self.assertEqual(blocked.status_code, 404)
        provider.assert_not_called()
        self.assertEqual(self.row(models.Flashcard, card).source_document_id, self.document_id)  # provenance intact
        self.client.post(f"/admin/learning/documents/{self.document_id}/restore")
        self.assertEqual(self.student.get(f"/student/materials/{self.document_id}").status_code, 200)

    def test_historical_quiz_results_survive_edit_archive_and_course_deletion(self):
        question = self.question_ids[0]
        quiz_id = self.create_draft_quiz()
        self.client.post(f"/admin/learning/questions/{question}/add-to-quiz", data={"quiz_id": quiz_id})
        db = self.db()
        quiz_question = db.query(models.Question).filter_by(quiz_id=quiz_id).one()
        live = models.Session(quiz_id=quiz_id, session_code="LIFE01", status="Completed")
        db.add(live)
        db.flush()
        participant = models.Student(quiz_id=quiz_id, session_id=live.id, name="Past", roll_number="P1",
                                     status="Finished", result_token="r" * 43)
        db.add(participant)
        db.flush()
        db.add(models.Response(
            student_id=participant.id, session_id=live.id, quiz_id=quiz_id, question_id=quiz_question.id,
            selected_option="Recommend a business action", is_correct=True, score=1,
            submitted_at=datetime.now(timezone.utc), question_text_snapshot=quiz_question.question_text,
            question_type_snapshot="mcq_single", question_options_snapshot=quiz_question.options,
            correct_answer_snapshot=quiz_question.correct_answer, question_points_snapshot=1))
        db.commit()
        session_id, participant_id, original_text = live.id, participant.id, quiz_question.question_text
        quiz_question_id = quiz_question.id
        db.close()
        before = self.client.get(f"/admin/results/data?session_id={session_id}").json()
        self.client.post(f"/admin/learning/questions/{question}/edit", data={
            "question_text": "Bank question reworded after the quiz", "question_type": "mcq_single",
            "answer_options": json.dumps(QUESTIONS[0]["answer_options"]),
            "correct_answer": json.dumps("Recommend a business action")})
        self.act("questions", question, "archive")
        self.act("questions", question, "delete", confirm="yes")
        self.client.post(f"/admin/learning/courses/{self.course_id}/delete", data={"confirm": "yes"})
        self.assertEqual(self.client.get(f"/admin/results/data?session_id={session_id}").json(), before)
        db = self.db()
        kept = db.get(models.Question, quiz_question_id)
        self.assertEqual((kept.question_text, kept.generated_question_id), (original_text, question))
        db.close()
        page = TestClient(smartquiz_app.app).get(f"/student/results/{participant_id}?token={'r' * 43}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(original_text, page.text)
        self.assertNotIn("Bank question reworded", page.text)

    def test_confirmation_pages_and_content_bank_have_no_approval_controls(self):
        card = self.card_ids[0]
        self.assertIn('name="confirm"', self.client.get(f"/admin/content/flashcards/{card}/delete").text)
        bank = self.client.get("/admin/content?status=all").text
        self.assertNotIn("Approve", bank)
        self.assertNotIn("Reject", bank)
        self.assertEqual(self.client.post(f"/admin/learning/flashcards/{card}/status", data={"status": "APPROVED"}).status_code, 404)
        self.assertEqual(self.client.post(f"/admin/learning/flashcards/{card}/approve").status_code, 404)


class LifecycleMigrationTests(unittest.TestCase):
    """Real pre-Phase-3B rows in every old review state are mapped safely, once."""

    LIFECYCLE = {
        "courses": ["status", "archived_at", "deleted_at"],
        "learning_modules": ["archived_at"],
        "flashcards": ["course_id", "is_published", "assigned_at", "published_at", "archived_at", "deleted_at"],
        "generated_questions": ["course_id", "is_published", "assigned_at", "published_at", "archived_at", "deleted_at"],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'pre3b.db'}")
        legacy = MetaData()
        for table in Base.metadata.sorted_tables:
            Table(table.name, legacy, *[c._copy() for c in table.columns if c.name not in self.LIFECYCLE.get(table.name, [])])
        legacy.create_all(bind=self.engine)
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO courses (id, name, code, description, created_at) VALUES (1,'Old','OLD','',:t)"), {"t": now})
            conn.execute(text("INSERT INTO learning_modules (id, course_id, title, description, learning_objectives, status, created_at) "
                              "VALUES (1,1,'M','','','PUBLISHED',:t)"), {"t": now})
            for card_id, state in enumerate(["APPROVED", "PENDING", "DRAFT", "REJECTED"], 1):
                conn.execute(text("INSERT INTO flashcards (id, module_id, front, back, explanation, topic, difficulty, "
                                  "source_reference, approval_status, generated_at) VALUES (:id,1,:f,'b','','','medium','',:s,:t)"),
                             {"id": card_id, "f": f"{state} card", "s": state, "t": now})
            conn.execute(text("INSERT INTO generated_questions (id, module_id, question_text, question_type, answer_options, "
                              "correct_answer, explanation, points_suggestion, difficulty, topic, source_reference, approval_status, "
                              "generated_at) VALUES (1,1,'Approved q','true_false','[]','\"True\"','',1,'medium','','','APPROVED',:t)"),
                         {"t": now})

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def _migrate(self):
        with patch.object(database, "engine", self.engine):
            database._run_migrations()

    def test_old_states_are_mapped_and_backfill_runs_once(self):
        self._migrate()
        with self.engine.connect() as conn:
            cards = {row.approval_status: row for row in conn.execute(text("SELECT * FROM flashcards")).mappings()}
            course = conn.execute(text("SELECT status FROM courses WHERE id=1")).scalar()
            question = conn.execute(text("SELECT course_id, is_published FROM generated_questions WHERE id=1")).one()
        self.assertEqual(course, "PUBLISHED")
        self.assertEqual((cards["APPROVED"]["course_id"], cards["APPROVED"]["is_published"]), (1, 1))
        for state in ("PENDING", "DRAFT"):
            self.assertEqual((cards[state]["course_id"], cards[state]["is_published"], cards[state]["archived_at"]), (None, 0, None))
        self.assertIsNotNone(cards["REJECTED"]["archived_at"])
        self.assertEqual(len(cards), 4)  # nothing discarded
        self.assertEqual(tuple(question), (1, 1))
        with self.engine.begin() as conn:  # teacher later unassigns the formerly approved card
            conn.execute(text("UPDATE flashcards SET course_id=NULL, is_published=0 WHERE approval_status='APPROVED'"))
        self._migrate()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT course_id FROM flashcards WHERE approval_status='APPROVED'")).scalar(), None)
            self.assertEqual(conn.execute(text("PRAGMA integrity_check")).scalar(), "ok")


if __name__ == "__main__":
    unittest.main()
