from django.urls import path

from . import views

app_name = "drivesync"

urlpatterns = [
    path("", views.status_view, name="status"),
    path("connect/", views.connect_view, name="connect"),
    path("callback/", views.callback_view, name="callback"),
    path("sync/", views.sync_view, name="sync"),
    path("inspect/", views.inspect_view, name="inspect"),
    path("restore/", views.restore_view, name="restore"),
    path("download/", views.download_backup_view, name="download"),
    path("upload-restore/", views.upload_restore_view, name="upload_restore"),
    path("auto-backup/toggle/", views.auto_backup_toggle_view,
         name="auto_backup_toggle"),
    path("disconnect/", views.disconnect_view, name="disconnect"),
]
