from datetime import date as calendar_date

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.http import JsonResponse
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.http import require_http_methods

from .forms import AnnouncementUploadForm, PlanItemCreateForm, PlanItemForm
from .models import (
    ActionItem,
    CampusDocument,
    DocumentDate,
    DocumentUpload,
    StudentActionState,
    StudentDateState,
    StudentPlanItem,
)
from .services.deadlines import CAMPUS_TIME_ZONE, deadline_intelligence, is_notice_date
from .services.document_extractor import process_document_upload
from .services.plan_builder import build_proposed_plan


# Create an account so task and date choices can be stored per student.
@require_http_methods(["GET", "POST"])
def sign_up(request):
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Your CampusPilot account is ready.")
        return redirect("dashboard")
    return render(request, "myapp/signup.html", {"form": form})


# Return students to the page they updated, but never redirect to an external URL.
def _personal_update_redirect(request, document_id):
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("document-detail-page", document_id=document_id)


# Mark a shared extracted task complete for the signed-in student only.
@login_required
@require_POST
def complete_action(request, action_item_id):
    action = get_object_or_404(ActionItem.objects.select_related("document"), pk=action_item_id)
    StudentActionState.objects.update_or_create(
        user=request.user,
        action_item=action,
        defaults={"is_completed": True},
    )
    messages.success(request, "Marked as complete for you.")
    return _personal_update_redirect(request, action.document_id)


# Restore a completed task to the student's pending dashboard list.
@login_required
@require_POST
def restore_action(request, action_item_id):
    action = get_object_or_404(ActionItem.objects.select_related("document"), pk=action_item_id)
    StudentActionState.objects.filter(
        user=request.user,
        action_item=action,
    ).delete()
    messages.success(request, "Task restored to your dashboard.")
    return _personal_update_redirect(request, action.document_id)


# Hide a branch-irrelevant date only from this student's dashboard.
@login_required
@require_POST
def dismiss_date(request, date_id):
    document_date = get_object_or_404(
        DocumentDate.objects.select_related("document"),
        pk=date_id,
    )
    StudentDateState.objects.update_or_create(
        user=request.user,
        document_date=document_date,
        defaults={"is_relevant": False},
    )
    messages.success(request, "Date hidden from your dashboard.")
    return _personal_update_redirect(request, document_date.document_id)


# Restore a previously hidden date to the student's dashboard.
@login_required
@require_POST
def restore_date(request, date_id):
    document_date = get_object_or_404(
        DocumentDate.objects.select_related("document"),
        pk=date_id,
    )
    StudentDateState.objects.filter(
        user=request.user,
        document_date=document_date,
    ).delete()
    messages.success(request, "Date restored to your dashboard.")
    return _personal_update_redirect(request, document_date.document_id)


def _eligible_plan_deadlines(user):
    dates = DocumentDate.objects.filter(
        normalized_date__isnull=False,
    ).select_related("document")
    if user.is_authenticated:
        hidden_date_ids = StudentDateState.objects.filter(
            user=user,
            is_relevant=False,
        ).values("document_date_id")
        dates = dates.exclude(id__in=hidden_date_ids)

    return [
        {"document": item.document, "date": item}
        for item in dates.order_by("normalized_date", "document__title", "position")
        if not is_notice_date(item.description)
    ]


def _action_plan_deadline(action, deadlines):
    document_dates = [
        item["date"]
        for item in deadlines
        if item["document"].id == action.document_id
    ]
    if action.due_date:
        return next(
            (
                item
                for item in document_dates
                if item.normalized_date == action.due_date
            ),
            None,
        )
    if len(document_dates) == 1:
        return document_dates[0]
    return None


def _show_plan_form_errors(request, form):
    errors = "; ".join(
        str(error)
        for field_errors in form.errors.values()
        for error in field_errors
    )
    messages.error(request, f"Plan not saved: {errors}")


def _dashboard_greeting(user, current_time=None):
    local_time = timezone.localtime(
        current_time or timezone.now(),
        timezone=CAMPUS_TIME_ZONE,
    )
    if local_time.hour < 12:
        greeting = "Good morning"
    elif local_time.hour < 17:
        greeting = "Good afternoon"
    elif local_time.hour < 21:
        greeting = "Good evening"
    else:
        greeting = "Good night"

    if user.is_authenticated:
        student_name = user.get_full_name().strip() or user.get_username()
    else:
        student_name = "there"
    return greeting, student_name


