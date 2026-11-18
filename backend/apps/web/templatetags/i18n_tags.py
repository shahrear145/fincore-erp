from django import template

from apps.web.i18n import tr

register = template.Library()


@register.simple_tag(takes_context=True)
def t(context, key):
    lang = context.get("ui_lang") or "bn"
    return tr(lang, key)


@register.filter
def trf(key, lang=None):
    return tr(lang or "bn", key)
