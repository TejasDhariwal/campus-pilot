from pathlib import Path

from django import forms

from .models import DocumentDate, DocumentUpload


MAX_ANNOUNCEMENT_SIZE = 10 * 1024 * 1024
SUPPORTED_SIGNATURES = {
    ".pdf": lambda header: header.startswith(b"%PDF-"),
    ".jpg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".png": lambda header: header.startswith(b"\x89PNG\r\n\x1a\n"),
    ".webp": lambda header: header.startswith(b"RIFF") and header[8:12] == b"WEBP",
}


# Accept common announcement formats while rejecting mislabeled or oversized uploads.
class AnnouncementUploadForm(forms.ModelForm):
    class Meta:
        model = DocumentUpload
        fields = ("original_file",)
        widgets = {
            "original_file": forms.ClearableFileInput(
                attrs={
                    "accept": ".pdf,.jpg,.jpeg,.png,.webp,application/pdf,image/jpeg,image/png,image/webp",
                }
            )
        }

    def clean_original_file(self):
        uploaded_file = self.cleaned_data["original_file"]
        if uploaded_file.size > MAX_ANNOUNCEMENT_SIZE:
            raise forms.ValidationError("Choose a file that is 10 MB or smaller.")

        extension = Path(uploaded_file.name).suffix.lower()
        signature_check = SUPPORTED_SIGNATURES.get(extension)
        if signature_check is None:
            raise forms.ValidationError(
                "Upload a PDF, JPEG, PNG, or WEBP announcement."
            )

        header = uploaded_file.read(12)
        uploaded_file.seek(0)
        if not signature_check(header):
            raise forms.ValidationError(
                "The file contents do not match its extension. Choose a valid PDF or image."
            )
        return uploaded_file


class PlanItemForm(forms.Form):
    description = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    planned_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class DocumentDateChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.description} — {obj.document.title}"


class PlanItemCreateForm(PlanItemForm):
    document_date = DocumentDateChoiceField(
        queryset=DocumentDate.objects.none(),
        required=False,
        empty_label="No specific deadline",
    )

    def __init__(self, *args, deadlines=(), **kwargs):
        super().__init__(*args, **kwargs)
        date_ids = [item["date"].id for item in deadlines]
        self.fields["document_date"].queryset = DocumentDate.objects.filter(
            id__in=date_ids,
        ).select_related("document")
