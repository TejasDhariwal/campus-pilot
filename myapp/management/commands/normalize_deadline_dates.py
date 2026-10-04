from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.models import DocumentDate
from myapp.services.deadlines import normalize_deadline_date


class Command(BaseCommand):
    help = "Normalize existing extracted date text when it contains a full calendar date."

    def handle(self, *args, **options):
        updated_count = 0
        with transaction.atomic():
            for document_date in DocumentDate.objects.filter(
                normalized_date__isnull=True,
            ).iterator():
                normalized_date = normalize_deadline_date(document_date.date_text)
                if normalized_date is None:
                    continue
                document_date.normalized_date = normalized_date
                document_date.save(update_fields=("normalized_date",))
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Normalized {updated_count} existing document date(s)."
            )
        )
