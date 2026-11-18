from __future__ import annotations

from celery import shared_task


@shared_task(bind=True, name="ops.create_backup")
def create_backup_task(self, user_id=None, note="scheduled"):
    from django.contrib.auth import get_user_model
    from apps.ops.backup import create_backup

    user = None
    if user_id:
        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
    record = create_backup(user, note=note)
    return {"backup_id": str(record.pk), "status": record.status,
            "location": record.location}
