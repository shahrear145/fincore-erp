from django.urls import path

from . import views

urlpatterns = [
    path("sync/push/", views.SyncPushView.as_view()),
    path("sync/pull/", views.SyncPullView.as_view()),
    path("sync/resolve/", views.SyncResolveView.as_view()),
    path("sync/status/", views.SyncStatusView.as_view()),
]
