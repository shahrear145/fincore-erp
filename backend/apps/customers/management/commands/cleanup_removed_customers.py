"""Soft-delete customers whose partner books were all closed.

"খাতা থেকে সরান" reverses the partner's history and closes that partner's
book, but older versions left the customer row itself alive.  Such rows show
up as "partner-less customers" in the audit and in customer searches even
though the user already deleted them.

This command is deliberately conservative: it only removes a customer when

- every partner book is closed (no active assignment),
- nothing is owed on any book,
- and no live (non-reversed) transaction still points at them.

By default it only lists what it would do.  Pass ``--apply`` to soft-delete.
The rows are soft-deleted, so the ledger/audit trail is never destroyed.
"""
from uuid import UUID

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.core.middleware import record_audit
from apps.customers.models import Customer
from apps.transactions.models import Transaction, TransactionStatus


class Command(BaseCommand):
    help = ("Soft-delete customers left behind after their last partner book "
            "was closed. Dry-run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--business",
            help="Only this business (UUID or exact name).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually soft-delete the eligible customers.",
        )

    def handle(self, *args, **options):
        customers = Customer.objects.all().select_related("business")
        raw_business = (options.get("business") or "").strip()
        if raw_business:
            try:
                customers = customers.filter(business_id=UUID(raw_business))
            except ValueError:
                customers = customers.filter(
                    Q(business__name__iexact=raw_business))

        eligible, skipped_open, skipped_due, skipped_live, skipped_never = (
            [], 0, 0, 0, 0)
        for customer in customers.order_by("business__name", "name"):
            books = customer.assignments.all()
            if not books.exists():
                skipped_never += 1
                continue
            if books.filter(is_active=True).exists():
                skipped_open += 1
                continue
            due = customer.total_due()
            if due != 0:
                skipped_due += 1
                continue
            if (Transaction.objects.filter(customer=customer)
                    .exclude(status=TransactionStatus.REVERSED).exists()):
                skipped_live += 1
                continue
            eligible.append(customer)

        for customer in eligible:
            self.stdout.write(
                f"  {customer.business.name} · {customer.name} "
                f"({customer.id})")
            if options["apply"]:
                customer.delete()
                try:
                    record_audit(
                        "delete", customer, business=customer.business,
                        reason="cleanup_removed_customers")
                except Exception:
                    pass

        self.stdout.write("")
        self.stdout.write(
            f"eligible: {len(eligible)} | deleted: "
            f"{len(eligible) if options['apply'] else 0} | "
            f"skipped — open book: {skipped_open}, still due: {skipped_due}, "
            f"live transactions: {skipped_live}, never linked: {skipped_never}")
        if eligible and not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "Dry run only — re-run with --apply to soft-delete."))
        elif not eligible:
            self.stdout.write(self.style.SUCCESS(
                "Nothing to clean up."))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Removed customers are soft-deleted; history rows remain."))
