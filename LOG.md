# LOG.md — Warden

## 2026-10-11 — Operator console: HTMX dashboard page

**Status:** Done (117 passed: 108 + 9 new; mypy strict clean, 27 files)

### What changed
- `src/warden/app/dashboard.py` — PURE server-rendered console (lock 12: no build step, HTMX
  from CDN): `page_shell` (escaping + dark theme), `journal_feed_fragment` (vanilla
  `EventSource("/stream?with_history=true")`, color classes per event, capped at 200 lines),
  `inbox_fragment` (HTMX `hx-get="/inbox"` every 3s with `Accept: text/html` — hits the
  existing partial, no new endpoint), `lease_card_fragment` (live policy state + plain form
  POSTing to `/toggles/lease`), `run_launcher_fragment`, and `render_dashboard`
  (2-column grid). All dynamic values escape via `_esc`.
- `src/warden/app/main.py` — `GET /dashboard` (renders from the live `state.lease_policy`)
  and `POST /run-web` (no-JS form bridge: drives the SAME governed graph as `/runs`, then
  shows the thread link back to the console; graph missing -> 503 fail-closed).
- `tests/test_dashboard.py` — 9 tests: shell escaping (title XSS neutralized), EventSource
  wiring, inbox poll wiring, lease card state reflection (ON/OFF + checked), grid assembly,
  endpoint 200 `text/html` with all three wirings, `/run-web` through a REAL governed graph
  (in-memory policy-allow graph) + back-link + echoed thread, 503 without graph.

### Notes
- Dashboard reads existing endpoints only (`/stream`, `/inbox`, `/toggles/lease`, `/runs`) —
  the SSE/streaming + toggles slices compose; zero new JSON surface added.
- The lease card's Apply button uses hx-post with hx-vals JS; the surrounding form degrades
  to a normal POST when JS is off.
- Raghav's approval-card checklist: this page + the /inbox partial is the consumable base —
  card styling and the Blast Radius diff slot in `inbox_fragment`'s swap region.

## 2026-10-11 — Web approval flow: inbox + gate decisions (lock 8 slice)

**Status:** Done (108 passed: 94 + 14 new; mypy strict clean, 25 files)

