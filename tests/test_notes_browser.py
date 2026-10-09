"""The notes window in a real Chromium: notes read as they should, nothing in a note becomes markup or script,
links between notes work, an edit is saved with the version it started from, and a conflict keeps what was typed.
The brain is played by answering the window's requests in the browser itself. Every note here is invented."""
import json
import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "src" / "jarvis" / "web"
SHARED = REPO / "desktop"
BROWSER = os.environ.get("JARVIS_TEST_BROWSER") or next(
    (p for p in ("/opt/pw-browsers/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "")
     if p and os.access(p, os.X_OK)), "")
playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not BROWSER, reason="needs a Chromium")

BASE = "https://brain.test:8443"
HOSTILE = ('# Odd things\n\n<script>window.pwned = 1</script>\n\n<img src=x onerror="window.pwned = 2">\n\n'
           '[click](javascript:window.pwned=3) and [mail](mailto:a@example.test)\n')
NOTES = {
    "README.md": "# Notes\n\nStart with [the garden](wiki/garden.md) or [the printer](wiki/printer).\n",
    "USER.md": "# Owner\n\n- Wants short answers.\n",
    "wiki/garden.md": ("# Garden\n\nThe **tomatoes** are watered *every* morning; see `water.sh`.\n\n"
                       "## Beds\n\n| Bed | Plant |\n|---|---|\n| 1 | beans |\n| 2 | kale |\n\n"
                       "- [x] mulch\n- [ ] stakes\n  - tall ones\n\n```\nwater --all\n```\n\n> Rain counts.\n\n"
                       "Back to [the notes](../README.md); [a site](https://example.org).\n"),
    "wiki/printer.md": "# Printer\n\nTwo paper trays.\n",
    "wiki/odd.md": HOSTILE,
    "raw/2025/filed.md": "Filed as it came.\n",
}


