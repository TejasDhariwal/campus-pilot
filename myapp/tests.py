from io import BytesIO
from datetime import date, datetime, timedelta, timezone as datetime_timezone
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from google.genai.errors import APIError

from .models import (
    ActionItem,
    CampusDocument,
    DocumentDate,
    DocumentSection,
    DocumentUpload,
    StudentActionState,
    StudentDateState,
    StudentPlanItem,
)
from .services.document_extractor import (
    DocumentExtractionError,
    ExtractedAnnouncement,
    ExtractedDate,
    ExtractedSection,
    extract_announcement,
)
from .services.deadlines import (
    deadline_intelligence,
    is_notice_date,
    normalize_deadline_date,
)

# Protect the document import and read-only data contract used by the website.


class DeadlineIntelligenceServiceTests(TestCase):
    def test_identifies_notice_publication_dates_without_matching_other_dates(self):
        self.assertTrue(is_notice_date("Document Date"))
        self.assertTrue(is_notice_date("Date of Issue:"))
        self.assertTrue(is_notice_date("Published on"))
        self.assertFalse(is_notice_date("Semester registration deadline"))

    def test_normalizes_full_calendar_dates_but_preserves_uncertain_wording(self):
        self.assertEqual(
            normalize_deadline_date("15 October 2026"),
            date(2026, 10, 15),
        )
        self.assertEqual(
            normalize_deadline_date("October 15th, 2026"),
            date(2026, 10, 15),
        )
        self.assertEqual(
            normalize_deadline_date("2026-10-15"),
            date(2026, 10, 15),
        )
        self.assertIsNone(normalize_deadline_date("20 October"))
        self.assertIsNone(normalize_deadline_date("6th week of the semester"))
        self.assertIsNone(normalize_deadline_date("2026-02-30"))

    def test_classifies_deadline_thresholds(self):
        expected = (
            (-1, "overdue", "high"),
            (0, "due_today", "high"),
            (1, "urgent", "high"),
            (3, "urgent", "high"),
            (4, "soon", "medium"),
            (7, "soon", "medium"),
            (8, "upcoming", "low"),
        )
        today = date(2026, 10, 4)

        for days_remaining, status, urgency in expected:
            with self.subTest(days_remaining=days_remaining):
                self.assertEqual(
                    deadline_intelligence(
                        today + timedelta(days=days_remaining),
                        today=today,
                    ),
                    {
                        "days_remaining": days_remaining,
                        "status": status,
                        "status_label": {
                            "overdue": "Overdue",
                            "due_today": "Due today",
                            "urgent": "Urgent",
                            "soon": "Soon",
                            "upcoming": "Upcoming",
                        }[status],
                        "urgency": urgency,
                    },
                )

    def test_uses_campus_calendar_date_near_utc_midnight(self):
        utc_time = datetime(2026, 10, 4, 19, tzinfo=datetime_timezone.utc)
        with patch("myapp.services.deadlines.timezone.now", return_value=utc_time):
            result = deadline_intelligence(date(2026, 10, 5))

        self.assertEqual(result["days_remaining"], 0)
        self.assertEqual(result["status"], "due_today")

    def test_backfill_command_updates_only_unambiguous_existing_dates(self):
        document = CampusDocument.objects.create(
            source_file="calendar.pdf",
            document_type="Academic Calendar",
            title="Academic Calendar",
            summary="Dates.",
        )
        clear_date = DocumentDate.objects.create(
            document=document,
            description="Registration",
            date_text="15 October 2026",
        )
        uncertain_date = DocumentDate.objects.create(
            document=document,
            description="Evaluation period",
            date_text="6th week of the semester",
        )

        call_command("normalize_deadline_dates", verbosity=0)

        clear_date.refresh_from_db()
        uncertain_date.refresh_from_db()
        self.assertEqual(clear_date.normalized_date, date(2026, 10, 15))
        self.assertIsNone(uncertain_date.normalized_date)


class CampusDocumentModelTests(TestCase):
    def test_document_related_data_preserves_extracted_values_and_order(self):
        document = CampusDocument.objects.create(
            source_file="guidelines.pdf",
            document_type="Academic Guidelines",
            title="Guidelines for I semester Project based Learning",
            summary="Project and evaluation guidelines.",
        )
        vague_date = DocumentDate.objects.create(
            document=document,
            description="CIE Evaluation Timeline",
            date_text="6th week of the semester",
            position=0,
        )
        action = ActionItem.objects.create(
            document=document,
            description="Assign a mini-project group of no more than 4 students.",
            position=0,
        )
        section = DocumentSection.objects.create(
            document=document,
            heading="SEE Guidelines - Single Discipline",
            content="Evaluation is based on the report, presentation, and viva.",
            position=0,
        )

        self.assertEqual(document.dates.get(), vague_date)
        self.assertEqual(vague_date.date_text, "6th week of the semester")
        self.assertIsNone(vague_date.normalized_date)
        self.assertEqual(document.action_items.get(), action)
        self.assertIsNone(action.due_date)
        self.assertEqual(document.sections.get(), section)


