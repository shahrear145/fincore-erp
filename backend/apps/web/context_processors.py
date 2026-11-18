"""Web context: active business (session), memberships, display helpers, i18n."""
from apps.organizations.models import BusinessMembership

#: Every theme the UI can wear — id, Bangla label and the two swatch
#: colours shown in the theme picker.
THEME_CHOICES = [
    ("aurora", "অরোরা · ডিপ ডার্ক (ইন্ডিগো-সিয়ান)", "#0a0e22", "#6366f1"),
    ("onyx", "অনিক্স · গ্রাফাইট গোল্ড (ডার্ক)", "#0d0c0b", "#f0c04a"),
    ("jade", "জ্যাড · এমারেল্ড নাইট (ডার্ক)", "#060f0c", "#34d399"),
    ("porcelain", "পোর্সেলিন · ইন্ডিগো (হালকা)", "#eef0fd", "#6366f1"),
    ("linen", "লিনেন · স্যান্ড টিলা (হালকা)", "#faf8f3", "#0f766e"),
    ("neon", "নিয়ন · ডার্ক নিয়ন (ডার্ক)", "#08162c", "#8b5cf6"),
    ("clean", "ক্লিন · লাইট ক্লিন (হালকা)", "#f4f7ff", "#2563eb"),
    ("premium", "প্রিমিয়াম · আইভরি-গোল্ড", "#faf6ed", "#b8860b"),
    ("midnight", "প্রিমিয়াম · মিডনাইট ব্লু", "#0a1526", "#f0c04a"),
    ("emerald", "প্রিমিয়াম · এমারেল্ড", "#f0faf5", "#10b981"),
    ("royal", "প্রিমিয়াম · রয়েল ভায়োলেট", "#f7f3fd", "#7c3aed"),
    ("rose", "প্রিমিয়াম · রোজ শ্যাম্পাইন", "#fdf5f3", "#db2777"),
    ("flat-teal", "স্ট্যান্ডার্ড · টিল", "#ffffff", "#0f766e"),
    ("flat-indigo", "স্ট্যান্ডার্ড · ইন্ডিগো", "#ffffff", "#4338ca"),
    ("flat-emerald", "স্ট্যান্ডার্ড · এমারেল্ড", "#ffffff", "#047857"),
    ("flat-slate", "স্ট্যান্ডার্ড · গ্রাফাইট", "#ffffff", "#334155"),
    ("flat-crimson", "স্ট্যান্ডার্ড · ক্রিমসন", "#ffffff", "#be123c"),
]
THEME_IDS = {t[0] for t in THEME_CHOICES}


VIEW_MODES = ("auto", "desktop", "mobile")


def web_context(request):
    ctx = {"active_business": None, "memberships": [],
           "active_membership": None, "ui_lang": "bn", "user_theme": "aurora",
           "theme_choices": THEME_CHOICES, "ui_view": "auto",
           "notif_unread": 0}
    if getattr(request, "session", None) is not None:
        lang = request.session.get("ui_lang") or "bn"
        if lang not in ("bn", "en"):
            lang = "bn"
        ctx["ui_lang"] = lang
        request.session["ui_lang"] = lang

        view_mode = request.session.get("ui_view") or "auto"
        if view_mode not in VIEW_MODES:
            view_mode = "auto"
        ctx["ui_view"] = view_mode
        request.session["ui_view"] = view_mode
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ctx
    theme = (getattr(request.user, "theme", "") or "").strip()
    if theme in THEME_IDS:
        ctx["user_theme"] = theme
    memberships = list(
        BusinessMembership.objects.filter(
            user=request.user, is_active=True, business__is_active=True)
        .select_related("business"))
    ctx["memberships"] = memberships
    bid = request.session.get("active_business_id")
    for m in memberships:
        if str(m.business_id) == str(bid):
            ctx["active_business"] = m.business
            ctx["active_membership"] = m
            break
    if ctx["active_business"] is None and memberships:
        m = memberships[0]
        request.session["active_business_id"] = str(m.business_id)
        ctx["active_business"] = m.business
        ctx["active_membership"] = m
    if ctx.get("active_business") and getattr(request.user, "is_authenticated", False):
        try:
            from apps.web.notifications import unread_count
            ctx["notif_unread"] = unread_count(ctx["active_business"],
                                              request.user)
        except Exception:
            ctx["notif_unread"] = 0
    return ctx
