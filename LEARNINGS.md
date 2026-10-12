# LEARNINGS.md — Warden

## Checkpointing / LangGraph

1. **`StateSnapshot.metadata` arrives as a plain `dict` after serde**, not a `CheckpointMetadata` object — `meta.step` raises `AttributeError`. Handle both: `meta["step"] if isinstance(meta, dict) else meta.step`. (Caught by first test run against real Postgres.)
2. **`AsyncPostgresSaver.from_conn_string()` is an `@asynccontextmanager`**, not a plain factory: use `ctx = from_conn_string(uri); saver = await ctx.__aenter__()`. Calling it and treating the return value as the saver silently loses the connection.
3. **`StateSnapshot` has a top-level `interrupts` field** — no need to walk `state.tasks[].interrupts`.
4. **`StateSnapshot` has NO `step` field** (older docs show it) — step lives in `metadata["step"]`. `created_at` is an ISO `str | None`, not a datetime.
5. **`AsyncPostgresSaver.conn` is `AsyncConnection | AsyncConnectionPool`** — health probes must branch (pool has no `.execute`; use `async with pool.connection()`).
6. **On resume, LangGraph restarts the interrupted node from the top** — code before `interrupt()` inside that node re-runs. Prior *completed* nodes do NOT re-run (verified by the no-duplicate-drafts test). Side effects before `interrupt()` must be idempotent.
7. **`CompiledStateGraph` is generic with 4 params** (`StateT, ContextT, InputT, OutputT`) — mypy strict needs explicit args (`CompiledStateGraph[Any, Any, Any, Any]`).
8. **Config params must be typed `RunnableConfig`** (TypedDict from `langchain_core.runnables`), not `dict[str, Any]`, or every `ainvoke`/`aget_state` call fails overload resolution under mypy strict.
9. **No Docker/sudo/root in this environment** — `pgserver` PyPI package bundles PostgreSQL 16 binaries and runs over a UNIX socket (`postgresql://postgres:@/postgres?host=<dir>`), works with psycopg3 async. Use it for real-DB tests.

## Dashboard (server-rendered)

38. **Keep renderers pure and the route thin** — `dashboard.py` exports string functions taking data, `main.py` only does HTTP; pure functions make XSS/wiring assertions possible without any HTTP machinery.
39. **Compose the console from existing JSON-clean endpoints** — the page adds zero new API surface (`/stream` SSE via vanilla EventSource, `/inbox` + `/toggles/lease` via hx-get/hx-post); lock 12's JSON-clean rule survives because the HTML fan-out is client-side.

## HITL web flow

