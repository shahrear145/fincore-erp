from django.urls import path

from . import views

urlpatterns = [
    path("assistant/ask/", views.AskView.as_view()),
    path("assistant/history/", views.AskHistoryView.as_view()),
]
