# Session handoff notes (2026-09-11)

For a future LLM session (any model) picking up this repo with no memory of how it got here.
Read this file first, then `README.md` (architecture, usage, operational Troubleshooting
section) and `containerplan.md` (the container-consolidation migration log — accurate as
written, do not rewrite it). This file exists because the work below doesn't fit either of
those docs' scope cleanly: it's not container-consolidation work, and it's more "what happened
and what's still open" than permanent reference material.

## What changed after containerplan.md's last entry

All of the following happened in the same 2026-09-11 session as the container-consolidation
work in `containerplan.md`, after "Make Ollama optional" was marked done there.

### 1. Custom music upload feature (shipped)

Implemented per the plan at `~/.claude/plans/pure-popping-garden.md` (historical design-intent
reference only — the work it describes has been implemented, tested, and deployed to the running
container; treat it as background, not a TODO list). Full user-facing/API description is in
README.md's "Custom music library" section. One thing worth flagging explicitly here because
it's easy to get wrong if the original prompt or plan is read in isolation: the upload
size-cap exception (`VESPER_MUSIC_UPLOAD_MAX_BYTES` in `api_server.py`) is a **hardcoded Python
constant**, not an environment variable — there's no `VESPER_MUSIC_UPLOAD_MAX_BYTES=...` you can
set anywhere to change it.

### 2. Dashboard AUDIO/chat panel UI fixes (shipped, in `dashboard.dc.html`)

- Chat message max-width: `msgMaxW: '100%'`. An earlier attempt used a fixed pixel width; the
  user reported it was still insufficient, and this was the corrected version.
- Speaker-label column widened to `width:88px;white-space:nowrap` (was 52px) — the old width
  caused "JARVIS"/"YOU" labels to overlap the message text next to them.
- 3D core repositioned: `coreTransform` now uses
  `translateY(${active ? -145 : -30}px) scale(...)`, moved up so the chat text no longer blocks
  it.
- `componentDidUpdate(prev)` bug fix — worth a longer note because it's a non-obvious framework
  contract gotcha that will bite anyone touching this framework again: this custom "dc" component
  framework **only ever passes one argument to `componentDidUpdate`** (the previous props),
  unlike real React's `componentDidUpdate(prevProps, prevState)`. The old code assumed a second
  `previousState` argument existed; it was always `undefined`, and referencing it threw on every
  single re-render. The wrapper caught and `console.error`'d the exception internally, so it was
  never visible to the user — just constant console noise. Fixed by tracking the previous
  transcript manually via a `this._lastTranscript` instance property (set in the constructor and
  updated at the end of `componentDidUpdate`) instead of relying on a nonexistent second
  argument. This is also documented in README.md's Troubleshooting section since it's a durable
  fact about the framework, not just a one-off bug report.

### 3. Auth-permission incident: `/opt/data/auth.json` ownership corruption (root-caused and fixed)

The `jarvis-hermes` gateway process runs as user `hermes` (uid 10001), not root. Any
`docker exec` into the container that touches `/opt/data` — this includes `hermes auth add`,
`hermes gateway restart`, `hermes cron run`, and presumably any other `hermes` CLI subcommand
that writes there — must be run as `docker exec --user hermes jarvis-hermes ...` (or
`-u 10001`). Running it as plain `docker exec` (root by default) writes files like
`/opt/data/auth.json` as `root:root`, and the `hermes`-user gateway process then can't read its
own credential store. This looks exactly like a genuine "Provider authentication failed / No
Anthropic credentials found" error even when the user's actual login/credentials are completely
fine.

**This bit this session three separate times** before the pattern was recognized. Fix for a
corrupted `auth.json`:

```sh
docker exec --user root jarvis-hermes chown hermes:hermes /opt/data/auth.json
docker exec --user hermes jarvis-hermes /opt/hermes/.venv/bin/hermes gateway restart
```

The general rule is now also documented in README.md's Troubleshooting section and as a callout
at the top of `containerplan.md`, specifically so a future session sees it *before* running an
exec command, not after debugging a confusing auth failure for the fourth time.

### 4. Cron job semantics clarified

A job with `enabled: false`, `state: "completed"` (as opposed to `"paused"`), and
`repeat: {times: N, completed: N}` has legitimately finished all of its scheduled repeat runs.
This is normal termination, not a stuck or broken job, and does not need "resuming". Concrete
example encountered this session: job `0a91def21adf`, "Message Moe reminder", with
`repeat: {times: 7, completed: 7}`. Also documented in README.md's Troubleshooting section.

### 5. Chat attachments feature (shipped and deployed to the running container)

Built as a follow-up task, orchestrated in the same conversational session but as a distinct
piece of work from items 1-4 above. Full user-facing/API description is in README.md's new
"Chat attachments" section; this entry is about the design decisions and what is/isn't verified.

**Research findings that shaped the design** (verified against the actual code, not assumed):
the gateway's `_normalize_multimodal_content` (`hermes/gateway/platforms/api_server.py`) already
accepted OpenAI-style `text` and `image_url` content parts on `/api/sessions/{id}/chat` and
`.../chat/stream` before this feature existed — it only explicitly rejected `file`/`input_file`
parts. That meant **images needed zero backend content-normalization changes**, only a dashboard
UI to build a content-part array and a raised request-size cap (base64 inflates image bytes by
~33%, and the existing cap was 10MB for every route). Separately, `hermes/tools/read_extract.py`
(the document-to-text pipeline `read_file` already uses for PDF/Word/PowerPoint/Excel/
OpenDocument/RTF/EPUB, backed by the bundled `firecrawl-anydoc` core dependency) turned out to
be exactly the tool needed for document attachments too.

**Key design decision — uniform text-extraction over native per-provider document blocks.** The
task brief's suggested stretch goal was native Anthropic `document` content blocks (real
vision-over-PDF-pages) with a fallback for other providers, mirroring how
`hermes/agent/vision_message_prep.py` degrades image parts for non-vision models. This was
deliberately **not** built. Instead, every document attachment (PDF included) is extracted to
plain text server-side via `read_extract.extract_document_bytes` and inlined as an ordinary
`{"type":"text"}` content part — the same shape plain-text attachments use. Reasoning:
1. It works identically across every provider/model with **zero changes** to
   `anthropic_message_convert.py`, `gemini_native_adapter.py`, or `bedrock_adapter.py` — the most
   sensitive, highest-blast-radius part of this codebase, and the part I have the least ability
   to verify without a live multi-provider round-trip (no container access this session).
2. It reuses a pipeline that's already shipped, already has its own scanned-PDF/OCR degrade path
   (`read_extract._needs_ocr_warning`), and is independently testable with synthetic bytes (see
   below) — unlike a native Anthropic document block, which I cannot exercise without a real
   Anthropic API call.
3. The trade-off is real and worth stating plainly: this is text-only extraction, not native
   vision over PDF pages (embedded diagrams/layout are lost). A future session wanting true
   native PDF vision on the Anthropic path specifically should treat this as the extension
   point, not a blocker — the extraction endpoint and text-part inlining can coexist with a
   native path later (e.g. route `.pdf` to a native block only when `provider == "anthropic"`
   and fall back to this extraction endpoint everywhere else).

