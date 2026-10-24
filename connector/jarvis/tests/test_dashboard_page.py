"""The dashboard page as served: event placeholders arrive pre-encoded (no inline handlers for the CSP to block), the
logic script is untouched, and the key and CSP are on it."""

import re
from pathlib import Path

import jarvis
from jarvis import core_routes

PAGE = (Path(jarvis.__file__).resolve().parent / "assets" / "vesper" / "dashboard.dc.html").read_text(encoding="utf-8")
INLINE = re.compile(r"\son[A-Z][A-Za-z0-9]*\s*=")


def _template(page):
    return page[page.find("<x-dc>"):page.rfind("</x-dc>")]


def test_template_has_placeholders_to_encode():
    assert len(INLINE.findall(_template(PAGE))) > 50


def test_served_template_has_no_inline_handlers():
    served = core_routes.encode_event_placeholders(PAGE)
    assert INLINE.findall(_template(served)) == []
    assert 'sc-camel-on-click="{{' in served
    assert 'sc-camel-on-key-down="{{' in served


def test_encoding_round_trips_to_the_runtime_names():
    """support.js decodes sc-camel-<kebab> with kebabToCamel; every encoded name must come back as the original."""
    served = core_routes.encode_event_placeholders(PAGE)
    kebab_to_camel = lambda s: re.sub(r"-([a-z])", lambda m: m.group(1).upper(), s)
    decoded = {kebab_to_camel(n[len("sc-camel-"):]) for n in re.findall(r"sc-camel-on-[a-z-]+", served)}
    original = {m.strip()[:-1].strip() for m in INLINE.findall(_template(PAGE))}
    assert decoded == original


def test_everything_outside_the_template_is_untouched():
    served = core_routes.encode_event_placeholders(PAGE)
    assert served[:served.find("<x-dc>")] == PAGE[:PAGE.find("<x-dc>")]
    assert served[served.rfind("</x-dc>"):] == PAGE[PAGE.rfind("</x-dc>"):]


def test_a_page_without_a_template_is_returned_as_is():
    assert core_routes.encode_event_placeholders("<p onClick=x>") == "<p onClick=x>"