@login_required
@require_POST
def save_action_plan_item(request, action_item_id):
    action = get_object_or_404(
        ActionItem.objects.select_related("document"),
        pk=action_item_id,
    )
    form = PlanItemForm(request.POST)
    if not form.is_valid():
        _show_plan_form_errors(request, form)
        return redirect("dashboard")

    matching_date = _action_plan_deadline(action, _eligible_plan_deadlines(request.user))
    StudentPlanItem.objects.update_or_create(
        user=request.user,
        action_item=action,
        defaults={
            "document_date": matching_date,
            "description": form.cleaned_data["description"],
            "planned_date": form.cleaned_data["planned_date"],
            "is_active": True,
        },
    )
    messages.success(request, "Your plan item was saved privately.")
    return redirect("dashboard")


@login_required
@require_POST
def save_plan_item(request, plan_item_id):
    plan_item = get_object_or_404(
        StudentPlanItem,
        pk=plan_item_id,
        user=request.user,
        action_item__isnull=True,
    )
    form = PlanItemForm(request.POST)
    if not form.is_valid():
        _show_plan_form_errors(request, form)
        return redirect("dashboard")

    plan_item.description = form.cleaned_data["description"]
    plan_item.planned_date = form.cleaned_data["planned_date"]
    plan_item.save(update_fields=("description", "planned_date", "updated_at"))
    messages.success(request, "Your plan item was updated.")
    return redirect("dashboard")


@login_required
@require_POST
def dismiss_action_plan_item(request, action_item_id):
    action = get_object_or_404(ActionItem, pk=action_item_id)
    StudentPlanItem.objects.update_or_create(
        user=request.user,
        action_item=action,
        defaults={"is_active": False},
    )
    messages.success(request, "This suggestion was removed from your plan.")
    return redirect("dashboard")


@login_required
@require_POST
def add_plan_item(request):
    deadlines = _eligible_plan_deadlines(request.user)
    form = PlanItemCreateForm(request.POST, deadlines=deadlines)
    if not form.is_valid():
        _show_plan_form_errors(request, form)
        return redirect("dashboard")

    document_date = form.cleaned_data["document_date"]
    planned_date = form.cleaned_data["planned_date"]
    if planned_date is None and document_date:
        planned_date = document_date.normalized_date
    StudentPlanItem.objects.create(
        user=request.user,
        description=form.cleaned_data["description"],
        planned_date=planned_date,
        document_date=document_date,
    )
    messages.success(request, "A plan item was added to your private plan.")
    return redirect("dashboard")


@login_required
@require_POST
def remove_plan_item(request, plan_item_id):
    plan_item = get_object_or_404(
        StudentPlanItem,
        pk=plan_item_id,
        user=request.user,
        action_item__isnull=True,
    )
    plan_item.delete()
    messages.success(request, "The plan item was removed.")
    return redirect("dashboard")


# Accept new announcements and turn successful extractions into dashboard documents.
@require_http_methods(["GET", "POST"])
def announcement_upload_page(request):
    if request.method == "POST":
        form = AnnouncementUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = form.save()
            if process_document_upload(upload):
                messages.success(
                    request,
                    f"Added “{upload.document.title}” to your dashboard.",
                )
                return redirect("dashboard")
            messages.error(request, upload.error_message)
            return redirect("announcement-upload")
    else:
        form = AnnouncementUploadForm()

    failed_uploads = DocumentUpload.objects.filter(
        status=DocumentUpload.Status.FAILED
    )[:5]
    return render(
        request,
        "myapp/upload_announcement.html",
        {"form": form, "failed_uploads": failed_uploads},
    )


# Allow a failed announcement to be processed again without asking for another copy.
@require_POST
def retry_announcement_upload(request, upload_id):
    upload = get_object_or_404(DocumentUpload, pk=upload_id)
    if upload.status != DocumentUpload.Status.FAILED:
        messages.error(request, "Only failed uploads can be retried.")
        return redirect("announcement-upload")

    if process_document_upload(upload):
        messages.success(
            request,
            f"Added “{upload.document.title}” to your dashboard.",
        )
        return redirect("dashboard")

    messages.error(request, upload.error_message)
    return redirect("announcement-upload")


