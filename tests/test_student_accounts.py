import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app as smartquiz_app
import auth
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
        db.close()

        def override_get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self.previous_override = smartquiz_app.app.dependency_overrides.get(get_db)
        smartquiz_app.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(smartquiz_app.app)

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

    def _assign_module(self, account_id, module=None):
        self._teacher_login()
        course_id = self.course_id if module is None else module.course_id
        module_id = self.module_id if module is None else module.id
        return self.client.post(
            f"/admin/student-accounts/{account_id}/enrollments",
            data={"course_id": course_id, "module_id": module_id},
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
            self.client.get("/admin/student-accounts", follow_redirects=False).status_code,
            303,
        )
        logout = self.client.post("/student/logout", follow_redirects=False)
        self.assertEqual(logout.status_code, 303)
        self.assertEqual(self.client.get("/student/learning").status_code, 401)

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
        student_client = TestClient(smartquiz_app.app)
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
        db.close()


if __name__ == "__main__":
    unittest.main()
