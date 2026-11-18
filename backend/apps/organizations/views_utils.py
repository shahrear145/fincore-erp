"""Shared helpers for resolving the active business on a request."""
from django.utils.http import quote  # noqa: F401  (kept for future pinning)


def resolve_business_id(view, request):
    """Business precedence: URL kwarg > X-Active-Business header > ?business= > default."""
    url_id = view.kwargs.get("business_pk") or view.kwargs.get("pk")
    if url_id:
        return url_id
    header_id = request.headers.get("X-Active-Business")
    if header_id:
        return header_id
    query_id = request.query_params.get("business")
    if query_id:
        return query_id
    return None
