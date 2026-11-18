from django.urls import path

from . import views

urlpatterns = [
    path("loans/book/", views.LoanBookView.as_view(), name="loan-book"),
    path("loans/parties/", views.LoanPartyCreateView.as_view(),
         name="loan-party-create"),
    path("loans/entries/", views.LoanEntryCreateView.as_view(),
         name="loan-entry-create"),
    path("loans/entries/<uuid:entry_id>/reverse/",
         views.LoanEntryReverseView.as_view(), name="loan-entry-reverse"),
]
