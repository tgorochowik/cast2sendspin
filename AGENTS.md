# Repository guidance

## Commands and verification

- Run commands from the repository root: configuration, assets, and subprocess paths are CWD-relative. Python subprocesses explicitly use `.venv/bin/python`; activating another virtualenv is insufficient.
- Local setup requires Node.js 22+, Python 3.14+, and FFmpeg: `npm ci --ignore-scripts`, `python3 -m venv .venv`, then `.venv/bin/pip install -r requirements.txt`.
- Run both test suites (as CI does):
  - `npm test`
  - `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`
- Focused tests:
  - `node --test tests/bridge-player.test.mjs`
  - `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_routing.py'`
- Tests are offline; do not start the app or supply real credentials to run them. There are no configured lint, formatter, typecheck, or frontend-build tasks.
- Container verification: `docker build --target test .`. The default/final image build does **not** run tests.

## Runtime wiring

- `npm start` runs `src/manager.mjs`: loopback management UI/API, MA inventory discovery, receiver registry, and child supervision. It spawns one `src/bridge.mjs` per enabled receiver; each bridge spawns `src/worker.py`.
- The Python worker resolves media through a persistent Node subprocess (`src/sender_resolver.py` → `src/youtube-resolver.mjs --serve`), decodes with FFmpeg, and sends PCM through `src/source_client.py`. MA output ownership lives in `src/routing.py`.
- Bridge↔worker and worker↔resolver stdin/stdout are JSON-line protocols; arbitrary stdout diagnostics break RPC. Use the existing structured events and sanitized logging.
- `src/web/` is served directly by the manager, without bundling.
- Root `config.json` seeds operator defaults; `.state/receivers.json` stores UI-managed routing and playback preferences, which override those defaults. Docker mounts root config read-only. Seek-policy changes use live manager→bridge→worker IPC; exclude only that policy from restart fingerprints. Generated per-route configs are startup snapshots passed via `BRIDGE_CONFIG`/`BRIDGE_STATE_DIR`.
- Preserve `.state/`: it includes receiver identities, Sendspin keys, pairing, and server pins. Current-track encoded audio is session-local RAM only (32 MiB limit); it is not next-track prefetch and must never share sender-authorized data between tracks/sessions.

## Playback invariants

- YouTube is the queue authority; track completion advances the receiver queue, not MA's queue. Track replacement may arrive as Stop-before-Play and must not release/reclaim the MA source or cycle power.
- Automatic/per-speaker routing supports individual non-Chromecast outputs only; validation also checks protocol provenance and group members. Groups are usable through manual/single-source routing, which must never select, stop, power, or change volume on MA output players.
- Per-speaker pause uses MA Stop to interrupt buffered output while retaining the cast session. Seeking optionally stops output (`stop_receiver_on_seek`, UI override); continuous seeking cannot flush audio already queued downstream. Distinguish intentional source-stop callbacks from external takeover; never reclaim or power down another owner's playback.
- Resolver initialization is reusable, sender authorization is not: serialize credential-bearing requests, clear sender context after each request, and kill/reap the resolver on cancellation or RPC timeout. Missing sender authorization fails closed unless `allow_anonymous_fallback` is explicitly `true`.
- FFmpeg shutdown must cancel any pending frame-read task, drain stdout and serialize reads with the capture loop; waiting without draining can deadlock on a full PCM pipe.
- Read `docs/playback-latency-notes.md` before changing queue behavior: next-track lookahead was deliberately removed. Current-track fetching is separate; `docs/seek-buffer-evaluation.md` records why first-PCM timings must not be mistaken for audible seek latency.

## Security and deployment gotchas

- Never expose `.ma_token`, sender tokens/context, signed media URLs, pairing secrets, or raw upstream exceptions in logs. Tests mock token reads; do not inspect the real token. `MA_TOKEN_FILE` overrides the native `.ma_token` path.
- Management is intentionally loopback-only with Host/Origin/CSRF checks; do not relax binding without implementing authentication.
- Compose uses Linux host networking for SSDP/DIAL; do not add port mappings. Stop a native instance before launching the container. `.env` supplies Compose interpolation, not application configuration.
- `.dockerignore` is deny-all with explicit allowlisting: new source file types or Docker `COPY` inputs may require allowlist changes. `tests/test_release.py` checks build inputs, private-path exclusion, entrypoint paths, and manifest/lockfile agreement.
