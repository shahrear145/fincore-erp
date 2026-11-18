"""Base models shared by every domain app.

Design rules enforced here:
- Every domain table gets a UUID primary key (`uuid`) so offline-mobile and
  server records can be identified globally (sync requirement, Section 19).
- `mono_id` is an auto-incrementing integer for human-friendly references and
  efficient joins/sorting.
- `created_at`/`updated_at` are always UTC instants (USE_TZ).
- Posted financial records are soft-deleted; `deleted_at` is set, never rows
  removed (Section 18).
"""
import uuid

from django.db import models
from django.utils import timezone


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)

    def delete(self):  # soft delete by default
        return self.update(deleted_at=timezone.now())


class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()


class AllObjectsManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteUUIDModel(UUIDModel):
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        """Never truly remove financial records — soft delete only."""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])

    def hard_delete(self):
        super().delete()

    def undelete(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])
