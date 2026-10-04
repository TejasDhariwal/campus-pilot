from django.conf import settings
from django.db import models


# Store extracted notice content separately from each student's personal plan.
class CampusDocument(models.Model):
    source_file = models.CharField(max_length=255)
    document_type = models.CharField(max_length=120)
    title = models.CharField(max_length=255)
    summary = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "id")

    def __str__(self):
        return self.title


# Track uploaded announcement files separately so extraction failures never create empty notices.
class DocumentUpload(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    original_file = models.FileField(upload_to="announcements/%Y/%m/")
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.UPLOADED,
    )
    error_message = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    document = models.ForeignKey(
        CampusDocument,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploads",
    )

    class Meta:
        ordering = ("-uploaded_at", "-id")

    def __str__(self):
        return self.original_file.name


class DocumentDate(models.Model):
    document = models.ForeignKey(
        CampusDocument,
        on_delete=models.CASCADE,
        related_name="dates",
    )
    description = models.CharField(max_length=255)
    date_text = models.CharField(max_length=120, blank=True)
    normalized_date = models.DateField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return self.description


class ActionItem(models.Model):
    document = models.ForeignKey(
        CampusDocument,
        on_delete=models.CASCADE,
        related_name="action_items",
    )
    description = models.TextField()
    due_date = models.DateField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return self.description


# Keep completion private to one account while the extracted action stays shared.
class StudentActionState(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="campus_action_states",
    )
    action_item = models.ForeignKey(
        ActionItem,
        on_delete=models.CASCADE,
        related_name="student_states",
    )
    is_completed = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "action_item"),
                name="unique_student_action_state",
            )
        ]


# Store branch relevance per student without removing an extracted date for others.
class StudentDateState(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="campus_date_states",
    )
    document_date = models.ForeignKey(
        DocumentDate,
        on_delete=models.CASCADE,
        related_name="student_states",
    )
    is_relevant = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "document_date"),
                name="unique_student_date_state",
            )
        ]


# Store each student's editable plan separately from shared extracted requirements.
class StudentPlanItem(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="campus_plan_items",
    )
    action_item = models.ForeignKey(
        ActionItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="student_plan_items",
    )
    document_date = models.ForeignKey(
        DocumentDate,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="student_plan_items",
    )
    description = models.TextField(blank=True)
    planned_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("planned_date", "created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "action_item"),
                condition=models.Q(action_item__isnull=False),
                name="unique_student_plan_action",
            )
        ]

    def __str__(self):
        return self.description or "Hidden suggested plan item"


class DocumentSection(models.Model):
    document = models.ForeignKey(
        CampusDocument,
        on_delete=models.CASCADE,
        related_name="sections",
    )
    heading = models.CharField(max_length=255)
    content = models.TextField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("position", "id")

    def __str__(self):
        return self.heading
