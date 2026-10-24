---
name: jarvis-notes
description: "Take, add to and read the user's notes (JARVIS Notes panel)."
version: 1.0.0
author: JARVIS dashboard
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Notes, JARVIS, Dashboard, Note taking]
    related_skills: [obsidian]
---

# JARVIS Notes

Load this whenever the user asks to take, make, save, write down or add to a note, to take notes on
something (an article, a video, a document, a meeting, this conversation), or what's in their notes.

The JARVIS dashboard (`/vesper`) has a **Notes** panel. It is the user's default notebook: when they say
"take notes", "make a note", "save that as a note", "add this to my grocery note" and don't name another
app, the note belongs here. The dashboard opens a note you create or change as soon as your reply arrives,
so the user sees it straight away.

The dashboard already handles simple requests itself without calling you ("take a note: buy milk",
"add eggs to my grocery note", "open my grocery note"). What reaches you is the work it can't do: notes
that need research, summarising, reading a file or a page, or a tidy write-up of this conversation.

## Storage

One JSON file per note in `$HERMES_HOME/vesper-notes/<32 lowercase hex>.json`:
`{"id", "title", "body", "created_at", "updated_at"}`. Timestamps are ISO-8601 UTC with milliseconds and a `Z`.
Bodies are plain text, and Markdown-style lists (`- item`) read well in the panel.

**Always write through the helper script.** It writes the exact format atomically, with the right permissions.
Don't hand-write these files with file tools. Reading them with `read_file` is fine.

```
S="$HERMES_HOME/skills/note-taking/jarvis-notes/scripts/jarvis_note.py"
python3 "$S" list                                     # titles, ids, previews (newest first)
python3 "$S" show --title "Groceries"                 # one note, as JSON (best title match)
python3 "$S" add --title "Article: Why sleep matters" <<'NOTE'
- Sleep consolidates memory (REM and slow-wave)
- Source: https://example.com/article
NOTE
python3 "$S" append --title "Groceries" --body "- oat milk"          # adds a line (keeps "- " lists going)
python3 "$S" append --title "Reading list" --create --body "- Dune"   # creates it if nothing matches
python3 "$S" set --title "Groceries" <<'NOTE'                 # EDIT in place: same note, full new text
- eggs (bought)
- oat milk
NOTE
python3 "$S" import --file /opt/data/workspace/notes.md   # a file note -> a panel note (title from its "# " heading);
                                                         # already imported? it returns that note (already_imported)
```

`--body` takes short text. For anything long or multi-line, pass the text on stdin with a quoted heredoc, as above.
That's one terminal call, with no temp file, which matters here: `write_file` may only write inside `$HERMES_HOME`,
and `execute_code` is blocked in dashboard sessions. Each command prints JSON (`id`, `title`, and `created`,
`appended` or `updated`). It exits 1 with `{"error": ...}` when no note matches, or when the note would be empty or
too long (title 200 chars, body 200,000).

## How to take good notes

- **Title**: short and specific ("Standup 14 Oct", "Article: Why we sleep"). Use the user's name for it when they give one.
- **Body**: lead with the point. Use short bullets for key facts, decisions, numbers, names and dates, and action
  items as `- [ ] ...`. Add a source line (URL, file name) when the notes come from one.
- **Adding to a note**: when the user says "add to my X note", `append` to it rather than creating a duplicate.
  Run `list` first if you're unsure which note they mean.
- **Reply**: after saving, reply with one or two sentences: what you saved and the note's title. Don't paste
  the whole note back. The dashboard is already showing it.
- **Notes you wrote as files**: before this skill existed, notes were sometimes saved as Markdown files (for
  example in `/opt/data/workspace/`). When the user refers to one of those ("add to that same note"), `import` the
  file into the panel, then add to the panel copy, and tell the user it now lives in Notes. Leave the old file alone.
- **Editing** (mark an item done, change a line, fix a typo): `show` the note, then `set` it with the full new text.
  `set` keeps the note's id, so the dashboard keeps showing the same note. Don't `add` a copy.
- **Never** delete note files (no `rm` in `vesper-notes/`); if the user wants a note gone, tell them to delete it
  in the panel. Don't overwrite a note's existing text unless the user asked for that change. Appending is the default.
