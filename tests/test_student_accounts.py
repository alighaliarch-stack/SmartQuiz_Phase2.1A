import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as smartquiz_app
import auth
import database as database_module
import models
from database import Base, get_db


class StudentAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.teacher_password_hash = auth.hash_password("Teacher-test-password-2026")

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        db = self.Session()
        db.add(models.AppSetting(key=auth.PASSWORD_HASH_KEY, value=self.teacher_password_hash))
        self.course = models.Course(name="Biology", code="BIO", description="")
        other_course = models.Course(name="History", code="HIST", description="")
        db.add_all([self.course, other_course])
        db.flush()
        self.module = models.LearningModule(
            course_id=self.course.id,
            title="Cells",
            description="Cell biology",
            learning_objectives="Identify cell parts",
            status="PUBLISHED",
        )
        self.other_module = models.LearningModule(
            course_id=other_course.id,
            title="Ancient History",
            description="",
            learning_objectives="",
            status="PUBLISHED",
        )
        db.add_all([self.module, self.other_module])
        self.quiz = models.Quiz(
            title="Compatibility quiz",
            description="",
            duration_minutes=10,
            passing_percentage=60,
            status="Live",
        )
        db.add(self.quiz)
        db.flush()
        self.live_session = models.Session(
            quiz_id=self.quiz.id,
            session_code="TST123",
            status="Live",
        )
        db.add(self.live_session)
        db.commit()
        self.course_id = self.course.id
        self.module_id = self.module.id
        self.other_module_id = self.other_module.id
        self.other_course_id = other_course.id
        self.quiz_id = self.quiz.id
        self.session_id = self.live_session.id
        db.close()

        def override_get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self.previous_override = smartquiz_app.app.dependency_overrides.get(get_db)
        smartquiz_app.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )

    def test_student_account_migration_is_additive_and_idempotent(self):
        migration_engine = create_engine("sqlite://")
        original_engine = database_module.engine
        try:
            with migration_engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE student_accounts (id INTEGER PRIMARY KEY, username VARCHAR(80))"
                )
            database_module.engine = migration_engine
            database_module._run_migrations()
            database_module._run_migrations()
            with migration_engine.connect() as conn:
                columns = {
                    row[1] for row in conn.exec_driver_sql("PRAGMA table_info(student_accounts)")
                }
            self.assertTrue({
                "session_version",
                "failed_login_attempts",
                "locked_until",
            }.issubset(columns))
        finally:
            database_module.engine = original_engine
            migration_engine.dispose()

    def tearDown(self):
        if self.previous_override is None:
            smartquiz_app.app.dependency_overrides.pop(get_db, None)
        else:
            smartquiz_app.app.dependency_overrides[get_db] = self.previous_override
        self.engine.dispose()

    def _teacher_login(self):
        response = self.client.post(
            "/login",
            data={"password": "Teacher-test-password-2026", "next": "/admin"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def _create_account(self, username="student.one", student_code="S-001"):
        self._teacher_login()
        response = self.client.post(
            "/admin/student-accounts",
            data={
                "username": username,
                "student_code": student_code,
                "full_name": "Student One",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotIn("password_hash", response.text)
        return body["account"], body["temporary_password"]

    def _change_temporary_password(
        self,
        temporary_password,
        new_password="new-student-password-2026",
        username="student.one",
    ):
        login = self.client.post(
            "/student/login",
            data={"username": username, "password": temporary_password},
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 303)
        self.assertEqual(login.headers["location"], "/student/change-password")
        denied = self.client.get("/student/learning")
        self.assertEqual(denied.status_code, 403)
        changed = self.client.post(
            "/student/change-password",
            data={"new_password": new_password, "confirm_password": new_password},
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        self.assertEqual(changed.headers["location"], "/student/learning")
        return new_password

    def _assign_module(self, account_id, module_id=None, course_id=None):
        self._teacher_login()
        return self.client.post(
            f"/admin/student-accounts/{account_id}/enrollments",
            data={
                "course_id": course_id or self.course_id,
                "module_id": module_id if module_id is not None else self.module_id,
            },
        )

    def test_teacher_creates_accounts_rejects_duplicate_and_searches(self):
        account, _ = self._create_account()
        duplicate = self.client.post(
            "/admin/student-accounts",
            data={"username": "STUDENT.ONE", "student_code": "S-002", "full_name": "Duplicate"},
        )
        self.assertEqual(duplicate.status_code, 409)
        searched = self.client.get("/admin/student-accounts?q=S-001")
        self.assertEqual(searched.status_code, 200)
        self.assertIn("student.one", searched.text)
        self.assertNotIn("password_hash", searched.text)
        self.assertEqual(account["student_code"], "S-001")

    def test_teacher_edits_and_reactivates_student_accounts(self):
        account, _ = self._create_account()
        self.client.post(
            f"/admin/student-accounts/{account['id']}/status",
            data={"active": "false"},
        )
        edited = self.client.post(
            f"/admin/student-accounts/{account['id']}/edit",
            data={
                "username": "student.updated",
                "student_code": "S-101",
                "full_name": "Updated Student",
            },
        )
        self.assertEqual(edited.status_code, 200)
        activated = self.client.post(
            f"/admin/student-accounts/{account['id']}/status",
            data={"active": "true"},
        )
        self.assertTrue(activated.json()["is_active"])
        listing = self.client.get("/admin/student-accounts")
        self.assertIn("Updated Student", listing.text)
        self.assertNotIn("password_hash", listing.text)

    def test_student_login_password_change_logout_and_wrong_password(self):
        _, temporary = self._create_account()
        bad_login = self.client.post(
            "/student/login",
            data={"username": "student.one", "password": "incorrect-password"},
            follow_redirects=False,
        )
        self.assertEqual(bad_login.status_code, 303)
        self.assertIn("Invalid", bad_login.headers["location"])

        self._change_temporary_password(temporary)
        self.assertEqual(self.client.get("/student/learning").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/student/logout",
                headers={"Origin": "https://attacker.example"},
                follow_redirects=False,
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/student/learning").status_code, 200)
        self.assertEqual(
            self.client.post(
                "/student/change-password",
                data={
                    "new_password": "forged-password-2026",
                    "confirm_password": "forged-password-2026",
                },
                headers={"Origin": "https://attacker.example"},
                follow_redirects=False,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/admin/student-accounts", follow_redirects=False).status_code,
            303,
        )
        other_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(
            other_client.post(
                "/student/login",
                data={"username": "student.one", "password": "new-student-password-2026"},
                follow_redirects=False,
            ).headers["location"],
            "/student/learning",
        )
        logout = self.client.post("/student/logout", follow_redirects=False)
        self.assertEqual(logout.status_code, 303)
        self.assertEqual(self.client.get("/student/learning").status_code, 401)
        self.assertEqual(other_client.get("/student/learning").status_code, 401)

    def test_csrf_origin_gate_blocks_cross_origin_and_originless_mutations(self):
        blocked_login = self.client.post(
            "/login",
            data={"password": "Teacher-test-password-2026"},
            headers={"Origin": "https://attacker.example"},
            follow_redirects=False,
        )
        self.assertEqual(blocked_login.status_code, 403)
        self.assertEqual(
            self.client.post(
                "/student/enrollments",
                data={"course_id": self.course_id},
                headers={"Origin": "http://testserver.evil"},
            ).status_code,
            403,
        )
        self._teacher_login()
        self.assertEqual(
            self.client.post(
                "/admin/student-accounts",
                data={
                    "username": "csrf.student",
                    "student_code": "CSRF-1",
                    "full_name": "CSRF Student",
                },
                headers={"Origin": "https://attacker.example"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/admin/student-accounts",
                data={
                    "username": "csrf.student",
                    "student_code": "CSRF-1",
                    "full_name": "CSRF Student",
                },
            ).status_code,
            201,
        )
        self.assertEqual(
            self.client.post(
                "/student/enrollments",
                data={"course_id": self.course_id},
                headers={"Origin": ""},
            ).status_code,
            403,
        )

    def test_unauthenticated_student_routes_and_teacher_routes_are_rejected(self):
        self.assertEqual(self.client.get("/student/learning").status_code, 401)
        self.assertEqual(self.client.get("/student/flashcards").status_code, 401)
        self.assertEqual(self.client.get("/student/flashcards/1/reviews").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/student/flashcards/1/review",
                data={"rating": "Good"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/student/enrollments",
                data={"course_id": self.course_id},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/admin/student-accounts", follow_redirects=False).status_code,
            303,
        )

    def test_student_login_lockout_and_expiry(self):
        _, temporary = self._create_account()
        self.client.post("/student/logout", follow_redirects=False)
        for _ in range(auth.STUDENT_LOGIN_FAILURE_LIMIT):
            response = self.client.post(
                "/student/login",
                data={"username": "student.one", "password": "wrong-password"},
                follow_redirects=False,
            )
            self.assertIn("Invalid", response.headers["location"])
        locked_login = self.client.post(
            "/student/login",
            data={"username": "student.one", "password": temporary},
            follow_redirects=False,
        )
        self.assertIn("Invalid", locked_login.headers["location"])

        db = self.Session()
        account = db.query(models.StudentAccount).filter_by(username="student.one").one()
        self.assertIsNotNone(account.locked_until)
        account.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        db.close()
        self.assertEqual(
            self.client.post(
                "/student/login",
                data={"username": "student.one", "password": temporary},
                follow_redirects=False,
            ).headers["location"],
            "/student/change-password",
        )

    def test_teacher_login_lockout_and_password_change_clears_it(self):
        for _ in range(auth.TEACHER_LOGIN_FAILURE_LIMIT):
            self.client.post(
                "/login",
                data={"password": "incorrect-teacher-password"},
                follow_redirects=False,
            )
        blocked = self.client.post(
            "/login",
            data={"password": "Teacher-test-password-2026"},
            follow_redirects=False,
        )
        self.assertIn("Incorrect", blocked.headers["location"])

        db = self.Session()
        locked_until = db.query(models.AppSetting).filter_by(
            key=auth.TEACHER_LOGIN_LOCKED_UNTIL_KEY
        ).one()
        locked_until.value = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
        db.close()
        self.assertEqual(self._teacher_login(), None)
        previous_teacher_session = self.client.cookies.get("session")
        changed = self.client.post(
            "/admin/settings/password",
            data={
                "current_password": "Teacher-test-password-2026",
                "new_password": "Teacher-updated-password-2026",
                "confirm_password": "Teacher-updated-password-2026",
            },
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        self.assertNotEqual(
            self.client.cookies.get("session"),
            previous_teacher_session,
        )
        db = self.Session()
        self.assertEqual(
            db.query(models.AppSetting).filter_by(
                key=auth.TEACHER_LOGIN_LOCKED_UNTIL_KEY
            ).one().value,
            "",
        )
        db.close()

    def test_student_password_change_renews_and_invalidates_other_sessions(self):
        _, temporary = self._create_account()
        self._change_temporary_password(temporary)
        other_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        second_login = other_client.post(
            "/student/login",
            data={"username": "student.one", "password": "new-student-password-2026"},
            follow_redirects=False,
        )
        self.assertEqual(second_login.headers["location"], "/student/learning")
        previous_cookie = self.client.cookies.get("session")

        changed = self.client.post(
            "/student/change-password",
            data={
                "new_password": "updated-student-password-2026",
                "confirm_password": "updated-student-password-2026",
            },
            follow_redirects=False,
        )
        self.assertEqual(changed.status_code, 303)
        self.assertNotEqual(self.client.cookies.get("session"), previous_cookie)
        self.assertEqual(self.client.get("/student/learning").status_code, 200)
        self.assertEqual(other_client.get("/student/learning").status_code, 401)

    def test_teacher_reset_and_deactivation_invalidate_existing_student_sessions(self):
        account, temporary = self._create_account()
        self._change_temporary_password(temporary)
        student_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(
            student_client.post(
                "/student/login",
                data={"username": "student.one", "password": "new-student-password-2026"},
                follow_redirects=False,
            ).headers["location"],
            "/student/learning",
        )
        self._teacher_login()
        self.client.post(f"/admin/student-accounts/{account['id']}/reset-password")
        self.assertEqual(student_client.get("/student/learning").status_code, 401)

        reset_password = self.client.post(
            f"/admin/student-accounts/{account['id']}/reset-password"
        ).json()["temporary_password"]
        second_student_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(
            second_student_client.post(
                "/student/login",
                data={"username": "student.one", "password": reset_password},
                follow_redirects=False,
            ).headers["location"],
            "/student/change-password",
        )
        self.client.post(
            f"/admin/student-accounts/{account['id']}/status",
            data={"active": "false"},
        )
        self.assertEqual(second_student_client.get("/student/change-password").status_code, 401)

    def test_deactivated_account_cannot_continue_linked_quiz_session(self):
        account, temporary = self._create_account()
        self._change_temporary_password(temporary)
        student_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(
            student_client.post(
                "/student/login",
                data={"username": "student.one", "password": "new-student-password-2026"},
                follow_redirects=False,
            ).headers["location"],
            "/student/learning",
        )
        joined = student_client.post(
            "/join",
            data={
                "quiz_id": self.quiz_id,
                "name": "Ignored",
                "roll_number": "IGNORED",
            },
            follow_redirects=False,
        )
        self.assertEqual(joined.status_code, 303)
        db = self.Session()
        participant = db.query(models.Student).filter_by(account_id=account["id"]).one()
        participant_id = participant.id
        token = participant.result_token
        db.close()
        self._teacher_login()
        self.client.post(
            f"/admin/student-accounts/{account['id']}/status",
            data={"active": "false"},
        )
        self.assertEqual(
            student_client.get(
                f"/session/TST123/status?student_id={participant_id}&token={token}"
            ).status_code,
            404,
        )
        self.assertEqual(
            student_client.get(
                f"/student/results/{participant_id}?token={token}"
            ).status_code,
            404,
        )

    def test_teacher_password_reset_and_deactivation(self):
        account, temporary = self._create_account()
        new_password = self._change_temporary_password(temporary)
        self._teacher_login()
        reset = self.client.post(f"/admin/student-accounts/{account['id']}/reset-password")
        self.assertEqual(reset.status_code, 200)
        reset_data = reset.json()
        self.assertNotIn("password_hash", reset.text)
        self.assertEqual(
            self.client.post(
                "/student/login",
                data={"username": "student.one", "password": new_password},
                follow_redirects=False,
            ).headers["location"],
            "/student/login?error=Invalid+username+or+password.",
        )
        student_client = TestClient(
            smartquiz_app.app,
            headers={"Origin": "http://testserver"},
        )
        reset_login = student_client.post(
            "/student/login",
            data={"username": "student.one", "password": reset_data["temporary_password"]},
            follow_redirects=False,
        )
        self.assertEqual(reset_login.headers["location"], "/student/change-password")
        self.assertEqual(student_client.get("/student/learning").status_code, 403)
        self.client.post(
            f"/admin/student-accounts/{account['id']}/status",
            data={"active": "false"},
        )
        self.assertEqual(student_client.get("/student/learning").status_code, 401)
        inactive_login = student_client.post(
            "/student/login",
            data={"username": "student.one", "password": reset_data["temporary_password"]},
            follow_redirects=False,
        )
        self.assertIn("Invalid", inactive_login.headers["location"])

    def test_enrollment_publication_and_approval_are_server_enforced(self):
        account, temporary = self._create_account()
        self._change_temporary_password(temporary)
        self.client.post("/student/logout", follow_redirects=False)
        assignment = self._assign_module(account["id"])
        self.assertEqual(assignment.status_code, 201, assignment.text)

        db = self.Session()
        approved = models.Flashcard(
            module_id=self.module_id,
            front="What is a cell?",
            back="The basic unit of life.",
            approval_status="APPROVED",
        )
        draft = models.Flashcard(
            module_id=self.module_id,
            front="Draft card",
            back="Not published.",
            approval_status="DRAFT",
        )
        db.add_all([approved, draft])
        db.commit()
        approved_id = approved.id
        draft_id = draft.id
        db.close()

        login = self.client.post(
            "/student/login",
            data={"username": "student.one", "password": "new-student-password-2026"},
            follow_redirects=False,
        )
        self.assertEqual(login.headers["location"], "/student/learning")
        home = self.client.get("/student/learning").json()
        self.assertEqual([row["id"] for row in home["modules"]], [self.module_id])
        cards = self.client.get("/student/flashcards").json()["cards"]
        self.assertEqual([card["id"] for card in cards], [approved_id])
        self.assertEqual(self.client.get(f"/student/flashcards?module_id={self.other_module_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/student/flashcards/{draft_id}/reviews").status_code, 404)

        self.client.post("/student/logout", follow_redirects=False)
        self._teacher_login()
        self.assertEqual(
            self.client.post(
                "/student/enrollments",
                data={"course_id": self.course_id},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/admin/student-accounts/{account['id']}/enrollments",
                data={"course_id": self.course_id, "module_id": self.other_module_id},
            ).status_code,
            400,
        )

    def test_assigned_but_unpublished_module_is_not_accessible(self):
        account, temporary = self._create_account()
        self._change_temporary_password(temporary)
        self.client.post("/student/logout", follow_redirects=False)
        db = self.Session()
        draft = models.LearningModule(
            course_id=self.course_id,
            title="Unpublished Unit",
            description="",
            learning_objectives="",
            status="DRAFT",
        )
        db.add(draft)
        db.commit()
        draft_id = draft.id
        card = models.Flashcard(
            module_id=draft_id,
            front="Hidden question",
            back="Hidden answer",
            approval_status="APPROVED",
        )
        db.add(card)
        db.commit()
        card_id = card.id
        db.close()
        self.assertEqual(
            self._assign_module(account["id"], module_id=draft_id).status_code,
            201,
        )
        self.client.post(
            "/student/login",
            data={"username": "student.one", "password": "new-student-password-2026"},
            follow_redirects=False,
        )
        self.assertEqual(
            self.client.get(f"/student/flashcards?module_id={draft_id}").status_code,
            404,
        )
        self.assertEqual(self.client.get(f"/student/flashcards/{card_id}/reviews").status_code, 404)

    def test_existing_quiz_question_types_submission_and_ai_grading_dispatch(self):
        question_specs = [
            ("mcq_single", '["A", "B"]', '"A"', {"selected_option": "A"}),
            ("mcq_multiple", '["A", "B", "C"]', '["A", "C"]', {"selected_options": ["A", "C"]}),
            ("true_false", '["True", "False"]', '"True"', {"selected_option": "True"}),
            ("short_answer", "[]", '["water"]', {"selected_option": "water"}),
            ("short_answer", "[]", '["mitochondria produces cellular energy"]', {"selected_option": "an unrelated answer"}),
        ]
        questions = []
        db = self.Session()
        for index, (question_type, options, correct_answer, _answer) in enumerate(question_specs, 1):
            question = models.Question(
                quiz_id=self.quiz_id,
                question_text=f"Question {index}",
                question_type=question_type,
                options=options,
                correct_answer=correct_answer,
                points=2,
            )
            db.add(question)
            questions.append(question)
        db.commit()
        question_ids = [question.id for question in questions]
        db.close()
        joined = self.client.post(
            "/join",
            data={"quiz_id": self.quiz_id, "name": "Quiz Learner", "roll_number": "R-201"},
            follow_redirects=False,
        )
        participant_id = int(joined.headers["location"].split("?")[0].rsplit("/", 1)[1])
        token = joined.headers["location"].split("token=", 1)[1]
        db = self.Session()
        participant = db.query(models.Student).filter_by(id=participant_id).one()
        participant.session_id = self.session_id
        db.commit()
        db.close()

        for question_id, spec in zip(question_ids[:4], question_specs[:4]):
            question_type, _options, _correct, answer = spec
            payload = {
                "student_id": participant_id,
                "token": token,
                "question_id": question_id,
                **answer,
            }
            response = self.client.post(
                "/student/session/TST123/question/1",
                data=payload,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["success"])
            duplicate = self.client.post(
                "/student/session/TST123/question/1",
                data=payload,
            )
            self.assertTrue(duplicate.json()["already_submitted"])

        with patch.object(smartquiz_app, "_run_ai_grading_task") as grading_task:
            ai_response = self.client.post(
                "/student/session/TST123/question/1",
                data={
                    "student_id": participant_id,
                    "token": token,
                    "question_id": question_ids[4],
                    **question_specs[4][3],
                },
            )
        self.assertEqual(ai_response.status_code, 200, ai_response.text)
        self.assertTrue(grading_task.called)
        db = self.Session()
        saved = db.query(models.Response).filter_by(
            student_id=participant_id,
            question_id=question_ids[4],
        ).one()
        self.assertEqual(saved.evaluation_status, "EVALUATING")
        self.assertEqual(db.query(models.Response).filter_by(student_id=participant_id).count(), 5)
        db.close()
        historical_results = self.client.get(
            f"/student/results/{participant_id}?token={token}"
        )
        self.assertEqual(historical_results.status_code, 200)
        self.assertIn("Question 1", historical_results.text)

    def test_auto_advance_advances_individual_participant_once(self):
        db = self.Session()
        quiz = db.query(models.Quiz).filter_by(id=self.quiz_id).one()
        quiz.auto_advance = True
        first_question = models.Question(
            quiz_id=self.quiz_id,
            question_text="First",
            question_type="mcq_single",
            options='["A", "B"]',
            correct_answer='"A"',
            points=1,
        )
        second_question = models.Question(
            quiz_id=self.quiz_id,
            question_text="Second",
            question_type="true_false",
            options='["True", "False"]',
            correct_answer='"True"',
            points=1,
        )
        live_session = db.query(models.Session).filter_by(id=self.session_id).one()
        live_session.current_question_index = 1
        db.add_all([first_question, second_question])
        db.commit()
        first_question_id = first_question.id
        second_question_id = second_question.id
        session_id = self.session_id
        db.close()
        joined = self.client.post(
            "/join",
            data={"quiz_id": self.quiz_id, "name": "Auto Advance", "roll_number": "R-202"},
            follow_redirects=False,
        )
        participant_id = int(joined.headers["location"].split("?")[0].rsplit("/", 1)[1])
        token = joined.headers["location"].split("token=", 1)[1]
        db = self.Session()
        participant = db.query(models.Student).filter_by(id=participant_id).one()
        participant.session_id = session_id
        db.commit()
        db.close()

        page = self.client.get(
            f"/student/session/TST123/question/1?student_id={participant_id}&token={token}"
        )
        self.assertEqual(page.status_code, 200)
        first = self.client.post(
            "/student/session/TST123/question/1",
            data={
                "student_id": participant_id,
                "token": token,
                "question_id": first_question_id,
                "selected_option": "A",
            },
        )
        self.assertEqual(first.json()["next_question_number"], 2)
        duplicate = self.client.post(
            "/student/session/TST123/question/1",
            data={
                "student_id": participant_id,
                "token": token,
                "question_id": first_question_id,
                "selected_option": "A",
            },
        )
        self.assertTrue(duplicate.json()["already_submitted"])
        second_page = self.client.get(
            f"/student/session/TST123/question/2?student_id={participant_id}&token={token}"
        )
        self.assertEqual(second_page.status_code, 200)
        second = self.client.post(
            "/student/session/TST123/question/2",
            data={
                "student_id": participant_id,
                "token": token,
                "question_id": second_question_id,
                "selected_option": "True",
            },
        )
        self.assertTrue(second.json()["success"])
        db = self.Session()
        self.assertEqual(
            db.query(models.Session).filter_by(id=session_id).one().current_question_index,
            1,
        )
        db.close()

    def test_student_progress_is_account_scoped_and_requires_assignment(self):
        account, temporary = self._create_account()
        other_creation = self.client.post(
            "/admin/student-accounts",
            data={"username": "student.two", "student_code": "S-002", "full_name": "Student Two"},
        ).json()
        other_account = other_creation["account"]
        other_temporary_password = other_creation["temporary_password"]
        self.assertEqual(self._assign_module(account["id"]).status_code, 201)
        self.assertEqual(self._assign_module(other_account["id"]).status_code, 201)

        db = self.Session()
        card = models.Flashcard(
            module_id=self.module_id,
            front="Define a cell.",
            back="Basic unit of life.",
            approval_status="APPROVED",
        )
        db.add(card)
        db.commit()
        card_id = card.id
        db.close()

        self._change_temporary_password(temporary)
        reviewed = self.client.post(
            f"/student/flashcards/{card_id}/review",
            data={"rating": "Good", "student_id": other_account["id"]},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(reviewed.status_code, 403)
        reviewed = self.client.post(
            f"/student/flashcards/{card_id}/review",
            data={"rating": "Good", "student_id": other_account["id"]},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(self.client.get(f"/student/flashcards/{card_id}/reviews").json()["reviews"][0]["rating"], "Good")
        self.client.post("/student/logout", follow_redirects=False)

        self._change_temporary_password(
            other_temporary_password,
            new_password="other-student-password-2026",
            username="student.two",
        )
        self.assertEqual(
            self.client.get(f"/student/flashcards/{card_id}/reviews").json()["reviews"],
            [],
        )

    def test_quiz_participation_keeps_anonymous_flow_and_links_logged_in_accounts(self):
        anonymous_join = self.client.post(
            "/join",
            data={
                "quiz_id": self.quiz_id,
                "name": "Anonymous Learner",
                "roll_number": "R-100",
                "course": "Biology",
                "section": "A",
            },
            follow_redirects=False,
        )
        self.assertEqual(anonymous_join.status_code, 303)
        db = self.Session()
        anonymous = db.query(models.Student).filter(models.Student.roll_number == "R-100").one()
        self.assertIsNone(anonymous.account_id)
        token = anonymous.result_token
        anonymous_id = anonymous.id
        db.close()
        self.assertEqual(
            self.client.get(f"/student/results/{anonymous_id}?token={token}").status_code,
            200,
        )

        account, temporary = self._create_account()
        self._change_temporary_password(temporary)
        linked_join = self.client.post(
            "/join",
            data={
                "quiz_id": self.quiz_id,
                "name": "Forged Name",
                "roll_number": "FORGED",
            },
            follow_redirects=False,
        )
        self.assertEqual(linked_join.status_code, 303)
        db = self.Session()
        linked = db.query(models.Student).filter(models.Student.account_id == account["id"]).one()
        self.assertEqual(linked.name, "Student One")
        self.assertEqual(linked.roll_number, "S-001")
        self.assertTrue(linked.result_token)
        self.assertIn("token=", linked_join.headers["location"])
        db.close()

    def test_legacy_quiz_participant_ids_require_the_matching_token(self):
        first_join = self.client.post(
            "/join",
            data={"quiz_id": self.quiz_id, "name": "First", "roll_number": "R-101"},
            follow_redirects=False,
        )
        second_join = self.client.post(
            "/join",
            data={"quiz_id": self.quiz_id, "name": "Second", "roll_number": "R-102"},
            follow_redirects=False,
        )
        self.assertEqual(first_join.status_code, 303)
        self.assertEqual(second_join.status_code, 303)
        db = self.Session()
        first = db.query(models.Student).filter_by(roll_number="R-101").one()
        second = db.query(models.Student).filter_by(roll_number="R-102").one()
        question = models.Question(
            quiz_id=self.quiz_id,
            question_text="Pick A",
            question_type="mcq_single",
            options='["A", "B"]',
            correct_answer="A",
            points=1,
        )
        db.add(question)
        db.commit()
        first_id, first_token = first.id, first.result_token
        second_id, second_token = second.id, second.result_token
        question_id = question.id
        db.close()

        self.assertEqual(
            self.client.get(f"/student/waiting/{first_id}").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/student/waiting/{first_id}?token={first_token}"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/student/waiting/{second_id}?token={first_token}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/session/TST123/status?student_id={second_id}&token={first_token}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/student/session/TST123/question/1?student_id={second_id}&token={first_token}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/student/session/TST123/question/1",
                data={
                    "student_id": second_id,
                    "token": first_token,
                    "question_id": question_id,
                    "selected_option": "A",
                },
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/student/results/{second_id}?token={first_token}"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/student/results/{first_id}?token={first_token}"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/student/session/TST123/waiting?student_id={first_id}"
                f"&token={first_token}&last_question=1"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/student/session/TST123/completed?student_id={first_id}&token={first_token}"
            ).status_code,
            200,
        )
        self.assertTrue(second_token)
        db = self.Session()
        self.assertEqual(db.query(models.Response).count(), 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