class ImportCampusDocumentCommandTests(TestCase):
    def test_import_is_repeatable_and_preserves_uncertain_dates(self):
        payload = {
            "source_file": "guidelines.pdf",
            "document_type": "Academic Guidelines",
            "title": "Project guidelines",
            "summary": "Project and evaluation guidelines.",
            "dates": [
                {
                    "description": "CIE Evaluation Timeline",
                    "date": "6th week of the semester",
                }
            ],
            "instructions": ["Form a group of no more than 4 students."],
            "sections": [{"heading": "SEE", "content": "Report, presentation, and viva."}],
        }

        with tempfile.TemporaryDirectory() as directory:
            json_file = Path(directory) / "guidelines.json"
            json_file.write_text(json.dumps(payload), encoding="utf-8")

            call_command("import_campus_document", str(json_file), verbosity=0)
            call_command("import_campus_document", str(json_file), verbosity=0)

        document = CampusDocument.objects.get(
            source_file="guidelines.pdf",
            title="Project guidelines",
        )
        self.assertEqual(CampusDocument.objects.count(), 1)
        self.assertEqual(document.dates.count(), 1)
        self.assertEqual(document.dates.get().date_text, "6th week of the semester")
        self.assertIsNone(document.dates.get().normalized_date)
        self.assertEqual(document.action_items.count(), 1)
        self.assertEqual(document.sections.count(), 1)

    def test_import_normalizes_full_calendar_date_without_replacing_date_text(self):
        payload = {
            "source_file": "calendar.pdf",
            "document_type": "Academic Calendar",
            "title": "Academic Calendar",
            "summary": "Registration deadline.",
            "dates": [
                {
                    "description": "Semester registration",
                    "date": "15 October 2026",
                }
            ],
            "instructions": [],
            "sections": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            json_file = Path(directory) / "calendar.json"
            json_file.write_text(json.dumps(payload), encoding="utf-8")

            call_command("import_campus_document", str(json_file), verbosity=0)

        deadline = DocumentDate.objects.get()
        self.assertEqual(deadline.date_text, "15 October 2026")
        self.assertEqual(deadline.normalized_date, date(2026, 10, 15))

    def test_import_rejects_invalid_document_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            json_file = Path(directory) / "invalid.json"
            json_file.write_text(json.dumps({"title": "Incomplete"}), encoding="utf-8")

            with self.assertRaises(CommandError):
                call_command("import_campus_document", str(json_file), verbosity=0)

        self.assertEqual(CampusDocument.objects.count(), 0)


class CampusDocumentApiTests(TestCase):
    def setUp(self):
        self.document = CampusDocument.objects.create(
            source_file="guidelines.pdf",
            document_type="Academic Guidelines",
            title="Project guidelines",
            summary="Project and evaluation guidelines.",
        )
        DocumentDate.objects.create(
            document=self.document,
            description="CIE Evaluation Timeline",
            date_text="6th week of the semester",
        )
        ActionItem.objects.create(
            document=self.document,
            description="Form a project group.",
        )
        DocumentSection.objects.create(
            document=self.document,
            heading="SEE",
            content="Evaluation guidelines.",
        )

    def test_document_list_returns_nested_website_data(self):
        response = self.client.get(reverse("document-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()["documents"][0]
        self.assertEqual(payload["title"], "Project guidelines")
        self.assertEqual(
            payload["dates"][0],
            {
                "description": "CIE Evaluation Timeline",
                "date_text": "6th week of the semester",
                "normalized_date": None,
            },
        )
        self.assertEqual(payload["action_items"][0]["description"], "Form a project group.")
        self.assertEqual(payload["action_items"][0]["due_date"], None)
        self.assertEqual(payload["sections"][0]["heading"], "SEE")

    def test_document_detail_returns_404_for_unknown_document(self):
        response = self.client.get(reverse("document-detail", args=(999,)))

        self.assertEqual(response.status_code, 404)

    def test_document_endpoints_are_read_only(self):
        response = self.client.post(reverse("document-list"))

        self.assertEqual(response.status_code, 405)


class DeadlineApiTests(TestCase):
    def setUp(self):
        self.first_document = CampusDocument.objects.create(
            source_file="academic_calendar.pdf",
            document_type="Academic Calendar",
            title="Academic Calendar",
            summary="Key registration dates.",
        )
        self.second_document = CampusDocument.objects.create(
            source_file="scholarship.pdf",
            document_type="Scholarship Notice",
            title="Scholarship Notice",
            summary="Application details.",
        )
        self.registration = DocumentDate.objects.create(
            document=self.first_document,
            description="Semester registration",
            date_text="15 October 2026",
            normalized_date=date(2026, 10, 15),
        )
        self.scholarship = DocumentDate.objects.create(
            document=self.second_document,
            description="Scholarship application",
            date_text="10 October 2026",
            normalized_date=date(2026, 10, 10),
        )
        self.notice_date = DocumentDate.objects.create(
            document=self.first_document,
            description="Document Date",
            date_text="21 September 2026",
            normalized_date=date(2026, 9, 21),
        )
        DocumentDate.objects.create(
            document=self.first_document,
            description="Uncertain academic timeline",
            date_text="6th week of the semester",
        )

    def test_deadline_api_returns_sorted_calculated_dates_and_source_links(self):
        utc_time = datetime(2026, 10, 4, 19, tzinfo=datetime_timezone.utc)
        with patch("myapp.services.deadlines.timezone.now", return_value=utc_time):
            response = self.client.get(reverse("deadline-list"))

        self.assertEqual(response.status_code, 200)
        deadlines = response.json()["deadlines"]
        self.assertEqual(
            [item["title"] for item in deadlines],
            ["Scholarship application", "Semester registration"],
        )
        self.assertEqual(
            deadlines[0],
            {
                "title": "Scholarship application",
                "date": "2026-10-10",
                "date_text": "10 October 2026",
                "days_remaining": 5,
                "status": "soon",
                "status_label": "Soon",
                "urgency": "medium",
                "source_document": {
                    "title": "Scholarship Notice",
                    "url": reverse(
                        "document-detail-page",
                        args=(self.second_document.id,),
                    ),
                },
            },
        )
        self.assertEqual(deadlines[1]["days_remaining"], 10)
        self.assertNotIn("id", deadlines[0])
        self.assertNotIn("Document Date", [item["title"] for item in deadlines])

    def test_deadline_api_respects_only_the_signed_in_students_hidden_dates(self):
        user = get_user_model().objects.create_user(
            username="deadline-student",
            password="CampusPilot-Test-Password-937!",
        )
        StudentDateState.objects.create(
            user=user,
            document_date=self.scholarship,
            is_relevant=False,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("deadline-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["title"] for item in response.json()["deadlines"]],
            ["Semester registration"],
        )
        self.assertTrue(DocumentDate.objects.filter(pk=self.scholarship.pk).exists())

    def test_deadline_api_rejects_non_get_requests(self):
        response = self.client.post(reverse("deadline-list"))

        self.assertEqual(response.status_code, 405)


# Verify that each website page shows notice data without confusing it with student status.
class CampusPilotPageTests(TestCase):
    def setUp(self):
        self.document = CampusDocument.objects.create(
            source_file="guidelines.pdf",
            document_type="Academic Guidelines",
            title="Project guidelines",
            summary="Project and evaluation guidelines.",
        )
        DocumentDate.objects.create(
            document=self.document,
            description="CIE Evaluation Timeline",
            date_text="6th week of the semester",
        )
        ActionItem.objects.create(
            document=self.document,
            description="Form a group of no more than 4 students.",
        )
        DocumentSection.objects.create(
            document=self.document,
            heading="SEE Guidelines",
            content="Evaluation is based on the report, presentation, and viva.",
        )

    def test_dashboard_shows_dates_actions_and_recent_notices(self):
        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "myapp/dashboard.html")
        self.assertContains(response, "Important dates")
        self.assertContains(response, "6th week of the semester")
        self.assertContains(response, "Pending actions")
        self.assertContains(response, "Form a group of no more than 4 students.")
        self.assertContains(response, "Sign in to update")
        self.assertContains(response, "Project guidelines")
        date_row = response.content.decode().split('<article class="date-row">', 1)[1].split(
            "</article>",
            1,
        )[0]
        self.assertIn('<div class="date-row-control">', date_row)
        self.assertLess(
            date_row.index('<div class="date-row-control">'),
            date_row.index("View notice"),
        )
        self.assertLess(
            date_row.index("Sign in to customize"),
            date_row.index("View notice"),
        )

    def test_dashboard_greeting_uses_campus_time_and_signed_in_student_name(self):
        user = get_user_model().objects.create_user(
            username="campus-user",
            first_name="Asha",
            password="CampusPilot-Greeting-Password-937!",
        )
        self.client.force_login(user)
        local_times = (
            (datetime(2026, 10, 4, 5, tzinfo=datetime_timezone.utc), "Good morning, Asha"),
            (datetime(2026, 10, 4, 7, tzinfo=datetime_timezone.utc), "Good afternoon, Asha"),
            (datetime(2026, 10, 4, 12, tzinfo=datetime_timezone.utc), "Good evening, Asha"),
            (datetime(2026, 10, 4, 16, tzinfo=datetime_timezone.utc), "Good night, Asha"),
        )

        for current_time, expected_greeting in local_times:
            with self.subTest(expected_greeting=expected_greeting):
                with patch("myapp.views.timezone.now", return_value=current_time):
                    response = self.client.get(reverse("dashboard"))
                self.assertContains(response, expected_greeting)
                self.assertContains(response, "One clear step at a time")

    def test_dashboard_uses_friendly_fallback_name_for_anonymous_student(self):
        current_time = datetime(2026, 10, 4, 12, tzinfo=datetime_timezone.utc)
        with patch("myapp.views.timezone.now", return_value=current_time):
            response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Good evening, there")

    def test_dashboard_shows_deadline_countdown_without_student_completion_claim(self):
        DocumentDate.objects.create(
            document=self.document,
            description="Semester registration",
            date_text="15 October 2026",
            normalized_date=date(2026, 10, 15),
        )
        DocumentDate.objects.create(
            document=self.document,
            description="Document Date",
            date_text="21 September 2026",
            normalized_date=date(2026, 9, 21),
        )
        utc_time = datetime(2026, 10, 4, 19, tzinfo=datetime_timezone.utc)
        with patch("myapp.services.deadlines.timezone.now", return_value=utc_time):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deadline intelligence")
        self.assertContains(response, "10 days remaining")
        self.assertContains(response, "Upcoming")
        self.assertContains(response, "Project guidelines")
        self.assertNotContains(response, "13 days overdue")
        self.assertNotContains(response, "You haven't completed")

    def test_document_library_links_to_detail(self):
        response = self.client.get(reverse("document-list-page"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "myapp/document_list.html")
        self.assertContains(response, "Project guidelines")
        self.assertContains(
            response,
            reverse("document-detail-page", args=(self.document.id,)),
        )

    def test_document_detail_shows_summary_dates_actions_and_sections(self):
        response = self.client.get(
            reverse("document-detail-page", args=(self.document.id,))
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "myapp/document_detail.html")
        self.assertContains(response, "Project and evaluation guidelines.")
        self.assertContains(response, "6th week of the semester")
        self.assertNotContains(response, "DATE TEXT ONLY")
        self.assertNotContains(response, "date-unconfirmed")
        self.assertContains(response, "Form a group of no more than 4 students.")
        self.assertContains(response, "SEE Guidelines")

    def test_document_detail_returns_404_for_unknown_document(self):
        response = self.client.get(reverse("document-detail-page", args=(999,)))

        self.assertEqual(response.status_code, 404)

    def test_section_only_document_hides_empty_panels_and_formats_delimited_info(self):
        self.document.delete()
        document = CampusDocument.objects.create(
            source_file="Semester_Scheme.pdf",
            document_type="Course Curriculum Scheme",
            title="I Semester Course Scheme and Credits",
            summary="First-semester courses, credits, and teaching departments.",
        )
        DocumentSection.objects.create(
            document=document,
            heading="I Semester Course Scheme",
            content=(
                "1BMATS101: CALCULUS AND NUMERICAL ANALYSIS (04 Credits, Teaching Dept: MATHS)"
                " | 1BEIT105: PROGRAMMING IN C (03 Credits, Teaching Dept: CS)"
                " | Total Credits: 20"
            ),
        )

        response = self.client.get(
            reverse("document-detail-page", args=(document.id,))
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "detail-grid-sections-only")
        self.assertContains(response, "I Semester Course Scheme")
        self.assertContains(response, "1BMATS101: CALCULUS AND NUMERICAL ANALYSIS")
        self.assertContains(response, "1BEIT105: PROGRAMMING IN C")
        self.assertContains(response, "Total Credits: 20")
        self.assertNotContains(response, "Important dates")
        self.assertNotContains(response, "Action items")

    def test_dashboard_hides_date_and_action_panels_when_no_items_exist(self):
        self.document.dates.all().delete()
        self.document.action_items.all().delete()

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent notices")
        self.assertNotContains(response, "Important dates")
        self.assertNotContains(response, "Pending actions")
        self.assertNotContains(response, "ACTIONS TO REVIEW")

    def test_dashboard_places_view_notice_below_relevance_control(self):
        student = get_user_model().objects.create_user(
            username="date-row-student",
            password="CampusPilot-Test-Pass-927!",
        )
        self.client.force_login(student)

        response = self.client.get(reverse("dashboard"))
        date_row = response.content.decode().split('<article class="date-row">', 1)[1].split(
            "</article>",
            1,
        )[0]

        self.assertLess(
            date_row.index("Not relevant to me"),
            date_row.index("View notice"),
        )


# Ensure each student's dismissals and completions stay private and reversible.
class StudentDashboardStateTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.student = user_model.objects.create_user(
            username="student-one",
            password="CampusPilot-Test-Pass-927!",
        )
        self.other_student = user_model.objects.create_user(
            username="student-two",
            password="CampusPilot-Test-Pass-927!",
        )
        self.document = CampusDocument.objects.create(
            source_file="branch_notice.pdf",
            document_type="Academic Notice",
            title="Branch-specific schedule",
            summary="A date and an action for selected branches.",
        )
        self.date = DocumentDate.objects.create(
            document=self.document,
            description="Branch examination",
            date_text="20 October",
        )
        self.action = ActionItem.objects.create(
            document=self.document,
            description="Submit the branch form.",
        )

    def test_completed_action_is_hidden_for_its_student_only_and_can_be_restored(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("complete-action", args=(self.action.id,)),
            {"next": reverse("dashboard")},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(
            StudentActionState.objects.filter(
                user=self.student,
                action_item=self.action,
                is_completed=True,
            ).exists()
        )
        dashboard = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard, "Submit the branch form.")
        self.assertTrue(ActionItem.objects.filter(pk=self.action.pk).exists())

        self.client.force_login(self.other_student)
        other_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(other_dashboard, "Submit the branch form.")

        self.client.force_login(self.student)
        detail = self.client.get(
            reverse("document-detail-page", args=(self.document.id,))
        )
        self.assertContains(detail, "Restore to my dashboard")
        restored = self.client.post(
            reverse("restore-action", args=(self.action.id,)),
            {"next": reverse("document-detail-page", args=(self.document.id,))},
            follow=True,
        )
        self.assertRedirects(
            restored,
            reverse("document-detail-page", args=(self.document.id,)),
        )
        self.assertContains(restored, "Mark complete")
        self.assertFalse(
            StudentActionState.objects.filter(
                user=self.student,
                action_item=self.action,
            ).exists()
        )

    def test_irrelevant_date_is_hidden_for_its_student_only_and_can_be_restored(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("dismiss-date", args=(self.date.id,)),
            {"next": reverse("dashboard")},
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(
            StudentDateState.objects.filter(
                user=self.student,
                document_date=self.date,
                is_relevant=False,
            ).exists()
        )
        dashboard = self.client.get(reverse("dashboard"))
        self.assertNotContains(dashboard, "Branch examination")
        self.assertTrue(DocumentDate.objects.filter(pk=self.date.pk).exists())

        self.client.force_login(self.other_student)
        other_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(other_dashboard, "Branch examination")

        self.client.force_login(self.student)
        detail = self.client.get(
            reverse("document-detail-page", args=(self.document.id,))
        )
        self.assertContains(detail, "Restore to my dashboard")
        restored = self.client.post(
            reverse("restore-date", args=(self.date.id,)),
            {"next": reverse("document-detail-page", args=(self.document.id,))},
            follow=True,
        )
        self.assertRedirects(
            restored,
            reverse("document-detail-page", args=(self.document.id,)),
        )
        self.assertContains(restored, "Not relevant to me")
        self.assertFalse(
            StudentDateState.objects.filter(
                user=self.student,
                document_date=self.date,
            ).exists()
        )

    def test_anonymous_student_must_sign_in_before_updating(self):
        response = self.client.post(reverse("complete-action", args=(self.action.id,)))

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('complete-action', args=(self.action.id,))}",
        )
        self.assertFalse(StudentActionState.objects.exists())

    def test_update_routes_require_post_and_reject_external_redirects(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse("complete-action", args=(self.action.id,)))
        self.assertEqual(response.status_code, 405)

        response = self.client.post(
            reverse("complete-action", args=(self.action.id,)),
            {"next": "https://example.invalid/"},
        )
        self.assertRedirects(
            response,
            reverse("document-detail-page", args=(self.document.id,)),
        )

    def test_signup_creates_signed_in_account(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new-student",
                "password1": "CampusPilot-New-Account-937!",
                "password2": "CampusPilot-New-Account-937!",
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(
            get_user_model().objects.filter(username="new-student").exists()
        )
        self.assertTrue(response.wsgi_request.user.is_authenticated)


class StudentProposedPlanTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.student = user_model.objects.create_user(
            username="plan-student",
            password="CampusPilot-Plan-Password-937!",
        )
        self.other_student = user_model.objects.create_user(
            username="other-plan-student",
            password="CampusPilot-Plan-Password-937!",
        )
        self.document = CampusDocument.objects.create(
            source_file="registration_notice.pdf",
            document_type="Academic Notice",
            title="Registration Notice",
            summary="Registration requirements and deadline.",
        )
        self.deadline = DocumentDate.objects.create(
            document=self.document,
            description="Semester registration deadline",
            date_text="15 October 2026",
            normalized_date=date(2026, 10, 15),
        )
        self.action = ActionItem.objects.create(
            document=self.document,
            description="Complete the semester registration form.",
        )

    def test_dashboard_proposes_extracted_action_linked_to_single_notice_deadline(self):
        utc_time = datetime(2026, 10, 4, 19, tzinfo=datetime_timezone.utc)
        with patch("myapp.services.deadlines.timezone.now", return_value=utc_time):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Proposed plan")
        self.assertContains(response, "Complete the semester registration form.")
        self.assertContains(response, "For Semester registration deadline")
        self.assertContains(response, "no student completion status is tracked")
        plan_item = response.context["proposed_plan"][0]
        self.assertEqual(plan_item["planned_date"], date(2026, 10, 15))
        self.assertTrue(plan_item["is_suggestion"])

    def test_dashboard_does_not_guess_between_multiple_deadlines_in_one_notice(self):
        DocumentDate.objects.create(
            document=self.document,
            description="Scholarship application deadline",
            date_text="20 October 2026",
            normalized_date=date(2026, 10, 20),
            position=1,
        )

        response = self.client.get(reverse("dashboard"))

        plan_item = response.context["proposed_plan"][0]
        self.assertIsNone(plan_item["planned_date"])
        self.assertIsNone(plan_item["document_date"])
        self.assertContains(response, "No clear deadline linked")

    def test_student_can_edit_suggestion_without_changing_shared_action_or_other_student(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("save-action-plan-item", args=(self.action.id,)),
            {
                "description": "Register and double-check the course list.",
                "planned_date": "2026-10-12",
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        plan_item = StudentPlanItem.objects.get(
            user=self.student,
            action_item=self.action,
        )
        self.assertEqual(
            plan_item.description,
            "Register and double-check the course list.",
        )
        self.assertEqual(plan_item.planned_date, date(2026, 10, 12))
        self.assertEqual(plan_item.document_date, self.deadline)
        self.action.refresh_from_db()
        self.assertEqual(
            self.action.description,
            "Complete the semester registration form.",
        )

        response = self.client.post(
            reverse("save-action-plan-item", args=(self.action.id,)),
            {
                "description": "Register and double-check the course list.",
                "planned_date": "",
            },
        )
        self.assertRedirects(response, reverse("dashboard"))
        plan_item.refresh_from_db()
        self.assertIsNone(plan_item.planned_date)
        self.assertIsNone(
            self.client.get(reverse("dashboard")).context["proposed_plan"][0][
                "planned_date"
            ]
        )

        self.client.force_login(self.other_student)
        other_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(
            other_dashboard,
            "Complete the semester registration form.",
        )
        self.assertNotContains(
            other_dashboard,
            "Register and double-check the course list.",
        )

    def test_student_can_dismiss_own_suggestion_without_hiding_shared_action(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("dismiss-action-plan-item", args=(self.action.id,)),
        )

        self.assertRedirects(response, reverse("dashboard"))
        hidden_item = StudentPlanItem.objects.get(
            user=self.student,
            action_item=self.action,
        )
        self.assertFalse(hidden_item.is_active)
        own_dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(own_dashboard.context["proposed_plan"], [])
        self.assertContains(own_dashboard, "Complete the semester registration form.")
        self.assertTrue(ActionItem.objects.filter(pk=self.action.pk).exists())

        self.client.force_login(self.other_student)
        other_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(
            other_dashboard,
            "Complete the semester registration form.",
        )

    def test_student_can_add_edit_and_remove_a_custom_plan_item(self):
        self.client.force_login(self.student)
        response = self.client.post(
            reverse("add-plan-item"),
            {
                "description": "Gather the documents needed for registration.",
                "planned_date": "",
                "document_date": str(self.deadline.id),
            },
        )

        self.assertRedirects(response, reverse("dashboard"))
        plan_item = StudentPlanItem.objects.get(
            user=self.student,
            action_item__isnull=True,
        )
        self.assertEqual(plan_item.document_date, self.deadline)
        self.assertEqual(plan_item.planned_date, self.deadline.normalized_date)

        response = self.client.post(
            reverse("save-plan-item", args=(plan_item.id,)),
            {
                "description": "Gather and scan registration documents.",
                "planned_date": "2026-10-11",
            },
        )
        self.assertRedirects(response, reverse("dashboard"))
        plan_item.refresh_from_db()
        self.assertEqual(plan_item.description, "Gather and scan registration documents.")
        self.assertEqual(plan_item.planned_date, date(2026, 10, 11))

        response = self.client.post(
            reverse("remove-plan-item", args=(plan_item.id,)),
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(StudentPlanItem.objects.filter(pk=plan_item.id).exists())

    def test_plan_updates_require_authentication_and_post(self):
        response = self.client.post(
            reverse("save-action-plan-item", args=(self.action.id,)),
            {"description": "Private plan", "planned_date": ""},
        )
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('save-action-plan-item', args=(self.action.id,))}",
        )
        self.assertEqual(StudentPlanItem.objects.count(), 0)

        self.client.force_login(self.student)
        response = self.client.get(
            reverse("save-action-plan-item", args=(self.action.id,)),
        )
        self.assertEqual(response.status_code, 405)

    def test_student_cannot_edit_another_students_custom_plan_item(self):
        other_item = StudentPlanItem.objects.create(
            user=self.other_student,
            description="Private other-student plan",
        )
        self.client.force_login(self.student)

        response = self.client.post(
            reverse("save-plan-item", args=(other_item.id,)),
            {"description": "Attempted edit", "planned_date": ""},
        )

        self.assertEqual(response.status_code, 404)
        other_item.refresh_from_db()
        self.assertEqual(other_item.description, "Private other-student plan")


# Verify uploaded announcements reach the dashboard only after successful extraction.
class AnnouncementUploadTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.media_settings = self.settings(MEDIA_ROOT=self.media_directory.name)
        self.media_settings.enable()
        self.addCleanup(self.media_settings.disable)
        self.addCleanup(self.media_directory.cleanup)

    def make_announcement(self, source_file="new_notice.pdf", date_text="20 October"):
        return ExtractedAnnouncement(
            source_file=source_file,
            document_type="Academic Announcement",
            title="Updated exam schedule",
            summary="A new exam date and preparation requirement.",
            dates=[
                ExtractedDate(
                    description="Exam",
                    date=date_text,
                )
            ],
            instructions=["Review the updated syllabus."],
            sections=[
                ExtractedSection(
                    heading="Schedule update",
                    content="The examination is scheduled for 20 October.",
                )
            ],
        )

    def test_pdf_upload_creates_dashboard_ready_document(self):
        uploaded_file = SimpleUploadedFile(
            "new_notice.pdf",
            b"%PDF-1.7 fake announcement bytes",
            content_type="application/pdf",
        )
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            return_value=self.make_announcement(),
        ):
            response = self.client.post(
                reverse("announcement-upload"),
                {"original_file": uploaded_file},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertRedirects(response, reverse("dashboard"))
        self.assertContains(response, "Updated exam schedule")
        document = CampusDocument.objects.get(title="Updated exam schedule")
        self.assertEqual(document.source_file, "new_notice.pdf")
        self.assertEqual(document.dates.get().date_text, "20 October")
        self.assertEqual(document.action_items.get().description, "Review the updated syllabus.")
        self.assertEqual(document.sections.get().heading, "Schedule update")
        upload = DocumentUpload.objects.get()
        self.assertEqual(upload.status, DocumentUpload.Status.PROCESSED)
        self.assertEqual(upload.document, document)

    def test_image_upload_is_accepted_and_processed(self):
        uploaded_file = SimpleUploadedFile(
            "notice.webp",
            b"RIFF\x00\x00\x00\x00WEBP announcement bytes",
            content_type="image/webp",
        )
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            return_value=self.make_announcement("notice.webp"),
        ) as extractor:
            response = self.client.post(
                reverse("announcement-upload"),
                {"original_file": uploaded_file},
            )

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(
            DocumentUpload.objects.get().status,
            DocumentUpload.Status.PROCESSED,
        )

    def test_upload_normalizes_full_calendar_date(self):
        uploaded_file = SimpleUploadedFile(
            "dated_notice.pdf",
            b"%PDF-1.7 fake announcement bytes",
            content_type="application/pdf",
        )
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            return_value=self.make_announcement(date_text="15 October 2026"),
        ):
            self.client.post(
                reverse("announcement-upload"),
                {"original_file": uploaded_file},
            )

        deadline = DocumentDate.objects.get()
        self.assertEqual(deadline.date_text, "15 October 2026")
        self.assertEqual(deadline.normalized_date, date(2026, 10, 15))

    def test_invalid_file_contents_and_oversized_uploads_are_rejected(self):
        invalid_file = SimpleUploadedFile(
            "not_really_a_pdf.pdf",
            b"plain text pretending to be a PDF",
            content_type="application/pdf",
        )
        response = self.client.post(
            reverse("announcement-upload"),
            {"original_file": invalid_file},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "file contents do not match")
        self.assertEqual(DocumentUpload.objects.count(), 0)

        oversized_file = SimpleUploadedFile(
            "large_notice.pdf",
            b"%PDF-" + b"x" * (10 * 1024 * 1024),
            content_type="application/pdf",
        )
        response = self.client.post(
            reverse("announcement-upload"),
            {"original_file": oversized_file},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10 MB or smaller")
        self.assertEqual(DocumentUpload.objects.count(), 0)

    def test_extraction_failure_is_recorded_and_does_not_create_a_notice(self):
        uploaded_file = SimpleUploadedFile(
            "unclear_notice.pdf",
            b"%PDF-1.7 unreadable scan",
            content_type="application/pdf",
        )
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            side_effect=RuntimeError("service unavailable"),
        ):
            with self.assertLogs("myapp.services.document_extractor", level="ERROR"):
                response = self.client.post(
                    reverse("announcement-upload"),
                    {"original_file": uploaded_file},
                    follow=True,
                )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Something went wrong while processing this upload.")
        self.assertEqual(CampusDocument.objects.count(), 0)
        upload = DocumentUpload.objects.get()
        self.assertEqual(upload.status, DocumentUpload.Status.FAILED)
        self.assertIsNone(upload.document)

    def test_failed_upload_can_be_retried(self):
        uploaded_file = SimpleUploadedFile(
            "retry_notice.pdf",
            b"%PDF-1.7 announcement bytes",
            content_type="application/pdf",
        )
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            side_effect=[
                RuntimeError("temporary failure"),
                self.make_announcement("retry_notice.pdf"),
            ],
        ):
            with self.assertLogs("myapp.services.document_extractor", level="ERROR"):
                self.client.post(
                    reverse("announcement-upload"),
                    {"original_file": uploaded_file},
                )
            upload = DocumentUpload.objects.get()
            response = self.client.post(
                reverse("retry-announcement-upload", args=(upload.id,)),
                follow=True,
            )

        self.assertRedirects(response, reverse("dashboard"))
        upload.refresh_from_db()
        self.assertEqual(upload.status, DocumentUpload.Status.PROCESSED)
        self.assertEqual(CampusDocument.objects.count(), 1)

    def test_gemini_api_failure_has_service_message_and_preserves_retry(self):
        uploaded_file = SimpleUploadedFile(
            "service_notice.pdf",
            b"%PDF-1.7 announcement bytes",
            content_type="application/pdf",
        )
        api_error = APIError(code=503, response_json={"message": "provider unavailable"})
        with patch(
            "myapp.services.document_extractor.extract_announcement",
            side_effect=api_error,
        ):
            with self.assertLogs("myapp.services.document_extractor", level="ERROR"):
                response = self.client.post(
                    reverse("announcement-upload"),
                    {"original_file": uploaded_file},
                    follow=True,
                )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "announcement service couldn&#x27;t process this file")
        self.assertContains(response, "Your upload is saved; please try again shortly.")
        self.assertNotContains(response, "provider unavailable")
        upload = DocumentUpload.objects.get()
        self.assertEqual(upload.status, DocumentUpload.Status.FAILED)
        self.assertIsNone(upload.document)

    def test_legacy_failure_message_prompts_retry_without_blaming_file_quality(self):
        upload = DocumentUpload.objects.create(
            original_file=SimpleUploadedFile(
                "older_notice.png",
                b"\x89PNG\r\n\x1a\nold image",
                content_type="image/png",
            ),
            status=DocumentUpload.Status.FAILED,
            error_message=(
                "We couldn't read this announcement. Try a clearer supported PDF or image."
            ),
        )

        response = self.client.get(reverse("announcement-upload"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Retry to check it with updated error reporting.",
        )
        self.assertNotContains(response, "Try a clearer supported PDF or image.")
        self.assertContains(
            response,
            reverse("retry-announcement-upload", args=(upload.id,)),
        )


# Verify the installed Gemini SDK receives images and PDFs with their actual MIME types.
class AnnouncementExtractorTests(TestCase):
    def make_response(self):
        announcement = ExtractedAnnouncement(
            source_file="notice.pdf",
            document_type="Academic Announcement",
            title="Updated exam schedule",
            summary="A revised exam date.",
            dates=[],
            instructions=[],
            sections=[],
        )
        return announcement.model_dump_json()

    def test_pdf_and_image_parts_use_matching_mime_types(self):
        samples = (
            ("notice.pdf", b"%PDF-1.7 sample", "application/pdf"),
            ("notice.png", b"\x89PNG\r\n\x1a\nsample", "image/png"),
            ("notice.webp", b"RIFFxxxxWEBPsample", "image/webp"),
        )
        for filename, content, expected_mime in samples:
            with self.subTest(filename=filename):
                with patch(
                    "myapp.services.document_extractor.genai.Client"
                ) as mock_client, patch.dict(
                    "os.environ", {"GEMINI_API_KEY": "test-key"}
                ):
                    client = mock_client.return_value
                    client.__enter__.return_value = client
                    client.models.generate_content.return_value.text = (
                        self.make_response()
                    )
                    result = extract_announcement(BytesIO(content), filename)

                request = client.models.generate_content.call_args
                self.assertEqual(
                    request.kwargs["contents"][0].inline_data.mime_type,
                    expected_mime,
                )
                self.assertEqual(result.source_file, filename)
                client.__enter__.assert_called_once_with()
                client.__exit__.assert_called_once()

    def test_missing_api_key_reports_configuration_error(self):
        with patch("myapp.services.document_extractor.os.getenv", return_value=None):
            with self.assertRaisesRegex(
                DocumentExtractionError,
                "Announcement processing is unavailable",
            ):
                extract_announcement(BytesIO(b"%PDF-1.7 sample"), "notice.pdf")

    def test_invalid_model_json_is_reported_as_unusable_extraction_data(self):
        with patch(
            "myapp.services.document_extractor.genai.Client"
        ) as mock_client, patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            client = mock_client.return_value
            client.__enter__.return_value = client
            client.models.generate_content.return_value.text = "{invalid json"

            with self.assertRaisesRegex(
                DocumentExtractionError,
                "couldn't organize the details",
            ):
                extract_announcement(BytesIO(b"%PDF-1.7 sample"), "notice.pdf")
