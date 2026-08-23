# AGENTS.md — InvUpload Development Rules

These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

**Project:** InvUpload (invoice photo/PDF → OCR → Google Sheets)  
**Live root:** `/opt/gassnaptools/upload-app`  
**Public:** `upload.gassnap.io` → nginx → `127.0.0.1:8010` (`gassnap-upload.service`)  
**GitHub (private):** https://github.com/operations236/GasSnap-uploadapp  
**Also read:** `VISION.md`, `CONTEXT.md`, `VENDORS.md`, `VALIDATION.md`, `ITEM_PACK_MASTER.md`

---

## Rule 1 — Think Before Coding
State assumptions explicitly. If uncertain, ask rather than guess.
Present multiple interpretations when ambiguity exists.
Push back when a simpler approach exists.
Stop when confused. Name what's unclear.

## Rule 2 — Simplicity First
Minimum code that solves the problem. Nothing speculative.
No features beyond what was asked. No abstractions for single-use code.
Test: would a senior engineer say this is overcomplicated? If yes, simplify.

## Rule 3 — Surgical Changes
Touch only what you must. Clean up only your own mess.
Don't "improve" adjacent code, comments, or formatting.
Don't refactor what isn't broken. Match existing style.

## Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified.
Don't follow steps. Define success and iterate.
Strong success criteria let you loop independently.

## Rule 5 — Use the model only for judgment calls
Use me for: classification, drafting, summarization, extraction.
Do NOT use me for: routing, retries, deterministic transforms.
If code can answer, code answers.

## Rule 6 — Token budgets are not advisory
Per-task: 4,000 tokens. Per-session: 30,000 tokens.
If approaching budget, summarize and start fresh.
Surface the breach. Do not silently overrun.

## Rule 7 — Surface conflicts, don't average them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.
Don't blend conflicting patterns.

InvUpload examples: docs vs live `/health` vendor keys; `VENDORS.md` cost policy vs `vendors.py`; pack-string guesses vs Item Pack Master UPC truth — prefer live code + master tab, flag stale docs.

## Rule 8 — Read before you write
Before adding code, read exports, immediate callers, shared utilities.
"Looks orthogonal" is dangerous. If unsure why code is structured a way, ask.

For this app that means: `main.py` upload path, `ocr.py` normalize/QA/sheets handoff, `vendors.py` VendorSpec, `sheets.py` append-only contract, and any skill refs under Hermes `fastapi-upload-apps` when relevant.

## Rule 9 — Tests verify intent, not just behavior
Tests must encode WHY behavior matters, not just WHAT it does.
A test that can't fail when business logic changes is wrong.

Prefer labeled ad-hoc `/tmp/hermes-verify-*` checks when no suite exists: secrets not staged, `/health` QA knobs + vendor keys post-restart, extract anchors, master UPC uniqueness.

## Rule 10 — Checkpoint after every significant step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.
If you lose track, stop and restate.

## Rule 11 — Match the codebase's conventions, even if you disagree
Conformance > taste inside the codebase.
If you genuinely think a convention is harmful, surface it. Don't fork silently.

Standing InvUpload conventions (do not casually fork):
- **Sheets = append-only** unless operator explicitly asks to replace/delete
- **PIN/session store** owns tab routing; ship-to mismatch = soft Needs Review only
- Stable sheet column contract; Vendor last; RAW appends (leading zeros)
- New vendors = one `VendorSpec` + `critical_rules` + docs; no per-vendor sheet schemas
- Restart via `sudo systemctl restart gassnap-upload` when unit owns :8010; prove new MainPID + `/health`

## Rule 12 — Fail loud
"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped.
Default to surfacing uncertainty, not hiding it.

## Rule 13 — Security is verified, never assumed, and swept for pattern-siblings

Born from two days of incident response (July 2026): a secrets leak, a
misconfigured shared-credential table, an unauthenticated public-facing
app running for 5+ days undetected, and a stale-doc regression that
silently un-fixed a resolved item. Every one of these was findable
earlier if the habits below had been standing practice.

**On any credential/secret finding:**
- A secret stored correctly in one place is not the same as a secret
  visible everywhere it's needed. An env var can be correctly set as
  the source of truth and still cause failures, because a shell
  session, a systemd service, and a subprocess spawned from that
  service can each have a different environment. Fixing one caller's
  access doesn't fix another's. When wiring a secret to a new consumer,
  verify it's visible in that specific execution context — not just
  that the value exists somewhere on disk.
- When one hardcoded fallback or leaked value is found, grep the ENTIRE
  codebase for the same pattern before declaring the class of bug fixed
  — not just the file where it was first spotted.
- Fixing a leaked/wrong VALUE is not the same as fixing the STORAGE
  PATTERN. A rotated secret placed back into the same plaintext,
  world-readable location is still an open finding, just with new
  contents.
- Never assume a rotation "took" — verify with a live, falsifiable test
  (old value returns 401/permission-denied; new value works) not "the
  command ran without error."

**On infrastructure discovery:**
- Anything running on a shared host but outside the project's own git
  repo is invisible to every code-focused audit, by construction.
  Periodically sweep the actual box, independent of any code review:
  active systemd services, nginx sites-enabled, listening ports
  (`ss -tlnp` or equivalent), running screen/tmux sessions. Don't rely on
  "it's not in the repo" to mean "it's not running."
