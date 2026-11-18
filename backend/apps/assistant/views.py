"""Assistant API (Phase 7).

POST /api/assistant/ask/ — one question per call.
- Scope resolution: if the request carries a business context (header /
  query param) and the user is an active member → BUSINESS mode with the
  server-side role scope; no explicit mode flag exists for the client to
  fake — membership + role always come from the server.
- Every answer's figures come from apps.assistant.facts (live queries on
  the user's own authorized rows). Invalid business ids yield 403/400 by
  the same permission class used everywhere else.
"""
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import BusinessAccessRequired
from apps.organizations.views_utils import resolve_business_id
from apps.assistant.models import AssistantQuery
from apps.assistant.engine import answer_question
from apps.assistant.facts import (
    build_business_facts, build_personal_facts,
)


class AskView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        question = (request.data.get("question") or "").strip()
        if not question:
            return Response({"detail": "A question is required."}, status=400)

        business = None
        membership = None
        scope = "PERSONAL"
        facts = build_personal_facts(request.user)
        if resolve_business_id(self, request):
            if not BusinessAccessRequired().has_permission(request, self):
                # an explicit business context the user cannot access must
                # be refused — never silently answered from personal data
                return Response(
                    {"detail": "You have no active access to that business."},
                    status=403)
            business = getattr(request, "business", None)
            membership = getattr(request, "business_membership", None)
            if business is not None:
                scope = "BUSINESS"
                facts = build_business_facts(business, membership)

        result = answer_question(request.user, question, facts=facts,
                                 scope=scope)
        AssistantQuery.objects.create(
            user=request.user, business=business, question=question,
            answer=result["answer"], intent=result.get("intent", ""),
            facts_used=result.get("facts_used", []), scope=scope,
        )
        return Response(result)


class AskHistoryView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        qs = request.user.assistant_queries.all()[:50]
        rows = [{
            "id": str(q.pk), "question": q.question, "answer": q.answer,
            "intent": q.intent, "scope": q.scope,
            "business_id": str(q.business_id) if q.business_id else None,
            "created_at": q.created_at,
        } for q in qs]
        return Response({"count": len(rows), "history": rows})