def version(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(executable_path=BROWSER, args=["--no-sandbox"] if os.geteuid() == 0 else [])
        yield b
        b.close()


def window(browser, notes=None, conflict_on=(), held=None, merge_on=()):
    """held: a list to put PUT requests in instead of answering them, so a test can answer them later."""
    notes = dict(notes or NOTES)
    saves, released = [], []
    page = browser.new_page(viewport={"width": 1100, "height": 760}, ignore_https_errors=True)

    def answer(route):
        url = urlparse(route.request.url)
        if url.path.startswith("/panel/"):
            name = url.path.split("/")[-1] or "notes.html"
            folder = SHARED if name in ("Oxanium.ttf", "icon.png") else WEB
            kinds = {"html": "text/html", "css": "text/css", "js": "text/javascript", "ttf": "font/ttf", "png": "image/png"}
            if not (folder / name).exists():
                return route.fulfill(status=404, body="")
            return route.fulfill(status=200, body=(folder / name).read_bytes(), content_type=kinds[name.rsplit(".", 1)[1]],
                                 headers={"content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                          "font-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if url.path == "/api/notes":
            listed = [{"path": p, "size": len(t), "protected": p in ("USER.md",), "read_only": p.startswith("raw/")} for p, t in sorted(notes.items())]
            return route.fulfill(status=200, json={"detail": "main at abc1234", "notes": listed})
        if url.path == "/api/notes/note" and route.request.method == "GET":
            path = parse_qs(url.query)["path"][0]
            if path not in notes:
                return route.fulfill(status=404, json={"error": "no such note: " + path})
            return route.fulfill(status=200, json={"path": path, "text": notes[path], "version": version(notes[path]),
                                                   "protected": path == "USER.md", "read_only": path.startswith("raw/")})
        if url.path == "/api/notes/note" and route.request.method == "PUT":
            body = json.loads(route.request.post_data)
            if held is not None and route not in released:
                saves.append(body)
                held.append(route)
                return None
            if route not in released:
                saves.append(body)
            current = notes.get(body["path"])
            if body["path"] in conflict_on or (current is not None and version(current) != body["base"]):
                return route.fulfill(status=409, json={"error": body["path"] + " changed since you opened it", "conflict": True})
            text = body["text"] if body["text"].endswith("\n") else body["text"] + "\n"
            if body["path"] in merge_on:
                text += "Merged from elsewhere.\n"   # as when the save rebased onto a change made meanwhile
            notes[body["path"]] = text
            return route.fulfill(status=200, json={"path": body["path"], "commit": "def5678", "version": version(text), "text": text, "protected": False})
        return route.fulfill(status=404, json={"error": "nothing here"})

    page.route(BASE + "/**", answer)
    page.answer, page.released = answer, released
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    return page, saves, notes, errors


def test_a_note_reads_as_it_should(browser):
    page, _, _, errors = window(browser)
    page.goto(BASE + "/panel/notes.html#wiki%2Fgarden.md")
    page.wait_for_selector("#view h1")
    assert page.inner_text("#path") == "wiki/garden.md"
    assert page.inner_text("#view h1") == "Garden" and page.inner_text("#view h2") == "Beds"
    assert page.inner_text("#view strong") == "tomatoes" and page.inner_text("#view em") == "every"
    assert page.eval_on_selector_all("#view td", "cells => cells.map(c => c.textContent)") == ["1", "beans", "2", "kale"]
    assert page.eval_on_selector_all("#view li.task .box", "boxes => boxes.map(b => b.className)") == ["box done", "box"]
    assert page.inner_text("#view li.task ul li") == "tall ones"
    assert page.inner_text("#view pre code") == "water --all" and page.inner_text("#view blockquote") == "Rain counts."
    web = page.get_attribute("#view a[href^='https']", "href")
    assert web == "https://example.org" and page.get_attribute("#view a[href^='https']", "rel") == "noopener noreferrer"
    # A link to another note opens it here.
    page.click("#view a.note-link")
    page.wait_for_function("() => document.getElementById('path').textContent === 'README.md'")
    page.click("text=the printer")
    page.wait_for_function("() => document.getElementById('path').textContent === 'wiki/printer.md'")
    # The shelf: standing files first, folders as headings, a filter.
    assert page.eval_on_selector_all(".list h2", "hs => hs.map(h => h.textContent)") == ["Standing files", "raw/2025", "wiki"]
    page.fill("#filter", "gar")
    assert page.eval_on_selector_all(".list button", "bs => bs.map(b => b.textContent)") == ["garden"]
    assert errors == []


def test_nothing_in_a_note_becomes_markup_or_script(browser):
    page, _, _, errors = window(browser)
    page.goto(BASE + "/panel/notes.html#wiki%2Fodd.md")
    page.wait_for_selector("#view h1")
    assert page.eval_on_selector_all("#view script, #view img, #view a[href^='javascript'], #view a[href^='mailto']", "n => n.length") == 0
    shown = page.inner_text("#view")
    assert "<script>window.pwned = 1</script>" in shown and '<img src=x onerror="window.pwned = 2">' in shown
    assert "click" in shown
    page.click("text=click")
    assert page.evaluate("window.pwned") is None and errors == []


def test_an_edit_is_saved_with_the_version_it_started_from(browser):
    page, saves, notes, errors = window(browser)
    page.goto(BASE + "/panel/notes.html#wiki%2Fprinter.md")
    page.wait_for_selector("#view h1")
    assert page.is_disabled("#save")
    page.click("#mode")
    page.fill("#editor", "# Printer\n\nThree paper trays.\n")
    assert page.is_enabled("#save") and "unsaved" in page.get_attribute("#save", "class")
    page.click("#mode")   # the preview shows what is typed, not yet saved
    assert "Three paper trays." in page.inner_text("#view")
    page.keyboard.press("Control+s")
    page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Saved as def5678')")
    assert saves == [{"path": "wiki/printer.md", "text": "# Printer\n\nThree paper trays.\n", "base": version(NOTES["wiki/printer.md"])}]
    assert notes["wiki/printer.md"] == "# Printer\n\nThree paper trays.\n" and page.is_disabled("#save")
    # A source cannot be edited; a standing file says what it is.
    page.click(".list button[data-path='raw/2025/filed.md']")
    page.wait_for_function("() => document.getElementById('path').textContent === 'raw/2025/filed.md'")
    assert page.is_disabled("#mode") and page.inner_text("#badge") == "Source, read only"
    page.click(".list button[data-path='USER.md']")
    page.wait_for_function("() => document.getElementById('badge').textContent === 'Shapes how Jarvis behaves'")
    assert errors == []


def test_a_conflict_keeps_what_was_typed_and_a_new_note_is_made_on_save(browser):
    page, saves, notes, errors = window(browser, conflict_on=("wiki/garden.md",))
    page.goto(BASE + "/panel/notes.html#wiki%2Fgarden.md")
    page.wait_for_selector("#view h1")
    page.click("#mode")
    page.fill("#editor", "# Garden\n\nMy own words.\n")
    page.click("#save")
    page.wait_for_selector(".status.trouble button")
    assert "changed since you opened it" in page.inner_text("#status")
    assert page.input_value("#editor") == "# Garden\n\nMy own words.\n", "nothing typed is lost"
    page.click(".status.trouble button")
    page.wait_for_function("() => document.getElementById('editor').value.includes('tomatoes')")
    page.fill("#new-path", "wiki/shed")
    page.click("#create button")
    assert page.inner_text("#path") == "wiki/shed.md" and page.input_value("#editor") == "# shed\n\n"
    page.fill("#editor", "# Shed\n\nThe key hangs by the door.\n")
    page.click("#save")
    page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Saved as')")
    assert saves[-1] == {"path": "wiki/shed.md", "text": "# Shed\n\nThe key hangs by the door.\n", "base": ""}
    page.wait_for_selector(".list button[data-path='wiki/shed.md']")
    for bad in ("../outside", ".hidden", "raw/new"):
        page.fill("#new-path", bad)
        page.click("#create button")
        assert "trouble" in page.get_attribute("#status", "class"), bad
    assert errors == []


# ---------------------------------------------------------------- found by review

def release(page, held):
    while held:
        route = held.pop(0)
        page.released.append(route)
        page.answer(route)


def test_one_save_at_a_time_and_a_save_lands_on_its_own_note(browser):
    held = []
    page, saves, notes, errors = window(browser, held=held)
    page.goto(BASE + "/panel/notes.html#wiki%2Fprinter.md")
    page.wait_for_selector("#view h1")
    page.click("#mode")
    page.fill("#editor", "# Printer\n\nThree trays.\n")
    page.keyboard.press("Control+s")
    page.wait_for_function("() => document.getElementById('status').textContent === 'Saving.'")
    page.keyboard.press("Control+s")
    page.type("#editor", "x")
    assert page.is_disabled("#save") and len(saves) == 1, "a second save would carry an old version"
    page.fill("#editor", "# Printer\n\nThree trays.\n")
    page.click(".list button[data-path='README.md']")   # the owner tries to move on while it saves: not yet
    assert page.inner_text("#path") == "wiki/printer.md" and "until the save is done" in page.inner_text("#status")
    release(page, held)
    page.wait_for_function("() => document.getElementById('status').textContent.startsWith('Saved as def5678')")
    page.click(".list button[data-path='README.md']")
    page.wait_for_function("() => document.getElementById('path').textContent === 'README.md'")
    assert page.is_disabled("#save") and page.input_value("#editor") == NOTES["README.md"], "README keeps its own text"
    assert notes["wiki/printer.md"] == "# Printer\n\nThree trays.\n" and errors == []


def test_windows_line_ends_are_not_a_change_and_odd_notes_read_quickly(browser):
    notes = dict(NOTES)
    notes["wiki/dos.md"] = "# Dos\r\n\r\nWritten on Windows.\r\n"
    notes["wiki/long.md"] = "# a" + " " * 20000 + "b\n\n# Learning C#\n\n" + "_a `x` " * 30000 + "\n\n" + ">" * 3000 + " deep\n"
    page, _, _, errors = window(browser, notes)
    page.goto(BASE + "/panel/notes.html#wiki%2Fdos.md")
    page.wait_for_selector("#view h1")
    assert page.is_disabled("#save")
    page.click(".list button[data-path='wiki/long.md']")
    page.wait_for_function("() => document.getElementById('path').textContent === 'wiki/long.md'", timeout=3000)
    assert page.eval_on_selector_all("#view h1", "hs => hs.map(h => h.textContent)")[1] == "Learning C#"
    assert errors == []


def test_a_merge_while_more_was_typed_makes_the_next_save_stop_rather_than_drop_it(browser):
    held = []
    page, saves, notes, errors = window(browser, held=held, merge_on=("wiki/printer.md",))
    page.goto(BASE + "/panel/notes.html#wiki%2Fprinter.md")
    page.wait_for_selector("#view h1")
    page.click("#mode")
    page.fill("#editor", "# Printer\n\nThree trays.\n")
    page.click("#save")
    page.wait_for_function("() => document.getElementById('status').textContent === 'Saving.'")
    page.type("#editor", "More.")
    release(page, held)
    page.wait_for_function("() => document.getElementById('status').textContent.includes('reload the note to see it')")
    page.click("#save")
    page.wait_for_function("() => document.getElementById('status').textContent === 'Saving.'")
    release(page, held)
    page.wait_for_selector(".status.trouble button")
    assert "changed since you opened it" in page.inner_text("#status")
    assert "Merged from elsewhere." in notes["wiki/printer.md"] and errors == []


def test_a_table_line_of_any_shape_reads_quickly(browser):
    notes = dict(NOTES)
    notes["wiki/table.md"] = "|a|\n|-" + " " * 200000 + "x\n\n|a|\n" + " " * 40000 + "x\n"
    page, _, _, errors = window(browser, notes)
    page.goto(BASE + "/panel/notes.html")
    page.wait_for_selector(".list button[data-path='wiki/table.md']")
    took = page.evaluate("() => { const s = performance.now(); Markdown.render(document.body.dataset.none || '|a|\\n|-' + ' '.repeat(200000) + 'x', () => null); return performance.now() - s; }")
    assert took < 200, took
    page.click(".list button[data-path='wiki/table.md']")
    page.wait_for_function("() => document.getElementById('path').textContent === 'wiki/table.md'", timeout=3000)
    assert errors == []