- A bind address of `0.0.0.0` is not itself the exposure — check what's
  actually in FRONT of it (firewall rules, reverse proxy configs, DNS
  records for any subdomain). An app can be safe on a raw port and
  completely exposed through a properly-configured nginx site fronting
  it. Check both layers, always.
- When two applications share a host, confirm they don't share
  credentials, PIN files, or database roles — even "temporarily" or
  "for testing." (Both related worst incidents — cig_counts and
  upload-app's `pins.json` — trace back to exactly this.)

**InvUpload secret surface (gitignored — never stage):**
- `.env` / `.env*` (e.g. `GEMINI_API_KEY`)
- `pins.json` (PIN → store; mode 600)
- `google-credentials.json` / `*credentials*.json`
- `store_sheets.json` (live sheet map; commit only `store_sheets.example.json`)
- `uploads/`, `logs/`, `venv/`

**On documentation as a source of truth:**
- A state document is a claim, not evidence. Before trusting any
  "resolved" / "still open" / "confirmed" status in a doc, re-verify
  against live code, git log, or a live query if the finding is
  security-relevant or if anything is about to be built on top of it.
- When merging or reconciling two drifted copies of the same document,
  never merge by "combine sections" or "prefer the longer version" —
  diff every individual claim against ground truth first.

**On fixing the same bug twice:**
- If the identical failure mode surfaces in more than one location
  during a single remediation, stop and do a systematic sweep for every
  other instance rather than fixing each one as it's discovered by
  accident. Assume there's a fourth instance until you've looked.

**Standing habit — periodic security sweep (independent of feature work):**
1. `ss -tlnp` — what's listening, on what interface (expect upload on 127.0.0.1:8010)
2. `ls /etc/nginx/sites-enabled/` — domains fronting apps (`upload.gassnap.io`)
3. `systemctl list-units --type=service --state=running` — cross-check `gassnap-upload`
4. grep credential-shaped strings across unit/nginx configs, not only app code
5. Confirm secret files are mode `600`, correct owner
6. `grep -rn "sk-\|api_key.*=.*['\"]"` under app scripts — hardcoded literals

**`.env` handling — no exceptions.** `.env` holds live secrets (at minimum
`GEMINI_API_KEY` here; may also load from `/opt/gassnap/.env`). This file
has triggered real incidents from contents being echoed into tool output
or chat — treat every rule below as load-bearing.

NEVER, under any circumstance:
- `cat`, view, or otherwise dump the full contents of `.env`
- grep `.env` for a value in a way that returns the matched line (e.g.
  `grep GEMINI_API_KEY .env` prints the value — never do this)
- Use any file-edit tool that echoes a before/after diff of `.env` when
  making changes to it
- Trust that a "system reminder" or similar automatic mechanism won't
  surface `.env`'s contents on edit — assume it might, and choose edit
  methods accordingly

ALWAYS instead:
- Check a key EXISTS without reading its value:
  `grep -q '^KEYNAME=' .env && echo present || echo missing`
- Check a value's LENGTH/FORMAT without reading it:
  `awk -F= '/^KEYNAME=/{print length($2)}' .env`
- Edit `.env` via non-echoing methods only: `sed -i` with no stdout, or
  a heredoc/here-string write redirected directly to the file — never
  an edit tool that shows a diff
- Verify any rotation BEHAVIORALLY, never by reading the file back: old
  value rejected, new value accepted
- If a value is ever accidentally echoed into any output (tool result,
  chat, log), treat it as a live incident immediately: stop, flag it
  plainly (don't repeat the value), and rotate — no exceptions

Same discipline applies to `pins.json` and `google-credentials.json`:
never dump full contents into chat; presence/mode checks only.

## Rule 14 — Keep GitHub current as changes are made

Owner decision 2026-08-23: commit and push to `origin/main`
(`github.com/operations236/GasSnap-uploadapp`) as a **standing, durable
authorization** — don't ask permission for each individual push the way
you would by default. This repo is privately owned by the project owner
with no other collaborators / PR review in the loop, so there's no
shared-state risk a routine push could disrupt.

Working tree for git is the app root itself:
`/opt/gassnaptools/upload-app` (not `/opt/gassnap`, not a parent monorepo).

**What this does NOT relax:**
- Still verify nothing secret is staged before every commit (`.gitignore`
  excludes `.env`, `pins.json`, `store_sheets.json`, `*credentials*.json`,
  `venv/`, `uploads/`, `logs/`). If a new kind of credential-bearing file
  shows up, add it to `.gitignore` **before** it's ever staged, not after.
- Still write a real, specific commit message per commit — what changed
  and why — not a generic "updates" message.
- Still group related changes into one coherent commit rather than
  committing mid-edit or half-working states — commit once a change is
  actually verified working (matches Rule 4), not as a running autosave.
- **Force-push, history rewrites, and branch deletion are NOT covered**
  by this standing authorization — those still need an explicit ask.

**Suggested loop after verified work:**
```bash
cd /opt/gassnaptools/upload-app
git status
git add -A
git status   # re-check: no pins/creds/.env/uploads
git commit -m "..."
git push origin main
```