### What changed
- `src/warden/hitl/gates.py` — the HITL web glue:
  - `NamedKeys` (lock 8): env `WARDEN_APPROVER_KEYS="alice=s3cret,..."`; identity = the KEY
    (client-supplied names are never used for attribution — `approver: alice` is a true
    statement in the journal). Unknown/missing key -> `ApproverError` -> 401.
  - `GateRegistry`: the approvals inbox as a PURE projection over journal events (lock 3:
    journal is evidence; the registry is only a view). `gate_requested` -> pending
    (idempotent on dup), `gate_decided` -> decided strip; single-decision guard
    (`assert_pending` -> 409 path); `resume_value()` = the exact Command(resume=...) payload
    shape (a plain dict to a parked thread starts a SECOND run — LEARNINGS #15).
- `src/warden/app/main.py` — `GET /inbox` (JSON or HTMX partial, always re-projected from the
  journal bus so it can't drift), `POST /gates/{gate_id}/decision`:
  auth (Warden-Key) -> 401s; verdict normalization -> 422; unknown gate -> 404; decided gate
  -> 409 (journal never rewritten); journal-FIRST (`gate_decided` appended before resume —
  lock 3 write-ahead discipline), then `Command(resume=...)` on the gate's thread_id; DENY
  resumes into the terminal Denied path (no dispatch). Dev default approver keys
  (`warden-alice`/`warden-bob`) overridable by env.
- `tests/test_hitl_gates.py` — 14 tests: key parsing/identity/attribution rules, registry
  projection (idempotency, unknown decided event, 409 guard, resume shape), endpoint auth
  matrix + validation + journal-untouched-on-failure, succeeded-path ordering
  (gate_decided BEFORE action_completed), real-PG e2e: genuinely parked graph resolved
  THROUGH the web endpoint — approve dispatches, deny aborts unexecuted.

### Bugs caught and fixed during verification
1. **The seam's `gate_requested` events carried `thread_id: ""`** — inside ops_node the
   Action was built with `run_id=thread_id` but WITHOUT `thread_id=`; the endpoint's inbox
   view then had no thread to resume (probe showed gate_requested with empty thread).
   Fix: `make_action(..., thread_id=thread_id)` — journaled identity is now complete, and
   the web flow resumes the correct parked thread.
2. Python 3.11 f-string with nested same-quotes in the HTMX builder — SyntaxError caught at
   test collection; rewrote that fragment with concatenation (no nested quoting tricks).
3. mypy strict narrowing on a reused `view` variable in `GateRegistry.consume` — separated
   the two branches' variables.

### Demo story (Phase 1 beats, now fully web-driven)
POST /runs "email the weekly report" -> SSE sees gate_requested -> GET /inbox shows the
request -> POST /gates/{id}/decision with Warden-Key -> email dispatches with
`decided_by: alice` in the journal. Kill the process mid-park, restart, decide from the web
— write-ahead intent still holds.

## 2026-10-11 — Lease toggles + per-run lease card (Yash task 5)

**Status:** Done (94 passed: 85 + 9 new; mypy strict clean, 23 files)

### What changed
- `src/warden/leases.py` — `LeasePolicy(enabled=True, ttl_seconds=300.0)`, the shared mutable
  knob. `check()` = the enforcement point (OFF -> HELD unconditionally; ON -> fail-closed
  evaluate); `grant_ttl()` read at every supervisor grant, so TTL changes hit the NEXT route;
  in-flight handoffs are never re-timed (a shrunk TTL must not strand an approved mid-flight
  action). Default ON: toggles exist to be observable, not to silently weaken custody.
- `src/warden/multi_agent.py` — supervisor + ops closures take the shared `LeasePolicy`;
  `build_multi_agent_graph(lease_policy=...)` wires BOTH to the same object (no policy passed
  -> legacy `lease_ttl_seconds` seeds a private one). Toggle off/on or change TTL at runtime
  WITHOUT rebuilding the graph. `ops_node` factory de-mangled after a bad str_replace (public
  API unchanged: `ops_node(executor, *, lease_policy=None)`).
- `src/warden/app/main.py` — `GET/POST /toggles/lease` (partial-body PATCH semantics: only
  provided keys change; ttl<=0 -> 422, policy untouched); `GET /runs/{thread_id}/lease`
  content-negotiated: JSON (lease + remains_seconds + verdict + policy) or `text/html` ->
  server-rendered HTMX partial (`lease-card`, hx-pollable, XSS-escaped via `_esc`). Lease
  state read from the CHECKPOINTER (agraph_state), per lock 3 (checkpointer owns custody).
- `tests/test_lease_toggles.py` — 9 tests: policy units (OFF=unconditional HELD incl. corrupt
  input, symmetric re-arm, state shape), real-PG runtime toggling with a shared policy object
  (refuse -> toggle OFF -> same graph object reaches the gate; OFF -> ON re-arms refusal),
  TTL-live (0 -> 300 lets the SAME graph park), HTTP endpoints via same-loop ASGITransport
  (round trip incl. partial-body semantics, invalid-TTL 422, per-run card JSON + HTMX).

### Debugging story (probes 1-9, all red->green)
1. `ops_node`'s closed-over `lease_policy or _default` made the no-shared-policy branch test
   the module default — confusing during debugging; tests now always pass the shared policy
   explicitly (the honest toggle contract).
2. The probe sequence showed OFF runs returning `__interrupt__` with no dispatch — WRONG
   inference initially ("toggle broken"); real cause: the seam's policy gate escalates
   send_email AFTER custody passes. Toggle tests now assert custody-passes -> PARKS at the
   gate, which is the true OFF semantics; custody refusal (ON) shows BEFORE any gate.
3. Attempted `create_app()` factory rejected: routes close over the module `state`, so a
   copied router would still share state. Replaced with an explicit testability note:
   ASGITransport skips the lifespan -> swap `state.bus/lease_policy/manager/graph` fixtures
   and hit the REAL routes (documented at the `app` definition).
4. mypy strict: langchain RunnableConfig (TypedDict) required for `ainvoke` args, and
   re-exported classes must be imported from their own module (`warden.streaming`), not via
   `app.main`.

### Next
- Yash's slice is functionally complete (multi-agent, leases, policy engine, SSE, toggles).
- Remaining nice-to-have: HTML dashboard page consuming /stream + lease-card polling.

## 2026-10-11 — Live SSE streaming (Yash task 4)

**Status:** Done (85 passed: 73 + 12 new; mypy strict clean, 22 files)

### What changed
- `src/warden/streaming.py` — `WardenJournalBus`, an in-process bus that IS a `JournalSink`:
  pass it as `GuardedExecutor(journal=bus)` and every seam event (requested/denied/
  gate_requested/completed/failed/gate_decided) is a live dashboard feed — one telemetry path
  (lock 1). Fan-out into per-subscriber bounded asyncio queues with drop-oldest (a slow client
  loses only its own frames, never stalling the run; end-of-stream sentinel never dropped);
  bounded catch-up history for late subscribers; monotonic ids + wall-clock ts per event.
  Pure SSE layer (`sse_format` / `sse_frame_retry` / `parse_sse_frame`): keepalive comments on
  idle intervals, multi-line JSON folded to a single data line, `retry:` on clean end.
- `src/warden/app/main.py` — FastAPI surface (lock 12, JSON-clean): `GET /stream` (SSE,
  `text/event-stream`, no-cache headers, unsubscribe in `finally` even on client disconnect),
  `GET /stream/health` probe, `POST /runs` driving the multi-agent graph. Lifespan reuses the
  CLI's pgserver resolution; startup fails closed without the checkpointer store.
- `pyproject.toml` — added fastapi + httpx (uv).
- `tests/test_streaming.py` — 12 tests: frame shapes and roundtrip, folding, retry; sink
  conformance from the real GuardedExecutor; live receipt, history catch-up, drop-oldest
  bounds ([3,4] kept of 5), unsubscribe sentinel, keepalive; HTTP framing via SAME-LOOP httpx
  ASGITransport (200 + content-type + both journal events + retry termination); deny-path e2e.

### Bugs caught and fixed during verification (TDD-red first)
1. **`unsubscribe` awaited a sync helper** (`await self._offer(...)` -> TypeError). `_offer`
   is intentionally non-async (put_nowait drop loop); removed the await.
2. **Streaming hangs (the interesting one): asyncio queues are LOOP-BOUND.** The threaded
   `TestClient` runs the app in a separate thread/loop — bus queues created in the app loop
   dead-wait in the client loop. Fix: httpx `ASGITransport` (same-loop ASGI), matching the
   LEARNINGS same-loop rule. Recorded as a hard test convention.
3. **End-of-stream race in tests**: `unsubscribe()` pops the map BEFORE the generator grabbed
   its queue (generator start is lazy until first `__anext__`) — frames lost / `StopAsync-
   Iteration`. Fixed tests to enter the generator first; production `sse_stream` grabs the
   queue at generator start which is correct for a connected client.
4. **Client-disconnect leak**: breaking out of `aiter_bytes` early left the feed's `finally`
   pending on the lock-held queue. Endpoint teardown hardens unsubscribe (safe on-already-
   popped), and `close_all()` gives deterministic test/shutdown termination.

### Notes
- Deterministic endpoint test protocol: publish → consume → close_all; no wall-clock races.
- `bus._subscribers` read in /stream/health is an internal probe (documented at the site).

## 2026-10-11 — L1/L2 policy engine (Yash task 3)

**Status:** Done (73 passed: 52 + 21 new; mypy strict clean, 19 files)

### What changed
- `src/warden/policy/engine.py` — `L1L2PolicyEngine`, drop-in replacement for the seam's
  `SequencingPolicy` (identical `evaluate(action) -> str` contract; seam + graph untouched):
  - **L1** fast deterministic rules, zero IO: destructive payload patterns (DROP TABLE /
    DELETE FROM / TRUNCATE / ALTER-DROP / rm -rf) -> `deny` terminal; risky tool names
    (send_email, run_shell, db_write, make_payment, upload_file) -> `escalate`. L1 matches are
    FINAL — vectors never soften a deny (lock 5) and never overrule an L1 escalate.
  - **L2** similarity over `PolicyVectorStore` (Protocol: `search(q, k) ->
    [(similarity, policy_id, verdict)]`): best policy at/above threshold applies its verdict;
    below threshold / empty store / unknown verdict string -> unmatched. Ships
    `InMemoryPolicyStore` (cosine, tests/CLI); Parth's pgvector adapter is a third
    implementation of the same Protocol — engine untouched.
  - **Fail-closed ladder**: L1 match -> L1 verdict; L2 match -> stored verdict; nothing
    recognized -> `escalate` (a human reviews unrecognized actions, never silent allow).
    Invalid stored verdicts fail-closed to unmatched.
  - `HashEmbeddings`: deterministic offline embedder for tests/demo (NOT semantic; production
    wires real embeddings).
- `tests/test_policy_engine.py` — 21 tests: L1 payload matrix, L2 threshold/most-similar/empty
  semantics, L1-final-over-L2, unrecognized->escalate, seam drop-in (deny/gate journal trail,
  allowed path through L2), multi-agent integration on real Postgres (destructive task denies
  short-circuit, email task parks -> approve -> dispatch — now through the real engine).

### Bugs caught and fixed during verification (TDD-red first)
1. **Test similarity mirroring**: HashEmbeddings is bag-of-hashed-tokens — seed texts must
   mirror the engine's exact action text form (`"<name> <payload values>"`) or cosine dips
   (~0.93 for payload-only seeds). Fixed seeds; production embeddings are real vectors.
2. **Invalid stored verdicts could pass through** (e.g. "maybe") and reach the seam's verdict
   branches unread. Fix: verdict validation in `evaluate_l2` — non-seam verdict strings are
   unmatched -> escalate (garbage rows can never authorize).
3. Graph routing: deny test initially fed a task that routed to research; deny needs the
   destructive text INSIDE the ops dispatch payload — fixed the scenario.

### Design notes
- L1 short-circuits the vector search: a fast deny never waits on IO (and works when the
  store is down).
- L2 threshold default 0.8; store-down behavior for InMemory is trivially N/A, pgvector
  adapter should translate connection failure per lock 4 in the seam's Error path.

## 2026-10-11 — Ephemeral lease evaluation logic (Yash task 2)

**Status:** Done (52 passed: 34 + 18 new; mypy strict clean, 17 files)

### What changed
- `src/warden/leases.py` — pure, IO-free lease logic (clock injected, no sleeps in tests):
  `Lease` (holder, thread_id, acquired_at, ttl_seconds, purpose) serializes to a JSON-safe dict so
  the Postgres checkpointer persists it inside AgentState across park/resume. Core calls:
  `grant` (supervisor mints custody), `evaluate(lease, holder, thread_id)` ->
  `HELD | EXPIRED | NOT_HELD`, `reacquire` (idempotent, NO ttl extension on resume, refuses
  stealing another holder's live lease), `require` (exception contract carrying the verdict),
  `remaining`/`expires_at`. Fail-closed: absent/corrupt/wrong-thread/wrong-holder -> NOT_HELD
  (locks 4/5 applied to custody: no provable ownership, no dispatch).
- `src/warden/multi_agent.py` — supervisor grants a lease on every route (`lease_ttl_seconds`
  build knob, default 300s; custody dies with the handoff — cleared on `assigned:""`);
  ops checks custody BEFORE crossing the seam — a dead lease refuses to dispatch (report, no
  journal entry, no gate, no email). Resume-safe: the lease rides checkpointed state, so an
  approval inside the TTL dispatches.
- `tests/test_leases.py` — 18 tests: verdict matrix incl. age>=ttl boundary, zero ttl,
  corrupt/bool-typed fields fail-closed, thread/holder mismatch, round-trip through jsonb-ish
  ints, no-TTL-extension on re-acquire, no-steal, require/remaining/expires_at; graph-
  integrated: supervisor grant -> END (research flow), ttl=0 dead-custody refusal (no park, no
  journal events), real-PG park/resume-inside-TTL dispatch.

### Bugs caught and fixed during verification (TDD-red first)
1. **`reacquire` allowed stealing**: `evaluate(lease, holder)` was NOT_HELD for a wrong holder,
   then fell through to "grant fresh" — takeover of someone else's live lease. Fix: before
   granting, first evaluate against the EXISTING holder; still HELD -> refuse (None).
2. **The graph could not exercise expiry**: the supervisor unconditionally re-grants on routing
   (and the default 300s TTL outlives any test), so a pre-seeded expired lease was silently
   overwritten. Fix: `lease_ttl_seconds` knob on the builder; a ttl=0 run gives dead-on-arrival
   custody, proving the fail-closed refusal path end-to-end.

### Design note
- Wall-clock (not monotonic) is correct for leases: custody must survive park/resume gaps
  that can include a full process restart; `hash()`/monotonic clocks are process-bound.

## 2026-10-11 — Multi-agent architecture + GuardedExecutor seam (Yash task 1)

**Status:** Done (34 passed: 22 pre-existing + 12 new; mypy strict clean, 15 files)

### What changed
- `src/warden/seam.py` — **GuardedExecutor**, the one seam every agent tool call crosses:
  `execute(action, *, approval) -> Allowed | Denied | Pending | Error`. Write-ahead journal of
  `action_requested` before deciding (best-effort `_MemoryJournal`-compatible `JournalSink` proto).
  `SequencingPolicy` stub (deny-wins-over-escalate routed table) is the plug-point for Parth's
  L1/L2 PolicyEngine (same `evaluate` signature). `decide_after_human(gate, decision)` completes
  the gate; DENY terminal (lock 5). Dispatch injectable for tests. Policy-engine internal bug ->
  `Error(policy_engine_failure)` — fail-closed, never allow-by-accident (lock 4).
- `src/warden/multi_agent.py` — **supervisor + specialists** graph (ops / research / finish),
  ONE shared executor instance = one audit trail for the whole cast. Deterministic keyword router
  (LLM-swap later, same node signature). `interrupt()` parks in the specialist node; factory
  closures annotated `Any` (LangGraph add_node overloads are structural).
- `tests/test_multi_agent.py` — 12 tests: seam verdicts + journal trail shape, deny terminal
  (no dispatch), deny-over-escalate, Pending carries gate + no side effects, policy-failure fail-
  closed, approve/deny resolution, approve-does-not-re-escalate regression, supervisor routing,
  multi-agent park/resume with journal ordering, gate-id stability across park→resume→restart.
- `tests/conftest.py` — Windows: `WindowsSelectorEventLoopPolicy()` (psycopg async cannot use the
  ProactorEventLoop; first time these tests ran on this Windows machine).

### Bugs caught and fixed during verification (all TDD-red first)
1. **Approval re-escalated forever**: `decide_after_human` re-called `execute()`, which re-ran
   policy -> `escalate` -> `Pending` loop. Fix: approval IS the allow verdict — approved re-entry
   skips policy via `execute(action, approval=...)`.
2. **Non-deterministic gate ids**: `make_action` drew `uuid4()` per call, but the node re-runs
   from the top at resume (per LEARNINGS #6) — the gate_id the human saw differed from the one
   the approval dispatched, and crash-restart gave a third id. Fix: `action_id` param; ops node
   derives `sha256(thread_id)`-prefixed stable id (hash() is process-salted, digest is not).
3. **Plain-dict resume silently started a NEW run**: passing `{"verdict": ...}` to a parked
   thread instead of `Command(resume=...)` re-ran the graph from START on that thread (task
dropped -> research -> finish; interrupt never consumed it — empty approvals). Confirmed against
   langchain docs ("Command(resume=...) is the only Command pattern intended as input"); caller
   contract documented on `build_multi_agent_graph`.

### Environment learnings (see LEARNINGS.md)
- This Windows checkout had an EMPTY .venv (only `_virtualenv.py`) — `uv run` silently hung
  trying to sync, timing out mypy twice. Rebuild: `rm -rf .venv && uv venv && uv sync --all-extras`.
- psycopg async on Windows needs the selector event loop (conftest fix, applies to all suites).

### Next
- Parth's PolicyEngine drops into `SequencingPolicy`'s slot (same evaluate signature);
- lease-evaluation logic hooks the `assigned` handoff trail (Yash task 2);
- SSE streaming reads the journal sink (Yash task 3).

## 2026-10-11 — re-verification round: harness bug + missing regression tests

**Status:** Done (22 passed, mypy clean, PR #1 merge=CLEAN at ba0c939)

User asked "have you tested everything?" — audit found two gaps, both closed:

1. **Real harness bug**: `/quit` at the `resume>` prompt was consumed as a decision value → demo graph routed to abort (silent deny). Root cause of confusing smoke-test output ("approved: False" where send was expected). Fix: exit commands at the resume prompt return False, thread stays parked. TDD: red test first.
2. **Missing regression tests** for the 3 CodeRabbit fixes from the previous round: added tests/test_cli_chat.py (4 tests: /quit, empty re-prompt, failed-resume re-prompt, re-park loop) + 6-case _redact parametrize in test_checkpoint.py.

Environment learnings (see LEARNINGS.md):
- pgserver `cleanup_mode='stop'` stops the server at process exit (atexit); data dir persists — CLI restart re-attaches. Docstring corrected.
- Passing `--pg` with a URI obtained from a throwaway `get_server` process races the atexit stop → use the CLI's own resolution.
- `tests` is a package (empty `__init__.py` since 4fd3ff0); mypy needs `from tests import cli_chat`, no mypy_path.

Verified: pytest 22 passed (12 checkpoint + 4 cli loop + 6 redact); mypy 0 errors;
CLI e2e: single-process park→approve→sent; two-process park→/quit→restart→approve→sent, approved: True.
Checks after push: CodeRabbit pass, GitGuardian pass, merge=CLEAN, not behind.

## 2026-10-11 — PR #1 filed + CodeRabbit review fixes

**Status:** Done (checks green, PR OPEN — not merged)

- OCR review + 7 fixes committed as `4fd3ff0`; PR #1 opened against main:
  https://github.com/parth5012/warden/pull/1 (linked to thread).
- CodeRabbit triggered via `@coderabbitai review` (auto-skip on OSS repo); 3 comments, ALL applied as `1ad4ce4`:
  1. `_redact` — mask complete URI userinfo (malformed `password` with `@` leaked suffix); urlsplit-based, query `@` untouched; quoted keyword passwords covered. Verified vs 6 cases.
  2. `_prompt_for_resume` empty input → `continue` (stay in prompt loop; returning let plain text take the invoke path on a parked thread).
  3. `_prompt_for_resume` failed resume (None) → re-prompt, don't treat as decision.
- Verified after fixes: pytest 12 passed, mypy 0 errors, redaction case-check.
- Checks: CodeRabbit pass, GitGuardian pass; mergeStateStatus CLEAN; not behind main.
- Babysit loop: stopped at green. PR intentionally NOT merged — `merge-pr` or human merge required.
- OCR config note: gateway model `agy/gemini-3.7-flash-medium` dead upstream; review ran with `--model auto/best-coding` (config backup `/tmp/opencode/ocr-config-backup.json`).

## 2026-10-11 — OCR review round 1 + fixes (pre-PR)

**Status:** Done (verification green)

### What changed
- OCR review (`session db2a663c-f08b-4a6c-bcd6-202c48e02798`, 13 files, 11 findings: 1 high, 6 medium, 4 low-discarded). Config fix required first: gateway model `agy/gemini-3.7-flash-medium` dead upstream (requests hang); added `auto/best-coding` to `~/.opencodereview/config.json` models list (backup at `/tmp/opencode/ocr-config-backup.json`) and ran with `--model auto/best-coding`. One core-review request failed HTTP 402 (partial coverage on `.gitignore/.python-version/pyproject` headers — findings for pyproject still produced).
- Fixes applied (all 7 non-low findings):
  1. `pyproject.toml` — declared direct deps imported by first-party code: `psycopg-pool`, `langchain-core`, `typing-extensions` (were transitive-only).
  2. `pyproject.toml` — unified dev deps: moved `pgserver` from `[dependency-groups]` into `[project.optional-dependencies].dev` (pip `.[dev]` now complete).
  3. `checkpoint.py get_state_history` — `aget_state_history` yields newest-first; added `history.reverse()` to honor "oldest first" docstring contract.
  4. `checkpoint.py resume` — log `resume_value_type` instead of raw resume payload (PII into log aggregation).
  5. `checkpoint.py _redact` — now covers keyword-DSN `password=...` form, not just URI form (invariant: never log conn string unredacted).
  6. `cli_chat.py _prompt_for_resume` — EOF/Ctrl-C safe (returns False → clean exit, was traceback); loops until graph no longer parked (re-park re-prompts instead of sending next input down the wrong path).
  7. `cli_chat.py _resume_and_render` — returns result so callers detect re-park; `/resume` command also re-prompts on re-park.
- Added `.gitignore`; removed `uv init` leftover `src/t3code_377f8a81/`.
- Test strengthened: history test now asserts non-decreasing steps (locks oldest-first contract).

### Verification
- `uv run pytest` → 12 passed (real Postgres).
- `uv run mypy src tests` → 0 errors.
- CLI: EOF at `resume>` exits cleanly (no traceback); park → approve → complete flow green.

### Skipped
- ocr-review: DONE (this round). pr/repo-quality-orchestrator/retro: run at PR filing (current iteration).

## 2026-10-11 — Checkpointing system (Phase 0/1 foundation)

**Status:** Done (verification green)

### What changed
- Project scaffold: `pyproject.toml` (uv, Python 3.11), `src/warden/` package, `tests/`.
- `src/warden/models.py` — `Action`/`Outcome` types, gate types, `CheckpointInfo` view type.
- `src/warden/checkpoint.py` — `CheckpointManager`, the deep module for checkpoint infra:
  - `create(conn_string)` async context manager owns `AsyncPostgresSaver` lifecycle (setup idempotent on every boot).
  - `health_check()` probes the connection (handles connection AND pool).
  - `get_state_info` / `get_state_history` — logged state inspection, **fail-closed**: return `Error("governance_unavailable")`, never raise (lock 4).
  - `resume(graph, config, value)` — logging wrapper around `graph.ainvoke(Command(resume=...))`, fail-closed.
  - Connection string redacted in all logs.
- `src/warden/demo_graph.py` — draft → `interrupt(approval)` → send/abort demo graph (park/resume proof).
- `tests/test_checkpoint.py` — 12 tests vs **real PostgreSQL** (pgserver, bundled, no root): idempotent setup, park/resume approve+deny, no duplicate output on resume, **manager-restart recovery**, history walk, fail-closed after close (state/history/resume), bad-connection startup failure.
- `tests/cli_chat.py` — reusable CLI chat harness: takes any compiled graph, `/state /history /new /resume /quit`, auto-prompts resume when rejoining a parked thread. Auto-starts bundled pgserver (`--pg` / `WARDEN_PG_URI` to override); server intentionally left running so restart-and-resume is demoable.
- `tests/conftest.py` — session-scoped real-PG fixture.

### Design decisions (grilling outcomes)
- CheckpointManager does **NOT** wrap `interrupt()` — interrupt lives inside graph nodes. (Shallow-wrapper rejection.)
- No "attach checkpointer to graph" helper — `builder.compile(checkpointer=...)` is enough.
- Depth earned by: connection lifecycle ownership + fail-closed + health check + redacted logging, not by pass-through wrappers.
- `resume()` is the one logging wrapper kept (explicit user requirement: cohesive import + logging at transitions).

### Verification
- `uv run pytest` → **12 passed** (real Postgres each run).
- `uv run mypy src tests` → **Success: no issues in 12 files** (strict mode).
- CLI crash-recovery smoke test: run 1 parks + quits while parked → run 2 (fresh process, `--thread crash-demo`) detects park, resumes with `approve` → "Email sent", `approved: True`.
- Skipped skills (not applicable): `ocr-review` (no UI), `pr` (no PR/commit requested — not committing without explicit ask), `repo-quality-orchestrator` (repo-wide run deferred to Phase 1 completion).

### Next
- Wire the HITL re-entry helper (`run_gated` in `warden/hitl/runner.py`) on top of this + AuditJournal write-ahead (lock 2/3).
