"""Phase 4A: student account deletion, source-document cleanup, shared teacher
UI (dropdowns, clear search), study-page layout and AI generation reliability."""

import json
import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors

import app as smartquiz_app
import learning_content_ai
import models
from tests.test_content_lifecycle import STUDENT_PASSWORD, _LifecycleBase
from tests.test_source_provenance import FLASHCARDS, GEN_ENV, QUESTIONS, _AppTestBase, _fake_provider, _lesson_docx_bytes

HTML = {"Accept": "text/html"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _select_html(page: str, select_id: str) -> str:
    match = re.search(r'<select[^>]*id="%s".*?</select>' % re.escape(select_id), page, re.S)
    assert match, f"select #{select_id} not found"
    return match.group(0)


class _AccountMixin:
    def create_account(self, username, code, name="Test Student"):
        response = self.client.post("/admin/student-accounts", data={
            "username": username, "student_code": code, "full_name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["account"]["id"], response.json()["temporary_password"]

    def student_client(self, username, temporary_password, new_password=STUDENT_PASSWORD):
        client = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        client.post("/student/login", data={"username": username, "password": temporary_password})
        changed = client.post("/student/change-password", data={
            "new_password": new_password, "confirm_password": new_password}, follow_redirects=False)
        self.assertEqual(changed.status_code, 303)
        return client

    def delete_account(self, account_id, **extra):
        return self.client.post(f"/admin/student-accounts/{account_id}/delete",
                                data={"confirm": "yes", **extra}, headers=HTML, follow_redirects=False)

    def account(self, account_id):
        db = self.db()
        try:
            return db.get(models.StudentAccount, account_id)
        finally:
            db.close()


# =========================================================== 1. student deletion

class StudentAccountDeletionTests(_AccountMixin, _AppTestBase):
    def setUp(self):
        super().setUp()
        db = self.db()
        self.course_id = db.get(models.LearningModule, self.module_id).course_id
        db.close()

    def add_quiz_history(self, account_id):
        quiz_id = self.create_draft_quiz("History quiz")
        db = self.db()
        question = models.Question(quiz_id=quiz_id, question_text="2 + 2?", question_type="mcq_single",
                                   options=json.dumps(["3", "4"]), correct_answer=json.dumps("4"), points=1)
        live = models.Session(quiz_id=quiz_id, session_code="HIST01", status="Completed")
        db.add_all([question, live])
        db.flush()
        participant = models.Student(quiz_id=quiz_id, session_id=live.id, name="Typed Name", roll_number="R-9",
                                     status="Finished", result_token="t" * 43, account_id=account_id)
        db.add(participant)
        db.flush()
        db.add(models.Response(student_id=participant.id, session_id=live.id, quiz_id=quiz_id,
                               question_id=question.id, selected_option="4", is_correct=True, score=1,
                               submitted_at=datetime.now(timezone.utc), question_text_snapshot="2 + 2?",
                               question_type_snapshot="mcq_single", question_options_snapshot=question.options,
                               correct_answer_snapshot=question.correct_answer, question_points_snapshot=1))
        db.commit()
        ids = (live.id, participant.id)
        db.close()
        return ids

    def test_delete_button_and_confirmation_page_identify_the_account(self):
        account_id, _ = self.create_account("ana.k", "S-100", "Ana Kumar")
        page = self.client.get("/admin/student-accounts")
        self.assertIn(f'href="/admin/student-accounts/{account_id}/delete"', page.text)
        self.assertIn("data-delete-account", page.text)
        confirm = self.client.get(f"/admin/student-accounts/{account_id}/delete")
        self.assertEqual(confirm.status_code, 200)
        for text in ("Ana Kumar", "ana.k", "S-100", 'name="confirm"', "permanently deleted"):
            self.assertIn(text, confirm.text)

    def test_delete_without_explicit_confirmation_is_refused(self):
        account_id, _ = self.create_account("no.confirm", "S-101")
        response = self.client.post(f"/admin/student-accounts/{account_id}/delete", data={})
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.account(account_id))
        self.assertIsNone(self.account(account_id).deleted_at)

    def test_account_without_history_is_permanently_deleted(self):
        account_id, _ = self.create_account("fresh.one", "S-102")
        response = self.delete_account(account_id)
        self.assertEqual(response.headers["location"], "/admin/student-accounts?notice=purged")
        self.assertIsNone(self.account(account_id))
        page = self.client.get(response.headers["location"])
        self.assertIn("permanently deleted", page.text)
        self.assertNotIn("fresh.one", page.text)

    def test_account_with_quiz_history_is_archived_and_results_are_preserved(self):
        account_id, _ = self.create_account("quiz.taker", "S-103", "Quiz Taker")
        session_id, participant_id = self.add_quiz_history(account_id)
        before = self.client.get(f"/admin/results/data?session_id={session_id}").json()
        confirm = self.client.get(f"/admin/student-accounts/{account_id}/delete")
        self.assertIn("1 quiz participation record(s)", confirm.text)
        self.assertIn("1 submitted quiz answer(s)", confirm.text)
        self.assertIn("will NOT be removed", confirm.text)
        response = self.delete_account(account_id)
        self.assertEqual(response.headers["location"], "/admin/student-accounts?notice=archived")
        account = self.account(account_id)
        self.assertIsNotNone(account.deleted_at)
        self.assertFalse(account.is_active)
        self.assertEqual(account.full_name, "Quiz Taker")  # kept for reporting
        db = self.db()
        participant = db.get(models.Student, participant_id)
        self.assertEqual((participant.account_id, participant.name), (account_id, "Typed Name"))
        self.assertEqual(db.query(models.Response).filter_by(student_id=participant_id).count(), 1)
        db.close()
        self.assertEqual(self.client.get(f"/admin/results/data?session_id={session_id}").json(), before)
        page = self.client.get("/admin/student-accounts")
        self.assertIn("data-deleted-accounts", page.text)
        self.assertNotIn(f'action="/admin/student-accounts/{account_id}/edit"', page.text)

    def test_flashcard_reviews_and_assignments_also_count_as_history(self):
        account_id, _ = self.create_account("assigned.only", "S-104")
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments", data={"course_id": self.course_id})
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments/1/revoke")
        self.assertEqual(self.delete_account(account_id).headers["location"], "/admin/student-accounts?notice=archived")
        db = self.db()
        enrollment = db.query(models.StudentAccountEnrollment).filter_by(account_id=account_id).one()
        self.assertEqual(enrollment.status, "Revoked")  # record kept
        db.close()

    def test_active_assignments_are_ended_on_deletion(self):
        account_id, _ = self.create_account("active.assign", "S-105")
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments", data={"course_id": self.course_id})
        self.delete_account(account_id)
        db = self.db()
        statuses = [e.status for e in db.query(models.StudentAccountEnrollment).filter_by(account_id=account_id)]
        db.close()
        self.assertEqual(statuses, ["Revoked"])

    def test_deleted_account_is_logged_out_and_cannot_log_in(self):
        account_id, temporary = self.create_account("logged.in", "S-106")
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments", data={"course_id": self.course_id})
        student = self.student_client("logged.in", temporary)
        self.assertEqual(student.get("/student/dashboard").status_code, 200)
        self.delete_account(account_id)
        self.assertEqual(student.get("/student/study/queue").status_code, 401)
        fresh = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        login = fresh.post("/student/login", data={"username": "logged.in", "password": STUDENT_PASSWORD},
                           follow_redirects=False)
        self.assertIn("error=", login.headers["location"])

    def test_restore_then_reactivate_allows_login_again(self):
        account_id, temporary = self.create_account("comes.back", "S-107")
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments", data={"course_id": self.course_id})
        self.student_client("comes.back", temporary)
        self.delete_account(account_id)
        restored = self.client.post(f"/admin/student-accounts/{account_id}/restore", headers=HTML,
                                    follow_redirects=False)
        self.assertEqual(restored.headers["location"], "/admin/student-accounts?notice=restored")
        account = self.account(account_id)
        self.assertIsNone(account.deleted_at)
        self.assertFalse(account.is_active)  # restore never silently re-enables login
        self.client.post(f"/admin/student-accounts/{account_id}/status", data={"active": "true"})
        fresh = TestClient(smartquiz_app.app, headers={"Origin": "http://testserver"})
        login = fresh.post("/student/login", data={"username": "comes.back", "password": STUDENT_PASSWORD},
                           follow_redirects=False)
        self.assertEqual(login.headers["location"], "/student/dashboard")

    def test_anonymised_account_keeps_history_but_not_identity_and_cannot_be_restored(self):
        account_id, _ = self.create_account("private.person", "S-108", "Private Person")
        session_id, participant_id = self.add_quiz_history(account_id)
        response = self.delete_account(account_id, anonymize="yes")
        self.assertEqual(response.headers["location"], "/admin/student-accounts?notice=anonymized")
        account = self.account(account_id)
        self.assertEqual(account.full_name, f"Deleted student #{account_id}")
        self.assertNotEqual(account.username, "private.person")
        self.assertEqual(account.student_code, f"DELETED-{account_id}")
        self.assertIsNotNone(account.anonymized_at)
        db = self.db()
        self.assertEqual(db.query(models.Response).filter_by(student_id=participant_id).count(), 1)
        db.close()
        self.assertEqual(self.client.post(f"/admin/student-accounts/{account_id}/restore").status_code, 409)
        self.assertNotIn("Private Person", self.client.get("/admin/student-accounts").text)
        self.create_account("private.person", "S-108")  # identifiers are free again

    def test_actions_on_a_deleted_account_are_blocked(self):
        account_id, _ = self.create_account("blocked.one", "S-109")
        self.client.post(f"/admin/student-accounts/{account_id}/enrollments", data={"course_id": self.course_id})
        self.delete_account(account_id)
        for path, data in [
            ("edit", {"username": "x.y.z", "student_code": "Z", "full_name": "Z"}),
            ("status", {"active": "true"}),
            ("reset-password", {}),
            ("enrollments", {"course_id": self.course_id}),
            ("delete", {"confirm": "yes"}),
        ]:
            self.assertEqual(self.client.post(f"/admin/student-accounts/{account_id}/{path}", data=data).status_code,
                             409, path)
        self.assertFalse(self.account(account_id).is_active)


# =========================================================== 2. document cleanup

class DocumentCleanupTests(_LifecycleBase):
    def filter_options(self):
        page = self.client.get("/admin/content?status=all")
        return _select_html(page.text, "filter-document")

    def upload(self, title):
        response = self.client.post(
            f"/admin/learning/modules/{self.module_id}/documents/upload",
            files={"file": (f"{title}.docx", _lesson_docx_bytes(), DOCX_MIME)}, data={"title": title})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["document"]["id"]

    def test_documents_of_a_deleted_course_leave_the_source_filter_but_keep_provenance(self):
        self.assertIn(f'value="{self.document_id}"', self.filter_options())
        self.client.post(f"/admin/learning/courses/{self.course_id}/delete", data={"confirm": "yes"})
        self.assertNotIn(f'value="{self.document_id}"', self.filter_options())
        page = self.client.get("/admin/content?status=all")
        self.assertIn("Module 2 lesson", page.text)  # provenance still names the source
        self.assertIn("source course deleted", page.text)
        self.assertEqual(page.text.count('data-content-row='), 5)  # no content removed

    def test_archived_document_leaves_filters_and_returns_when_restored(self):
        self.client.post(f"/admin/learning/documents/{self.document_id}/archive")
        self.assertNotIn(f'value="{self.document_id}"', self.filter_options())
        # Filtering by it explicitly still works and is labelled.
        selected = _select_html(self.client.get(f"/admin/content?status=all&document_id={self.document_id}").text,
                                "filter-document")
        self.assertIn("(archived)", selected)
        self.client.post(f"/admin/learning/documents/{self.document_id}/restore")
        self.assertIn(f'value="{self.document_id}"', self.filter_options())

    def test_soft_deleted_document_is_unavailable_everywhere_and_restorable(self):
        confirm = self.client.get(f"/admin/learning/documents/{self.document_id}/delete")
        self.assertIn("NOT deleted", confirm.text)
        self.assertEqual(self.client.post(f"/admin/learning/documents/{self.document_id}/delete").status_code, 400)
        done = self.client.post(f"/admin/learning/documents/{self.document_id}/delete", data={"confirm": "yes"},
                                follow_redirects=False)
        self.assertEqual(done.headers["location"], "/admin/learning/dashboard?notice=document_deleted")
        self.assertNotIn(f'value="{self.document_id}"', self.filter_options())
        dashboard = self.client.get("/admin/learning/dashboard").text
        self.assertIn(f'data-deleted-document="{self.document_id}"', dashboard)
        self.assertNotIn(f'id="source-document-{self.document_id}"', dashboard)  # not in the active list
        self.assertEqual(self.student.get(f"/student/materials/{self.document_id}").status_code, 404)
        with patch.dict(os.environ, GEN_ENV), patch.object(learning_content_ai, "_request_provider") as provider:
            blocked = self.client.post(f"/admin/learning/documents/{self.document_id}/generate/flashcards",
                                       data={"count": "1"})
        self.assertEqual(blocked.status_code, 404)
        self.assertIn("deleted", blocked.json()["detail"])
        provider.assert_not_called()
        listed = self.client.get(f"/admin/learning/modules/{self.module_id}/documents").json()["documents"]
        self.assertEqual(listed, [])
        db = self.db()
        self.assertEqual(db.query(models.Flashcard).filter_by(source_document_id=self.document_id).count(), 2)
        db.close()
        self.client.post(f"/admin/learning/documents/{self.document_id}/restore")
        self.assertIn(f'value="{self.document_id}"', self.filter_options())

    def test_permanent_deletion_is_blocked_while_content_depends_on_the_document(self):
        self.client.post(f"/admin/learning/documents/{self.document_id}/delete", data={"confirm": "yes"})
        page = self.client.get(f"/admin/learning/documents/{self.document_id}/purge")
        self.assertIn("data-blocked", page.text)
        self.assertIn("2 flashcard(s)", page.text)
        response = self.client.post(f"/admin/learning/documents/{self.document_id}/purge", data={"confirm": "yes"})
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.row(models.Document, self.document_id))

    def test_unused_document_can_be_purged_after_soft_delete_and_its_file_is_removed(self):
        spare = self.upload("Spare handout")
        db = self.db()
        stored = smartquiz_app.LEARNING_UPLOADS_DIR / db.get(models.Document, spare).storage_filename
        db.close()
        self.assertTrue(stored.is_file())
        early = self.client.post(f"/admin/learning/documents/{spare}/purge", data={"confirm": "yes"})
        self.assertEqual(early.status_code, 409)  # must be in Recently deleted first
        self.client.post(f"/admin/learning/documents/{spare}/delete", data={"confirm": "yes"})
        done = self.client.post(f"/admin/learning/documents/{spare}/purge", data={"confirm": "yes"},
                                follow_redirects=False)
        self.assertEqual(done.headers["location"], "/admin/learning/dashboard?notice=document_purged")
        self.assertIsNone(self.row(models.Document, spare))
        self.assertFalse(stored.exists())
        self.assertTrue(Path(smartquiz_app.LEARNING_UPLOADS_DIR).is_dir())

    def test_quiz_questions_and_results_survive_source_document_deletion(self):
        quiz_id = self.create_draft_quiz()
        self.client.post(f"/admin/learning/questions/{self.question_ids[0]}/add-to-quiz", data={"quiz_id": quiz_id})
        db = self.db()
        quiz_question = db.query(models.Question).filter_by(quiz_id=quiz_id).one()
        live = models.Session(quiz_id=quiz_id, session_code="DOCDEL", status="Completed")
        db.add(live)
        db.flush()
        participant = models.Student(quiz_id=quiz_id, session_id=live.id, name="Past", roll_number="P1",
                                     status="Finished", result_token="d" * 43)
        db.add(participant)
        db.flush()
        db.add(models.Response(student_id=participant.id, session_id=live.id, quiz_id=quiz_id,
                               question_id=quiz_question.id, selected_option="Recommend a business action",
                               is_correct=True, score=1, submitted_at=datetime.now(timezone.utc),
                               question_text_snapshot=quiz_question.question_text, question_type_snapshot="mcq_single",
                               question_options_snapshot=quiz_question.options,
                               correct_answer_snapshot=quiz_question.correct_answer, question_points_snapshot=1))
        db.commit()
        session_id, question_id, text = live.id, quiz_question.id, quiz_question.question_text
        db.close()
        before = self.client.get(f"/admin/results/data?session_id={session_id}").json()
        self.client.post(f"/admin/learning/documents/{self.document_id}/delete", data={"confirm": "yes"})
        self.assertEqual(self.client.post(f"/admin/learning/documents/{self.document_id}/purge",
                                          data={"confirm": "yes"}).status_code, 409)
        self.assertEqual(self.client.get(f"/admin/results/data?session_id={session_id}").json(), before)
        kept = self.row(models.Question, question_id)
        self.assertEqual((kept.question_text, kept.source_document_id), (text, self.document_id))
        provenance = self.client.get(f"/admin/quizzes/{quiz_id}/questions/{question_id}/provenance")
        self.assertEqual(provenance.status_code, 200)
        self.assertIn("Module 2 lesson", provenance.text)


# =========================================================== 3/4. shared teacher UI

class TeacherUiTests(_LifecycleBase):
    PAGES = ["/admin/content", "/admin/student-accounts", "/admin/learning/dashboard", "/admin/question-banks",
             "/admin/quizzes/assemble", "/admin/sessions"]

    def test_every_teacher_page_loads_the_shared_dropdown_design(self):
        for path in self.PAGES:
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn("--sq-option-bg: #8FBE9D", page.text, path)
            self.assertIn("--sq-option-selected-bg: #176B3A", page.text, path)
            self.assertIn('select.form-select', page.text, path)
            self.assertEqual(page.text.count('role", "combobox"'), 1, path)

    def test_custom_dropdown_keeps_the_native_select_for_submission_and_keyboard_support(self):
        page = self.client.get("/admin/content").text
        for marker in ('dispatchEvent(new Event("change"', '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"',
                       "aria-activedescendant", "aria-expanded", "aria-labelledby", "aria-selected", '"invalid"'):
            self.assertIn(marker, page)

    def test_selected_filter_values_still_round_trip_through_the_server(self):
        page = self.client.get(f"/admin/content?kind=flashcards&document_id={self.document_id}&status=all").text
        self.assertIn('<option value="flashcards" selected>', page)
        self.assertRegex(_select_html(page, "filter-document"), rf'<option value="{self.document_id}" selected>')
        self.assertEqual(page.count("data-content-row="), 2)

    def test_student_accounts_course_choices_exclude_unusable_courses(self):
        self.client.post(f"/admin/learning/courses/{self.other_course_id}/archive")
        page = self.client.get("/admin/student-accounts").text
        chooser = _select_html(page, f"assignment-course-{self.account_id}")
        self.assertIn(f'value="{self.course_id}"', chooser)
        self.assertNotIn(f'value="{self.other_course_id}"', chooser)

    def test_search_fields_have_clear_control_and_no_autocomplete(self):
        for path, field_id in [("/admin/content", "filter-q"), ("/admin/question-banks", "filter-q")]:
            page = self.client.get(path + "?q=lesson").text
            field = re.search(r'<input[^>]*id="%s"[^>]*>' % field_id, page).group(0)
            for attr in ('data-clear-search', 'autocomplete="off"', 'type="search"', 'value="lesson"'):
                self.assertIn(attr, field, path)
        accounts = self.client.get("/admin/student-accounts").text
        self.assertRegex(accounts, r'<input[^>]*name="q"[^>]*data-clear-search')
        self.assertIn('aria-label", "Clear search"', accounts)

    def test_any_course_and_any_document_choices_do_not_break_filter_forms(self):
        # The filter forms submit "" for "Any course" / "Any document" / "All courses".
        for path in ("/admin/content?status=all&kind=all&course_id=&document_id=&q=",
                     "/admin/question-banks?status=active&course_id=&q=",
                     "/admin/quizzes/assemble?course_id="):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn('"detail"', response.text, path)
        self.assertEqual(self.client.get("/admin/content?status=all&course_id=&document_id=").text
                         .count("data-content-row="), 5)

    def test_clearing_search_keeps_other_filters(self):
        # The clear control re-submits the same form with q empty: other fields keep their values.
        filtered = self.client.get(f"/admin/content?status=all&kind=questions&document_id={self.document_id}&q=").text
        self.assertEqual(filtered.count("data-content-row="), 3)
        self.assertIn('<option value="questions" selected>', filtered)
        searched = self.client.get(f"/admin/content?status=all&kind=questions&q=graded").text
        self.assertEqual(searched.count("data-content-row="), 1)


# =========================================================== 5. study layout

class StudyLayoutTests(_LifecycleBase):
    def test_study_page_starts_below_the_fixed_navbar_with_aligned_header(self):
        page = self.student.get("/student/study")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="container study-shell sq-page"', page.text)
        self.assertIn("--sq-navbar-h", page.text)
        self.assertIn("padding-top: calc(var(--sq-navbar-h)", page.text)
        self.assertIn("data-study-header", page.text)
        self.assertIn('href="/student/dashboard"', page.text)
        for element in ('id="study-empty"', 'id="study-panel"', 'id="reviewed-today"', 'id="remaining-count"'):
            self.assertIn(element, page.text)
        for path in ("/student/dashboard", "/student/practice"):
            self.assertIn("sq-page", self.student.get(path).text, path)

    def test_queue_for_empty_and_non_empty_study_states(self):
        empty = self.student.get("/student/study/queue")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["cards"], [])
        self.assign("flashcards", self.card_ids[0])
        filled = self.student.get("/student/study/queue").json()
        self.assertEqual([card["id"] for card in filled["cards"]], [self.card_ids[0]])
        self.assertEqual(self.student.get("/student/study").status_code, 200)