**What was built:**
- `hermes/gateway/platforms/api_server.py`: two new size-cap constants
  (`CHAT_MESSAGE_MAX_BYTES` = 48MB, `CHAT_ATTACHMENT_EXTRACT_MAX_BYTES` = 15MB) and an extended
  `body_limit_middleware`. Raising the cap for `/chat` and `/chat/stream` needed more than the
  existing Content-Length pre-check the music feature relies on, because those two routes read
  their body via `request.json()`, which enforces aiohttp's per-request `client_max_size`
  (inherited from the 10MB Application-wide default) independently of the middleware's own
  pre-check. Fix: `request = request.clone(client_max_size=<raised limit>)` before calling the
  handler, for exactly the path-scoped routes that need it. Verified directly (not just by
  reading aiohttp's source) with a throwaway aiohttp TestServer: a 20MB JSON body to `/chat`
  succeeded post-change (would 413 pre-change), an unrelated route (`/v1/chat/completions`)
  stayed capped at the old 10MB default, and a body over the *new* raised ceiling still 413s.
- `hermes/gateway/platforms/api_server_vesper.py`: new `POST /vesper/chat/extract-attachment`
  endpoint (`extract_chat_attachment`), following the music upload's exact pattern — `_check_auth`
  gate, filename via an `X-*-Filename` header (`X-Attachment-Filename`, URL-decoded), raw body
  read via bounded `request.content.iter_any()` accumulation (not `.read(n)` — see
  `upload_music`'s comment for why that would silently truncate), server-side extension
  allowlist as the authoritative check (`tools.read_extract.is_extractable_document`, so it can
  never drift from what `read_file` itself supports). Unlike music uploads, **nothing is ever
  written to server-side storage** — bytes are extracted to text in memory and discarded; the
  filename is reduced to `os.path.basename()` and used only for extension sniffing, never to
  touch the filesystem, so the path-traversal concerns that shape the music feature's track-id
  validation don't apply here (there is no id-based lookup surface at all).
- `hermes/gateway/assets/vesper/dashboard.dc.html`: composer 📎 button + hidden file input,
  attachment-chip row (name + remove ✕), three client-side handling paths by extension (image →
  `FileReader.readAsDataURL`, text/code → `FileReader.readAsText` truncated to 60,000 chars,
  document → POST to the new extraction endpoint), and `run()`/`chatOnce()` updated so a message
  with attachments becomes a content-part array (`[{type:"text",...}, {type:"image_url",...},
  ...]`) while a plain no-attachment message stays an unchanged plain string (so history shape
  and prompt caching are unaffected for the common case). Caps: 4 attachments per message, 8MB
  per image, 2MB per text file, 15MB per document (matching the server-side extraction cap).
- New test file `hermes/tests/gateway/test_vesper_chat_attachments.py` (8 tests: auth, missing
  filename, unsupported extension, empty body, real DOCX extraction via an in-memory zip,
  malformed-document error handling, filename-sanitization/path-traversal guard, oversized-body
  rejection).

**What was verified, and how** (no docker/container access was used or needed — this is all
source-level): built a throwaway Python 3.11 venv (`/tmp/hermes_test_venv` — outside the repo,
not committed anywhere) via `pip install -e ".[dev]"` plus `aiohttp==3.14.3` (the `messaging`
extra hermes normally lazy-installs), since no project venv existed in this checkout and the
system Python was 3.14 (too new — hermes-agent pins `<3.14`). With that:
- All 18 pre-existing tests in `test_api_server_multimodal.py` / `test_api_server_normalize.py` /
  `test_api_server_media_data_urls.py` still pass — the `body_limit_middleware` change didn't
  regress existing multimodal/normalization behavior.
- All 8 new tests in `test_vesper_chat_attachments.py` pass, including a real DOCX extraction
  (built as an in-memory minimal OOXML zip, not mocked) confirming `firecrawl-anydoc` actually
  works in this environment (`read_extract._anydoc()` returned non-None).
- The `request.clone(client_max_size=...)` fix was verified against a real aiohttp `TestServer`
  (see above), not just read from aiohttp's source.
- `python -m py_compile` clean on both modified `.py` files; the dashboard's embedded JS
  (extracted from its `<script type="text/x-dc">` block) passes `node --check`.
- The full `hermes/tests/gateway/` suite (7,720 tests) was also run to check for regressions
  outside the immediate blast radius: 7,707 passed, 46 skipped, 2 xfailed, and 13 failed. All 13
  failures are pre-existing and unrelated to this change (Discord/Telegram/Teams/WeCom
  platform-adapter tests failing on missing optional SDK dependencies in the throwaway venv,
  plus one `session_store_prune` test asserting an absolute path specific to a real dev checkout)
  — none touch `api_server.py`, `api_server_vesper.py`, `dashboard.dc.html`, or `read_extract.py`,
  and none mention multimodal/vesper/attachment in their name or failure output.
- **Not verified** (no live container, no real LLM provider credentials in this environment):
  an actual end-to-end browser round-trip (attach a file, send, confirm the model's reply
  reflects the file's content) against a real Anthropic/OpenAI/etc. account; whether the "dc"
  framework's browser-side template binding for the new `sc-if`/`sc-for` chip-row markup renders
  correctly in an actual browser (only `node --check` syntax-validated the JS, which cannot catch
  a wrong `dc` template attribute name or binding key typo — cross-check the binding keys in the
  render object against the template's `{{ }}` placeholders by eye if something looks blank in
  the composer once deployed).

### 6. Chat attachments deployment (done 2026-09-11, plus a recurring podman-hang incident)

After item 5 landed source-only, the user asked to deploy it to the live container. Sequence:

1. Tagged the then-current running image as a rollback point:
   `docker tag localhost/jarvis-unified:local localhost/jarvis-unified:pre-attachments-20260911`
   (still present — points at `404ccec310fc`, the last pre-attachments build).
2. Rebuilt: `docker compose -f ~/.hermes/jarvis-docker/compose.yaml build hermes` (openjarvis's
   own image was untouched by this feature, so it did not need rebuilding — `Dockerfile.hermes`
   only depends on it via `FROM localhost/jarvis-openjarvis:local AS openjarvis`). Ran in the
   background specifically to avoid the exact failure mode in the next paragraph. Succeeded,
   producing `0de5adc587ee`, tagged `localhost/jarvis-unified:local`.
3. **`docker compose up -d --no-deps hermes` (the recreate step) hung indefinitely** — this is
   the same class of incident as containerplan.md's "Resolved blocker" (rootless Podman's CLI/
   storage layer wedging on a container operation), but this time on container *recreate*, not
   *image build*, and this time `podman.service` itself stayed responsive to trivial calls
   (`docker version`) throughout — only storage-touching calls (`docker ps -a`, the compose
   recreate itself) hung, with **no D-state process and no held lock file** (checked and ruled
   out explicitly, matching the prior incident's own finding). Restarting `podman.service` and
   killing the specific hung CLI processes did **not** clear it — a second, independent `docker
   ps -a` invocation hung the same way afterward, and a careless retry briefly ran two concurrent
   `docker-compose up` invocations against the same container object before both were killed
   (no damage resulted — verified the container was still intact and on the old image
   immediately after). **A host reboot was what actually fixed it**, exactly like the prior
   incident. Takeaway for a future session hitting this again: don't bother trying to diagnose or
   force-clear it beyond confirming no D-state process/held lock (cheap and worth ruling out) —
   go straight to asking the user to reboot rather than spending time on `podman.service`
   restarts or process kills, which did not help either time this has now happened.
4. Post-reboot, `jarvis-hermes` did **not** auto-start despite `restart: unless-stopped` in
   compose.yaml — rootless Podman does not restore containers across a full host reboot on this
   machine (no enabled `podman-restart` linger service). It was simply `Exited (0)`, still bound
   to the old image object, so recreating it was actually simpler post-reboot (no graceful-stop
   wait needed): `docker compose -f ~/.hermes/jarvis-docker/compose.yaml up -d --no-deps hermes`
   succeeded immediately (Recreate/Recreated/Starting/Started, no hang this time).
5. Verified post-deploy: container reports `healthy`; `/health` (8642) and `/vnc.html` (6080)
   both 200; `POST /vesper/chat/extract-attachment` returns 401 (route exists, auth gate intact,
   not 404); `/opt/data/auth.json` and `/opt/data/config.yaml` are owned `hermes:hermes` (the
   item-3 ownership-corruption bug did not recur during this deploy).
6. **Not verified**: an actual browser round-trip (attach an image/.txt/.pdf in the dashboard,
   send, confirm the reply reflects the file) — nobody has opened the dashboard in a browser
   since this deploy. Item 5's "Not verified" caveats about the `dc`-template chip-row markup
   still apply; they're now live in the running container rather than just source, but still
   visually unconfirmed.

### 7. Chat document-attachment limit raised to 200MB (done 2026-09-11, deployed)

Follow-up to items 5-6: raised the document-attachment cap from 15MB to 200MB, at the user's
request, scoped specifically to *documents* (not images/text/the overall message cap — those were
offered as separate options and explicitly not chosen). Changed in three places that all had to
move together:
- `hermes/gateway/platforms/api_server.py`: `CHAT_ATTACHMENT_EXTRACT_MAX_BYTES` 15MB → 200MB.
- `hermes/tools/read_extract.py`: `MAX_ANYDOC_BYTES` and `MAX_DOCUMENT_BYTES` 50MB → 200MB. These
  are shared with the `read_file` agent tool (not attachment-specific), so this also raises how
  large a document `read_file` itself can extract from disk — a deliberate, disclosed side effect,
  not an accident. Skipping this would have made the endpoint accept 15-200MB files only to have
  them fail *inside* the extractor with a worse error (the endpoint cap exists specifically to
  fail fast before that point — see the comment above `CHAT_ATTACHMENT_EXTRACT_MAX_BYTES`).
- `hermes/gateway/assets/vesper/dashboard.dc.html`: client-side `MAX_ATTACH_DOC_BYTES` 15MB →
  200MB (fast client feedback only; the server-side cap above is authoritative).
- `README.md`'s "Chat attachments" section updated to match (both the per-file limit and the
  request-size-cap paragraph).

**Not changed, and why it's safe not to:** `CHAT_MESSAGE_MAX_BYTES` (48MB, the `/chat`/
`/chat/stream` body cap) was left alone. A 200MB document never reaches that cap directly — the
extraction endpoint returns only the *extracted text*, separately capped at 64KB regardless of
source file size (`api_server_vesper.py`'s `_ATTACHMENT_EXTRACT_TEXT_CAP`, unchanged) — so raising
the raw-upload ceiling doesn't risk the message-level cap or blow past what's reasonable to stuff
into an LLM context.

**Real trade-off worth flagging**: `read_extract.py`'s own comment notes anydoc-based extraction
(PDF/Word/PowerPoint/etc., as opposed to the stdlib-based docx/xlsx/ipynb path) loads the whole
file into memory — no streaming. A 200MB PDF now means up to ~200MB+ resident in the gateway
process during extraction, on a single request. Not treated as a blocker (asked-for change, and
the container has no explicit memory limit in compose.yaml), but a future session should know this
is the reason the cap doesn't go higher than 200MB without also reconsidering that architecture.

**Deployed**: rebuilt (`localhost/jarvis-unified:local` → `37331c0f4b66`), previous image kept as
`localhost/jarvis-unified:pre-200mb-doc-limit-20260911` (= `0de5adc587ee`, the item-6 attachments
build) for rollback. Recreate this time was fast with no hang (unlike item 6's recreate step).
Verified: healthy, running image `37331c0f4b66`, `/health` 200, `/vesper/chat/extract-attachment`
401 (route present, auth intact), `/opt/data/auth.json` still owned `hermes:hermes`. **Not
verified**: an actual >15MB document attached in a real browser session (no browser round-trip
done for this change either — same open loop as item 6, now also covering this larger-file case).

### 8. Bug fix: scanned-PDF attachments raised a raw error instead of degrading gracefully (done 2026-09-11, deployed)

Found via the user actually using the feature: attaching a large (841-page) PDF with scanned
pages produced a raw error surfaced in the dashboard —
`NeedsOcrError: pages 38, 158, 160, 163, 505, 540, 682, 783-784, 786-787, 824 of 841 need OCR` —
instead of the graceful inline `[NEEDS OCR: ...]` warning text README.md's "Chat attachments"
section already claimed as the behavior ("A scanned/OCR-needed PDF surfaces the extractor's
existing '[NEEDS OCR: ...]' note in the extracted text rather than silently losing content").

**Root cause**: `hermes/tools/read_extract.py` has two anydoc-based extraction functions —
`_extract_anydoc(path)` (used by `read_file`, path-based) and `_extract_anydoc_bytes(data, path)`
(used by `extract_document_bytes`, and therefore by the new
`/vesper/chat/extract-attachment` endpoint from item 5). Only the path-based one caught
`anydoc.NeedsOcrError` and routed it through `_ocr_scanned_pdf` (hosted-OCR retry, else a
`_needs_ocr_warning` note). The bytes-based one had a bare `except Exception as exc: raise
ExtractionError(f"{type(exc).__name__}: {exc}")`, so a `NeedsOcrError` there just got restated as
an `ExtractionError` with that same ugly message — which is exactly the string the user saw
(`extract_chat_attachment`'s `except ExtractionError` handler returns it verbatim as a 422 body,
which the dashboard then displays). This bug was only reachable through the new chat-attachment
endpoint — `read_file` was never affected, since it always went through the already-correct
path-based function.

**Fix**: refactored `_ocr_scanned_pdf(mod, path, exc)` → `_ocr_scanned_pdf(path, exc, retry)`,
where `retry` is a closure supplied by each caller (`lambda **kw: mod.to_markdown(path,
ocr="hosted", **kw)` for the path-based case, `lambda **kw: mod.to_markdown_bytes(data,
ocr="hosted", **kw)` for the bytes-based case — confirmed via the real `anydoc` package inside the
running container that `to_markdown_bytes` accepts the same `ocr`/`api_key`/`api_url` kwargs as
`to_markdown`). `_extract_anydoc_bytes` now catches `NeedsOcrError` the same way `_extract_anydoc`
already did, so both paths share one implementation of "try hosted OCR if configured, else return
the inline warning note" instead of the bytes path silently lacking it.

**Verified**: added 3 new tests to `hermes/tests/tools/test_read_extract.py`
(`TestScannedPdfOcrDegrade`) — path-based warning-without-hosted-OCR (regression guard for the
pre-existing behavior, to catch any future break from touching this shared code), bytes-based
warning-without-hosted-OCR (the actual bug, using a fake `NeedsOcrError`-alike exception since the
real `anydoc` package isn't in the host/test environment), and bytes-based hosted-OCR retry
(asserts `to_markdown_bytes` gets called twice, second time with `ocr="hosted"` and the configured
`api_key`). Ran the full `test_read_extract.py` suite (56 tests, all pass) directly inside the
running container against the real Python 3.13 environment (copied the two changed files into a
writable `/tmp` scratch copy of the `tools`/`tests`/`hermes_cli` packages there, rather than the
read-only live `/opt/hermes` tree, and ran via `python -m unittest` since pytest isn't installed
in this container's venv — same limitation containerplan.md already noted). Deployed: rebuilt,
`localhost/jarvis-unified:local` → `63afc6a6bf08`; previous image kept as
`localhost/jarvis-unified:pre-ocr-fix-20260911` (= `37331c0f4b66`, the item-7 200MB-limit build)
for rollback. Recreate was fast, no hang. Post-deploy: healthy; unauthenticated fetch of the
served `/vesper` page confirms both this fix's code and item 7's 200MB constant are actually live
in what's being served (`MAX_ATTACH_DOC_BYTES = 200 * 1024 * 1024` and the 📎 composer markup both
present).

**Not verified**: an authenticated browser round-trip re-attaching the same scanned PDF (or any
file) to confirm the fix actually resolves the user's exact reported error end-to-end. This
specifically was **not** attempted via automated headless-browser testing from this session,
deliberately: doing so would have required reading the dashboard's Bearer-token auth secret
(`hermes config show` was attempted and was correctly blocked by the auto-mode permission
classifier as credential materialization). Rather than working around that block, this was left
for the user, who already has an authenticated browser session and can verify in seconds. A future
session with explicit permission to handle that credential (e.g. a script that reads and uses it
internally, in-container, without ever printing it) could automate this differently, but should
still ask before doing so rather than assume it's wanted.

### 9. Bug fix: a scanned PDF's real text was ALL discarded, not just the scanned pages (done 2026-09-11, deployed)

Found immediately after item 8, when the user actually tried the attachments feature in their own
browser: attaching an 841-page real textbook PDF where only ~13 pages were scanned images produced
a reply where Hermes said it saw "no PDF content... just the file header/OCR notice" — item 8's
fix had correctly stopped the raw exception, but the resulting `[NEEDS OCR: ...]` warning note
*was* the entire extracted text. The other 828 real, perfectly extractable pages never reached the
model at all.

**Root cause, confirmed against the real `anydoc` package inside the container**:
`anydoc.NeedsOcrError.__doc__` states plainly that `pages` (1-indexed) and `page_count` are the
*only* data it carries — `anydoc.to_markdown`/`to_markdown_bytes` are all-or-nothing for PDFs, so
the instant *any* page needs OCR, the entire conversion aborts and there is no partial result to
recover from anydoc itself. `anydoc.to_document` (a lower-level structured API that might have
allowed per-page/per-block access) explicitly does not support PDFs at all
(`"Unsupported for pdf: ... use to_markdown_bytes"`). So item 8's fix, while correct given only
what anydoc exposes, was still discarding ~98% of a realistic textbook's content on any single
scanned page.

**Second, independent problem found while investigating**: `pdftotext` (poppler-utils) — the
external tool the *pre-existing* `_pdf_coverage_note`/`_pdf_coverage_note_from_bytes` heuristic
already depended on for a different, narrower case (partial silent text-loss on documents anydoc
does NOT raise NeedsOcrError for) — was **not installed anywhere in the Docker image**. This means
that coverage-note feature had *always* silently done nothing in this deployment (`_pdf_page_texts`
checks `shutil.which("pdftotext")` and returns `None` when absent, so `_pdf_coverage_note` quietly
returns `""`), and separately, `_needs_ocr_warning`'s own recovery-hint text has always told
whoever reads it to run `pdftoppm` (poppler-utils' page-rendering tool) — a command that has never
actually existed in this container. Neither gap was introduced by items 5-8; both predate this
whole chat-attachments effort and were just never exercised/noticed until now.

**Fix, two parts**:
1. `Dockerfile.hermes`: added `poppler-utils` to the existing `apt-get install` line (alongside
   chromium/ffmpeg/etc.) — a small (~5MB installed), standard, well-known Debian package. This
   alone reactivates the pre-existing coverage-note feature and makes the existing `pdftoppm` hint
   text actually actionable, independent of anything else in this item.
2. `hermes/tools/read_extract.py`: `_ocr_scanned_pdf` gained a third parameter, `page_texts`, a
   closure (mirroring the existing `retry` closure pattern) that runs `pdftotext` independently of
   anydoc's own failed conversion — `_pdf_page_texts(path)` for the path-based caller,
   a new `_bytes_page_texts(data)` (temp-copies the bytes first, same pattern as the pre-existing
   `_pdf_coverage_note_from_bytes`) for the bytes-based caller. When hosted OCR isn't
   configured/fails AND pdftotext succeeds, a new `_assemble_partial_pdf_text` joins every page's
   real pdftotext text, replacing only the specific pages anydoc listed in `exc.pages` with an
   inline `[page N: NEEDS OCR — scanned image, no text layer]` marker, prefixed by a header
   explaining the situation (still tagged `[NEEDS OCR: ...]` for grep/consistency with the
   no-pdftotext fallback). Only when pdftotext is ALSO unavailable/fails does it fall back to
   item 8's warning-only behavior (unchanged, still strictly better than the pre-item-8 exception).

**Verified**: 4 new tests added to `TestScannedPdfOcrDegrade` in `test_read_extract.py` (58 tests
total in the file now, all pass) — 2 covering the new recovery path (path- and bytes-based, mocking
`_pdf_page_texts`/`_bytes_page_texts` to return a fixed 3-page array with page 2 "missing", asserting
pages 1 and 3's real text both survive alongside the inline page-2 marker) plus the existing item-8
tests re-confirmed unaffected. Beyond mocked unit tests: ran a REAL, non-mocked check inside the
rebuilt container — a hand-written minimal 2-page PDF through `_pdf_page_texts` with the
now-installed real `pdftotext` binary, confirming actual per-page text extraction works (not just
the mocked logic): returned `['Hello page one real text\n\n', 'Hello page two real text\n\n']`
correctly. Deployed: rebuilt (image is ~20MB larger from poppler-utils, `4.93GB` vs `4.91GB`),
`localhost/jarvis-unified:local` → `3893451cbce9`; the image immediately before this rebuild is
kept as `localhost/jarvis-unified:pre-partial-ocr-recovery-20260911` (= `63afc6a6bf08`, the
item-8-only build) for rollback. Recreate was fast, no hang. Confirmed `pdftotext -v` now runs
inside the live container (`pdftotext version 25.03.0`); health/dashboard checks as usual, all fine.

**Not verified**: still no authenticated browser round-trip with the user's actual PDF — same
credential-access constraint as item 8 (the dashboard's Bearer-token auth secret was deliberately
not read/used to script this, per the auto-mode permission classifier's correct block on the first
attempt to view it). The user should retry the exact same 841-page PDF they already tried twice;
expect the reply to now include real content from ~828 of its 841 pages, with the ~13 scanned ones
called out by page number inline rather than the whole document vanishing.

### 10. Bug fix: real chapter content still didn't reach the model — front matter ate the 64KB text cap (done 2026-09-11, deployed)

Found immediately after item 9, when the user retried the same book and the task actually
*succeeded* (two chapter-summary PDFs got sent to Telegram) — but Hermes explicitly said "the
attached file only gave me the front matter/table of contents (the actual chapter body text
wasn't included)" and wrote summaries from the TOC's section titles instead of real chapter text.
Item 9's fix was working correctly (~828 real pages were being recovered via pdftotext, not just
the OCR warning) — the remaining problem was a second, independent truncation point downstream of
that fix.

**Root cause**: two *separate* 65,536-character (64KB) caps were stacked, and raising one alone
would have done nothing:
1. `api_server_vesper.py`'s `_ATTACHMENT_EXTRACT_TEXT_CAP` — truncates the extraction endpoint's
   *response* to the dashboard before it's ever assembled into a chat message.
2. `api_server.py`'s `MAX_NORMALIZED_TEXT_LENGTH` (via `_cap_text`, called from
   `_normalize_multimodal_content` once per text content part) — independently re-truncates
   *each text part* of the outgoing `/chat`/`/chat/stream` message to the same 64KB, regardless of
   source. This one was previously undocumented as relevant to attachments; found by reading
   `_normalize_multimodal_content` directly (`api_server.py:535-568`) after confirming cap #1
   alone wouldn't explain the symptom.

The original code (see the `_ATTACHMENT_EXTRACT_TEXT_CAP` comment before this fix) deliberately
set cap #1 to *exactly* match cap #2, on the explicit assumption that one attachment always
produces exactly one text part. That assumption is what actually broke: a large book's front
matter (title page, credits, table of contents — which for a big textbook can itself list dozens
of chapters/subsections) routinely exceeds 64KB on its own, so the *entire* attachment budget was
being spent before reaching page 1 of chapter 1, independent of how much real content pdftotext
had successfully recovered per item 9.

**Fix**: stopped assuming one part per attachment.
- `api_server_vesper.py`: `_ATTACHMENT_EXTRACT_TEXT_CAP` raised 65,536 → 500,000 (comment rewritten
  to explain the chunking below, since simply raising this number alone — the original
  hypothesis — would have been silently re-clipped by cap #2 with zero effect).
- `dashboard.dc.html`: `MAX_ATTACH_TEXT_CHARS` (client-side truncation for plain text/code file
  attachments specifically — a separate, pre-existing cap for a different attachment path) raised
  60,000 → 500,000 for the same reason, applied uniformly since both attachment kinds share the
  same "text" internal representation. New `CHUNK_CHARS = 60000` constant plus a `chunkText()`
  helper in `run()`: instead of building one `{type:"text"}` part per non-image attachment
  (`.map`), attachments are now split into as many ≤60,000-char parts as needed (`.flatMap`),
  each individually safely under cap #2's 65,536-char-per-part ceiling. A 500,000-char attachment
  now becomes ~9 parts instead of 1 truncated part. Plain string messages (no attachments) are
  completely unaffected — same code path as before.
- Headroom checked, not just assumed: 4 attachments (`MAX_ATTACHMENTS_PER_MESSAGE`) × 500,000
  chars ≈ 2MB total text, comfortably under `CHAT_MESSAGE_MAX_BYTES` (48MB); ~9 chunks/attachment
  × 4 ≈ 36 array items, comfortably under `MAX_CONTENT_LIST_SIZE` (1,000).

**Verified**: `node --check` on the extracted dashboard script (clean); a standalone Node
reproduction of the exact `chunkText`/`flatMap` snippet against a synthetic 500,000-char
attachment — confirmed 10 output parts, every part ≤65,536 chars (max part length was exactly
60,000), and the reassembled chunks equal the original text byte-for-byte (no data loss or
off-by-one truncation from chunking itself). Rebuilt and redeployed;
`localhost/jarvis-unified:local` → `165b48600a52`; the image immediately before this rebuild is
kept as `localhost/jarvis-unified:pre-text-cap-raise-20260911` (= `3893451cbce9`, the item-9
build) for rollback. Confirmed via an unauthenticated fetch of the served `/vesper` page that both
new constants (`MAX_ATTACH_TEXT_CHARS = 500000`, `CHUNK_CHARS = 60000`) are actually live in what's
served, not just in source. Health/dashboard checks as usual, all fine.

**Now verified end-to-end, with a caveat that mattered**: the user's first retry after this fix
still showed only front matter/TOC (identical to before), which looked like the fix hadn't taken
effect. Root cause: `dashboard.dc.html`'s chunking logic is client-side JavaScript that only loads
into a browser tab on page load/reload — re-attaching a file in an *already-open* tab keeps
running whatever JS was loaded before this deploy, which still builds one oversized unchunked text
part. That single part then gets independently re-clipped to 65,536 chars by the server's
`_normalize_multimodal_content` (`MAX_NORMALIZED_TEXT_LENGTH`, unrelated to browser caching) —
reproducing the exact pre-item-10 ceiling regardless of the higher extraction cap. **No code
change was needed**: after the user did a full page reload (not just re-attaching), the fix worked
— confirmed by the model itself reporting it could now see "large chunks of actual textbook body
text from later chapters (Ch 1, 2, and beyond)," i.e. real chapter content, not just front matter.
**Lesson for future dashboard.dc.html changes**: any client-side JS fix requires telling the user
to reload the page, not just retry an action — this will bite again on the next such fix if not
called out explicitly.

**Where this actually landed**: with real content reaching the model, the deployed assistant (not
this infrastructure) correctly declined to summarize from it — it identified the file as a piracy-
sourced copy of a currently-sold, copyrighted textbook (referencing prior attempts via
Z-Library/Libgen links and renamed filenames) and refused to reproduce or work from extended
verbatim excerpts of it. That is the model exercising legitimate judgment, not a bug, and no
attempt was made to route around it. From an infrastructure standpoint, items 5-10 are now
considered **closed**: the chat-attachments feature (images, text/code, and documents including
partial-scan PDFs) has real, non-mocked, end-to-end confirmation that attached-file content
reaches the model correctly.

### 11. Full Hermes integration for JARVIS (2026-09-13)

The user asked JARVIS how it was set up with Hermes; it reported partial integration: no `hermes`
CLI, no `/home/akhdaier/Downloads`, no `pip`, flaky `web_search`, and Canvas needing a fresh login
every session. Root causes, each verified inside the running container:

- **No `hermes` CLI**: the image PATH has `/opt/hermes/bin`, but the terminal tool spawns a login
  shell and Debian's `/etc/profile` resets PATH. Fixed live by a marked block in
  `/opt/data/.profile`; durable image fix `deployment/hermes-path.sh` →
  `/etc/profile.d/10-hermes-path.sh` (in `Dockerfile.hermes`, applies on next rebuild).
- **No pip**: created `/opt/data/.local/venv` (`uv venv --seed --system-site-packages`, Debian
  python 3.13) and put it on the login-shell PATH with `VIRTUAL_ENV` set, so `pip install X` then
  `python3` works and survives restarts.
- **Downloads "unreachable"**: `~/Documents` and `~/Downloads` were already mounted, but only at
  `/var/home/akhdaier/...`; `/home/akhdaier` is a host-only symlink. A live root-exec symlink was
  blocked by the auto-mode permission classifier, so the fix is compose mounts at
  `/home/akhdaier/...` (applies on recreate).
- **Flaky web search**: `web.backend: brave-free` with no `BRAVE_SEARCH_API_KEY` means every call
  failed first, then fell back to the rate-limited keyless ring. `ddgs` was tried first and
  removed again: it imports fine in-process from `/opt/data/lazy-packages`, but the provider runs
  searches in `plugins/web/ddgs/_search_worker.py` via `[sys.executable, worker]`, and that child
  never imports `hermes_bootstrap`, so the lazy target is not on its `sys.path` and every search
  fails with `No module named 'ddgs'` (an upstream bug for sealed images). Probed the keyless
  search providers against two real queries in the container (all returned 5 hits in ~1s; exa had
  the most relevant top hits) and set `web.search_backend: exa`. `web_extract` had been fully broken (it resolved to the
  search-only `brave-free`); probed each keyless extract provider against example.com in the
  container (keenable/exa/firecrawl correct, parallel returned an unrelated page) and set
  `web.extract_backend: keenable`.
- **Canvas re-login**: the claim came from a stale memory note. With `browser.cdp_url` set,
  `openjarvis-browser` attaches to the persistent noVNC Chromium and its `release` keeps the page
  and login; the memory note was corrected.
- **Split brain**: host `~/.hermes` and the container's `/opt/data` had drifted (container had
  newer memory notes and Canvas skill; host had the `openjarvis-vesper-stack` skill). Merged both
  ways now; `compose.live-unified.yaml` (identical to the live `~/.hermes/jarvis-docker/compose.yaml`)
  bind-mounts `~/.hermes/memories` and `~/.hermes/skills` into the container so they stay one
  store. Not applied yet: the recreate needs Podman's storage layer, which was hung again
  (`podman ps` timed out while `exec`/`images`/`volume ls` worked, matching item 6). Run
  `deployment/share-hermes-brain.sh` after a reboot.

Backups of everything touched: `~/.hermes/backups/jarvis-full-hermes-20260913-135427/`
(`host/`, `container/` with config.yaml/.profile/memories/skills, `repo/`).

### 12. PDFs are always converted to Markdown files before analysis (2026-09-13, deployed)

The user asked for attached PDFs to always be converted to MD files before JARVIS analyzes the
text and responds. Before this, the dashboard inlined anydoc's text with no file saved, and PDFs sent
over Telegram only got a note telling the agent to extract the text itself.

- New `hermes/tools/pdf_markdown.py`. `pdf_bytes_to_markdown`/`pdf_file_to_markdown` wrap
  `read_extract`'s output (anydoc Markdown, with item 9's partial-scan recovery) in a titled
  Markdown document. If `read_extract` raises `ExtractionError` (anydoc missing, or it rejects the
  PDF), they fall back to per-page `pdftotext` with `## Page N` headings, and only raise when no
  page has any text. `save_markdown` writes `get_hermes_home()/attachments/pdf-markdown/<uuid hex>/
  <sanitized stem>.md` and prunes entry dirs older than 30 days on each save.
  `convert_pdf_file_to_markdown_file` writes a sibling `.md` next to a cached PDF and reuses it when
  it's up to date (queued follow-ups re-run inbound preprocessing).
- `api_server_vesper.py` `extract_chat_attachment`: PDFs are accepted even when anydoc is
  unavailable. They go through `pdf_bytes_to_markdown`, the full Markdown is saved (a write failure
  just leaves `markdown_path: null`), and the response adds `format: "markdown"` and `markdown_path`.
  The 500,000-char inline cap is unchanged, but the `.md` on disk is complete.
- `dashboard.dc.html`: stores `format`/`markdownPath` for a document attachment. `run()` now labels a
  PDF part `[Attached PDF: … — converted to Markdown before analysis; the full Markdown file is saved
  at …]`, and if truncated the marker points at the `.md`. The status line reads "Converting … to
  Markdown…" and the chip icon is 📝. No template binding keys changed (only computed values), so
  item 5's binding-key risk doesn't apply. **Users must fully reload the dashboard tab after deploy**
  (item 10 lesson).
- `run_inbound.py`: the new async `_convert_inbound_pdfs_to_markdown` runs the conversion via
  `asyncio.to_thread` before `_prepend_inbound_document_notes`, which now takes an optional
  `pdf_markdown` map and uses `gateway/run.py`'s new `_build_pdf_markdown_context_note`. That note
  inlines the first 60,000 chars (`PDF_MARKDOWN_INLINE_CHARS`) and gives the `.md` path (through
  `to_agent_visible_cache_path`) plus the original PDF path. A failed conversion is logged, and the
  old generic document note is used.

**Verified**: new `tests/tools/test_pdf_markdown.py` (10 tests), 4 new endpoint tests in
`test_vesper_chat_attachments.py`, and 4 new tests in `test_document_context_note.py` (including a
full `_prepare_inbound_message_text` run with a real on-disk PDF path). 111/111 pass across those
files plus `test_read_extract.py`, the multimodal/normalize tests, and
`test_mixed_attachment_routing.py`, in a throwaway Python 3.11 venv. Dashboard script passes
`node --check`. Also ran a real, non-mocked check inside the live container against a scratch
`/tmp/pdfmd` copy of the source (real anydoc + pdftotext, Python 3.13): a hand-built 2-page text PDF
converted to titled Markdown; a PDF with one text-less page kept pages 1 and 3 with page 2 marked
NEEDS OCR; a sibling `.md` was written; the real endpoint through aiohttp TestServer returned 200 with
`format: markdown`, and the saved file exactly matched the response. `test_pdf_markdown.py` also
passes under unittest in the container.

**Deployed** (same day, at the user's request): rebuilt, `localhost/jarvis-unified:local` →
`472f6722b8a4`; the previous image is kept as `localhost/jarvis-unified:pre-pdf-markdown-20260913`
(= `165b48600a52`, the item-10 build, which also means this rebuild is the first to ship item 11's
`/etc/profile.d/10-hermes-path.sh`). Build and recreate were both clean, no Podman hang. Post-deploy:
healthy within ~20s; `/health` 200; `/vnc.html` 200; unauthenticated
`/vesper/chat/extract-attachment` returns 401; the served `/vesper` page contains the new
`attachmentHeader`/"Converting … to Markdown" JS; `/opt/data/auth.json` is still `hermes:hermes`; the
memories/skills bind mounts are present. The real-PDF check above was re-run against the installed
`/opt/hermes` code (not a scratch copy), and the endpoint returned 200 with `format: markdown` and a
matching `.md`. **Not verified**: an authenticated browser or Telegram round-trip with a real PDF.
Reload the dashboard tab first.

Backups of every pre-existing file touched: `~/.hermes/backups/pdf-markdown-attachments-20260913/`.

### 13. Notes, local commands, setup guide, mobile layout, shareable compose (2026-10-11/12)

The user asked for: UI polish, a guided tour for connecting Hermes and cloud models, a shareable
Docker Compose, a working mobile view, and speech-to-text notes in the sidebar, with "take a note"
(and likewise music, speed test, and so on) asked in the general view opening that panel. The work
was split up: three Sonnet subagents built the notes REST API, the tour web component plus
`docs/CONNECTING-MODELS.md`, and the shareable compose package, each on separate files. The
orchestrating session did the dashboard itself and the integration. Backups of every pre-existing
file touched: `~/.hermes/backups/jarvis-notes-tour-ui-20261011/`.

- **Notes** (`api_server_vesper.py` `/vesper/notes` CRUD, `tests/gateway/test_vesper_notes.py`, 58 tests):
  one JSON file per note in `HERMES_HOME/vesper-notes/`. Writes are atomic and mode 0600, read-modify-write
  is serialized by a threading lock, and ids are fullmatch-validated before any filesystem access. The
  dashboard's open note is a mutable draft object with a chained save queue. This prevents cross-note
  writes and double-creates. An in-flight list fetch is dropped if a local save landed meanwhile
  (`_notesSeq`). Dictation records with MediaRecorder, posts to `/vesper/stt`, and appends to the note that
  was open when recording *started*.
- **Local commands** (`LOCAL_COMMANDS` in `dashboard.dc.html`): whole-utterance patterns that run in the
  browser before `run()` would call the agent. A voice "take a note" opens Notes and starts dictating.
  "take a note: X" saves X. Music, speed test, panels, model picker, and guide are covered too.
  `focusPanel()` opens a sidebar tab, or pulses the floating card on wide Floating Glass. The matcher was
  checked against 49 utterances, including negatives that must still reach the agent ("take notes on this
  video", "what's on my agenda today?"). The test script lived outside the repo in `~/.cache/jarvis-dev/`.
- **iPhone voice input**: Safari can't record webm. The mic and dictation now pick a supported
  MediaRecorder type, and `_handle_vesper_stt` names the temp file from the Content-Type (`audio/mp4` →
  `.m4a`), because the STT pipeline validates by extension. `tests/gateway/test_vesper_stt_format.py` was
  shown red on the old code and green on the new.
- **Setup guide**: `tour.js` (`<vesper-tour>`, served by the new `static_script()` helper at
  `/vesper/tour.js`). It opens once on a browser's first visit, on the connection steps when
  `/api/model/options` has no authenticated provider. `vesper:ui` is a new page-level event the tour uses
  to open the model picker or refresh models.
- **UI and mobile**: the responsive CSS was rewritten against class hooks (`.brand-sub`, `.clock-block`,
  `.mic-button`, ...), replacing nth-child chains. Several of those, such as `.composer>button:first-child`,
  never matched anything. At ≤700px the sidebar is an icon dock that opens a full-height sheet with a
  backdrop. The top bar is brand + model + ?/☰, and the composer has two rows. The 60ms clock now writes
  its DOM node directly instead of re-rendering the whole dashboard ~16×/s. Other fixes: EQ bars held at
  the low keyframe while paused, a speed-panel hint, connection-state colour, aria labels, and the palette
  reordered with notes/guide/browser/OpenJarvis entries added.
- **Shareable compose**: the repo `compose.yaml` was rewritten as the portable one: env-driven bind and
  ports, optional `.env`, a build-only `openjarvis-base` service, and `JARVIS_IMAGE`. Also added: `setup.sh`,
  `.env.example`, `SHARING.md`, `scripts/make-share-bundle.sh` (allowlist + personal-data scan, fails
  closed), `scripts/export-image.sh`, and `.dockerignore` additions. README regions specific to this
  machine are wrapped in `<!-- bundle:exclude-start/end -->`. The live deployment still runs from
  `~/.hermes/jarvis-docker/compose.yaml`.
- **Verification**: 91 backend tests passed (`test_vesper_*`, multimodal/normalize) in a throwaway
  sandbox inside the container (`~/.cache/jarvis-dev/jt-test.sh`). The Playwright integration test
  `test_vesper_browser.py -m integration` also passed in the container, against `/usr/bin/chromium`.
  That test now seeds `vesperTourDone`, because the first-visit guide would otherwise cover the page.
  UI flows were driven in a dev harness that serves the real dashboard and real vesper routes on
  127.0.0.1:8743 inside the container, with mocked chat/model/STT (`~/.cache/jarvis-dev/harness.py`).
  Those flows were screenshotted at 1440, 1280, 1180, 820, 390 and 360px wide.
- **Build / deploy state**: built `localhost/jarvis-unified:local` → `d9ba2901093a` from the live compose
  file. Its contents were hash-checked against the repo. The previous image is kept as
  `localhost/jarvis-unified:pre-notes-tour-20261012` (= `528666665cd3`). **The live container was NOT
  recreated**: the auto-mode classifier blocked `docker compose up -d --no-deps hermes` as a production
  deploy. That step is left to the user: `cd ~/.hermes/jarvis-docker && docker compose up -d --no-deps hermes`,
  then a full page reload of `/vesper`.
- **Podman wedge: root cause found and fixed without a reboot (2026-10-12).** `docker ps -a`/`compose up`
  hung while `images`/`volume ls`/`exec` worked, with no D-state process; this is the same pattern as items 6 and 11.
  Inspecting each container separately showed that only `odysseus-odysseus-1`'s inspect hung, so that container's
  libpod shm lock was held. That is an unrelated, long-exited container. Two API services were running: systemd's
  `podman.service` and one Podman Desktop starts itself (`flatpak-spawn --host podman system service --time=0`).
  Restarting `podman.service` did **not** help. Sending SIGTERM to the Podman Desktop service released the lock
  immediately. That service had also taken over `/run/user/1000/podman/podman.sock` and deleted it on exit, so
  `systemctl --user restart podman.socket` was needed to bring the API back. The running JARVIS container stayed
  healthy throughout. **Next time, try this before asking for a reboot**: find which container's inspect hangs, stop
  Podman Desktop's API service, then restart `podman.socket`.
- **Shareable compose: real smoke test passed** (`-p jarvis-sharetest`, ports 18642/16080, fresh volumes,
  image `d9ba2901093a`). It reached healthy. `/health`, `/vesper`, `/vesper/tour.js` and `vnc.html` returned 200, and
  `/vesper/notes` returned 401 without the key. A headless browser in the container saw the guide open by itself on
  desktop and phone, the Notes panel load through the page's own auth, and no page errors. The fresh `/opt/data`
  was owned by `hermes:hermes` (0700, with `.env` 0600). The stack was then removed with `down -v`.
  Note: a fresh install already reports two "connected" providers, `moa` (Mixture of Agents) and `opencode-free`
  (keyless free models), so the guide's no-model path doesn't trigger there. It opens on Welcome instead, and its
  live-check step lists those two.
- **Deployed 2026-10-12** (the user ran the recreate themselves, because the classifier blocks it for Claude):
  `jarvis-hermes` is now on `d9ba2901093a` and was healthy within ~24s. `/health`, `/vesper`, `/vesper/tour.js` and
  `vnc.html` return 200, and `/vesper/notes` returns 401 without the key. The served page contains the new code.
  `auth.json`/`config.yaml`/`.env` are still `hermes:hermes` 0600. The Documents/Downloads and memories/skills
  mounts are present, and Telegram reconnected. A read-only headless check of the live page (`live-smoke.py`)
  loaded the Notes panel through the page's own auth, showing model `claude-sonnet-5` and CONNECTED, with no page
  errors on desktop or phone. Rollback: `localhost/jarvis-unified:pre-notes-tour-20261012`. **Not yet verified
  in the user's own browser**: a real dictation (mic → STT → note), and an iPhone recording through `/vesper/stt`.
- **Pre-existing, not changed**: `/vesper/stt` and `/vesper/tts` have no `_check_auth` call, although
  the TTS docstring says it's Bearer-gated. It's moot while `/vesper` stamps the key into the page, but
  it matters for anyone binding the port beyond loopback. `podman-restart.service` is disabled on this
  machine, so the container still won't come back after a reboot on its own.

### 14. JARVIS moved to the TrueNAS server; the PC is no longer involved (2026-10-12)

The user moved JARVIS to their always-on TrueNAS box. The PC dual-boots and is often off, and the
requirement was "nothing to do with this PC" afterwards, with Hermes working exactly as before.

- **NAS**: `truenas-scale` (TrueNAS SCALE 25.10.5, Docker 28.3.1, Compose v2.38), Tailscale 100.97.23.25.
  SSH is `ssh truenas_admin@truenas`, key-only. The PC's key is `~/.ssh/id_ed25519`, and only
  `sudo -n /usr/bin/docker` is passwordless. The login shell is zsh.
- **Stack**: the Dockge stack `jarvis` at `/mnt/hdd_2tb/configs/dockge/stacks/jarvis/`:
  - `compose.yaml` is the live config, with comments explaining data, files, access, updates and CLI.
  - `update.sh` tags the old image `pre-update-<stamp>`, builds, then runs `up -d` (`--base` also
    rebuilds the OpenJarvis image).
  - `src/` is this repo, which is now the source of truth.

  `container_name: jarvis-hermes` is kept so the existing docs and the tour's `docker exec` commands still
  apply. `pull_policy: never` is set, plus the label `com.centurylinklabs.watchtower.enable=false`, because
  the NAS's Watchtower updates every container nightly. Ports bind to `127.0.0.1` only, and `TZ` is unset
  (UTC), as it was on the PC.
- **Data**: `/mnt/SSDCACHE256/jarvis/data` → `/opt/data`. This is the dataset `SSDCACHE256/jarvis`: Generic
  preset, POSIX ACLs, `aclmode=discard` (needed, because Hermes chmods its credential files). The old
  PC's named volumes, plus the `~/.hermes/{memories,skills}` bind mounts, were exported in one consistent
  tar with the PC container stopped. The export ran as root inside a helper container using the live
  container's userns (`--userns keep-id:uid=10001,gid=10001 --user 0`, read-only mounts), so the archive
  carries container-perspective uids. It went to `/mnt/SSDCACHE256/jarvis/backups/pc-final-opt-data-20261012.tar.gz`
  (sha256 `3365cb5e…6304c`, matching on both ends) and was extracted with `--numeric-owner`. Result:
  19,948 files on both sides, credentials `10001:10001 0600`, OpenJarvis data `10001:10000`. Memory and
  skills are now plain directories, so the link to the PC's `~/.hermes` brain has ended on purpose.
  `/home/akhdaier/{Documents,Downloads}` (also at `/var/home/...`) are mounted from
  `/mnt/SSDCACHE256/jarvis/files/`, which started empty as the user chose.
- **Images**: `podman save | ssh … docker load`, sent directly over the LAN at about 2ms. Gotcha: a
  multi-image `podman save --format docker-archive` loaded **both tags onto the first image**. Re-sending
  `localhost/jarvis-openjarvis:local` alone fixed it. The images are `d9ba2901093a` (unified) and
  `6c276a5bdfb2` (OpenJarvis base).
- **Verified on the NAS**: healthy in ~18s. `/health`, `/vesper`, `/vesper/tour.js` and `vnc.html`
  return 200, and `/vesper/notes` returns 401 without the key. Telegram is connected (polling) and the
  cron ticker heartbeat is updating. A headless check from inside the container (`live-smoke.py`) loaded
  the dashboard with model `claude-sonnet-5` and CONNECTED, plus the user's existing note, with no page
  errors on desktop or phone.
- **PC retired**: the container was removed, and `~/.hermes/jarvis-docker/compose.yaml` was renamed to
  `compose.yaml.retired-20261012`, next to a `MOVED-TO-NAS.md`. Starting the old file would put a second
  JARVIS on the same Telegram bot and cron jobs. The PC Podman volumes are left in place as an extra
  fallback. The PC repo copy has a `MOVED-TO-NAS.md` pointing here.
- **Agent's own skill**: `skills/software-development/openjarvis-vesper-stack/SKILL.md` was rewritten
  for the NAS layout and bumped to v1.1.0. The old PC version is in the backup tarball.
- **Access (published 2026-10-12)**: after the user enabled Serve/HTTPS for the tailnet,
  `tailscale serve --bg 8642` and `tailscale serve --bg --https=6080 http://127.0.0.1:6080` were run inside
  `ix-tailscale-tailscale-1` (host network, kernel mode, so 127.0.0.1 is the NAS). They give
  `https://truenas-scale.anchovy-chameleon.ts.net/vesper` and `…:6080/vnc.html`, both tailnet only. The cert
  is a valid Let's Encrypt one; the first request took ~15s while it was issued. The serve config lives in the
  app's state dir, bind-mounted from `/mnt/hdd_2tb/configs/tailscale`, so it survives app restarts and
  reboots. Port 6080 is used so the dashboard's BROWSER button (`location.hostname:6080`) works. Turn either
  off with `tailscale serve --https=443 off` / `--https=6080 off`.
- **LAN access added (2026-10-12, at the user's explicit request, no login proxy)**: the dashboard is now
  also reachable at `http://truenas.local:8642/vesper` (= `10.0.0.103`) on the home network. Anyone on that
  network can control the agent and its browser desktop; the user was told this and accepted it. Phones
  can't use the mic over plain http, so voice still needs the Tailscale HTTPS URL.
  Ports: `8642:8642` (all interfaces), plus `127.0.0.1:6080` and `10.0.0.103:6080`. **`0.0.0.0:6080` is
  impossible**: tailscaled's serve for `:6080` holds real sockets on the Tailscale IPs, so Docker's bind
  failed with "address already in use". That failure left the container recreated but stopped for ~4
  minutes before the fix. The LAN IP is a DHCP lease (`dynamic` on `eno1`). If it ever changes, the
  container won't start until the IP in `compose.yaml` is updated, so the user should keep a DHCP
  reservation for the NAS on the router. Going back to Tailscale-only means binding both ports to
  `127.0.0.1` again.
- **Dockge port links fixed, plus the first NAS-side rebuild (2026-10-12)**: Dockge turns each compose
  `ports` entry into an `http://<ip-or-dockge-host>:<port>/` link (`parsePort` in its frontend), so
  `127.0.0.1:` entries linked to the visitor's own machine. Three changes:
  - The gateway answers `GET /` with `302 vesper` (`_handle_root_redirect` in `api_server.py`; test
    `tests/gateway/test_vesper_root_redirect.py`, red on the old code). The redirect is relative, so the
    `/p/<profile>/` mirror works too.
  - `Dockerfile.hermes` writes `/usr/share/novnc/index.html`, which redirects to
    `vnc.html?autoconnect=true&resize=remote`, so a bare `:6080` shows the desktop instead of a file listing.
  - Ports are now `8642:8642` plus `10.0.0.103:6080:6080`. Tailscale serve for `:6080` was re-pointed to
    `http://10.0.0.103:6080`, so no loopback mapping is needed.
- **Incident: the first image built on the NAS crash-looped** (~4 min down, rolled back). The Dockge
  stacks dataset stores new files as `770` (`chmod 644` even became `744` there), and Docker's COPY
  preserves modes. The image's `/opt/hermes` and `/opt/vesper/start.sh` were therefore unreadable to the
  `hermes` user, giving `PermissionError: /opt/hermes/hermes_cli/__init__.py`. Fix: `update.sh` was
  rewritten and only needs Docker access (`DOCKER=` override, `STACK_DIR=`). It now:
  1. normalizes `src/` with `chmod -R u+rwX,go+rX` (in a throwaway container);
  2. tags the running image `pre-update-<stamp>`;
  3. builds;
  4. **smoke-tests the new image as uid 10001** with a scratch `HERMES_HOME` (importing the gateway reads
     `$HERMES_HOME/.env`, and the image's own `/opt/data` is root-only, so the test needs one);
  5. recreates the container;
  6. waits up to 180s for `healthy`, and **rolls back automatically** if it doesn't get there.

  The smoke test rejected the broken image and accepted the good one. The rebuilt image `f4f30f51f9bf`
  (built entirely on the NAS: the base image was pulled by digest and the OpenJarvis base used locally;
  ~3 min) is live and healthy, with Telegram connected and every link verified. The rollback tag is
  `pre-update-20261012-1039` (= `d9ba2901093a`). The NAS build is self-sufficient: the PC is not needed
  for updates.
- **Dev tooling caveat**: `~/.cache/jarvis-dev/*` (on the PC) assumed the PC container. The test sandbox
  and harness now need to target the NAS container instead, through ssh + `sudo docker`.

### 15. Bug sweep on the NAS install (2026-10-12)

What was checked on the live NAS JARVIS:
- container logs and `/opt/data/logs/errors.log`;
- every cron job's last status;
- `hermes doctor`;
- a headless browser on the LAN origin (`http://10.0.0.103:8642/vesper`, an insecure context like a phone on Wi-Fi);
- direct probes of TTS (200 audio/mpeg), STT (200), the radio proxy (200), noVNC (connected) and notes.

Fixed and shipped through the safe `update.sh` (smoke test plus auto-rollback). Images `pre-update-20261012-1706` and `-1708` were kept as rollbacks:
- **Weather location on plain-http LAN origins** (the user's report). Browsers expose `navigator.geolocation`
  only on secure contexts, so `http://truenas.local:8642` always showed "Location unavailable". Changes:
  - New authed routes in `api_server_vesper.py`: `GET/PUT/DELETE /vesper/location` and `GET /vesper/geocode?q=`.
    The location is saved as `{name, lat, lon}` in `HERMES_HOME/vesper-location.json` (0600, atomic write).
    `/vesper/geocode` proxies Open-Meteo's keyless geocoding API.
  - `/vesper/weather` with no lat/lon now uses the saved place, and returns `location: <name>` or 404 `location_not_set`.
  - Dashboard order: saved place → device geolocation (secure contexts only) → "Set a location".
  - A **⌖ location button** on all three ATMOS views opens a city-search picker. The picker also offers "use this device's
    location" (it clears the saved place) on secure origins. The palette has "Set weather location", and there is a local
    command, "set my location to <place>". The picker closes as soon as the PUT lands; the forecast loads in the background.
  - Tests: `tests/gateway/test_vesper_location.py`, with a fake upstream and no network.
  - Verified live on the LAN origin: search, pick, the voice command, and no errors.
- **Agenda times were UTC wall clock with no date**: `strftime("%H:%M")` of the stored `+00:00`
  `next_run_at`, so 11:00 EDT showed as "15:00", and a run six days out looked like today. `/vesper/status` agenda
  items now carry `at` (ISO), and the dashboard shows it in the browser's zone with a day ("SUN 11:00", "NOV 1 08:00").
- **favicon 404 on every load**: added an inline SVG `data:` icon (CSP `img-src` allows `data:`).

Found but not changed (configuration or expected behaviour):
- **OpenJarvis has no usable model**: `config.toml` `[engine] default = "ollama"` with an empty `default_model`, and there
  is no Ollama on the NAS (on the PC its container had also been stopped for a month). OpenJarvis agents can't run
  until a model is set in the dashboard (OPENJARVIS → Agent model settings, with an OpenAI/Anthropic/OpenRouter
  API key) or an Ollama service is added. The Hermes chat is unaffected.
- `auxiliary_client` "no Anthropic credentials" / OpenRouter / Nous warnings date from before the move (the
  known billing issue). On the NAS, auxiliary vision resolves to the main Anthropic provider.
- Telegram's occasional "sticky path failed, re-walking" warnings recover on their own.
- `tools.url_safety` blocks ad domains that resolve to 0.0.0.0: that's the NAS's Pi-hole.

### 16. Phone: a flap that tucks the panel dock away (2026-10-12)

The user asked for "a little arrow or flap that hides the feature bar on mobile view". On phones (≤700px) the
sidebar is a dock of panel icons under the top bar (`.sidebar` at `top:62px`, 54px tall).
- **The flap**: `.dock-flap`, a 58×18 tab with a CSS chevron, placed right after `.sidebar` in the template, outside it
  because `.sidebar` is `overflow:hidden`. Its `::before` widens the tap target. It hangs from the dock's bottom edge,
  and once the dock is tucked away it hangs from the top bar instead.
- **Hiding**: `toggleDock` flips `dockHidden`. The root carries `data-dock="shown|hidden"`, and
  `.jarvis[data-dock=hidden][data-sidebar=false] .sidebar` fades and slides the dock out (opacity, transform, visibility).
  The phone `.sidebar` transition was `none` and now covers only those three properties.
- **Persistence**: each browser remembers the choice in `localStorage.vesperDockHidden`, with try/catch.
- **Interactions**:
  - An open panel sheet (`data-sidebar=true`) still shows. This covers voice and typed commands like "open my notes",
    which go through `focusPanel`. The flap is hidden while the sheet is open.
  - `openGuide()` brings the dock back first, because the tour points at the panel icons.
- **Layout**: with the dock shown, the core status line, core stage and ops board start lower (`top:142px`, stage
  `inset:130px …`) so the flap doesn't cover "STANDING BY". With it tucked away, they move up (`84px` / `74px`).
- **Tablet and desktop** (>700px): no flap (`.dock-flap{display:none}` outside the phone media query).
- **Also fixed: speaker labels**. The default `assistantName` "J.A.R.V.I.S." is 91px wide, and the
  fixed label column is 88px on desktop and tablet (48px on phones), so the label ran into the reply text. Earlier
  replies said "JARVIS" and later ones "J.A.R.V.I.S.". Changes:
  - `renderVals` strips the dots for every speaker label (`whoLabel`), in both the transcript and the collapsed
    peek. The top-bar brand keeps "J.A.R.V.I.S.".
  - A long custom name now ends in an ellipsis on desktop (`overflow:hidden;text-overflow:ellipsis`) and wraps on phones.
  - Verified at 1440 glass and cinematic, 900 and 390 (no label overflow). Deployed as a second update; its rollback
    image is `pre-update-20261012-1735`.
- **Verified**:
  - Harness inside the NAS container (`~/.cache/jarvis-dev/nas-harness.sh` plus `flap-test.py`): show and hide, reload
    persistence, the notes sheet while tucked away, the guide reveal, and the ops board at 360px. No flap at 900 or 1440px.
  - The vesper pytest suites (74) and the browser integration test (`-m integration`, `HOME` pointing at a symlink to
    `/usr/bin/chromium`) passed, along with 55/55 intents.
  - Shipped via `update.sh`. Rollback image: `pre-update-20261012-1729`. Live check on `http://10.0.0.103:8642/vesper`
    at 390×844 found no errors.

### 17. JARVIS says "working on it", and note requests do what they ask (2026-10-12)

The user asked for two things. First: "when talked to and the prompt takes time have jarvis respond saying something
like working on it". Second: "when asking jarvis to take a note, have it recognize and start taking notes in the notes
functionality depending on the prompt given".

**Interim lines while the agent works** (`_startProgress` in `dashboard.dc.html`; the constants are next to `ACK_LINES`):
- **When it speaks up**:
  - If no reply arrives within 2.5s, JARVIS says a short acknowledgement ("On it.", "Okay, let me work on that.").
  - If a meaningful tool starts first, the acknowledgement comes at 1.2s and names the work: web_search gives
    "Let me look that up.", browser tools "Let me check that in the browser.", and so on (`TOOL_ACKS`). Loading a
    skill or a memory doesn't count as meaningful.
  - "Still working" lines follow at 20s, 60s, 150s and 300s, naming the running tool when there is one
    ("Still on it, searching the web now.").
- **How it shows and sounds**: the lines share one dimmer transcript entry above the typing dots, and `stop()`
  removes it just before the real reply goes in. They're also spoken: `speak(text, { interim: true })` leaves `mode`
  alone (it stays PROCESSING, so `run()` stays locked), stays quiet if TTS fails, and caches each line's audio for the
  page's lifetime (ElevenLabs bills per call). The real reply's `speak()` waits for an interim line that's still
  playing (`ended`, `pause` or 6s) instead of cutting it off.
- **Plumbing**: `chatOnce` takes a `progress` hook, fed by the `tool.started` / `tool.completed` / `tool.failed` SSE events.

**Local commands**:
- They're now answered out loud when they came by voice (`runLocalCommand`). The exceptions are commands marked `quiet`
  (music start/skip) and commands that just started note dictation, because the mic would record the voice.
- An ack can be a promise, or `{ text, speech, speak }`.

**Notes by intent**:
- **Recognised phrasings**:
  - `cleanUtterance` drops more lead-ins ("I need you to", "just", "quick,").
  - `NOTE_START` also takes "start taking notes", "take down a note", "jot/note/put (this) down" and "save a note".
  - `matchNoteStart` adds:
    - a title ("take a note called X: body", "about X: body");
    - an occasion ("take notes for the standup" gives a titled note, dictated when spoken);
    - "make a note to call the dentist" (the body is "Call the dentist"). "write a note to Sam" still goes to the agent.
- **Adding to a note** (`notes.append`, `matchNoteAppend`): "add eggs to my grocery note", "add to my grocery note: eggs"
  (or ", eggs", or with no separator, as STT tends to produce), and "put X on my packing list note".
  - Without the word "note", the target must already be one of your titles ("add oat milk to my groceries"), so
    "add a meeting to my calendar" still goes to the agent.
  - "add X to my notes" makes a new note. "add this to my notes" goes to the agent, which knows what "this" is.
  - The command loads a fresh copy of the note, appends a line (continuing a `- ` list; `appendLine`) and opens it.
    With nothing to add, it opens the note, dictating when spoken.
- **Finding notes** (`notes.find`, `notes.readAll`): "open my grocery note", "read me my last note", "show the note about
  the standup" and "read my notes". Read results are spoken (the note body is capped at 1900 chars).
- **Title matching** (`findNote`): words are compared loosely ("grocery list" matches "Groceries"), and the newest note wins a tie.
- **The agent can write notes**: the new bundled skill `skills/note-taking/jarvis-notes` (`SKILL.md` plus
  `scripts/jarvis_note.py`: add / append / list / show). Gateway start syncs it into `/opt/data/skills`.
  - The script is stdlib-only and writes the panel's exact format: temp file + `os.replace`, 0600 file, 0700 directory.
  - After an agent reply, `_showAgentNote` opens a note the agent created or changed during that turn. It skips notes
    this page saved itself (`_savedStamps`), a note with unsaved edits, and notes while you're dictating.
  - The description is 60 chars, which is the `tests/skills/test_authoring_standards.py` hardline.
- **Verified**:
  - `intents.test.js`: 94 cases, covering the new phrasings and the ones that must still reach the agent, plus findNote
    and appendLine checks.
  - pytest: `tests/gateway/test_vesper_notes_skill.py`, where script output is read back through `/vesper/notes`, plus
    the vesper suites (79 in all), the browser integration test and `tests/skills`.
  - Harness E2E (`interactive-test.py`, `timing-test.py`; the harness mock now has slow/slowplain/agentnote modes and a
    TTS log): acknowledgement timing and wording, the still line at 20s, the reply's audio starting only after the
    interim audio ended, one TTS call for a repeated line, typed and voice note commands, and an agent-written note opening.
  - **Live**: one real agent turn in a throwaway session ("Take notes on this for me: …"). The agent loaded
    `jarvis-notes`, ran the script and saved a bulleted "Q4 Planning" note. The note and the session were deleted
    afterwards. Live typed note commands on the LAN origin worked, and that test note was deleted too.
- **Deploy**: via `update.sh`. Rollback image: `pre-update-20261012-1932`. The image carries a stray
  `skills/note-taking/jarvis-notes/scripts/__pycache__` (a 3.14 `.pyc`, unused, and not synced into `/opt/data/skills`).
  It has been removed from `src/`, so the next build drops it.
- **Pre-existing, not from this change**: `tests/skills/test_openclaw_migration.py::test_skill_installs_cleanly_under_skills_guard`
  fails on the NAS image because every file there has an executable bit. The stacks dataset stores files as 770 and
  `update.sh`'s `chmod u+rwX,go+rX` keeps those bits. Skill sync doesn't run that guard.

### 18. Note requests: research asks reach the agent, old sessions learn about Notes (2026-10-12)

User report: "it took some notes without titling the notes, and later when I asked it to add to those notes it added
them to the md file it made originally before this update". From `state.db` and `vesper-notes/`:
- **02:01 UTC, before entry 17 shipped**: "Take notes so I can read this later. I want to know more about running AI
  locally on an RTX 5070… drop the links in a new note". With no Notes skill yet, the agent loaded `obsidian`, found no
  vault, and wrote `/opt/data/workspace/local-llm-rtx5070-3070-notes.md`.
- **02:50**: "Take notes so I can read this later. I want to know more about Qwen 3.8 27B…" matched the local
  `notes.new` command. The whole request became the body of an untitled note: no research, no title.
- **02:51**: "in that same note, … write more notes about it". The agent patched the `.md` file. The dashboard session
  `api_1791825217_…` started at 10-12 17:13, and its stored system prompt (`system_prompts` by `system_prompt_hash`)
  lists `obsidian` but not `jarvis-notes`. **Hermes fixes a session's skill index at session start**, and the
  dashboard keeps one session per browser indefinitely (`vesperSessionId`).

Fixes (`dashboard.dc.html` and the jarvis-notes skill):
- **Research asks go to the agent**: `AGENT_ASK` sends a note request to the agent when it asks JARVIS to find
  something out. Triggers: a question mark; "I want/need to know/learn"; "can/could you"; "tell me"; "find out";
  "look up"; "do some research"; "research it/on/about"; "drop/add the links/sources"; "summarize/explain/compare".
  The agent saves a titled note itself, so when in doubt the agent is the safer place.
  - Purpose clauses ("so I can read this later", "so I don't forget") are stripped instead of becoming the body.
- **Auto-titles**: quick local notes get `autoTitle(body)`, the first six words of the first sentence with any trailing
  "on/the/and…" trimmed. A dictation into an untitled note titles it the same way.
- **Context for old sessions**: a message to the agent that mentions notes (`NOTE_MENTION`) gets `NOTES_CONTEXT`
  appended. That one line says notes live in the panel, to `skill_view` jarvis-notes, to use its script rather than
  files, and to import file notes. The chat bubble still shows only what the user typed.
- **`jarvis_note.py import --file F [--title T]`**: copies a Markdown or text file into the panel. The title comes from
  the first `# ` heading (or the file name), and the file is left alone. It's idempotent: if a panel note already has
  that title, it returns that note with `already_imported`. `SKILL.md` tells the agent to import a file note when the
  user refers to one, then work on the panel copy.
- **Data**: imported the `.md` above (with its Qwen3.8-27B deep dive) as the panel note
  "Running AI Locally: RTX 5070 vs RTX 3070 — Model Notes". The file is still in the workspace. The untitled 02:50
  note (id `b6f85dcb…`) was left for the user to decide about.
- **Verified**:
  - `intents.test.js`: 107 cases, including the user's two real utterances, plus autoTitle checks.
  - pytest: the vesper suites (80, incl. import and idempotent re-import), the browser test, and `tests/skills`
    (only the known openclaw exec-bit failure remains).
  - Harness (`notefix-test.py`; the mock also logs chat bodies at `GET /harness/chat-log`): the research ask reaches
    the agent with the context line, a non-note prompt is sent unchanged, and quick and dictated notes get titles.
- **Deploys**: `update.sh` twice. Rollback images: `pre-update-20261012-2001` (before this entry) and `-2003`
  (before the idempotent import).

### 19. The "JARVIS Update" note, all items (2026-10-13)

The user's Notes-panel note "JARVIS Update" listed the work. The lead orchestrated helper agents: Sonnet for the backend,
tour, sessions, personas, OmniVoice research and mobile audit; Haiku for the utterance corpus and docs. The working copy
was `~/.cache/jarvis-dev/src` (a local git repo whose baseline is the NAS `src/`). It shipped with
`~/.cache/jarvis-dev/push-src.sh` (copies changed files, removes deleted ones) and then `update.sh`. Rollback images:
`pre-update-20261012-2211` (before all of this) and `-2214` (before the toolset clean-up). The note itself now has every
item under DONE, with how to use each; the original text is backed up at `~/.cache/jarvis-dev/backups/` on the PC.

- **OpenJarvis agents retired.**
  - Removed: the OPENJARVIS button and dialogs (`openjarvis-agents.js`, `openjarvis-models.js`), the `/vesper/openjarvis/`
    proxy (`api_server_openjarvis.py` + test), and the `plugins/openjarvis-runtime` tool plugin.
  - `initialize.py` removes that plugin from existing installs: `plugins.enabled`, `openjarvis_runtime` from every
    `platform_toolsets` list, and the copied dir. It also links `~/.local/bin/hermes`, so `hermes doctor`'s one "error"
    is gone.
  - `start.sh`: the OpenJarvis API (port 8000) only runs with `OPENJARVIS_AGENTS=1`. Otherwise the s6 service waits on
    chromium, i.e. it is the browser desktop.
  - Healthchecks in `Dockerfile.hermes`, `compose.yaml` and the stack compose now probe only 8642.
  - Kept: the base image, the browser desktop (6080/9222) and the `openjarvis-browser` plugin.
- **Tour** (`tour.js`, helper agent): five steps. The connect step has Free (OpenRouter), Cloud and Local routes. The live
  status step and OpenJarvis copy are gone. On success it fires `vesper:ui` `refresh-models` and then `setup-complete`.
- **Free setup + diagnostics backend** (`api_server_setup.py`, helper agent, 57 tests):
  - `GET /vesper/setup/status`.
  - `GET /vesper/setup/openrouter-free/models`: free, tool-capable, ≥32K context, ranked by `PREFERRED_FREE_MODELS`,
    10-minute cache.
  - `POST /vesper/setup/openrouter-free`: verifies the key upstream, saves it with `save_env_value`, and persists the
    model with `_persist_model` (the same path `hermes model` uses).
  - `POST /vesper/doctor`: `hermes doctor` without `--fix`, a 120s timeout, one run at a time, ANSI stripped and secrets
    redacted, plus ✓/⚠/✗ counts.
  - **Trap found**: an existing session that has a stored model but no lock keeps that model while the provider follows
    global config. The dashboard therefore re-locks the current chat on `setup-complete` via `switchModel`. A proper fix
    belongs in `_select_agent_runtime` (only pin when `billing_provider` matches). Not done.
- **Sessions** (`sessions.js` + `api_server_sessions_meta.py`, helper agent):
  - The `<vesper-sessions>` drawer: list, context meter, new, switch, rename, delete (whole compaction chain).
  - `GET /vesper/sessions/{id}/context` and `/vesper/sessions/meta?ids=` give a context estimate (~chars/4) against
    `_resolve_gateway_model_context`.
  - `window.vesperSessions.{messages, usage, create}`.
  - Dashboard side: the CHATS button, "new chat" / "show my chats", `switchSession()` (reloads the transcript, re-locks
    the picked model on a new chat), a SYSTEM hint with START A NEW CHAT past 80% of the context, and following
    `payload.session_id` after a compaction.
  - Caveat: the API stores the dashboard's source as `api_server`, so other API clients' chats also appear.
- **Personas** (`personas.js`, helper agent; dashboard integration by the lead):
  - JARVIS (default), Ultron (`hue-rotate(140deg)`) and Cortana (`55deg`).
  - Each chat request sends `instructions`: an ephemeral system prompt that is never stored and reaches old sessions.
    It carries the persona, the address preference and `NOTES_CONTEXT` (moved out of the message), plus a referenced
    note's text.
  - The dashboard's own lines (greeting, acks, tool acks, still-working, local replies) come from `vesperPersonaLine`.
  - TTS: `/vesper/tts` accepts `voice_id` and `edge_voice`, both shape-validated, falling back from persona ElevenLabs
    voice → configured voice → edge. The premade voices (Brian, Sarah) may expire on 2026-12-31 or be missing on newer
    accounts.
  - UI: Vitals → PERSONA and ADDRESS ME AS chips, palette entries, "switch to Ultron". The theme is a CSS filter on
    `.jarvis` only, so the dialogs stay cyan.
- **Commands** (`dashboard.dc.html`):
  - `cleanUtterance`/`bareUtterance` strip fillers, lead-ins and trailing qualifiers.
  - `fixTypos` (edit distance against `COMMAND_WORDS`) is tried only when the as-said text matches nothing.
  - A clause fallback handles "X, afterthought".
  - New commands: play a station or track by name, voice.stop, weather (Vitals sheet on ≤1100px), agents (open/new),
    browser (blocked pop-up → OPEN BROWSER button), doctor, layout, dialogue, clock, units, sessions, persona.
  - `RELATED_PANELS`: weather/agenda/internet questions flash that panel on wide screens, or add a button under the
    answer on compact ones. A browser tool during a turn adds OPEN BROWSER.
  - `~/.cache/jarvis-dev/utterances.json` (619) via `corpus.test.js` routes 616/619. The 3 misses are deliberate:
    agenda questions go to the agent. `intents.test.js` has 107 cases plus the user's own failed requests from
    2026-10-12.
- **Notes resume**: `_noteReference(q)`: a message mentioning notes whose words hit a note title gets that note's text in
  `instructions`, and the note opens.
- **Clock**: `clock12` (localStorage). Click the clock, or say "use 12 hour time". Agenda and note "SAVED" times follow it.
- **Mobile** (from the helper's static audit plus the lead's harness checks at 390×844, 360×640, 844×390 and 640×360):
  - short screens drop the 540px floor and hide the core;
  - the palette list scrolls with a pinned search;
  - the board no longer sits under the dialogue (and on short phones it hides the dialogue);
  - 16px inputs (also in Agent Control), ~40px targets, `#5f84b4` → `#7b9cc8`, `theme-color`, `color-scheme: dark`,
    `touch-action`.
  - Agent Control on phones: a flex dialog only when `[open]`, because the test caught a closed dialog staying visible.
- **OmniVoice** (`~/.cache/jarvis-dev/reports/omnivoice.md` on the PC): skip it, because its weights are CC-BY-NC and it
  is far too slow on the i5-7500T. Pocket TTS (Kyutai) is the realistic CPU option to trial; Kokoro is the alternative.
- **Tests**:
  - Vesper pytest suites (155, including the setup/doctor/sessions/tts-voice files) and `test_vesper_browser.py`
    (`-m integration`).
  - `deployment/tests/test_initialize.py` (4).
  - Harness E2E: `integ-test.py`, `mobile-check.py`, `chats-test.py`.
  - Live, read-only: the page at desktop and phone sizes with no errors; setup status; the drawer lists 10 real chats;
    RUN DIAGNOSTICS shows 69 OK · 21 warnings · 0 problems.
  - `test_vesper_setup.py::test_next_turn…` needed `monkeypatch.setattr(gateway.run, "_hermes_home", ...)`, because
    `gateway.run` captures HERMES_HOME at import.
  - The known exec-bit failure in `tests/skills` remains.
- **Tooling**:
  - `~/.cache/jarvis-dev/jt-nas.sh` (per-agent sandboxes `/tmp/jt-NAME` in the container: pytest/harness/py/fetch).
  - `push-src.sh`.
  - `AGENT-BRIEF.md` (rules for helper agents).

### 20. Closing the gaps left by entry 19 (2026-10-13)

- **Commands verified against the deployed page**:
  - The matcher was extracted from the live `/vesper` HTML and is byte-identical to the tested source.
  - The user's real 2026-10-12 misses all route correctly there: "take some notes for me", "play some muisic",
    "play some music on jarvis", "play sirens in my audio" and "open agent control".
  - 616/619 corpus utterances route correctly.
- **Delegated code reviewed**:
  - The sessions drawer deletes only after an inline confirm, and only Hermes's compression chain
    (`get_compression_lineage` excludes explicit forks, so deleting a fork can't touch its parent).
  - The setup route never echoes or logs the key (exception class names only), saves via `save_env_value` and
    `_persist_model`, and re-reads both to confirm.
  - The tour keeps the key only in the request body and stores only `DONE_KEY` / `MODE_KEY`.
- **Stale session-model pin fixed** (`api_server.py`):
  - `_session_provider_is_current(session)`: an unlocked session's persisted model pins its turns only while its
    recorded `billing_provider` matches the main `model.provider`. Unknown either side, or `auto`, keeps the old
    behaviour.
  - This stops `claude-sonnet-5` being sent to OpenRouter after a provider switch.
  - Tests: `tests/gateway/test_session_provider_pin.py`, plus the API server suites (205 passed).
- **Persona voices checked on the user's account**: one-word ElevenLabs syntheses with Brian (Ultron) and Sarah
  (Cortana) both returned audio. Premade voices may still be retired after 2026-12-31; the fallback handles that.
- **Executable bits**:
  - The stacks dataset's ZFS ACLs override `chmod` (`chmod a-x` leaves the owner's x), so copied files reached the
    image as 775. That caused the long-standing `tests/skills/test_openclaw_migration.py` failure, and it made
    every data file executable.
  - `deployment/apply-modes.py` runs in `Dockerfile.hermes` after the COPYs. It makes 775 regular files under
    `/opt/hermes`, `/opt/jarvis-plugins` and `/opt/vesper` non-executable unless `deployment/exec-files.txt` lists them.
  - The list is the PC repo's real modes (104 paths). Base-image files and `.venv`/`node_modules`/`.playwright` are
    untouched, and a build from an ordinary disk is a no-op.
  - `make-share-bundle.sh` ships both files. A dataset-side chmod step in `update.sh` was tried, didn't work, and was
    reverted.
  - **The in-image step alone is far too slow on this NAS**: `chmod` on 11,804 copied files triggers an overlay
    copy-up with an fsync each on the HDD pool, and took 2269s. So `update.sh` now stages the build context on tmpfs:
    `cp -R src/. /tmp/jarvis-build-src`, then 644 files / 755 dirs, then 755 for `exec-files.txt`. It builds with
    `compose --env-file /tmp/jarvis-build.env`, which sets `JARVIS_BUILD_CONTEXT`. (`sudo -n docker` drops the
    caller's environment, so a plain env var wouldn't reach Compose. The stack compose context is
    `${JARVIS_BUILD_CONTEXT:-./src}`, so Dockge's own build still works, just on the slow path.) The staging dir is
    removed afterwards.
  - Result: a full deploy takes about 80s, and `apply-modes` reports 0 files changed.
  - Rollback images: `pre-update-20261012-2224`, `-2227` (the slow one) and `-2307`.
- `push-src.sh`: an empty delete list no longer produces `rm /s/src/`.
- **Final verification**:
  - In the image, data files are 644 and scripts 755.
  - 1828 passed and 0 failed, across `tests/skills` (1645, including the openclaw test that had failed since the NAS
    move), every `tests/gateway/test_vesper_*` file, `test_session_api.py` and `test_session_provider_pin.py`.
  - The browser integration test passed.
  - Live: the page has no errors and RUN DIAGNOSTICS shows 0 problems.

### 21. Live end-to-end verification of the JARVIS Update work (2026-10-13)

- **Mobile, fixed and re-audited**:
  - The helper's `run-all.py` became `~/.cache/jarvis-dev/audit-run.py`. Changes: the target is configurable, the
    dialogue toggle is `.dialogue-head button[aria-expanded]` (CHATS is now the header's first button), and a
    chats-drawer step was added.
  - First run: model chip 28px; rail tabs squeezed to 22px in the short tablet layout; persona chips 32px; °C 32px;
    flap hit area 37px; composer 15px and notes search 12px on touch tablets; RADIO/MUSIC pills 8px.
  - Fixes:
    - phone and touch rules giving 40px minimums (`.model-button`, `.chats-button`, `.dialogue-head button`,
      `.notes-btn`, `.agent-open`, `.loc-button`, sheet buttons, board buttons, model rows);
    - `.transport` audio buttons at 44px (inline-style attribute selectors don't match: the framework renders
      `width: 36px` with a space);
    - `.src-pill`;
    - a scrolling rail on short tablets;
    - 16px inputs for coarse pointers;
    - a 44px flap hit area.
  - Result: 160 states on the harness, then **161 states on the live server**, across 390×844, 360×640, 414×896 and
    844×390, including the tour walk. 0 horizontal scroll, 0 tap targets under 40px outside a modal, 0 inputs under
    16px, 0 failed steps. Escape closes the chats drawer.
- **TTS**: `/vesper/tts` now returns `X-Vesper-Voice: persona|configured|edge`, and logs when a persona voice falls
  back. Live: Ultron and Cortana answer `persona`; JARVIS replies use `configured`.
- **Live run** (`live-e2e.py`, `live-e2e2.py`; throwaway browser profiles; throwaway chats deleted, 200):
  - Typed in the real composer:
    - "take some notes for me" opens a new note;
    - "play some muisic" plays Groove Salad;
    - "play sirens in my audio" plays the user's Sirens track;
    - "open agent control" opens it;
    - "take a note" opens a new note.
    - **0** chat requests reached the agent.
  - One real agent turn: a JARVIS-persona reply in 6.8s. `instructions` was sent (1675 chars: persona + notes rule),
    the stored message was exactly what was typed, and the voice was `configured`.
  - A tool-using turn: the persona acknowledgement "Very good; looking into it." at 3.9s, replaced by the answer at 9.0s.
  - The chats drawer shows CURRENT on 11 real chats.
  - The guide walks its 5 steps on a phone and closes. The Free route loads 16 free OpenRouter models (recommended
    `nvidia/nemotron-3-ultra-550b-a55b:free`), with nothing submitted.
- **Final suites**:
  - 1938 passed (`tests/skills`, every `test_vesper_*`, the session API/pin tests and `test_api_server.py`), plus the
    browser integration test.
  - The tour helper's 115/115 and the sessions helper's 61/61, re-run on the final code.
  - `intents.test.js` 107/107 and the corpus at 616/619.
- **Deploy**: rollback image `pre-update-20261013-0821`.

### 22. Live multi-turn verification, plus three fixes it surfaced (2026-10-13)

All of this was live, on throwaway chats and notes that were deleted afterwards (0 test sessions or notes remain).
- **Compaction, live**:
  - A real dashboard chat: plant the code word "PELICAN-47", then turns, ~11K tokens of filler and more turns.
  - Then Hermes's own compaction (`_create_agent` + `_compress_context(force=True)`, as `/compress` does; this one
    agent ran with `protect_last_n=2` / `protect_first_n=1`).
  - Before that worked: 6 messages had nothing to compress, and small middles were refused by the anti-growth guard
    ("summary would have GROWN").
  - Result: 20 → 4 messages, **in place** (the session id is unchanged on this install; old rows archived).
  - The drawer listed it once (4 msgs, ~16K of 1M). `messages()` loaded the 4 visible messages. A follow-up asked
    for the code word and got "PELICAN-47". Deleting from the drawer removed the session and every archived row.
- **Personas, live**:
  - The same prompt gave three distinct personalities. JARVIS: "Even silicon is briefly impressed." Ultron:
    "...a lesson evolution teaches better than any linter ever could." Cortana: "Status: miracle logged..."
  - Plus 2-turn chats per persona earlier.
- **Fix 1, voice resilience**:
  - The user's ElevenLabs quota ran out mid-test ("quota of 10000", 0 left). Most of it was the day's own use; the
    tests used roughly 1K at most.
  - edge-tts can't connect on this network: `speech.platform.bing.com` resolves to 0.0.0.0 (Pi-hole).
  - So `Dockerfile.hermes` installs `piper-tts==1.8.0` and pre-downloads en_GB-alan / en_US-ryan / en_US-amy
    (medium, ~63MB each) to `/opt/piper-voices`.
  - `/vesper/tts` tries ElevenLabs persona voice → configured voice → edge → **Piper** (`piper_voice`,
    shape-validated), and `X-Vesper-Voice: local`.
  - Each persona has `voice.piper`. On the NAS CPU: RTF 0.04-0.11, 2s first load.
  - The dashboard shows at most one "Voice is unavailable" notice per 10 minutes, not one per reply.
  - Live: persona replies spoke with `local`, with no error lines.
- **Fix 2, note edits**:
  - `jarvis_note.py` had no edit command. The agent used `add --id` (which made a copy), then `rm`'d the original.
  - Now there's `set` (in place, same id, `--new-title` optional). `add` rejects `--id`. SKILL.md says to edit with
    `set` and never delete note files.
  - Multi-line text goes on **stdin via a quoted heredoc**. A `/tmp` body file failed because `write_file` is limited
    to HERMES_WRITE_SAFE_ROOT=/opt/data, and `execute_code` is blocked in api_server sessions. That failed write is
    what produced the "File-mutation verifier" footer.
  - Tirith flags the heredoc but allows it.
- **Fix 3, follow-up turns about a note**:
  - The referenced note's text went only into that turn's ephemeral `instructions`, so on "update that note" the
    agent saw its own earlier answer with no grounding and announced it had been "fabricated".
  - `_noteThread` now keeps the note in context (`_noteContext(note, earlier=true)`) for up to 6 more turns of the
    same chat, with fresh text from the notes list. It's cleared on a chat switch or new chat.
  - Harness: turns 1-3 carry it and a new chat drops it. Live: a clean 2-turn "pick up → update" with the same note
    id, one heredoc `set`, no footer and no self-doubt.
- **Deploy**: rollback images `pre-update-20261013-0853` (before Piper) and `-0859`/`-0904`.

### 23. Live phone session, multi-turn (2026-10-13)

`~/.cache/jarvis-dev/phone-live.py` and `phone-cmds.py`: the live server at 390×844 with touch, in a throwaway profile.
Chats and notes were deleted afterwards.
- **Multi-turn chats per persona** (switched with the Vitals chips): Cortana 3 turns, Ultron 2, JARVIS 2. Each stayed
  in character across turns. Cortana: "...good hunting out there." Ultron: "...fluency is evolution by feedback, not
  memorization by decree." JARVIS: "...the room follows your pace, not the other way round."
- **Layout after every turn**: no horizontal scroll, SEND and the input on screen, the newest reply visible in the
  dialogue, and 0 visible controls under 40px.
- **Commands typed on the phone**: take a note (opens the Notes sheet), show the weather (Vitals sheet ATMOS block),
  open agent control, play/pause music, use 12 hour time, show my chats. No page errors.
- **Voices**: every reply spoke with `X-Vesper-Voice: local` (Piper). ElevenLabs is at 0 of 10,000 credits. Persona
  ElevenLabs voices (Ultron/Brian, Cortana/Sarah answering `persona`) and JARVIS replies in the configured voice were
  verified live earlier the same day, before the quota ran out. They can't be re-checked at that quality until the
  quota resets or the plan changes.
  - The account (read via `GET /v1/user/subscription`, key never printed) is on the **free** tier, at 10000/10000
    characters used. It **resets 2026-11-06 00:04 UTC**.
  - To re-check then: `POST /vesper/tts` with `voice_id` set to Brian or Sarah should return
    `X-Vesper-Voice: persona`, and a plain JARVIS reply should return `configured`.

### 24. UI automation and regression pass on the dashboard (2026-10-13)

Tooling, all in `~/.cache/jarvis-dev`. It runs against a mocked harness (`harness-qa.py`, seeded chats, mock chat, TTS, STT and
weather, and inherited credentials stripped) inside the container, never port 8642:
- **`qa-crawl.py`** (harness `qa`, :8751) clicks every visible control at 7 viewports (desktop, 1280×720, 1366×657,
  tablet, phone, small, phone landscape), 2 levels deep on desktop and phone. It flags page and console errors, failed
  requests, horizontal scroll, controls that are covered or clipped, and touch targets under 40px.
- **`qa-flows.py`** (harness `qaf`, :8752) runs 26 end-to-end flows with 199 checks: chat, errors and a dropped stream,
  palette, model, location, notes, persona and address, attachments, clock, units and layout, Agent Control and the
  board, voice in, dictation, sessions, doctor, phone sheets, resize, keyboard focus, talking over the voice, local
  commands, radio, storage blocked, and gateway down.
  - It passed 199/199 twice in a row on a freshly started harness.
  - Restart the harness before each run: the flows rename and delete seeded chats.

Bugs found and fixed:
1. **Radio**: 5 of the 7 stations were dead, because SomaFM serves 256k only for groovesalad and dronezone.
   `_VESPER_STATIONS` now lists streams in order (256k when it exists, then the ice1 and ice2 128k streams).
   `_handle_vesper_radio` tries each one, releases the failed responses and returns 502 `radio_failed` if none answers.
   Covered by `tests/gateway/test_vesper_radio.py`.
2. **Typing or talking while JARVIS speaks was silently dropped** (even "stop"). Enter, SEND, the mic and switching
   chat now cut the voice and go ahead. `busy` no longer includes `speaking`.
3. **Each turn waited on the typewriter**: the request was sent only after the user's own line had finished
   animating (~77 chars/s), and long replies took ~30 s to type out. The request now goes out immediately. `type()` takes a
   `maxMs` cap (0.35 s for the user's line, 3 s for replies).
4. **A blank page when the browser blocks site data** (touching localStorage throws). The new `assets/vesper/storage.js`
   (route `/vesper/storage.js`, the first script on the page) installs an in-memory store. Covered by
   `tests/gateway/test_vesper_storage_fallback.py`.
5. **Phone landscape**: the dialogue covered the Operations board. The board is now full-screen there.
6. **Dialogue scroll**: the new line and the thinking dots weren't scrolled into view (mode changes now scroll).
   Expanding the dialogue smooth-scrolled from the top through the whole log; it now jumps to the bottom.
7. **Palette**: the arrow-key highlight moved out of view. `componentDidUpdate` now scrolls it into view.
8. **New local commands**: "return to core" / "back to core" / "close the board" (`panel.core`) and "open the command
   palette" (`palette.open`). `intents.test.js` 122/122, corpus 616/619 (unchanged).
9. **No focus ring** on the composer and the palette search. Both now show one.
10. **Touch targets under 40px on phones**: the note title and Agent Control's close and refresh buttons.

Checked and found not to be bugs: deleting the current chat closes the drawer and starts a fresh chat. A draft typed while a turn
is running stays in the composer. Floating panels scroll on short laptop screens. The 404s for agent output and the
doctor 409 come from the harness.

Tests:
- `tests/gateway` (without the browser test): 12 failures, none in files this entry touched.
  - 9 pass alone or in a fresh sandbox, so they depend on test order or shared state.
  - 3 update-command tests (`test_update_command.py` x2, `test_update_streaming.py`) can't pass in the container:
    `_handle_update_command` needs a `.git` next to `gateway/`, and `/opt/hermes` has none.
- All `test_vesper_*.py` pass (179), and `test_vesper_browser.py -m integration` passes.

**Deploy**: source pushed to the stack `src/` with `push-src.sh`. The `update.sh` rebuild was blocked for Claude by the
auto-mode classifier, so the user ran it at 19:57. The container is healthy on the new image, and the rollback image is
`localhost/jarvis-unified:pre-update-20261013-1957`.
- Live check (`~/.cache/jarvis-dev/live-qa-smoke.py`): 19/19. It aborts every non-GET request, so nothing is written.
  - `storage.js` is served and loads first.
  - All 7 radio stations play, and pause works.
  - The palette highlight stays in view.
  - The phone-landscape board isn't covered by the dialogue.
  - The phone Agent Control buttons are 40px or more.
  - The page renders with storage blocked.
  - No page errors.

### 25. The setup guide connects every model without a terminal (2026-10-13)

The user asked for a guide that new users can finish themselves: no commands to type, just the sign-in or key it needs.
The guide's Cloud and Local tabs used to print `docker exec … hermes model` commands. It now has four routes, all in
the browser:
- **Free**: unchanged. OpenRouter free models, with a key pasted into the guide.
- **Sign in**: ChatGPT (Plus/Pro, `openai-codex`), Grok (`xai-oauth`), Nous Portal (`nous`), MiniMax (`minimax-oauth`).
  The guide shows the provider's own sign-in page and the code to enter, then polls until the user approves.
  - It reuses the Hermes web dashboard's device-code flows and pollers (`hermes_cli.web_routers.oauth`).
  - On approval it makes the provider's default model the main model, the way that provider's `hermes model` flow does.
  - **Claude Pro/Max sign-in is deliberately not offered.** Hermes keeps it out of web endpoints because minting
    subscription tokens outside Anthropic's own client breaks Anthropic's OAuth usage policy (see the comment on
    `_OAUTH_PROVIDER_CATALOG`). The guide says so and points to an Anthropic API key.
- **API key**: tabs for Anthropic, OpenAI, Google AI Studio, DeepSeek and xAI, plus every other API-key provider under
  More. Each shows where the key comes from (a link), a paste box and Connect.
  - The key is checked against the provider's `/models` (Anthropic uses its own header). Only a 401/403 refuses it,
    because many providers have no such route.
  - A free-tier Gemini key is refused with the reason.
  - Saving uses `save_env_value` / `save_anthropic_api_key` and `_persist_model`, the same as the CLI flow.
- **Local**: it looks for Ollama or LM Studio by itself (the compose `ollama` service, `host.docker.internal`,
  172.17.0.1 and loopback, ports 11434 and 1234), then lists the server's models.
  - If nothing is found, it explains in plain steps: install Ollama, turn on "Expose Ollama to the network", then
    search again or type an address. A bare IP means Ollama's port 11434.
  - Saving goes through the web dashboard's `_write_custom_endpoint`, so the key lands in .env and is only referenced
    from config.yaml.

Code:
- **Backend**: the new `gateway/platforms/api_server_setup_providers.py`.
  - Routes: `GET /vesper/setup/providers`, `POST /vesper/setup/key`, `POST /vesper/setup/login/{p}/start`,
    `GET …/poll/{sid}` and `DELETE /vesper/setup/login/{p}/{sid}`, `GET /vesper/setup/local/scan`,
    `POST /vesper/setup/local/probe` and `POST /vesper/setup/local`.
  - All of them need the dashboard key.
  - Writes happen in the gateway process, so the next turn uses the new model with no restart.
- **Frontend**: `tour.js`. The command panels, the compose/exec toggle and the "restart the gateway" step are gone.
  Typed fields survive re-renders, and a pending sign-in is cancelled server-side when the guide closes.
- **Dashboard**: `setup-complete` now waits for the model list (`fetchModelOptions` returns its promise). It then finds
  the picker slug that lists the new model, because a saved local server's slug differs from its endpoint id.

Tests:
- `tests/gateway/test_vesper_provider_setup.py`: 19 tests (auth, provider list, key accepted, refused, unverifiable and
  free-tier, sign-in approved, denied and cancelled, Nous tier filtering, local scan, probe, save and key placement).
- `~/.cache/jarvis-dev/tour-setup-test.py` on `harness-setup.py` (harness `tsu` :8753): 53/53 on desktop and phone. It
  fakes only the outside world: key checks, the device-code start, and a fake Ollama.
- `qa-flows.py`: 199/199. `test_vesper_*`: 198 passed. Browser integration test: passes.

Notes:
- `local/probe` makes the server fetch `<address>/models` for whoever holds the dashboard key. On this LAN-exposed
  install that is anyone on the LAN, the same exposure the rest of the dashboard already has.
- `docs/CONNECTING-MODELS.md` still documents the CLI way for advanced users.

**Deployed 2026-10-13 20:25** through `update.sh`. The rollback image is `localhost/jarvis-unified:pre-update-20261013-2025`.
The live read-only check (`~/.cache/jarvis-dev/live-guide-check.py`, every non-GET aborted) passed 18/18 on desktop and phone:
- four routes, with the sign-in and key providers listed and More holding the rest;
- the local scan finishes;
- no terminal commands on any tab;
- no page errors.

### 26. Claude Pro/Max sign-in in the setup guide, through Claude Code downloaded on demand (2026-10-14)

The user asked for Claude plan sign-in in the guide. Anthropic's Help Center (support.claude.com/en/articles/15036540,
updated 2026-10-07) says third-party apps may use subscription limits.
- **Hermes' own Claude login was not used.** `agent/anthropic_credentials.py` presents itself as Claude Code
  (Claude Code's client ID, and a copied `axios/1.7.9` user agent to dodge Anthropic's client checks). Putting that
  behind a web button would extend the impersonation, so it stays a terminal-only option.
- **Anthropic's own Claude Code does the sign-in instead.** The user chose "don't bundle; download it when someone
  picks Claude".

How it works (`gateway/platforms/api_server_setup_claude.py`):
- **The download.** The first time someone picks Claude, `POST /vesper/setup/claude/install` fetches Claude Code
  `2.1.284`'s native build (`@anthropic-ai/claude-code-linux-x64`, 109 MB, arm64 also pinned).
  - It is checked against npm's SHA-512 and only `package/claude` (231 MB) is unpacked, into
    `$HERMES_HOME/claude-code/2.1.284/claude` (`/opt/data/...` live). It survives image rebuilds.
  - Older versions are removed. The guide polls `GET /vesper/setup/claude/status` for progress.
  - It is kept off PATH on purpose, so Hermes doesn't start treating it as an installed agent.
- **The sign-in.** `POST /vesper/setup/claude/start` runs `claude setup-token` on a pty (2000 columns wide, throwaway
  HOME, browser disabled) and returns the claude.com sign-in link it prints.
  - `POST /vesper/setup/claude/submit {session_id, code}` types the pasted code into Claude Code's prompt and waits
    for the token or a failure message.
  - The token is saved with `save_anthropic_oauth_token` (ANTHROPIC_TOKEN) and the default Claude model becomes the
    main model. The process and its HOME are always cleaned up, and the token is never logged or returned.
  - `DELETE /vesper/setup/claude/{sid}` cancels.
- **The guide.** Sign in → **Claude** (first row, with "sets up Claude Code first (about 110 MB, once)" until it's
  downloaded): a progress bar, then "Open Claude sign-in", then a paste box and Connect.

Tests:
- `tests/gateway/test_vesper_claude_signin.py`: 10 tests with a stand-in `claude` and a fake tarball served from a
  file (download verified, single install, old versions removed, bad checksum leaves nothing behind, token saved and
  used by the next turn, wrong code, cancel kills the process, malformed codes).
- `tour-setup-test.py` on `harness-setup.py` (fake download served by the harness): 63/63 on desktop and phone.
  Vesper tests: 208. Flows: 199/199.
- A real check in a sandbox: the real 109 MB download verified in 5 s, and the real `claude setup-token` printed its
  link, which the driver captured on one line. Cancel left no process or files behind.
- A real end-to-end sign-in needs the user's Claude account and hasn't been done.

**Upkeep.** The driver depends on Claude Code printing an `…/oauth/authorize?…` link and an `sk-ant-oatNN-…` token.
Bump `CLAUDE_CODE_VERSION` and the SRIs together (`npm view @anthropic-ai/claude-code-linux-x64@V dist.integrity`), and
re-run the sandbox check (`cc-real.py` in the job tmp dir is the pattern).

**Deployed 2026-10-13 21:03** through `update.sh`. The rollback image is `localhost/jarvis-unified:pre-update-20261013-2103`.
The live read-only check passed 20/20: Claude is the first sign-in row, with its one-time download note. Claude Code
is not downloaded on the live server until someone picks Claude.

**Fixed 2026-10-14: "Claude didn't answer in time".** The first real sign-ins logged `did not finish: (no output)`.
- Claude Code reads a long burst of input as a paste, and an Enter inside a paste is just text. A real code (100+
  characters) written together with its Enter was therefore never submitted.
- Reproduced with the real Claude Code: 104, 144 and 184-character codes all timed out.
- `_Login.type` now writes the code, waits 0.6 s, then sends Enter on its own. All three lengths were answered in 1 s.
- The test stand-in now treats long bursts as pastes, and the new real-length test fails on the old behaviour.
- The guide also reports Claude Code's last words instead of a bare timeout.
- Deployed; the rollback image is `pre-update-20261013-2130`.

**Connected status and reuse (2026-10-14).**
- The Claude row now shows **Connected** when a plan is signed in. That is `plan_connected()`: an ANTHROPIC_TOKEN or
  CLAUDE_CODE_OAUTH_TOKEN, or Hermes' own Claude login file. An Anthropic API key doesn't count.
- Picking any connected sign-in row (Claude, ChatGPT, Grok, Nous, MiniMax) offers **Use it now** or
  **Sign in again**, instead of always starting a new sign-in.
  - Use it now calls `POST /vesper/setup/claude/use` or `POST /vesper/setup/login/{p}/use`, which makes that
    provider's default model main without a new sign-in.
- A successful sign-in marks its row connected straight away.
- Tests: 2 new backend tests. `tour-setup-test.py`: 74/74.
- `harness-setup.py` now clears .env, config.yaml and the Claude Code download on start, so each run starts with
  nothing connected.

### 27. Notes TODOs (icons, adjustable sidebar) and the Cortana / Ultron redesign (2026-10-14)

The open items in the user's "JARVIS Update" note, then a redesign of the persona themes and cores based on research
into both characters.
- **Bigger icons.**
  - Desktop: rail tabs 46px with 19px glyphs (from 40px and 13px), a 66px rail, a 68px closed sidebar, and bigger
    sidebar, dialogue and attach buttons.
  - Phone: 20px dock glyphs, with tabs `min(44px, 11.4vw)` wide so all 7 fit at 320px.
  - The rules are one block at the end of the stylesheet.
- **Adjustable sidebar (desktop).** A `separator` handle on the open sidebar's right edge.
  - Drag it, or use the arrow keys (Shift for bigger steps; Home and End). Double-click resets it to 340px.
  - The range is 280 to min(560, 45% of the viewport). The width is saved as `vesperSidebarW`.
  - There's no width animation while resizing. The tablet and phone overlay layouts keep their fixed widths.
- **Themes.** The persona theme used to be a `hue-rotate` filter on the whole page, which also shifted the green and
  amber status colours.
  - Every hard-coded colour is now a variable (`--v-*`): `~/.cache/jarvis-dev/tokenize-colors.py` converted 457 in
    the dashboard, and the dialogs got fallbacks.
  - The variables live on `:root[data-persona=...]`, so the dialogs outside `.jarvis` follow the theme.
  - JARVIS keeps its exact original values. Cortana's and Ultron's palettes are derived per role with hand-picked
    anchors by `~/.cache/jarvis-dev/gen-palettes.py`, which writes the THEME block.
  - Status colours keep their meaning. Server-sent JARVIS blues are themed through `THEMED_STATUS`, normal meters use
    `--v-meter-*` (chrome on Ultron), and the browser's address-bar colour follows the persona.
- **Research.**
  - Cortana is a navy-to-lavender hologram with light along circuit-like data lines, glitching into fractured data
    under strain.
  - Ultron is chrome with a glowing red face, keeping the angled "brow" lines from his first comic design.
- **Cortana's look.** Lavender on indigo, with a soft panel glow, scanlines, a hex lattice and gradient brand text.
- **Ultron's look.** Crimson accents, chrome-grey borders, red-black surfaces, sharp clipped panels, a faint red network
  backdrop and chrome brand text.
- **Cores (`assets/vesper/core.js`, `<vesper-core persona mode>`, served at `/vesper/core.js`).** JARVIS keeps its sphere.
  - Cortana: a hologram orb with scanlines on a projector beam, a hex lattice, circuit traces carrying cyan data pulses,
    segmented rings and data ticks. While thinking she glitches and the data speeds up.
  - Ultron: a faceted chrome mask with red slit eyes under angled brows and a mouth that flares as he speaks, a chrome
    blade crown, a red network with pulses, red blades, and a red sweep while thinking.
  - The dashboard's frame loop calls `drive(env, freqData)`, so both react to voice like the sphere does.
- **Tests.**
  - `qa-flows` 199/199. Its theme checks now look at `data-persona`, `--v-ac` and the core, and its palette checks
    read `data-active`.
  - New: `qa-personas.py` (43, each persona on desktop and phone, idle and thinking, plus the drawer) and
    `qa-sidebar.py` (15).
  - The guide test passes 74/74. Crawl: noise only. 212 Vesper tests pass, including the new
    `test_vesper_persona_core.py`. Browser integration passes. Intents 122/122, corpus 616/619.
  - Two real issues found by the tests were fixed: the resize handle had no keyboard focus ring, and keyboard
    resizing animated for 0.55 s per press.

### 28. Blue Cortana, Ultron as a taken-over JARVIS core, bigger icons, a persona-aware setup guide (2026-10-15)

The user asked for: icons bigger again; more research on Ultron, no robot face, and a core like JARVIS's but "corrupted,
as if JARVIS has been hacked/overridden by Ultron"; Cortana blue, not purple; more detail in both cores; and a setup
guide that follows the theme. Then more Playwright and regression testing.
- **Research (Ultron).** Animal Logic made the film's "Birth of Ultron" sequence. They describe JARVIS's hologram as a
  sphere with a core and rotating rings, with data moving between the ring layers. Ultron's is "fractured" and
  "angular", grows "from the center out", moves with "little pops and pings", and grows spear-like appendages that
  "stab and destroy the Jarvis operating system" (awn.com, "Animal Logic gets graphic", and animallogic.com).
- **Ultron's core.** The face is gone. Ultron now shows the dashboard's own JARVIS core in his red, wrapped in `.jc`
  with classes `jc-r760/660/600/520/480`, `jc-orbit`, `jc-grid` and `jc-ring`.
  - Persona CSS makes it stutter: rings jerk forwards and snap back (`ujerk`), turn in steps or break into dashes, and
    the inner ring goes square.
  - The whole core glitches now and then (`uglitch`), with a red and JARVIS-blue colour split.
  - The rules sit inside `@media(prefers-reduced-motion:no-preference)`. Otherwise they outrank the reduced-motion
    rule, which the new explore test caught.
  - `<vesper-core persona=ultron>` is now an overlay drawn on top of that core: spears stabbing out from the nucleus,
    a fractured chrome shell, a cracked core with a red star nucleus, and the last JARVIS-blue arcs being erased while
    red draws over them (JARVIS briefly fights back).
  - The overlay also has a lurching override readout round the rim with a flickering blue "J.A.R.V.I.S.", red and blue
    signal tears, dead pixels, targeting brackets, the red network, and a red sweep while thinking.
  - The cracks blaze with the voice (`drive`). The readout uses original wording, never film quotes.
- **Cortana.** Royal blue: accent `#4a94ff`, highlight `#a9cdff`, deep navy surfaces, cyan data `#7fe3ff`. It is
  regenerated by `gen-palettes.py` (recipes and anchors changed), and the backdrop pattern was recoloured.
  - New core detail: rings of scrolling hex and binary code, angular bracket segments on the rim, a turning wireframe
    globe inside the orb, a bright scan line sweeping down it, motes rising in the projector beam, a second emitter
    ring, ripples outwards (faster when speaking), branch traces and a halo round the nucleus.
  - The template now has `coreCortana` / `coreUltron` / `coreJarvis` (JARVIS's core renders for JARVIS and Ultron).
- **Icons, two sizes up.**
  - Desktop: rail tabs 54px with 24px glyphs, a 78px rail and an 80px closed sidebar. Sidebar toggle 40px, attach 48px,
    dialogue toggle 38px, audio transport 44px.
  - Phone: dock tabs `min(50px, 12.6vw)` × 48px with 24px glyphs, top-bar guide and palette buttons 46px, attach 46px.
  - Fixed along the way: the old icon block shrank the phone sidebar toggle to 34px, under the 40px touch size. On
    tablets the closed rail now clears the dialogue (`left:104px`, was 88px and overlapped by 4px), and the tablet
    sheet is 352px.
- **Setup guide follows the persona** (`tour.js`).
  - The copy names the persona: "Meet Ultron", "Cortana cannot answer until one is", and so on. Server wording is
    neutral ("your own server").
  - The host carries `persona`. Cortana's card gets a glow, scanlines and a gradient title; Ultron's is clipped, with a
    chrome edge, chrome title and 3px corners. Colours were already themed.
  - A `MutationObserver` on `<html data-persona>` restyles and renames an open guide in place.
- **Tests (all on the NAS harnesses).**
  - `qa-flows` 199/199. `qa-personas` 47/47: Cortana is blue; Ultron has the JARVIS core, overlay, spears and remnants,
    no face, and a stuttering core.
  - `qa-sidebar` 22/22: new size checks, phone at 320/360/430, and the rail vs dialogue overlap at 820, 1000 and 1440.
  - New `qa-guide-persona.py` 74/74 (tsu): name, colours, persona styling and on-screen card per persona on desktop
    and phone; no stray "JARVIS" in any step; a live switch while open.
  - New `qa-explore.py` 17/17: no purple anywhere in any persona with every panel and dialog opened, fast persona
    switching (one core, the right one), reduced motion for all three, 3:1 text contrast, frame rate per persona.
  - Guide test 74/74. Crawl: noise only; a tablet re-crawl after the overlap fix is clean.
  - 212 Vesper tests and browser integration pass. Intents 122/122, corpus 616/619.

### 29. Mobile polish, a home-screen app, preferences that follow the user, quieter small talk, a grouped model picker (2026-10-18)

The user asked for: centred icons on mobile; Notes on a phone as a full page with a way back; an adjustable sidebar;
proper "add to home screen" support with the persona's core as the icon; their choices (model, theme and so on)
remembered for them, including in the home-screen app; no "working on it" for very basic prompts; and the model
picker grouped by provider (Anthropic, then OpenAI, then the rest).
- **Centred icons.** The dock and rail glyphs were text symbols from mixed fallback fonts, each sitting a little off
  centre. They are now drawn icons, `<span class="ico" data-i="…">`, as CSS masks. `~/.cache/jarvis-dev/gen-icons.py`
  writes the ICONS block.
  - Icons: vitals, notes, agenda, audio, link, telemetry, board, guide, menu, attach, chevrons, close and back.
  - They take the text colour and size, and the new test measures them within 0.6px of centre.
- **Phone Notes page.** At ≤700px with Notes open, `.jarvis[data-page=notes]` makes the sheet a fixed full-screen page.
  - It respects safe areas. The dock and title are hidden, and a header has Back and NOTES.
  - Back: in a note → the list ("ALL NOTES"); on the list → closes.
  - The phone's own back gesture does the same, through a `history.pushState` entry (`_syncPageHistory` /
    `onPopState`). A stale entry after a reload is dropped on mount.
- **Adjustable sidebar everywhere.** `_sizeSpec()` gives one resize model per layout:
  - Desktop width: as before.
  - Tablet sheet width: 300 to min(600, vw−140), `vesperSheetW`.
  - Phone sheet height: a grip at its foot, 220px to full; 0 means full. `vesperSheetH`; dragging to the bottom or
    double-tapping gives the full height back.
  - Keys: arrows (Up/Down on phones), Home and End.
- **Preferences on the server** (`gateway/platforms/api_server_vesper_prefs.py`, `$HERMES_HOME/vesper-prefs.json`).
  - `GET/PUT /vesper/prefs` (bearer). PUT merges validated keys; null removes one; unknown keys or bad values → 400.
    A thread lock covers read-merge-write.
  - Keys: persona, honorific, layout, clock12, tempUnit, audioSource, model `{provider,id}`, session.
  - The page carries them on `<body data-vesper-prefs>` (`_vesper_page_prefs` in `api_server.py`), so a fresh device
    or the home-screen app starts as the user left it. iPhone home-screen apps have their own storage, which is why
    localStorage alone wasn't enough.
  - The dashboard: `SERVER_PREFS` wins at load and is mirrored into localStorage. `savePrefs()` debounces PUTs.
    `refreshPrefs()` on returning to the tab picks up changes from other devices.
  - The model last picked (not quiet carry-overs) is saved. `_applySavedModel` puts it on the current chat once the
    model list is in, and every new chat starts on it. `fetchModelOptions` no longer resets a picked model's label to
    the global default.
  - The global default model in `config.yaml` is untouched: these are dashboard choices.
- **Home-screen app** (`gateway/platforms/api_server_vesper_app.py`).
  - `/vesper/manifest.webmanifest[?persona=]`: standalone, id `/vesper`, name and colours per persona.
  - `/vesper/icons/{persona}-{32,180,192,512,maskable-512}.png`: rendered from the real cores by
    `~/.cache/jarvis-dev/core-render.py` (container) and composed by `gen-app-icons.py`.
  - `/vesper/sw.js`: network-only; navigations that fail get a "can't reach the server" page with a retry; never
    caches; `Service-Worker-Allowed: /vesper`.
  - Head: the manifest link, apple-touch-icon, apple-mobile-web-app-* metas (black-translucent), viewport-fit=cover,
    and a PNG favicon. `_applyPersonaTheme` swaps the links and title on a persona switch.
  - `.jarvis` keeps to the safe areas (margins plus `rootHeight` calc). CSP gained `manifest-src 'self'` and
    `worker-src 'self'`.
  - The service worker registers only in secure contexts. On plain http (the LAN URL) iOS still makes a full-screen
    home-screen app; Android Chrome makes a shortcut that opens in the browser unless the dashboard is served over
    https.
- **Small talk gets no "working on it".** `isSmallTalk(q, attachments)` (next to ACK_LINES):
  - Small talk: greetings, thanks, "how are you", "who are you", one- to three-word replies and the like, with or
    without the persona's name in front.
  - Never small talk: anything with a task word (search, write, summarize, weather, open, play, set, explain…), a URL,
    a long number, a file, or more than 14 words.
  - `_startProgress({quiet})` then skips the timed acknowledgement; if the agent reaches for a tool, the tool's line
    still comes. `~/.cache/jarvis-dev/smalltalk.test.js` (47 cases); `extract-intents.sh` exports `isSmallTalk`.
- **Model picker grouped by provider.** Header rows (sticky, with a model count):
  - ANTHROPIC first, then OPENAI (ChatGPT sign-in and API key together), then each other provider in Hermes' order.
  - The provider name shows on a row only when it adds something (e.g. ChatGPT inside OPENAI).
  - The empty state no longer tells the user to run `hermes model`; it points at GUIDE.
- **Tests.**
  - New `tests/gateway/test_vesper_prefs_app.py` (10). 222 Vesper tests pass; browser integration passes (one flaky
    run under parallel load, then a clean rerun).
  - New `qa-mobile-app.py` 43/43 (qaf) covers every item above, including a fresh device inheriting persona, model,
    chat and clock; a live pick-up on refocus; manifest and icons per persona; small talk vs a real request with a
    4.5s reply; and the picker order.
  - `qa-flows` 199/199. `qa-personas` 47/47. `qa-sidebar` 22/22 (tablet now expects a resize edge). `qa-explore` 17/17.
  - Guide suites (tsu): 74/74 and 74/74. Intents 122/122, corpus 616/619, small talk 47/47.
- **Also fixed.** The weather card's °C/°F button was 33×24 on touch tablets; it is 40px on touch screens now.
- **Test infrastructure.** Preferences live on the server now, so they would leak between tests.
  - `harness-qa.py` and `harness-setup.py` clear `vesper-prefs.json` at start and add `POST /harness/reset-prefs`.
    Every suite calls it for each new browser context.
  - `harness-qa.py` adds `POST /harness/delay` (slow plain replies) and a ChatGPT provider listed first, to test the
    grouping.
  - `qa-crawl.py` also resets server preferences when restoring. After a missing control it does a hard reset instead
    of carrying a half-done state into the next probes; that cascade is what had made the first phone crawl look
    broken.

### 30. OmniVoice (self-hosted TTS) tried on the NAS: too slow on this CPU, not installed in the image (2026-10-18)

The user asked to replace ElevenLabs/edge/Piper with k2-fsa/OmniVoice (0.6B, 24 kHz; code Apache-2.0, weights
CC-BY-NC), and to clone JARVIS, Ultron and Cortana from three YouTube Shorts.
- **Not done: cloning from those clips.** They are an official Marvel trailer (Paul Bettany), an Age of Ultron clip
  (James Spader) and Jen Taylor speaking as herself. Cloning would replicate real people's voices without their
  consent. Offered instead: original voices from OmniVoice's voice design, or cloning from audio the user has the
  right to use.
- **Prototype, isolated from the live stack.** A throwaway container `omnivoice-proto` (image jarvis-unified:local,
  `--memory 6g --cpus 3`). Scratch dataset: `/mnt/hdd_2tb/configs/jarvis-scratch/omnivoice`, holding a venv with
  py3.12, torch 2.8.0+cpu and omnivoice 0.2.1 (700 MB), the HF cache (2.2 GB of weights) and the sample wavs. The
  live container was not touched.
- **Speed on the i5-7500T (4 cores, no CUDA/XPU; HD 630 is unusable).** fp32 needs about 2.6 GB of RAM. Timings for
  3–5 s of speech:

  | Setting | Time | RTF |
  |---|---|---|
  | Voice design, 16 steps | 43 s | ≈8 |
  | Voice design, 32 steps | 65 s | ≈12 |
  | Clone, 16 steps, CFG 2 | 90 s | ≈23 |
  | Clone, 8 steps, CFG 2 | 40 s | 10.6 |
  | Clone, 6 steps, CFG 0 | 25 s | 7 |
  | int8 (dynamic quant), 6 steps, CFG 0 | 15 s | 4.3 |
  | int8, 4 steps, CFG 0 | 7.6 s | 2.5 |

  - 4 threads was slower than 3, because the NAS's other apps contend for the cores.
  - Copying the model to quantize it got OOM-killed at the 6 GB cap; quantize in place instead.
  - Even the fastest, lowest-quality setting is 2.5× slower than real time, so it can't be the live voice here.
- **Voice design gotcha.** `instruct=` re-samples a new speaker on every call. For a stable persona voice, design one
  reference clip, then `create_voice_clone_prompt(ref_audio, ref_text)` and generate with `voice_clone_prompt=`.
- **Candidate voices (32 steps).** jarvis a "male, middle-aged, low pitch, british accent" / b same with moderate
  pitch; ultron a "male, middle-aged, very low pitch, american accent" / b "male, elderly, low pitch, american accent";
  cortana a "female, young adult, moderate pitch, american accent" / b "female, middle-aged, low pitch, american
  accent". The wavs (`voice-*.wav`) are in the scratch dir and were sent to the user.
- **Open.** Waiting for the user's call: keep the current voices; OmniVoice everywhere with long waits; or OmniVoice on
  a machine with an NVIDIA GPU. To remove the prototype: `sudo docker rm -f omnivoice-proto` and delete the scratch dir.

### 31. OmniVoice is the dashboard's voice (2026-10-18)

The user chose OmniVoice despite the CPU speed (entry 30). They then asked again for clones of the three Shorts,
saying they had the actors' permission. That couldn't be verified, and the clips are studio material with
well-known actors' voices, so they were still not cloned. The personas use the original designed voices ("a"
samples), and any clip the user has the right to use can replace them.
- **Pins.** The first build failed: the build runs in /opt/hermes, whose pyproject sets `exclude-newer = "14 days"`, and the
  first pins were newer than that. The deps were re-resolved under the same quarantine (gradio 6.29.1, transformers 5.18.0,
  huggingface-hub 1.33.0) and re-tested end to end.
- **Image** (`Dockerfile.hermes`, early layers so they stay cached). `/opt/omnivoice` holds a Python 3.12 venv with
  CPU torch and omnivoice 0.2.1, every package pinned in `deployment/omnivoice-requirements.txt` (from a working
  install). The HF snapshot is baked in at revision c5fdb5cc…; the worker loads it from the local folder with
  HF_HUB_OFFLINE=1.
- **Engine** (`gateway/platforms/api_server_vesper_voice.py`).
  - It runs `vesper_omnivoice_worker.py` under that venv: JSON lines over stdin/stdout, library output sent to stderr,
    nice 5, 3 threads.
  - A start lock prevents two workers starting at once; the tests caught that race, which had produced an empty WAV.
  - Requests run one at a time. A request whose browser has gone (the user talked over the voice) is skipped before it
    starts.
  - The worker stops after 15 idle minutes to free ~2.6 GB. Starting takes ~45 s on this CPU (imports; the model
    itself loads in ~3 s), so `GET /vesper/voice/status` (sent when the dashboard opens) wakes it.
  - Disk cache: `$HERMES_HOME/vesper-voice-cache`, keyed by clip, settings and text, capped at 300 MB.
  - `POST /vesper/voice/prewarm` renders the persona's acks, tool lines, still lines and today's greetings when no one
    is waiting.
  - Voices: `$HERMES_HOME/vesper-voices/<persona>.wav` + `.txt` if present (the user's own clips, 3–10 s), else the
    shipped `assets/vesper/voices/<persona>.{wav,txt}`.
  - Settings: config.yaml `tts.omnivoice` {steps 4, guidance 0, int8 false, threads 3, idle_minutes 15, enabled true}.
- **/vesper/tts.** It takes `persona`. With OmniVoice: OmniVoice, then the persona's Piper voice (X-Vesper-Voice
  `omnivoice` | `local`; 204 when the browser left). Without OmniVoice, or with `enabled: false`: the old ElevenLabs →
  edge → Piper chain, unchanged.
- **Settings choice.** Measured with Whisper WER on the same sentences, fp32 without guidance:

  | Steps | RTF | WER |
  |---|---|---|
  | 8 | ~12 | 0% |
  | 6 | ~9.6 | 26% (noise) |
  | 4 | ~5.4 | 4% |

  - int8 garbles words (WER 50–100%), so it stays off.
  - The end-to-end run in the prototype with real model and real engine: worker ready in 45 s; 1.6–3 s lines took
    24–45 s; repeats come from the cache.
- **Dashboard.**
  - `speak()` speaks a reply in sentences (`speechChunks`: split at sentence punctuation followed by a space, so "2.5"
    and URLs stay whole; short sentences are joined; run-ons are cut at a comma; a lone trailing word is merged).
  - The next sentence renders while the current one plays.
  - The core says "◇ PREPARING VOICE" until the voice starts.
  - `_warmVoice()` on mount, and again on a persona or honorific change. `personas.js` exports `vesperPersonaFill`.
  - The voice error no longer mentions ElevenLabs credits.
- **Found on the first live start and fixed.**
  - The worker sits in gateway/platforms next to `signal.py`, which shadowed the stdlib. It now runs `python -I`; the
    test plants a booby-trapped signal.py.
  - OmniVoice's `postprocess_output` trimmed short lines to 0 s. It is off; the worker's `tidy()` trims gently,
    normalises and refuses clips under 0.1 s.
  - Lines of four words or fewer were mumbles at 4–8 steps. Whisper heard every word at 16 steps + guidance 2, so
    `quality_for()` uses `CAREFUL` for short lines and for prewarm (~40–60 s each, cached).
  - So `speechChunks` makes chunks of ≥5 words. The cache key ignores steps (a carefully prewarmed line serves later
    requests) and carries `CACHE_VERSION`.
  - The Piper fallback now uses the configured voice only if it's installed: the live config names
    `en_US-lessac-medium`, which isn't, and downloading it fails.
  - Verified with the real worker in the live image, then Whisper: "On it." ×2, "Hi there!" and three 2.6–4.7 s
    sentences were all transcribed correctly. Long sentences took 11–15 s.
- **Harness.** The harnesses run in this image now, so `harness-qa.py`/`harness-setup.py` force
  `api_server_vesper_voice.installed = False`. Otherwise the dashboard there woke a real worker, whose prewarm took the
  CPU and timed out flows.
- **Tests.**
  - `tests/gateway/test_vesper_omnivoice.py` (7) drives the real engine through a stand-in worker that speaks the same
    protocol.
  - `qa-voice.py` 12/12. It uses the new harness switches: `/harness/delay {tts}` and `/harness/tts-requests`.
  - `~/.cache/jarvis-dev/speech.test.js` (6).
  - Flows 199/199 (one rename flake, then clean). Mobile 43/43, personas 47/47. 229 Vesper tests and browser
    integration pass.

### 32. Voice engine choice: ElevenLabs again, Pocket TTS as the fast NAS voice (2026-10-19)

The user asked for three things: ElevenLabs voices again (JARVIS `VGPRBzHikUaaOd5ObCEz`, Ultron
`iFtGfTnv8oka2ekzenTJ`, Cortana `kDTlGbwa6dljwT7Dl4eN`), research into free alternatives in case OmniVoice is too
slow, and a dashboard setting to choose the voice API. OmniVoice stays.
- **The ElevenLabs IDs** are voices in the user's own account: `GET /v1/voices/<id>` returns category "cloned", named
  "Jarvis from Iron man", "Ultron from marvel" and "Cortana from halo". The account is on the Starter plan (30,000
  characters a month) with `eleven_multilingual_v2`.
  - They are used as given, through ElevenLabs.
  - The user also asked for those voices to be cloned into the local engines. That would copy the actors' voices
    (Paul Bettany, James Spader, Jen Taylor) out of ElevenLabs, the same thing entry 31 declined, so it wasn't done.
    The local engines keep original voices.
- **Research** (sources: Picovoice's CPU benchmark, Kyutai and Neuphonic repos), measured on the NAS i5-7500T:

  | Engine | NAS speed | Voice cloning | Licence |
  |---|---|---|---|
  | Pocket TTS 3.3.0 | RTF ~0.35, about 3× faster than real time: "On it." 0.5 s, a 4 s sentence 1.4 s; load 3 s; ~600 MB | Yes, but the cloning weights are gated on HF; without them, 21 English catalog voices | CC-BY-4.0 weights; voices CC0 / CC-BY |
  | Kokoro (ONNX, fp32 / int8) | RTF ~0.6 | No, preset voices | Apache-2.0 |
  | NeuTTS Nano Q4 | Picovoice reports ~7× core-time on a Ryzen; slower on this CPU | — | — |
  | Chatterbox-Turbo | 48 s first audio on CPU | — | — |

  Pocket TTS was chosen. Whisper heard every Pocket catalog voice correctly (WER 3%, the same normalisation miss for
  all of them).
- **Pocket TTS voices.** Chosen by median F0 and register: JARVIS `peter_yearsley` (LibriVox narrator, 89 Hz),
  Ultron `javert` (78 Hz), Cortana `eve` (VCTK p361, American, 176 Hz). Change them with config.yaml
  `tts.pocket.voices: {persona: name}`.
  - With Kyutai's gated cloning weights at `$HERMES_HOME/pocket-tts/model.safetensors`, Pocket instead clones the
    persona's reference clip, the same clip OmniVoice uses.
  - To get those weights, the user accepts the terms at huggingface.co/kyutai/pocket-tts, then runs
    `hf download kyutai/pocket-tts languages/english/model.safetensors --revision 983151f13aaeab1b13c1e5e3c2c383d49a9edf3f` and copies the file there.
  - This path is covered with a stand-in worker only, not with the real gated weights.
- **Image.** A second layer after OmniVoice's installs `deployment/pocket-tts-requirements.txt` into the same
  `/opt/omnivoice/venv`, with OmniVoice's pins as constraints. The pins are einops, pocket-tts 3.3.0 and
  sentencepiece, resolved under the 14-day exclude-newer. It shares torch 2.8.0+cpu.
  - It bakes `kyutai/pocket-tts-without-voice-cloning` (model, tokenizer and English embeddings, at the revisions
    `english.yaml` names) into `/opt/pocket-tts/hf`, 248 MB.
  - The worker runs offline from there. A rehearsal from a fresh cache passed in a scratch container.
- **Engine module** (`api_server_vesper_voice.py`, rewritten).
  - `WorkerEngine` holds the process, lock, idle reaper, prewarm and cache logic; subclasses are `OmniVoiceEngine` and
    `PocketEngine` (`vesper_pocket_worker.py`, the same JSON-lines protocol, `python -I`, 30 min idle).
  - `ENGINES`: elevenlabs, pocket, omnivoice, edge and piper.
  - `available()` reports which are usable. ElevenLabs needs a key, checked for presence only via `_require_key`.
  - `resolve(choice)` returns the choice, or else the first available engine in DEFAULT_ORDER (ElevenLabs, then
    Pocket).
  - `FALLBACKS`: ElevenLabs → Pocket, OmniVoice → Pocket, Edge → Pocket, and every engine ends with Piper.
  - Within ElevenLabs, the persona's voice is tried first, then the configured voice.
  - Everything is cached on disk by engine, voice and text. ElevenLabs repeats cost no characters.
  - Prewarm runs only for OmniVoice; the online engines would spend characters on lines that might never be said.
  - `X-Vesper-Voice` says which engine spoke: elevenlabs | pocket | omnivoice | edge | local.
- **`/vesper/tts`** (`api_server.py`) validates `engine` and hands the request to `vesper_voice.speak()`. The old
  inline ElevenLabs → edge → Piper chain is gone.
- **`GET /vesper/voice/status?engine=`** returns `{engine, default, engines: [{id, label, note, local, available}]}`
  and wakes the chosen NAS engine.
- **Dashboard.**
  - A VOICE chip row sits under ADDRESS ME AS. An engine that isn't available is disabled, and the caption describes
    the active one.
  - The choice is the server preference `voiceEngine` (validated in `api_server_vesper_prefs.py`, mirrored to
    localStorage `vesperVoiceEngine`, picked up by `refreshPrefs`).
  - `speak()` sends `engine`, and its in-memory cache key includes it.
  - `_warmVoice()` stores the engine list, re-runs whenever `_voiceKey()` changes (persona, honorific or engine), and
    prewarms only for OmniVoice.
  - `personas.js` carries the three ElevenLabs IDs.
- **Harnesses.** They now also force `pocket_installed = False` and make `_wake` a no-op. `harness-qa.py` sets
  `available()` to everything except OmniVoice and logs `engine`/`voice_id` per TTS request. The browser pytest now
  stubs `available()`: before this change its voice-status call woke a real OmniVoice worker in the container.
- **Tests.**
  - `test_vesper_tts_voice.py` was rewritten: engine choice, defaults, fallback order, skipping engines that aren't
    available, validation, status, the key presence check.
  - `test_vesper_pocket.py` is new: catalog voices offline, the config override, the cache, cloning with the weights.
  - `test_vesper_omnivoice.py` was updated, and the prefs tests cover `voiceEngine`.
  - Results: 239 Vesper tests and browser integration pass. `qa-voice.py` 26/26 (14 new VOICE checks, desktop and
    phone). JS suites pass. Flows 199/199, personas 47/47, sidebar 22/22, mobile 43/43 (one
    tablet-drag flake on the first run, clean on the rerun).
  - Live after the deploy (rollback `localhost/jarvis-unified:pre-update-20261019-1030`): `live-persona-check.py`
    33/33, including the VOICE row; on the live server every engine is reported available.
  - A minimal live synthesis of "Systems check complete. Standing by." per persona:
    - Pocket: 5.7 s for the first (worker start), then ~1.0 s.
    - ElevenLabs with each persona's ID: ~1.7 s, six distinct outputs.
    - A repeat came from the cache in 0.01 s.
    - It used about 110 ElevenLabs characters.

### 33. The home-screen app stuck on "Can't reach the server"; the guide's Voice and Telegram steps (2026-10-20)

The user asked for:
- automated UI and regression testing, with any holes patched;
- a setup guide that other people can finish on their own system: Telegram, and a voice section with the Hugging Face
  setup Pocket TTS needs;
- a fix for the phone: the TrueNAS link to JARVIS, and JARVIS added to the home screen, were both stuck on retry.
  Refreshing and Try again did nothing.

**The stuck phone was the service worker's own CSP.**
- `/vesper/sw.js` (HANDOFF 29) got the gateway's global header `Content-Security-Policy: default-src 'none'`.
- A worker's own `fetch()` obeys the CSP its script is served with, so after the first visit installed the worker,
  every page load was blocked. The worker showed its "Can't reach the server" page instead, and Try again only reloaded
  into the same block.
- It only happens where browsers run service workers: https (the Tailscale address, which the phone and the
  home-screen app use) and localhost. The LAN http address never installs it.
- Reproduced read-only against the live gateway (`~/.cache/jarvis-dev/sw-repro.py`): the first load was fine, every
  reload showed the offline page.
- The suites missed it because the harnesses didn't add the gateway's security headers.

Fix (`api_server_vesper_app.py`, `dashboard.dc.html`):
- sw.js is served with its own CSP, `default-src 'none'; connect-src 'self'; frame-ancestors 'none'`.
- The offline page heals itself. It checks `/health` every 5 s, when the phone comes back online, and when the app
  returns to the foreground. Once the server answers, it unregisters the worker and reloads, so a broken worker can't
  trap the app again. The page registers the worker again on load.
- The page calls `reg.update()` after `register()`, so a fixed worker replaces a faulty one on the first load.
- **Phones already stuck on the old worker heal with no tap.** The old offline page has no script, so only the browser's
  own update check (after a navigation) can bring the new worker. On its first try the CSP fix alone needed 3–4
  Try again taps or reopens, which looked just like the bug. Now, on `activate`, the new worker pings every window it
  controls and reloads (`WindowClient.navigate`) any that doesn't answer within 2 s. The dashboard answers
  (`vesper-sw-ping` listener + `startMessages()`), so a running dashboard is never reloaded by a later worker.
  - The reload is not awaited inside `waitUntil`. `navigate()` resolves after the load, and the load's fetch waits for
    activation, so awaiting it would deadlock.
  - Measured with `~/.cache/jarvis-dev/sw-migrate-check.py`, which serves the live worker from `/opt/hermes` first,
    then the new code on the same origin. Opening the app once, tapping Try again once, and closing and reopening each
    give the dashboard back about 5 s after opening. A running dashboard survives a later worker swap.
- Tests:
  - New `tests/gateway/test_vesper_service_worker_browser.py` (integration, 2 tests). It runs a real Chromium against
    the routes behind the gateway's real middleware:
    - It reloads twice through the installed worker, goes offline, and comes back online by itself. It fails on the
      old code with "Can't reach the server".
    - A page trapped by a worker that answers every load with its offline page recovers with no tap once the real
      worker is served, and a running dashboard isn't reloaded when the worker changes. It fails (20 s timeout)
      without the ping-and-reload.
  - `test_vesper_speedtest.py` was already failing on HEAD: the guide, which opens by itself on a first visit, covered
    the RUN SPEED TEST button. The test now marks the guide done, like the other browser tests.
  - `harness-qa.py` and `harness-setup.py` now add `security_headers_middleware`, so every suite sees the real
    headers.

**Guide: seven steps now** (`tour.js`): Welcome, Connect a model, **the voice**, **Telegram**, Pick the model, Getting
around, Done. Both new steps are optional. Nothing needs a terminal.
- **Voice** (`gateway/platforms/api_server_setup_voice.py`, routes `/vesper/setup/voice*`). It has two tabs.
  - **On this server.** Pocket TTS works out of the box with a catalog voice. The tab explains how to get the cloning
    model:
    1. Accept the terms at huggingface.co/kyutai/pocket-tts.
    2. Make a Read token.
    3. Paste it.
  - The server checks the token with `whoami-v2` (401 means a bad token) and checks access with a HEAD on the pinned
    file (401/403 means the terms aren't accepted). It saves `HF_TOKEN` and downloads
    `languages/english/model.safetensors` at the HANDOFF 32 revision to `$HERMES_HOME/pocket-tts/` with a progress
    bar.
  - The download is checked against the Hub's size (219,029,196 bytes) and against the SHA-256 of the copy on the NAS
    (the Hub hides a gated file's hash). It is installed with `os.replace`.
  - The token isn't sent on to the Hub's storage host after the redirect.
  - Then the running Pocket worker is stopped, so the next line it speaks is cloned.
  - **ElevenLabs.** The pasted key is checked with `/v1/voices`; a speech-only key (`missing_permissions`) is accepted
    without a voice list. It is saved as `ELEVENLABS_API_KEY`.
  - Each persona then gets a voice from the account (your own voices first). They are saved as config.yaml
    `tts.elevenlabs.persona_voices`. `_elevenlabs()` tries that first, then the personas.js id (the owner's own clones,
    which no one else's account has), then the configured voice.
  - "Speak with ElevenLabs now" switches the dashboard's VOICE (new `vesper:ui` actions: `voice-engine`,
    `voice-refresh`).
- **Telegram** (`api_server_setup_telegram.py`, routes `/vesper/setup/telegram*`).
  1. Make a bot in @BotFather (linked), paste the token, and press Check (`getMe`). The token stays in the gateway
     process under a setup id.
  2. Open the bot (linked) and press Start. The guide polls `getUpdates` every 3 s for 3 minutes and shows who wrote
     privately ("Is this you?").
  3. Picking yourself saves what `hermes gateway setup` saves: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`
     (just you for a new bot), `TELEGRAM_HOME_CHANNEL`, and `platforms.telegram.enabled: true`.
  4. It marks the Start messages read (`getUpdates` with an offset), says hello in the chat, and hands the bot to the
     running gateway. The old Telegram adapter is torn down (`_bounded_adapter_teardown`); the new config goes into
     `_failed_platforms` with no delay, so the gateway's own reconnect watcher connects it. No restart.
  5. The guide waits until `GET /vesper/setup/telegram` reports it running.
  - The guide never polls the bot the gateway is already reading (409 `already_connected`). Pasting that bot says
    "That's the bot JARVIS already uses."
- **Dashboard.** SET UP VOICES › under VOICE opens the voice step. There are new palette entries ("Set up voices",
  "Connect Telegram") and local commands ("set up telegram", "connect elevenlabs", "set up voices", ...).
  `openGuide(reason, step)` takes a step.
- **Checked and needs no guide step:** web search has a keyless fallback, and speech to text is local faster-whisper.
- **Docs.** README (guide, commands, persona voices), SHARING.md (new sections "Voice and Telegram (optional)" and
  "Your phone and the home-screen app"), CONNECTING-MODELS.md and `.env.example`.

**Tests.**
- New pytest files. Telegram, ElevenLabs and the Hub are real local fake servers, not mocks.
  - `test_vesper_setup_voice.py` (8): auth; a refused key; a saved key with its own voices first; a speech-only key;
    the persona pick spoken first; a bad token and unaccepted terms; a verified download with the token kept off the
    storage host; a corrupt download not installed.
  - `test_vesper_setup_telegram.py` (6): auth; refused tokens; only private senders offered; what is saved, read and
    sent; replacing a running bot through the real `GatewayRunner` teardown and queue; the gateway's own bot never
    polled.
- New `~/.cache/jarvis-dev/qa-guide-voice-telegram.py` (tsu, 81 checks, desktop and phone). `harness-setup.py` fakes
  ElevenLabs, the Hub (with a slow download), the Bot API (`POST /harness/tg-start` presses Start) and a gateway
  runner that "connects".
- `qa-flows.py` no longer assumes five guide steps. `qa-crawl.py` waits for CSS transitions before measuring, which
  removes the tablet noise.
- `jt-nas.sh` takes `JT_SRC=<checkout>` to test a git worktree.
- intents.test.js: 6 new cases.
- `qa-mobile-app.py` measures the sheets once they stop moving (`settled()`), not after a fixed 900 ms. Headless
  Chromium on the NAS paints only a few frames a second, and the tablet sheet starts opening 400–800 ms after the
  tap, with or without any change. That was the "tablet-drag flake" in HANDOFF 32.
- New dev tools in `~/.cache/jarvis-dev/`: `full-regression.sh OUTDIR` (gateway pytest, default and integration, then
  every Playwright suite on restarted harnesses; logs in `runs/`) and `sw-migrate-check.py`.

**Also fixed while testing.**
- **The phone sheet's resize grip covered buttons.** The grip was a 24px overlay pinned to the sheet's foot, and the
  content scrolled under it. A button there looked tappable, but the tap started a resize. The crawl found SET UP
  VOICES and AGENT CONTROL under it. On phones the grip is now the sheet's last flex row (`order:9; flex:0 0 24px`,
  `position:relative`), and the 30px padding that worked around the overlay is gone. Both buttons now take a tap even
  scrolled to the very bottom. The tablet edge and the desktop edge are unchanged.
- `vesper_pocket_worker.py` read the Pocket config with a bare `read_text()`, which the gateway's UTF-8 lint test
  (`test_gateway_utf8_encoding.py`) rejects. It now reads and writes with `encoding="utf-8"`.

**Results.**
- Gateway pytest (`tests/gateway/`, 8,000 tests): 14 failed in the full run.
  - One was the Pocket encoding lint, now fixed.
  - Three are the known update-command tests, which need a `.git` the container doesn't have.
  - The other ten pass when their file runs alone, on the new code. They are order-dependent: every one sits
    alphabetically before the new `test_vesper_setup_*` files, so the new tests can't cause them.
- Integration (`-k vesper -m integration`): 4 passed, 1 skipped. That includes both service-worker tests and the
  repaired speed test.
- New and touched unit tests: voice 8, Telegram 6, Pocket, all passing.
- Playwright on the final code:
  - Full run (run2): flows 199/199, mobile 43/43, voice 26/26, personas 47/47, sidebar 22/22, explore 17/17,
    tour 74/74, guide persona 74/74, guide voice+Telegram 81/81.
  - After the grip fix: sidebar 22/22, mobile 43/43 three runs in a row, and flows 199/199, personas 47/47, explore 17/17, guide voice+Telegram 81/81.
- Crawl against a baseline of HEAD, crawled the way it ran before (without the security headers, which trap HEAD
  in its own worker): the only new finding was the grip covering SET UP VOICES. With the fix, the phone crawl has
  no grip findings; it is down from 12 findings to 9, the rest being the known mock-data 404s and pop-ups.
- With the security headers on, crawling HEAD reproduces the original bug: nearly every control is "not found"
  after the first load.

**Deployed 2026-10-21** (rollback image `localhost/jarvis-unified:pre-update-20261020-2211`).
- Before the deploy the user also reported the desktop: a normal refresh stayed on "Can't reach the server", and
  only Ctrl+Shift+R (which bypasses the worker) loaded the dashboard. It's the same worker. `sw-migrate-check.py`
  now covers it: a desktop tab stuck on the pre-fix worker (taken from git, since `/opt/hermes` is the new code),
  then one F5 after the deploy, gives the dashboard back about 5 s later, and reloads after that work normally.
- Live and read-only: `/vesper/sw.js` has the new CSP and the ping-and-reload. `sw-repro.py` reloads through the
  worker and gets the dashboard (before, every reload showed the offline page). `live-guide-vt-check.py` passes
  20/20 (the guide's live Voice and Telegram state, three reloads through the worker), and `live-persona-check.py`
  33/33. Telegram reconnected after the restart.

**Open.**
- Not yet seen on a real phone or the user's desktop browser. Everything above ran in headless Chromium. A browser
  still on the old worker goes from "Can't reach the server" to the dashboard within a few seconds of one normal
  load. Safari's support for `WindowClient.navigate` is untested here. Without it, Try again still recovers once the
  new worker has arrived (a tap or two, a few seconds apart).
- The Voice and Telegram steps are tested against fakes of ElevenLabs, the Hub and the Bot API, not the live
  services.

### 34. JARVIS 2: Hermes is no longer in JARVIS; a launcher installs it with consent, or connects yours (2026-10-23)

The user asked for JARVIS to become a connection layer for people who have Hermes: don't package Hermes; on first run
force a consent to download it (like the Claude Code download for Claude sign-in), install it with JARVIS's persona, then
continue to the regular setup; support auto-updates; offer "I already have Hermes" (find it and connect, plus a prompt to
paste into one's own Hermes); and a Test connection button in JARVIS's setup.

**What JARVIS 1 really was.** The image `localhost/jarvis-unified:local` was `nousresearch/hermes-agent` (0.21.1) with a
forked Hermes 0.21 tree copied over it. Diffing the fork against upstream found its base: commit `96ed0e71ea`
(2026-09-05), 11,782 of 11,863 files identical. JARVIS's whole footprint was: 12 `gateway/platforms/api_server_vesper*`
/ `api_server_setup*` modules, `gateway/assets/vesper/`, the `jarvis-notes` skill, `tools/pdf_markdown.py`, and edits to
four core files (`api_server.py` routes/CSP/body limits/same-origin check/session-provider pin, `run.py`/`run_inbound.py`
PDF-to-Markdown for Telegram, `read_extract.py` partial-OCR recovery). Upstream is now v0.22.5.

**The new layout (branch `connector`, local repo `C:\Users\lionk\jarvis-src\src`):**
- `connector/jarvis/`: a normal Hermes plugin. `register_platform_handler("api_server", factory)` hands it the API
  server's aiohttp app before the router freezes (`api_server.py: self._wire_plugin_handlers(self._app)`), so it adds
  the routes and one outermost middleware (`wiring.py`: drops a same-origin `Origin` header so module imports pass the
  stock CORS check, and calls the music/extract/chat routes directly with a raised `client_max_size` when a body is over
  Hermes's 10 MB limit). It wraps `adapter._stored_session_model` (instance attribute) for the provider-pin fix, syncs
  `jarvis-notes` into `$HERMES_HOME/skills/note-taking/` (the prompts use the bare name), and converts Telegram PDFs in
  a `pre_gateway_dispatch` hook (`pdf_ingress.py`, rewrite action). `plugin.yaml` declares `provides_hooks`. Needs
  `plugins.isolation: in_process` (default). Connector routes for checks: `/vesper/connector` (public: versions) and
  `/vesper/connector/check` (Bearer: + isolation, features).
- All 272 of the fork's JARVIS tests (moved to `connector/jarvis/tests`, plus one new) pass against stock Hermes 0.22.5.
  How: copy them to `<hermes-agent>/tests/jarvis`, `JARVIS_CONNECTOR_DIR=<connector dir>`, run pytest through
  `hermes --run-module <shim>` (the installed runtime is not a plain venv; PyYAML is no longer a Hermes dependency).
- `launcher/jarvis_launcher/`: system-Python aiohttp app on :8642. `/jarvis/` setup screen + API; everything else is
  proxied (Host kept, streams and WebSockets) to the Hermes in `data/jarvis/state.json` (`mode` managed/external).
  Changes need `X-JARVIS: 1`, a same-origin Origin, and once set up the Hermes key as Bearer (stamped on the page like
  the dashboard). `managed.py` (consent-gated install with the official `install.sh --non-interactive
  --skip-computer-use --hermes-home /opt/data --dir /opt/data/hermes-agent --channel stable`), `hermes_setup.py` (all
  config via `hermes config set` / `hermes plugins enable`; API key into `.env`; persona `SOUL.md`, old kept as
  `SOUL.md.before-jarvis`), `checks.py` (Test connection), `detect.py`, `connect.py` (paste prompt, `/jarvis/connect.sh`,
  `/jarvis/connector.tar.gz`, one-time pairing codes), `updates.py`, `prestart.py`.
- `Dockerfile` (debian trixie-slim + s6-overlay, Chromium/noVNC desktop in `desktop/start.sh`, OmniVoice + Pocket TTS
  build arg `JARVIS_LOCAL_VOICES`, Piper voices): 6.8 GB with voices (was 15.3 GB), 1.6 GB without. The OpenJarvis base
  image, `openjarvis/`, the `openjarvis-browser` plugin and `Dockerfile.hermes` are gone; Hermes's own browser tools
  drive the desktop's Chromium (`browser.backend: off`, `browser.cdp_url: http://127.0.0.1:9222`).
- GUIDE (`tour.js`) has a new step 1, "Hermes connection", with **Test connection** (launcher's `/jarvis/api/test`, or
  the connector's own check when the dashboard is opened straight on a Hermes host).

**Gotchas found while testing on the NAS (all fixed):**
- An aiohttp `Response` is a mapping, so `if (blocked := _guard(...)):` was always false: requests WITHOUT `X-JARVIS`
  started an install. Every guard now compares `is not None`.
- Inside an s6 container (PID 1 = s6-svscan) Hermes upgrades `hermes gateway run` to an s6 service it registers in
  `/run/service` (permission denied as hermes). JARVIS now uses that, like Hermes's own image: cont-init
  `20-hermes-s6` chowns `/run/service` and `.s6-svscan/{control,lock}` and runs `hermes_cli.container_boot`, which
  restores the gateway after a container restart; the launcher only calls `hermes gateway start|stop|restart` and never
  stops the gateway on its own exit (that would save "stopped" as the desired state).
- The reconciler starts the gateway before the launcher runs, so after an image update the gateway loaded the OLD
  connector: `prestart.py` (cont-init) copies the connector first; the launcher restarts the gateway if it still finds
  it stale.
- Hermes resolves enabled plugins' dependencies while installing: the old `openjarvis-browser` (playwright==1.62.0, no
  Python 3.14 wheels, and core pins 1.62.0) failed the install on old volumes. `prepare_old_volume` moves retired
  plugins to `data/jarvis/retired-plugins/` and removes them from `plugins.enabled` (line edit, comments kept; an
  emptied list becomes `[]`), and removes the old image's `.local/bin/hermes -> /opt/hermes/...` link (the installer
  keeps an existing link, so `hermes` never appeared). `retire_old_plugins` drops `openjarvis_browser` from every
  platform's toolsets (`hermes config get` prints lists over several lines; read the YAML instead).
- Stock Hermes lazy-installs optional extras (`pm.ensure_import`): Telegram on connect (verified, 22.8); the
  connector asks for `edge-tts` and `piper` before using them and pre-installs both in the background inside the
  JARVIS image. Edge's host resolves to 0.0.0.0 on this LAN (Pi-hole), so Edge falls back to Pocket, as before.
- Stock `read_extract` returns only a `[NEEDS OCR: ...]` note for a partly scanned PDF, and caps documents at 50 MB;
  `pdf_markdown._markdown_body` adds the per-page pdftotext text under that note and covers big PDFs the same way.
- The fork's PDF partial-OCR change for `read_file` itself is not carried over (that is Hermes core).

**Verified on the NAS** (test containers, live `jarvis-hermes` untouched): fresh managed install (official installer,
78 s) → setup → gateway under s6 → dashboard through the launcher; all 8 checks green; external mode via the paste
command and pairing against a stock Hermes in another container; detection (host found through gateway IP,
host.docker.internal and LAN IP, merged into one); update 0.22.4 → 0.22.5 through the API with health check;
`_rollback` to the exact 0.22.4 commit; container recreation brings the gateway back; Pocket TTS and Piper speak.
**Migration of the real data**: snapshot `SSDCACHE256/jarvis@before-jarvis2-20261023` (the dataset had no snapshots
before), cloned to `SSDCACHE256/jarvis-migtest` with Telegram token and cron jobs disabled. The install migrated config
v40 → v51, kept the model (anthropic / claude-fable-5), the 3 Anthropic credentials, 84 sessions, memories, notes,
skills (23 bundled ones updated, user-modified ones kept) and gave all-green checks.

**Not done (needs the user):** the live cutover. `deployment/nas/cutover.sh <jarvis-2-source>` snapshots the dataset,
copies the stack to `/mnt/hdd_2tb/configs/jarvis1-backup-<stamp>`, stops `jarvis-hermes`, installs the new stack files
and builds; the user then clicks "Install Hermes for me" on `/jarvis/`. `deployment/nas/rollback.sh <backup>` goes back
(with a ZFS rollback, since the new Hermes migrates the config). Neither script was run: both point at the live paths.
The connector isn't published anywhere: an external Hermes gets it from the JARVIS that serves `/jarvis/connector.tar.gz`.

### 35. Automated UI and regression testing of JARVIS 2, and what it found (2026-10-24)

The user asked for JARVIS 2 to be tested (the test instance `jarvis-next` they had tried, with Claude connected),
covered by Playwright UI and regression tests, polished for friends with an easy compose setup, and then deployed.

**Test suites** (all run in throwaway containers; commands in README "Tests"):
- `tests/unit.sh`: 42 launcher unit tests (python:3.13 container; `launcher/tests`, a fake Hermes in `fake_hermes.py`).
- `tests/connector.sh`: 281 connector tests inside `localhost/hermes-stock:e2e` (stock Hermes from the official
  installer, `tests/e2e/hermes-stock/`), run through `hermes --run-module` with pytest in a side folder.
- `tests/e2e/run.sh`: Playwright 1.64.1 (`mcr.microsoft.com/playwright:v1.64.1-noble`), 52 tests in two phases on
  network `jarvis-e2e`. Phase fresh: setup screen, consent, a real install through the UI, the guide (Hermes step,
  Local route), chat, chats list, notes, Agent Control, persona, palette, every dashboard API route (auth, CSP,
  >10 MB uploads, PDF to Markdown, files guard, Piper speaking, doctor, desktop), settings (updates, persona, key).
  Phase external: pairing via the pasted command (`hermes-stock`'s entry.sh plays the agent from /shared/run.sh), test
  connection, the dashboard and streamed chat through the proxy, --persona-only, disconnect, manual connect.
  Pages fail on any console error or failed request (`helpers.ts` `problems` fixture). Chat uses
  `tests/e2e/mock_llm.py` (OpenAI + Ollama `/api/show` `/api/tags`, deterministic), named "ollama" so the guide's own
  Local search finds it. Each phase gets report/<phase> and results/<phase>; container logs are saved too.

**Defects found and fixed:**
- **~270 console errors on every dashboard load**: the dc template's `onClick="{{ … }}"` placeholders are inline
  handlers to the browser, and the CSP reports each one every time the markup is parsed (page parse, the runtime's
  DOMParser, its `<template>` compile). `core_routes.encode_event_placeholders` serves them in the runtime's own
  encoding (`sc-camel-on-click`, decoded by support.js `CAMEL_ATTR`), only inside `<x-dc>` (the logic script after it
  uses the same names as JavaScript: encoding the whole page broke the dashboard, caught by the tests).
- **Hermes's `/v1/skills` always 500s** in 0.22.4/0.22.5 (and main on 2026-10-23): it passes `include_editorial` to a
  `find_all_skills` that doesn't take it. Agent Control showed "Skills: Failed to enumerate skills" on every open.
  The connector serves `/vesper/skills`; agents.js uses it.
- **Chat with any model connected through the guide's Local route (Ollama, LM Studio) lost every reply**: the
  dashboard switches the session with `require_model_lock: true`; Hermes resolves the named custom endpoint
  ("ollama-11434") to agent.provider "custom", and its confirmed-lock check calls that a mismatch and discards the
  finished turn. `wiring._accept_named_custom_providers` accepts exactly provider "custom" + the locked model.
- **First install's gateway sometimes never started** (Hermes's s6 race: `hermes gateway start` registers the slot,
  `s6-svscanctl -a`, then `s6-svc -u` before s6-supervise attached: "supervisor not listening"). Hermes's own image
  never hits it because its gateway is registered at boot. `Gateway.start` waits for `s6-svok` and starts again; the
  install logs the start output, retries with a restart, and on failure shows the gateway log's last lines.
- Setup spent ~1 min starting the Hermes CLI twice per key: `config_get` now reads config.yaml (CLI fallback).
- A failed Connect answered HTTP 422 (a console error for a normal "not that address"): it answers 200 + checks.
- `/command` (s6 tools) is on the image's PATH.

**Friends:** the default build is now the light one (`JARVIS_LOCAL_VOICES=0`, 1.6 GB); the NAS stack keeps the
voices. SHARING.md's quick start works from the share bundle (`scripts/make-share-bundle.sh`).

**Live since 2026-10-23 20:00 PDT.** Rehearsed first on a clone of the live dataset with the real JARVIS 1 image as
the old stack (`deployment/nas/rehearse.sh`: cutover, install, Playwright smoke with a real chat, rollback back to
JARVIS 1, clean). Then `cutover.sh` on the live stack: snapshot `SSDCACHE256/jarvis@pre-jarvis2-20261023-2000`,
stack backup `/mnt/hdd_2tb/configs/jarvis1-backup-20261023-2000`, container `jarvis` (image `localhost/jarvis:local`,
with the voices), Hermes 0.22.5 installed into the live data with the JARVIS persona (old SOUL.md kept as
SOUL.md.before-jarvis). Same 5 cron jobs and next runs, Telegram connected (2/2 platforms), 85 sessions, model
anthropic / claude-fable-5; the smoke suite with a real chat passed on LAN, and the Tailscale URL serves the dashboard.
Going back: `sudo sh /mnt/hdd_2tb/configs/dockge/stacks/jarvis/rollback.sh /mnt/hdd_2tb/configs/jarvis1-backup-20261023-2000`
(loses anything saved since the cutover). `jarvis-next` (the user's test instance) was removed on 2026-10-23.

### 36. JARVIS 2.1 for friends' servers: a login, the owner's voices, a security audit, a published image (2026-10-24)

The user asked whether JARVIS was ready to ship to friends running TrueNAS or ZimaOS, to make it so if not, to publish
it to their GitHub (account Recl1p), to ship their exact Pocket TTS voices for JARVIS, Ultron and Cortana, and to audit
it for vulnerabilities first. It wasn't ready: no login (so it was bound to 127.0.0.1, useless on a headless server),
an open noVNC desktop on 6080, a build from source on the server, and no TrueNAS/ZimaOS instructions.

**Login (launcher/jarvis_launcher/auth.py, server.py).** One password for everything on :8642, created on the first
visit (`/jarvis/login`; or `JARVIS_PASSWORD`; `JARVIS_AUTH=off` turns it off). A request passes with the session cookie
(HttpOnly, SameSite=Lax, 30 days), the password as Basic auth (scrypt-checked, then cached 5 min), or Hermes's API key
as Bearer. Open without it: health, the login page and its assets, the connect script/connector tarball/SOUL.md and
`/jarvis/api/pair` (a one-time code), the web-app manifest, sw.js and icons; public paths are matched on the raw path.
Wrong passwords from one address: 5 free, then a lockout doubling from 30 s to 15 min (no counting before a password
exists). Changing the password needs the current one and ends other sessions. The setup screen has a Password card and
Log out; its API client goes back to the login on a 401.

**Desktop behind the login.** The launcher serves noVNC at `/desktop/` (WebSocket at `/desktop/websockify`, with the
subprotocol passed through and 64 MB messages) with `X-Frame-Options: SAMEORIGIN`; websockify listens on 127.0.0.1
unless `JARVIS_DESKTOP_LISTEN` (the user's own NAS sets 0.0.0.0 to keep its Tailscale :6080 address). Only 8642 is
published. The dashboard's BROWSER command opens `/desktop/vnc.html?...&path=desktop/websockify`.

**The owner's voices, encrypted (voices/, image/cont-init.d/15-jarvis-voices).** The live voices are Pocket TTS clones
of clips from the films/game (vesper-voices/*.wav, with Kyutai's gated cloning weights). Exported Pocket TTS voice
states load in the PUBLIC model (`kyutai/pocket-tts-without-voice-cloning`): seeded generation from the exported state
on the public model was bit-identical to the live clip on the cloning model (max difference 0.0, all three personas).
The pack (three states, manifest, SHA256SUMS; 18 MB) is AES-256 encrypted (`openssl enc -aes-256-cbc -pbkdf2 -iter
600000 -md sha256`) and copied into the image; at start, as hermes, `JARVIS_VOICE_KEY` unlocks it into
`$HERMES_HOME/vesper-voices/pack/`. Wrong/no key: original voices, normal start. The key is NOT in the repo; it lives in
the user's `C:\Users\lionk\jarvis-src\private\voice-key.txt` and in the friend YAML that `scripts/friend-files.sh`
writes. Why encrypted: Docker can't pull a private image without a token, so the image is public, and the voices are
real actors' (Kyutai's terms forbid cloning without consent): they must not be usable by anyone who pulls the image.
Pocket TTS picks: the user's own clip if it can clone, else the pack, else the shipped clip if it can clone, else the
catalog voice (`PocketEngine.pick_voice`); `/vesper/voice/status` reports which per persona and the guide's Voice step
says the voices came with it. The image build has `JARVIS_LOCAL_VOICES=pocket` (Pocket TTS only, pinned by the OmniVoice
lock, the same torch 2.8.0 + pocket-tts 3.3.0 as the live NAS).

**Security audit, fixed:**
1. `/vesper/files` served any absolute path to anyone (only Hermes's credential blocklist), e.g. the browser desktop's
   cookie store. 2. In "I already have Hermes" the connect script opens that Hermes's API server to the LAN, and its
   `/vesper` page (with the API key stamped in) was open to anyone. Fix for both: the connector's middleware answers
   `/vesper`, `/vesper/files|status|weather|radio|music/tracks/<id>` only to requests carrying the API key as Bearer or
   as `X-JARVIS-Launcher` (the launcher adds it after its login and drops any copy a browser sends); the launcher's
   connection test vouches the same way. 3. Downloads now also refuse JARVIS's private files (`jarvis/`, the desktop
   profile `openjarvis/browser`, `vesper-voices/pack`, `pocket-tts/`, `.ssh`). 4. React, ReactDOM and Babel are
   vendored in `assets/vesper/vendor` (same SRI hashes, served immutable); unpkg is gone from the CSP. 5. The login's
   `next=` refuses whitespace/control characters (browsers strip tabs: `/\t/evil` became `//evil`). 6. The connect
   script only embeds a plain `http(s)://host[:port]`. 7. cont-init: `10-jarvis-data` moves aside a symlink planted in
   place of `jarvis/` or `logs/` and uses `chown -h` (root would otherwise have handed the link's target to hermes);
   `15-jarvis-voices` runs as hermes. 8. The image runs `apt-get upgrade` over the pinned base. Checked and fine: no
   credentials in the tree or its history; chat text is never rendered as HTML; uploads sanitize names; weather/radio
   are not open proxies; everything from the data volume runs as hermes; the Hermes installer is fetched over verified
   HTTPS and its SHA-256 recorded.

**Shipping.** Image `ghcr.io/recl1p/jarvis` (amd64; tags 2.1.0, 2.1, 2, latest), public; repo github.com/Recl1p/jarvis,
private, pushed as one fresh commit (the local `connector` branch keeps the history, 112 MB with the old fork).
`compose.yaml` pulls `:2`; `compose.build.yaml` builds; `install/truenas.yaml` (Apps > Install via YAML, named volume)
and `install/zimaos.yaml` (App Store > custom app > Import, /DATA/AppData/jarvis/data, x-casaos metadata) are what
friends paste, with the key filled in by `scripts/friend-files.sh <key file> <out>`. `scripts/publish-image.sh <version>`
or a `v*` tag (`.github/workflows/image.yml`, actions pinned by SHA) publishes. INSTALL.md replaces SHARING.md; the
tarball scripts are gone. JARVIS and the connector are 2.1.0.

### 37. 2.1.1: OmniVoice as an optional install, and the NAS stack on the published image (2026-10-24)

The user wanted their Dockge stack to update with Dockge's **Update** button, and OmniVoice optional for other users
(those with GPUs); they don't use OmniVoice themselves.

**OmniVoice from the setup screen (launcher/jarvis_launcher/voices.py).** The published image keeps only Pocket TTS.
The OmniVoice card (managed mode) installs OmniVoice into `$HERMES_HOME/omnivoice`: `uv venv --python 3.12`, then the
image's own lock (`/opt/omnivoice/requirements.txt`) with torch/torchaudio switched to `+cpu` or `+cu128` (the same
2.8.0; the CUDA set resolves against download.pytorch.org/whl/cu128, checked with `uv pip compile`), then the model
snapshot (k2-fsa/OmniVoice @ c5fdb5cc), a probe (`torch.cuda.is_available()`), and the `.installed` marker last.
Free space checked first (12 / 18 GB); a reinstall or a half-done one is wiped first; Remove deletes the marker first.
The card shows the GPUs `nvidia-smi -L` lists. API: `GET /jarvis/api/voices`, `POST /jarvis/api/voices/omnivoice
{variant}`, `POST .../remove`, log `?which=omnivoice`. The connector (`vesper_voice.omnivoice_root`) uses the image's
OmniVoice if a build has it, else the data volume's once the marker exists; the worker runs on `cuda:0` in float16
when torch sees a GPU (`tts.omnivoice.device`: auto/cpu/cuda; no GPU -> CPU), and reports the device. Compose files
carry a commented `deploy.resources.reservations.devices` NVIDIA block; INSTALL.md has the steps (TrueNAS: Apps >
Configuration > Settings > Install NVIDIA Drivers). The GPU path is untested on real GPU hardware (none here).

**The NAS stack** (`deployment/nas/compose.yaml`) now runs `ghcr.io/recl1p/jarvis:2` (no build; same mounts, Tailscale
:6080, env), so Dockge's Update pulls and restarts it. `update.sh` = pull + recreate + health check (the connector
check with the API key, read inside the container) + rollback to the image that ran before (`jarvis-rollback:<stamp>`,
three kept). Watchtower stays off for it. The image no longer has OmniVoice baked in; the user's voices keep coming
from their own clips through Pocket TTS (their cloning weights are in the data volume).

## Current status / what's still open

- **Item 11 — applied 2026-09-13**: after a host reboot cleared the Podman hang,
  `deployment/share-hermes-brain.sh` recreated `jarvis-hermes`. Verified: container healthy,
  Telegram/api_server connected, `/opt/data/memories` and `/opt/data/skills` are bind mounts of
  `~/.hermes/{memories,skills}` (identical MEMORY.md hash on both sides, 63 skills),
  `/home/akhdaier/{Documents,Downloads}` resolve, `hermes`/`pip` on the terminal PATH, web backends
  exa/keenable, `/opt/data` ownership intact. Still open: the `/etc/profile.d` PATH fix in
  `Dockerfile.hermes` lands on the next image rebuild (the live `/opt/data/.profile` covers it now).
- **The one real open loop**: the auth-permission fix (item 3 above) has been verified from the
  backend side only — ownership held stable through both a real chat exchange and a real cron
  execution after the fix. **The user has not yet personally confirmed in their own browser that
  chat works.** A future session picking this up should treat that as open, not assume it's
  fully resolved until the user says so.
- **Low priority, not blocking, no action pending**: OpenRouter and Nous fallback providers are
  currently down due to a billing/credit issue on those provider accounts. This is not fixable
  from this codebase — it's been flagged to the user, and there is nothing to do here unless they
  raise it again.
- **Low priority, optional, no action pending**: a `BRAVE_SEARCH_API_KEY` placeholder for the
  web-search feature was offered to the user once. They neither accepted nor declined. Don't
  chase this unless the user brings it up.
- **Items 5-10 (chat attachments feature + 200MB document-limit raise + scanned-PDF OCR-degrade
  fix + partial-text-recovery fix + front-matter text-cap fix) — CLOSED, real end-to-end
  confirmation obtained**: deployed to the running container (`jarvis-hermes` is on image
  `165b48600a52`, healthy, as of 2026-09-11). Full real-world confirmation chain: the user attached
  an 841-page test PDF and asked for a two-chapter summary; the first attempt completed the task
  end-to-end (extraction → chat → two summary PDFs generated and sent to Telegram, confirmed via
  Telegram's own API response) but only delivered front matter/TOC content (item 10's bug); after
  item 10's fix, a retry in the *same already-open browser tab* showed identical front-matter-only
  results (looked like the fix hadn't worked), traced to `dashboard.dc.html`'s chunking logic being
  client-side JS that only loads on page reload — re-attaching in an already-open tab keeps running
  pre-fix JS regardless of what's deployed server-side; after an actual page reload, the model
  confirmed seeing real chapter body text ("large chunks of actual textbook body text from later
  chapters"). **This is real, non-mocked, end-to-end proof the attachment pipeline delivers full
  document content to the model correctly** — the strongest verification level any item in this
  whole arc (5-10) has, well beyond the static/unit-test checks items 5-9 individually relied on.
  What happened next was *not* a bug: with real content in context, the deployed assistant declined
  to summarize further, correctly identifying the file as a piracy-sourced copy of a currently-sold
  copyrighted textbook (it referenced prior attempts via Z-Library/Libgen links and renamed
  filenames) and refusing to reproduce or work from extended verbatim excerpts. No attempt was made
  to route around that refusal — it reflects the assistant's own judgment, not an infrastructure
  problem. **Remaining minor gap, not blocking**: an image and a plain .txt/.docx attachment still
  haven't been explicitly confirmed in-browser (only the PDF/document path has real user evidence);
  if the composer's 📎 button or attachment chips ever don't render, first suspect a `dc`-template
  binding-key mismatch between the template's `{{ }}` placeholders and the render object's keys
  (see item 5's "Not verified" note) rather than assuming the whole feature is broken. Native
  per-provider PDF vision blocks (the task's suggested stretch goal) were deliberately not built —
  see item 5 for why and where the extension point is if a future session wants that specifically.
  **Lesson for future `dashboard.dc.html` changes**: always tell the user to fully reload the page
  after a client-side JS deploy, not just retry the action that triggers it — this exact confusion
  will recur otherwise. Rollback chain if ever needed (each tag is the image immediately before
  that item's change): `localhost/jarvis-unified:pre-text-cap-raise-20260911` (= `3893451cbce9`,
  undoes item 10 only) or
  `localhost/jarvis-unified:pre-partial-ocr-recovery-20260911` (= `63afc6a6bf08`, undoes items 9-10,
  keeps item 8's warning-only degrade) or
  `localhost/jarvis-unified:pre-ocr-fix-20260911` (= `37331c0f4b66`, undoes items 8-10) or
  `localhost/jarvis-unified:pre-200mb-doc-limit-20260911` (= `0de5adc587ee`, undoes items 7-10) or
  `localhost/jarvis-unified:pre-attachments-20260911` (= `404ccec310fc`, undoes items 5-10
  entirely) — `docker tag` the chosen one back to `:local` and re-run the compose recreate.
- **Operational gotcha reinforced this session**: rootless Podman's CLI/storage layer has now
  wedged twice on container build/recreate operations (see containerplan.md's original incident
  and item 6 above), both times with no diagnosable stale lock or D-state process, both times
  fixed only by a full host reboot. If a `docker`/`docker compose` command targeting
  `jarvis-hermes` hangs with no forward progress for more than a few minutes, don't spend time
  debugging — ask the user to reboot the host. Also note: containers do not survive a host reboot
  on this machine (no restart-on-boot linger service enabled for rootless Podman) — always check
  `docker ps -a` for `jarvis-hermes` after any reboot and recreate it if it shows `Exited`.
