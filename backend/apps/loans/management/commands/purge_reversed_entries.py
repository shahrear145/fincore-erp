"""Permanently erase every REVERSED loan / investment entry.

History entries the shop deleted used to stay in the book with a
"বাতিলকৃত / Reversed" badge on them. They are gone from the page now, but
the rows — and their (net-to-zero) cash transactions and ledger postings —
were still left in the database.

This command hard-deletes ALL reversed entries in both books: the entry
row, its cash transaction and both sides of the ledger posting (the
original and its reversal mirror), so the books stay exactly in balance.

Usage on the server:

    python manage.py purge_reversed_entries             # dry run: show count
    python manage.py purge_reversed_entries --force     # actually erase
"""
from types import SimpleNamespace

from django.core.management.base import BaseCommand

from apps.loans.models import LoanEntry
from apps.loans.service import hard_delete_entry


class Command(BaseCommand):
    help = "Permanently erase all REVERSED loan/investment entries (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Actually erase them (without it, only counts are shown).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        rows = list(
            LoanEntry.objects.filter(status=LoanEntry.Status.REVERSED)
            .select_related("party")
            .order_by("business_id", "transaction_date")
        )
        if not rows:
            self.stdout.write(self.style.SUCCESS(
                "No reversed entries left — nothing to purge."))
            return

        self.stdout.write(f"Reversed entries found: {len(rows)}")
        for e in rows:
            self.stdout.write(
                f"  {e.id} | {e.party.name} | {e.kind} | {e.amount} "
                f"| {e.transaction_date}")
        if not force:
            self.stdout.write(self.style.WARNING(
                "\nDry run only. Nothing was deleted. "
                "Re-run with --force to erase them for good."))
            return

        # one shared actor whose user is the entry's creator (audit trail)
        erased = 0
        for entry in rows:
            try:
                actor = SimpleNamespace(user=entry.created_by,
                                        business=entry.business,
                                        business_membership=None)
                hard_delete_entry(actor, entry,
                                  reason="Purged reversed entry")
                erased += 1
            except Exception as exc:  # keep going, report at the end
                self.stdout.write(self.style.ERROR(
                    f"  FAILED {entry.id}: {exc}"))

        self.stdout.write(self.style.SUCCESS(
            f"\nErased {erased}/{len(rows)} reversed entries (ledger stays balanced)."))