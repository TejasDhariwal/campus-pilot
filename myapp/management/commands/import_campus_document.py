import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.models import ActionItem, CampusDocument, DocumentDate, DocumentSection
from myapp.services.deadlines import normalize_deadline_date


# Persist extracted notice JSON so the website can query its dates, actions, and sections.
def _text(value, field_name):
    if not isinstance(value, str):
        raise CommandError(f"Expected '{field_name}' to be a string.")
    return value


def _list(value, field_name):
    if not isinstance(value, list):
        raise CommandError(f"Expected '{field_name}' to be a list.")
    return value


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise CommandError("The JSON root must be an object.")

    document = {
        field: _text(payload.get(field), field)
        for field in ("source_file", "document_type", "title", "summary")
    }

    dates = []
    for index, item in enumerate(_list(payload.get("dates"), "dates")):
        if not isinstance(item, dict):
            raise CommandError(f"Expected 'dates[{index}]' to be an object.")
        date_text = item.get("date")
        if date_text is not None and not isinstance(date_text, str):
            raise CommandError(f"Expected 'dates[{index}].date' to be a string or null.")
        dates.append(
            {
                "description": _text(item.get("description"), f"dates[{index}].description"),
                "date_text": date_text or "",
                "normalized_date": normalize_deadline_date(date_text or ""),
            }
        )

    instructions = [
        _text(item, f"instructions[{index}]")
        for index, item in enumerate(_list(payload.get("instructions"), "instructions"))
    ]

    sections = []
    for index, item in enumerate(_list(payload.get("sections"), "sections")):
        if not isinstance(item, dict):
            raise CommandError(f"Expected 'sections[{index}]' to be an object.")
        sections.append(
            {
                "heading": _text(item.get("heading"), f"sections[{index}].heading"),
                "content": _text(item.get("content"), f"sections[{index}].content"),
            }
        )

    return document, dates, instructions, sections


class Command(BaseCommand):
    help = "Import one extracted CampusPilot JSON document into the database."

    def add_arguments(self, parser):
        parser.add_argument("json_file", type=Path, help="Path to the extracted JSON file.")

    def handle(self, *args, **options):
        json_file = options["json_file"]
        try:
            with json_file.open(encoding="utf-8") as source:
                payload = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommandError(f"Could not read valid JSON from '{json_file}': {error}") from error

        document_data, dates, instructions, sections = _validate_payload(payload)

        with transaction.atomic():
            document, created = CampusDocument.objects.get_or_create(
                source_file=document_data["source_file"],
                title=document_data["title"],
                defaults={
                    "document_type": document_data["document_type"],
                    "summary": document_data["summary"],
                },
            )
            if not created:
                document.document_type = document_data["document_type"]
                document.summary = document_data["summary"]
                document.save(update_fields=("document_type", "summary"))
                document.dates.all().delete()
                document.action_items.all().delete()
                document.sections.all().delete()

            DocumentDate.objects.bulk_create(
                [
                    DocumentDate(document=document, position=index, **item)
                    for index, item in enumerate(dates)
                ]
            )
            ActionItem.objects.bulk_create(
                [
                    ActionItem(document=document, description=instruction, position=index)
                    for index, instruction in enumerate(instructions)
                ]
            )
            DocumentSection.objects.bulk_create(
                [
                    DocumentSection(document=document, position=index, **section)
                    for index, section in enumerate(sections)
                ]
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Saved '{document.title}' with {len(dates)} dates, "
                f"{len(instructions)} action items, and {len(sections)} sections."
            )
        )