34. **Journal every identity field the inbox will need** — an action built without `thread_id` journals empty identity, and downstream web consumers (inbox -> decision -> resume) lose the thread. Set `thread_id` on the Action at creation, not on individual events.
35. **Python 3.11 f-strings cannot nest same-quote strings** (`f"...{x or ""}"`) — SyntaxError at parse; use concatenation or a variable for the inner expression. (3.12 lifted this restriction.)
36. **Journal-first decision discipline at the web boundary**: append `gate_decided` BEFORE `graph.ainvoke(Command(resume=...))` — if the process dies between them, the decision is recorded (evidence) even though the resume didn't run; the opposite order loses the human's verdict on crash.
37. **Attribution must come from the key, never the body** — accept any `decided_by`/`name` JSON the client sends, then IGNORE it: the `Warden-Key` header resolves identity (lock 8's "must be a true statement in the journal").

## Lease toggles

30. **A shared mutable policy object is the runtime toggle** — pass the SAME `LeasePolicy` instance to supervisor + ops factory closures; they read `.enabled`/`grant_ttl()` at node-run time, so flipping changes behavior without recompiling the graph. Module-level defaults (`_default_lease_policy`) make the no-shared-policy branch test TWO things at once — always pass the policy explicitly in toggle tests.
31. **Custody-OFF ≠ allow-everything** — with enforcement off, the run still hits the seam's policy gate (`escalate` parks); the observable OFF-semantics at the graph level is "reaches the gate", not "dispatches". Test what the knob actually controls.
32. **FastAPI routes close over module state** — copying `app.router.routes` into a new `FastAPI()` does NOT decouple handlers from the module-level state; for lifespan-less testing, swap the state's fields and drive the real app via `httpx.ASGITransport` (which skips lifespan)
33. **Endpoint content negotiation**: one route can serve JSON (default Accept) and an HTMX partial (`Accept: text/html`) — poll with `hx-get` + `hx-trigger="every Ns"` for live cards; escape all interpolated values server-side.

## SSE streaming

26. **asyncio queues are event-loop-bound** — a threaded `TestClient` runs the ASGI app in a different loop than the test; bus queues created in the app's loop dead-wait when the test loop awaits them. Use httpx `AsyncClient(transport=httpx.ASGITransport(app=...))` for SSE/streaming tests (same-loop), never the threaded TestClient.
27. **Async generators start lazily** — none of `sse_stream` runs until the first `__anext__()`; a test that unsubscribes before entering the generator loses the queue lookup (flow ends with `StopAsyncIteration` instead of the sentinel frame). Enter first, then trigger lifecycle events.
28. **`put_nowait`-with-drop never belongs in an awaited helper** — making `_offer` sync keeps drop-oldest O(1) and reentrancy-safe; awaiting a sync fn raises `TypeError: object NoneType can't be used in 'await' expression`.
29. **Client disconnect mid-`aiter_bytes` skips the endpoint generator's normal exit** — teardown (`finally`) must be safe to run from cancellation (unsubscribe idempotent; wrap await in try/except), and tests need a deterministic `close_all()` rather than timing races.

## Policy engine

23. **Hashed bag-of-token embedders require seed texts that mirror the query composition** — appending the action name to the payload changes cosine materially (1.0 -> ~0.93); seed/store policy texts with the same composition the engine embeds.
24. **Stored verdicts need validation at the engine boundary** — a policy row's verdict string is untrusted data; non-seam verdicts fail-closed to escalate/ unmatched, never passthrough.
25. **mypy strict: `sum()` over mixed generic math returns Any** — wrap float conversions or annotate the result variable to avoid `no-any-return`.

## Leases

20. **Wall-clock beats monotonic for custody that spans park/resume** — a resume can happen in a fresh process, so time must be comparable across processes (epoch seconds), and lease timestamps must be JSON-safe in AgentState (floats round-trip to ints through jsonb — tolerate both).
21. **A re-grant on route is NOT idempotence** — the supervisor re-running always overwrites lease state; expiry paths are only reachable/easy to test when the TTL is a build knob (ttl=0 = dead on arrival).
22. **Stealing check must evaluate against the EXISTING holder, not the caller** — `evaluate(lease, caller)` returns NOT_HELD for a wrong holder, which alone cannot distinguish "no custody anywhere" from "someone else owns it live".

## Multi-agent / interrupt + resume

15. **Resuming a parked thread requires `Command(resume=...)`, never a plain dict.** A plain dict passed to `ainvoke` on a parked thread starts a SECOND run on that thread (fresh state from the input) — the interrupt never consumes it and the human's decision is silently dropped. `Command(resume=...)` is the only Command pattern intended as input to invoke/stream.
16. **The node re-runs from the top on resume, so `interrupt()`-gated action ids must be durable.** Random ids (`uuid4`) change between park and resume (and again after a process restart); derive ids from durable identity (e.g. sha256 of thread_id). `hash()` is salted per Python process — never use it for cross-restart identity.
17. **`interrupt()` requires a checkpointer** — `Command(resume=...)` against a checkpointer-less graph raises `RuntimeError("Cannot use Command(resume=...) without checkpointer")`. Gate tests must compile with Postgres (or InMemorySaver).
18. **LangGraph `add_node` overloads are structural** — hand-written `Callable[[State], Command[...]]` aliases do not match; annotate node-factory returns as `Any` under mypy strict.
19. **psycopg async on Windows needs `WindowsSelectorEventLoopPolicy()`** — ProactorEventLoop raises `InterfaceError` at connection time.

## Tooling

10. `uv init` expects `src/<pkg>/` layout when a build backend is set — package must live under `src/`.
11. `rg` treats patterns starting with `-` as flags — use `rg -n -- '<pattern>'`.
12. **pgserver stops PostgreSQL at process exit** (`cleanup_mode='stop'` default + `atexit`). The *data directory* persists, so a new process re-attaches to the same checkpoints — that's what makes the CLI crash-recovery demo work, not a long-lived server. Never hand a URI obtained from a throwaway `get_server` process to another CLI invocation: the server is already (or about to be) stopped. Let each CLI resolve pgserver itself.
13. **Piped-input CLI tests: every line is consumed by whatever prompt is active.** `/quit` right after a parking input lands in the *resume* prompt, not the main loop, and was silently treated as a deny decision. Slash commands must be recognized at every prompt (or documented as decisions).
14. **`tests/` is a package** (empty `__init__.py` since 4fd3ff0). Import helpers as `from tests import cli_chat`; adding `mypy_path = ["tests"]` makes mypy see the module twice (`tests.cli_chat` + `cli_chat`) and fail hard.