# =========================================================== 6. AI reliability

def _api_error(cls, code, status, message):
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


def _ok(items):
    response = MagicMock()
    response.text = json.dumps({"items": items})
    return response


class GenerationReliabilityTests(_AppTestBase):
    FRESH = [dict(card, front="Reliability: " + card["front"]) for card in FLASHCARDS]

    def setUp(self):
        super().setUp()
        self.document_id = self.upload_lesson()
        self.sleeps = []
        sleep_patch = patch.object(learning_content_ai, "_sleep", side_effect=self.sleeps.append)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def run_gemini(self, outcomes, path=None, data=None, headers=None):
        """POST a generation form while google.genai returns/raises `outcomes` in turn."""
        path = path or f"/admin/learning/modules/{self.module_id}/generate/flashcards"
        data = data or {"document_id": self.document_id, "count": "2"}
        with patch.dict(os.environ, GEN_ENV), patch("google.genai.Client") as client_class:
            client_class.return_value.models.generate_content.side_effect = outcomes
            response = self.client.post(path, data=data, headers=headers or {}, follow_redirects=False)
            calls = client_class.return_value.models.generate_content.call_count
        return response, calls

    def saved(self, model=models.Flashcard):
        db = self.db()
        try:
            return db.query(model).count()
        finally:
            db.close()

    def test_success_on_first_attempt(self):
        response, calls = self.run_gemini([_ok(self.FRESH)])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((calls, self.sleeps, self.saved()), (1, [], 2))

    def test_temporary_503_then_success_is_retried_once(self):
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "This model is currently experiencing high demand.")
        response, calls = self.run_gemini([busy, _ok(self.FRESH)], headers=HTML)
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=saved&generated=2&generated_kind=flashcards", response.headers["location"])
        self.assertEqual((calls, self.sleeps, self.saved()), (2, [2], 2))
        notice = self.client.get(response.headers["location"]).text
        self.assertIn("2 flashcards generated from “Module 2 lesson”", notice)

    def test_temporary_429_then_success_is_retried(self):
        limited = _api_error(genai_errors.ClientError, 429, "RESOURCE_EXHAUSTED", "Quota exceeded.")
        response, calls = self.run_gemini([limited, _ok(self.FRESH)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual((calls, self.sleeps, self.saved()), (2, [2], 2))

    def test_repeated_503_exhausts_retries_and_shows_a_readable_page(self):
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "This model is currently experiencing high demand.")
        response, calls = self.run_gemini([busy, busy, busy, busy], headers=HTML)
        self.assertEqual((calls, self.sleeps, self.saved()), (3, [2, 4], 0))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/admin/learning/dashboard#source-document-{self.document_id}")
        page = self.client.get("/admin/learning/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-generation-error="unavailable"', page.text)
        self.assertIn("Gemini is temporarily unavailable", page.text)
        self.assertIn("Your content has not been lost", page.text)
        self.assertIn("SmartQuiz tried 3 times", page.text)
        self.assertIn("data-retry-generation", page.text)
        self.assertIn(">Retry<", page.text)
        self.assertNotIn('{"detail"', page.text)
        self.assertNotIn("test-key-not-sent", page.text)
        # The message is shown once, not on every later visit.
        self.assertNotIn("data-generation-error", self.client.get("/admin/learning/dashboard").text)

    def test_json_clients_still_get_a_json_error(self):
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "High demand.")
        response, calls = self.run_gemini([busy] * 3)
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 503 UNAVAILABLE", response.json()["detail"])

    def test_invalid_api_key_is_not_retried(self):
        bad_key = _api_error(genai_errors.ClientError, 400, "INVALID_ARGUMENT",
                             "API key not valid. Please pass a valid API key.")
        response, calls = self.run_gemini([bad_key, _ok(self.FRESH)], headers=HTML)
        self.assertEqual((calls, self.sleeps, self.saved()), (1, [], 0))
        page = self.client.get("/admin/learning/dashboard").text
        self.assertIn('data-generation-error="configuration"', page)
        self.assertIn("Retrying will not help", page)
        self.assertNotIn("data-retry-generation", page)

    def test_authentication_failure_is_not_retried(self):
        denied = _api_error(genai_errors.ClientError, 403, "PERMISSION_DENIED", "Permission denied.")
        _, calls = self.run_gemini([denied, _ok(self.FRESH)])
        self.assertEqual((calls, self.sleeps), (1, []))

    def test_unsupported_model_is_not_retried(self):
        missing = _api_error(genai_errors.ClientError, 404, "NOT_FOUND", "models/test-model is not found.")
        response, calls = self.run_gemini([missing, _ok(self.FRESH)], headers=HTML)
        self.assertEqual((calls, self.sleeps, self.saved()), (1, [], 0))
        self.assertIn('data-generation-error="configuration"', self.client.get("/admin/learning/dashboard").text)

    def test_malformed_provider_output_is_not_retried_and_nothing_is_saved(self):
        broken = MagicMock()
        broken.text = "this is not json"
        response, calls = self.run_gemini([broken, _ok(self.FRESH)], headers=HTML)
        self.assertEqual((calls, self.sleeps, self.saved()), (1, [], 0))
        page = self.client.get("/admin/learning/dashboard").text
        self.assertIn('data-generation-error="output"', page)
        self.assertIn("did not pass SmartQuiz", page)
        self.assertIn(">Try again<", page)

    def test_timeout_is_retried_only_within_the_total_time_budget(self):
        clock = [0.0]

        def slow_timeout(*args, **kwargs):
            clock[0] += learning_content_ai.REQUEST_TIMEOUT_SECONDS
            raise httpx.ReadTimeout("read timed out")

        with patch.object(learning_content_ai, "_clock", side_effect=lambda: clock[0]), \
                patch.object(learning_content_ai, "_sleep", side_effect=lambda s: (self.sleeps.append(s),
                                                                                  clock.__setitem__(0, clock[0] + s))):
            response, calls = self.run_gemini(slow_timeout, headers=HTML)
        # 45 s + 2 s + 45 s = 92 s; a third attempt could end at 141 s > 120 s budget.
        self.assertEqual((calls, self.sleeps, self.saved()), (2, [2], 0))
        page = self.client.get("/admin/learning/dashboard").text
        self.assertIn('data-generation-error="timeout"', page)
        self.assertIn("did not respond in time", page)

    def test_network_failure_is_retried(self):
        response, calls = self.run_gemini([httpx.ConnectError("offline"), _ok(self.FRESH)])
        self.assertEqual((response.status_code, calls, self.sleeps), (200, 2, [2]))

    def test_resubmitting_the_same_form_never_duplicates_content(self):
        data = {"document_id": self.document_id, "count": "2", "request_token": "tok_" + "a" * 20}
        first, calls_first = self.run_gemini([_ok(self.FRESH)], data=data, headers=HTML)
        second, calls_second = self.run_gemini([_ok(self.FRESH)], data=data, headers=HTML)
        self.assertEqual((calls_first, calls_second, self.saved()), (1, 0, 2))
        self.assertIn("repeat=1", second.headers["location"])
        self.assertIn("already completed", self.client.get(second.headers["location"]).text)

    def test_a_request_already_running_is_not_started_twice(self):
        token = "tok_" + "b" * 20
        smartquiz_app._generations_in_progress.add(token)
        try:
            response, calls = self.run_gemini([_ok(self.FRESH)], headers=HTML,
                                              data={"document_id": self.document_id, "count": "2",
                                                    "request_token": token})
        finally:
            smartquiz_app._generations_in_progress.discard(token)
        self.assertEqual((calls, self.saved()), (0, 0))
        self.assertIn("already running", self.client.get("/admin/learning/dashboard").text)

    def test_retries_cannot_duplicate_and_an_invalid_batch_saves_nothing(self):
        bad_batch = [self.FRESH[0], dict(self.FRESH[1], source_reference="P999")]
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "busy")
        response, calls = self.run_gemini([busy, _ok(bad_batch)])
        self.assertEqual((calls, self.saved()), (2, 0))
        response, calls = self.run_gemini([busy, _ok(self.FRESH)])
        self.assertEqual((calls, self.saved()), (2, 2))

    def test_failed_save_rolls_back_the_whole_batch(self):
        with patch("sqlalchemy.orm.Session.commit", side_effect=RuntimeError("disk full")):
            with patch.dict(os.environ, GEN_ENV), patch.object(
                    learning_content_ai, "_request_provider", side_effect=_fake_provider(self.FRESH)):
                with self.assertRaises(RuntimeError):
                    self.client.post(f"/admin/learning/documents/{self.document_id}/generate/flashcards",
                                     data={"count": "2"})
        self.assertEqual(self.saved(), 0)

    def test_both_generation_forms_answer_with_pages_not_json(self):
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "busy")
        routes = [
            (f"/admin/learning/modules/{self.module_id}/generate/flashcards", {"document_id": self.document_id, "count": "2"}),
            (f"/admin/learning/modules/{self.module_id}/generate/questions", {"document_id": self.document_id, "count": "3"}),
            (f"/admin/learning/documents/{self.document_id}/generate/flashcards", {"count": "2"}),
            (f"/admin/learning/documents/{self.document_id}/generate/questions", {"count": "3"}),
        ]
        for path, data in routes:
            response, _ = self.run_gemini([busy] * 3, path=path, data=data, headers=HTML)
            self.assertEqual(response.status_code, 303, path)
            self.assertNotIn("application/json", response.headers.get("content-type", ""), path)
            page = self.client.get(response.headers["location"])
            self.assertIn("Gemini is temporarily unavailable", page.text, path)
            self.assertIn(f'action="{path}"', page.text, path)  # Retry posts to the same form
        with patch.dict(os.environ, GEN_ENV), patch.object(
                learning_content_ai, "_request_provider", side_effect=_fake_provider(QUESTIONS)):
            ok = self.client.post(f"/admin/learning/modules/{self.module_id}/generate/questions",
                                  data={"document_id": self.document_id, "count": "3"}, headers=HTML,
                                  follow_redirects=False)
        self.assertIn("generated_kind=questions", ok.headers["location"])
        self.assertIn("3 quiz questions generated", self.client.get(ok.headers["location"]).text)

    def test_failed_form_keeps_the_teachers_settings(self):
        busy = _api_error(genai_errors.ServerError, 503, "UNAVAILABLE", "busy")
        self.run_gemini([busy] * 3, path=f"/admin/learning/modules/{self.module_id}/generate/questions",
                        data={"document_id": self.document_id, "count": "7"}, headers=HTML)
        page = self.client.get("/admin/learning/dashboard").text
        self.assertRegex(page, rf'id="question-count-{self.document_id}"[^>]*value="7"')
        self.assertRegex(page, r'name="count" value="7"')

    def test_generation_forms_have_request_tokens_and_loading_labels(self):
        page = self.client.get("/admin/learning/dashboard").text
        self.assertIn('data-busy-label="Generating flashcards…"', page)
        self.assertIn('data-busy-label="Generating quiz questions…"', page)
        tokens = re.findall(r'name="request_token" value="([^"]+)"', page)
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)
        self.assertIn('form.dataset.submitting === "1"', page)  # repeated clicks are ignored

    def test_configured_model_is_used_unchanged(self):
        with patch.dict(os.environ, {"AI_CONTENT_PROVIDER": "gemini", "GEMINI_API_KEY": "k",
                                     "GEMINI_MODEL": "gemini-3.5-flash"}, clear=True):
            self.assertEqual(learning_content_ai._provider_config()[1], "gemini-3.5-flash")


class ErrorClassificationTests(unittest.TestCase):
    def test_http_codes_map_to_retry_policy(self):
        transient = {429, 500, 502, 503, 504}
        for code in (400, 401, 403, 404, 409, 422, 429, 500, 501, 502, 503, 504):
            kind = learning_content_ai._http_kind(code)
            self.assertEqual(kind in learning_content_ai.TRANSIENT_KINDS, code in transient, code)
        self.assertEqual(learning_content_ai._http_kind(400, "API key not valid"), learning_content_ai.CONFIGURATION)

    def test_missing_key_is_a_configuration_error(self):
        with patch.dict(os.environ, {"AI_CONTENT_PROVIDER": "gemini"}, clear=True):
            with self.assertRaises(learning_content_ai.ContentGenerationError) as caught:
                learning_content_ai.generate_flashcards("[[SOURCE: P1]]\nLong enough source text here.", "T", 1)
        self.assertEqual(caught.exception.kind, learning_content_ai.CONFIGURATION)
        self.assertFalse(caught.exception.transient)


if __name__ == "__main__":
    unittest.main()
