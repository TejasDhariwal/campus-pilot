import logging
import os
from pathlib import Path

from django.db import transaction
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from google.genai import types
from pydantic import BaseModel, ValidationError

from myapp.models import ActionItem, CampusDocument, DocumentDate, DocumentSection, DocumentUpload
from myapp.services.deadlines import normalize_deadline_date

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Keep PDF and image extraction aligned with the structured notice data used by the dashboard.
EXTRACTION_PROMPT = """
Read this college announcement carefully and extract information that students can act on.
Set source_file to "{filename}". Return the document title, document type, a concise summary,
all dates using the original wording when no exact calendar date is provided, student
requirements as instructions, and useful sections with their complete relevant content. Do
not invent deadlines or details. Keep separate course, credit, and department entries
distinct in section content, separated by " | " when a table or list can be represented that
way. Include an empty list when no dates, instructions, or sections are present.
"""


class ExtractedDate(BaseModel):
    description: str
    date: str | None = None


class ExtractedSection(BaseModel):
    heading: str
    content: str


class ExtractedAnnouncement(BaseModel):
    source_file: str
    document_type: str
    title: str
    summary: str
    dates: list[ExtractedDate]
    instructions: list[str]
    sections: list[ExtractedSection]


class DocumentExtractionError(Exception):
    """Raised when announcement extraction cannot produce usable structured data."""


class ExtractionConfigurationError(DocumentExtractionError):
    """Raised when the extraction service has not been configured."""


class ExtractionResponseError(DocumentExtractionError):
    """Raised when the model returns no usable announcement data."""


def extract_announcement(uploaded_file, filename):
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ExtractionConfigurationError(
            "Announcement processing is unavailable right now. Please try again later."
        )

    mime_type = SUPPORTED_MIME_TYPES.get(Path(filename).suffix.lower())
    if mime_type is None:
        raise DocumentExtractionError("The uploaded announcement format is unsupported.")

    uploaded_file.seek(0)
    file_part = types.Part.from_bytes(
        data=uploaded_file.read(),
        mime_type=mime_type,
    )
    # Keep the SDK client open for the request and always release its HTTP resources afterward.
    with genai.Client(api_key=api_key) as client:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[file_part, EXTRACTION_PROMPT.format(filename=Path(filename).name)],
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractedAnnouncement,
            },
        )
    if not response.text:
        raise ExtractionResponseError(
            "We received the file, but couldn't find readable announcement details. "
            "Try a clearer PDF or image."
        )

    try:
        announcement = ExtractedAnnouncement.model_validate_json(response.text)
    except ValidationError as error:
        raise ExtractionResponseError(
            "We couldn't organize the details in this announcement. "
            "Try a clearer or more complete file."
        ) from error
    announcement.source_file = Path(filename).name
    return announcement


def _mark_upload_failed(upload, error_message):
    # Store a safe, actionable message while keeping provider details only in server logs.
    upload.document = None
    upload.status = DocumentUpload.Status.FAILED
    upload.error_message = error_message
    upload.save(update_fields=("document", "status", "error_message"))


def process_document_upload(upload):
    upload.status = DocumentUpload.Status.PROCESSING
    upload.error_message = ""
    upload.save(update_fields=("status", "error_message"))

    try:
        with upload.original_file.open("rb") as source:
            announcement = extract_announcement(source, upload.original_file.name)

        with transaction.atomic():
            document = CampusDocument.objects.create(
                source_file=Path(upload.original_file.name).name,
                document_type=announcement.document_type,
                title=announcement.title,
                summary=announcement.summary,
            )
            DocumentDate.objects.bulk_create(
                [
                    DocumentDate(
                        document=document,
                        description=date.description,
                        date_text=date.date or "",
                        normalized_date=normalize_deadline_date(date.date or ""),
                        position=index,
                    )
                    for index, date in enumerate(announcement.dates)
                ]
            )
            ActionItem.objects.bulk_create(
                [
                    ActionItem(
                        document=document,
                        description=instruction,
                        position=index,
                    )
                    for index, instruction in enumerate(announcement.instructions)
                ]
            )
            DocumentSection.objects.bulk_create(
                [
                    DocumentSection(
                        document=document,
                        heading=section.heading,
                        content=section.content,
                        position=index,
                    )
                    for index, section in enumerate(announcement.sections)
                ]
            )
            upload.document = document
            upload.status = DocumentUpload.Status.PROCESSED
            upload.save(update_fields=("document", "status", "error_message"))
        return True
    except ExtractionConfigurationError as error:
        logger.exception("Announcement extraction is not configured for upload %s", upload.pk)
        _mark_upload_failed(upload, str(error))
        return False
    except ExtractionResponseError as error:
        logger.exception("Gemini returned unusable data for upload %s", upload.pk)
        _mark_upload_failed(upload, str(error))
        return False
    except APIError:
        logger.exception(
            "Gemini API request failed for upload %s: %s",
            upload.pk,
            error,
        )
        _mark_upload_failed(
            upload,
            "The announcement service couldn't process this file right now. "
            "Your upload is saved; please try again shortly.",
        )
        return False
    except DocumentExtractionError:
        logger.exception("Announcement extraction failed for upload %s", upload.pk)
        _mark_upload_failed(
            upload,
            "This file format couldn't be processed. Upload a supported PDF or image.",
        )
        return False
    except Exception:
        logger.exception("Unexpected announcement processing error for upload %s", upload.pk)
        _mark_upload_failed(
            upload,
            "Something went wrong while processing this upload. "
            "Your file is saved; please try again later.",
        )
        return False
