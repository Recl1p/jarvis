#!/usr/bin/env python3
"""Save to, append to, list and read the notes in the JARVIS dashboard's Notes panel.

The panel keeps one JSON file per note at $HERMES_HOME/vesper-notes/<32 hex>.json holding
{"id", "title", "body", "created_at", "updated_at"} (ISO-8601 UTC, milliseconds, "Z"). This writes
the same shape the dashboard's /vesper/notes routes do (gateway/platforms/api_server_vesper.py):
temp file + os.replace, mode 0600, directory 0700. Standard library only.

  jarvis_note.py add --title "Groceries" --body "- eggs"      (or body on stdin via a quoted heredoc, or --body-file PATH)
  jarvis_note.py append --title "Groceries" --body "milk"     (--id ID also works; --create makes it if missing)
  jarvis_note.py list
  jarvis_note.py show --title "Groceries"
  jarvis_note.py set --title "Groceries" <<'NOTE'   (edit in place: same note, full new text; --new-title renames)
  jarvis_note.py import --file /opt/data/workspace/notes.md   (title from its first "# " heading, or --title)

Prints JSON. Exit code 1 with {"error": ...} when a note can't be found or the input is invalid.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

TITLE_MAX = 200
BODY_MAX = 200_000
ID_RE = re.compile(r"[0-9a-f]{32}")
STOP = {"my", "the", "a", "an", "our", "note", "notes", "list"}


def hermes_home():
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    # <home>/skills/note-taking/jarvis-notes/scripts/jarvis_note.py
    here = Path(__file__).resolve()
    return here.parents[4] if len(here.parents) > 4 else Path.home() / ".hermes"


def notes_dir():
    return hermes_home() / "vesper-notes"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) for k in ("title", "body", "created_at", "updated_at")):
        return None
    return {"id": path.stem, **{k: raw[k] for k in ("title", "body", "created_at", "updated_at")}}


def write(note):
    directory = notes_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(note, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, directory / f"{note['id']}.json")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def all_notes():
    directory = notes_dir()
    if not directory.is_dir():
        return []
    notes = [n for p in directory.iterdir() if p.suffix == ".json" and ID_RE.fullmatch(p.stem) for n in [read(p)] if n]
    return sorted(notes, key=lambda n: (n["updated_at"], n["created_at"], n["id"]), reverse=True)


def label(note):
    return note["title"].strip() or (note["body"].strip().splitlines() or ["Untitled note"])[0][:60]


def words(text):
    out = []
    for w in re.split(r"[^a-z0-9]+", text.lower().replace("'", "").replace("’", "")):
        if w and w not in STOP:
            out.append(re.sub(r"ies$", "y", w) if w.endswith("ies") else (w[:-1] if w.endswith("s") and not w.endswith("ss") else w))
    return out


def find(title=None, note_id=None):
    """The note with this id, or the best title match (newest wins ties); None if nothing fits."""
    if note_id:
        return read(notes_dir() / f"{note_id}.json") if ID_RE.fullmatch(note_id) else None
    want = words(title or "")
    if not want:
        return None
    best, best_score = None, 0
    for note in all_notes():
        have = words(label(note))
        common = sum(1 for w in want if w in have)
        score = 3 if common == len(want) == len(have) else 2 if common == len(want) else 0
        if score > best_score:
            best, best_score = note, score
    return best


def body_arg(args):
    if args.body is not None:
        return args.body
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return "" if sys.stdin.isatty() else sys.stdin.read()


def append_text(existing, addition):
    """Adds *addition* on a new line, continuing a "- " / "* " / "• " list if the note is one."""
    addition = addition.strip()
    lines = [ln for ln in existing.splitlines() if ln.strip()]
    bullet = re.match(r"^\s*([-*•])\s+", lines[-1]) if lines else None
    if bullet and not re.match(r"^\s*[-*•]\s", addition):
        addition = "\n".join(f"{bullet.group(1)} {ln.strip()}" if ln.strip() else ln for ln in addition.splitlines())
    if not existing.strip():
        return addition
    return existing.rstrip("\n") + "\n" + addition


def fail(message):
    print(json.dumps({"error": message}))
    sys.exit(1)


def checked(title, body):
    if len(title) > TITLE_MAX:
        fail(f"title is longer than {TITLE_MAX} characters")
    if len(body) > BODY_MAX:
        fail(f"body is longer than {BODY_MAX} characters")
    if not title.strip() and not body.strip():
        fail("a note needs a title or a body")
    return title, body


def summary(note, **extra):
    return json.dumps({"id": note["id"], "title": label(note), "updated_at": note["updated_at"], **extra}, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("add", "append", "show", "set"):
        p = sub.add_parser(name)
        p.add_argument("--title", default=None)
        if name != "add":                 # add always makes a new note; editing one is `set`
            p.add_argument("--id", default=None)
        if name != "show":
            p.add_argument("--body", default=None)
            p.add_argument("--body-file", default=None)
        if name == "set":
            p.add_argument("--new-title", default=None)
        if name == "append":
            p.add_argument("--create", action="store_true", help="start the note if no title matches")
    sub.add_parser("list")
    imp = sub.add_parser("import", help="copy a Markdown/text file into the panel as one note (the file is left as is)")
    imp.add_argument("--file", required=True)
    imp.add_argument("--title", default=None)
    args = parser.parse_args()

    if args.cmd == "import":
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"can't read {args.file}: {exc}")
        title = args.title
        heading = re.match(r"\s*#\s+(.+?)\s*(?:\n|$)", text)
        if not title and heading:
            title, text = heading.group(1), text[heading.end():]
        title = (title or Path(args.file).stem.replace("-", " ").replace("_", " ").strip().capitalize())[:TITLE_MAX]
        title, body = checked(title.strip(), text.strip("\n"))
        # Safe to repeat: a file that's already in the panel (same title) isn't copied in twice.
        existing = next((n for n in all_notes() if n["title"].strip().lower() == title.lower()), None)
        if existing:
            print(summary(existing, already_imported=True, imported_from=args.file))
            return
        stamp = now()
        note = {"id": uuid.uuid4().hex, "title": title, "body": body, "created_at": stamp, "updated_at": stamp}
        write(note)
        print(summary(note, created=True, imported_from=args.file))
        return

    if args.cmd == "list":
        print(json.dumps([{"id": n["id"], "title": label(n), "updated_at": n["updated_at"],
                           "preview": n["body"].strip()[:120]} for n in all_notes()], ensure_ascii=False, indent=1))
        return
    if args.cmd == "show":
        note = find(args.title, args.id)
        if not note:
            fail(f"no note matches {args.title or args.id!r}")
        print(json.dumps(note, ensure_ascii=False, indent=1))
        return

    body = body_arg(args)
    if args.cmd == "set":
        # Edit in place (mark an item done, fix a line): same id, so the dashboard keeps showing this note.
        note = find(args.title, args.id)
        if not note:
            fail(f"no note matches {args.title or args.id!r}")
        if args.body is None and args.body_file is None and not body:
            fail("set needs the note's full new text (--body, --body-file or stdin)")
        title = (args.new_title if args.new_title is not None else note["title"]).strip()
        note["title"], note["body"] = checked(title, body.strip("\n"))
        note["updated_at"] = now()
        write(note)
        print(summary(note, updated=True))
        return
    if args.cmd == "append":
        note = find(args.title, args.id)
        if note:
            note["body"] = append_text(note["body"], body)
            checked(note["title"], note["body"])
            note["updated_at"] = now()
            write(note)
            print(summary(note, appended=True))
            return
        if not args.create:
            fail(f"no note matches {args.title or args.id!r}; use add, or append --create")
    title, body = checked((args.title or "").strip(), body.strip("\n"))
    stamp = now()
    note = {"id": uuid.uuid4().hex, "title": title, "body": body, "created_at": stamp, "updated_at": stamp}
    write(note)
    print(summary(note, created=True))


if __name__ == "__main__":
    main()