# Build the dashboard from extracted notice data without implying student-tracked status.
def dashboard_page(request):
    greeting, student_name = _dashboard_greeting(request.user)
    documents = list(
        CampusDocument.objects.prefetch_related("dates", "action_items").order_by(
            "-created_at",
            "-id",
        )
    )
    all_dates = [
        {"document": document, "date": date}
        for document in documents
        for date in document.dates.all()
    ]
    all_dates.sort(
        key=lambda item: (
            item["date"].normalized_date is None,
            item["date"].normalized_date or calendar_date.max,
            item["document"].title,
        )
    )
    action_items = [
        {"document": document, "action": action}
        for document in documents
        for action in document.action_items.all()
    ]
    completed_action_ids = set()
    irrelevant_date_ids = set()
    if request.user.is_authenticated:
        action_ids = [item["action"].id for item in action_items]
        date_ids = [item["date"].id for item in all_dates]
        completed_action_ids = set(
            StudentActionState.objects.filter(
                user=request.user,
                is_completed=True,
                action_item_id__in=action_ids,
            ).values_list("action_item_id", flat=True)
        )
        irrelevant_date_ids = set(
            StudentDateState.objects.filter(
                user=request.user,
                is_relevant=False,
                document_date_id__in=date_ids,
            ).values_list("document_date_id", flat=True)
        )
        action_items = [
            item for item in action_items if item["action"].id not in completed_action_ids
        ]
        all_dates = [
            item for item in all_dates if item["date"].id not in irrelevant_date_ids
        ]
    deadlines = []
    important_dates = []
    for item in all_dates:
        normalized_date = item["date"].normalized_date
        if normalized_date and not is_notice_date(item["date"].description):
            timing = deadline_intelligence(normalized_date)
            deadlines.append(
                {
                    **item,
                    **timing,
                    "days_overdue": abs(timing["days_remaining"]),
                }
            )
        elif not normalized_date:
            important_dates.append(item)
    deadlines.sort(
        key=lambda item: (
            item["date"].normalized_date,
            item["document"].title,
            item["date"].position,
        )
    )
    deadlines_by_id = {item["date"].id: item for item in deadlines}
    proposed_plan = []
    add_plan_form = PlanItemCreateForm(deadlines=deadlines)
    if request.user.is_authenticated:
        action_plan_items = StudentPlanItem.objects.filter(
            user=request.user,
            action_item_id__in=[item["action"].id for item in action_items],
        ).select_related("document_date")
        saved_items_by_action = {
            item.action_item_id: item for item in action_plan_items
        }
        custom_plan_items = StudentPlanItem.objects.filter(
            user=request.user,
            action_item__isnull=True,
            is_active=True,
        ).select_related("document_date", "document_date__document")
        custom_plan_items = [
            item for item in custom_plan_items
            if not item.document_date_id or item.document_date_id not in irrelevant_date_ids
        ]
    else:
        saved_items_by_action = {}
        custom_plan_items = []

    proposed_plan = build_proposed_plan(
        action_items,
        deadlines,
        saved_items_by_action,
    )
    for plan_item in custom_plan_items:
        document_date = plan_item.document_date
        proposed_plan.append(
            {
                "action": None,
                "document": document_date.document if document_date else None,
                "description": plan_item.description,
                "planned_date": plan_item.planned_date,
                "plan_item": plan_item,
                "document_date": document_date,
                "is_suggestion": False,
            }
        )
    proposed_plan.sort(
        key=lambda item: (
            item["planned_date"] is None,
            item["planned_date"] or calendar_date.max,
            item["document"].title if item["document"] else "",
            item["description"],
        )
    )
    planned_deadline_ids = {
        item["document_date"].id
        for item in proposed_plan
        if item["document_date"]
    }
    for deadline in deadlines:
        deadline["has_plan_item"] = deadline["date"].id in planned_deadline_ids
    action_items.sort(
        key=lambda item: (
            item["action"].due_date is None,
            item["action"].due_date or calendar_date.max,
            item["document"].title,
        )
    )
    return render(
        request,
        "myapp/dashboard.html",
        {
            "greeting": greeting,
            "student_name": student_name,
            "motivational_message": "One clear step at a time—you’ve got this.",
            "important_dates": important_dates,
            "deadlines": deadlines,
            "proposed_plan": proposed_plan,
            "add_plan_form": add_plan_form,
            "important_date_count": len(important_dates) + len(deadlines),
            "action_items": action_items,
            "recent_documents": documents[:3],
            "document_count": len(documents),
            "action_count": len(action_items),
        },
    )


# List notices for quick navigation to their full extracted details.
def document_list_page(request):
    documents = CampusDocument.objects.prefetch_related("dates", "action_items").order_by(
        "-created_at",
        "-id",
    )
    return render(request, "myapp/document_list.html", {"documents": documents})


# Show the full extracted data for one notice, returning a clear 404 for unknown IDs.
def document_detail_page(request, document_id):
    document = get_object_or_404(
        CampusDocument.objects.prefetch_related("dates", "action_items", "sections"),
        pk=document_id,
    )
    sections = []
    for section in document.sections.all():
        items = [item.strip() for item in section.content.split("|") if item.strip()]
        sections.append(
            {
                "section": section,
                "items": (
                    [
                        {
                            "text": item,
                            "is_total": item.lower().startswith("total credits:"),
                        }
                        for item in items
                    ]
                    if len(items) > 1
                    else []
                ),
            }
        )
    action_state_map = {}
    date_state_map = {}
    if request.user.is_authenticated:
        action_state_map = {
            state.action_item_id: state
            for state in StudentActionState.objects.filter(
                user=request.user,
                action_item_id__in=document.action_items.values("id"),
            )
        }
        date_state_map = {
            state.document_date_id: state
            for state in StudentDateState.objects.filter(
                user=request.user,
                document_date_id__in=document.dates.values("id"),
            )
        }
    action_items = [
        {
            "action": action,
            "is_completed": (
                action_state_map[action.id].is_completed
                if action.id in action_state_map
                else False
            ),
        }
        for action in document.action_items.all()
    ]
    dates = [
        {
            "date": document_date,
            "is_relevant": (
                date_state_map[document_date.id].is_relevant
                if document_date.id in date_state_map
                else True
            ),
        }
        for document_date in document.dates.all()
    ]
    return render(
        request,
        "myapp/document_detail.html",
        {
            "document": document,
            "sections": sections,
            "action_items": action_items,
            "dates": dates,
        },
    )


# Serialize extracted notice data into the read-only shape consumed by the website.
def _document_payload(document):
    return {
        "id": document.id,
        "source_file": document.source_file,
        "document_type": document.document_type,
        "title": document.title,
        "summary": document.summary,
        "created_at": document.created_at.isoformat(),
        "dates": [
            {
                "description": item.description,
                "date_text": item.date_text,
                "normalized_date": (
                    item.normalized_date.isoformat() if item.normalized_date else None
                ),
            }
            for item in document.dates.all()
        ],
        "action_items": [
            {
                "description": item.description,
                "due_date": item.due_date.isoformat() if item.due_date else None,
            }
            for item in document.action_items.all()
        ],
        "sections": [
            {"heading": section.heading, "content": section.content}
            for section in document.sections.all()
        ],
    }


@require_GET
def document_list(request):
    documents = CampusDocument.objects.prefetch_related(
        "dates",
        "action_items",
        "sections",
    )
    return JsonResponse({"documents": [_document_payload(item) for item in documents]})


@require_GET
def document_detail(request, document_id):
    document = get_object_or_404(
        CampusDocument.objects.prefetch_related("dates", "action_items", "sections"),
        pk=document_id,
    )
    return JsonResponse(_document_payload(document))


@require_GET
def deadline_list(request):
    deadlines = DocumentDate.objects.filter(
        normalized_date__isnull=False,
    ).select_related("document")
    if request.user.is_authenticated:
        hidden_date_ids = StudentDateState.objects.filter(
            user=request.user,
            is_relevant=False,
        ).values("document_date_id")
        deadlines = deadlines.exclude(id__in=hidden_date_ids)

    payload = []
    for item in deadlines.order_by(
        "normalized_date",
        "document__title",
        "position",
        "id",
    ):
        if is_notice_date(item.description):
            continue
        payload.append(
            {
                "title": item.description,
                "date": item.normalized_date.isoformat(),
                "date_text": item.date_text,
                **deadline_intelligence(item.normalized_date),
                "source_document": {
                    "title": item.document.title,
                    "url": reverse("document-detail-page", args=(item.document_id,)),
                },
            }
        )
    return JsonResponse({"deadlines": payload})
