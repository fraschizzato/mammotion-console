# Test and validation guide

Run the commands in README after building the frontend. Backend tests use temporary SQLite/filesystem storage, sanitized fixture maps and local/mock HA transports. They do not call a real mower. Python emits deprecation notices from the installed Starlette/httpx test-client combination; the tests do not suppress them.

The fixture corpus contains two full properties and one explicitly invalid active-progress-only payload. Tests cover byte-level archive immutability across A → B → A, unchanged deduplication, changed versions, concurrent captures, persistence after restart, metadata-only edits, ZIP integrity/path protection, static geometry, local-coordinate transformation, read-only cache access, scoped controls, offline denial, schema validation, secret removal, actual HA WebSocket authentication/reconnect/events, and scoped WebRTC signaling cleanup.

Frontend unit tests verify error propagation and same-origin semantic API writes. Playwright runs desktop and mobile viewport tests for map imports, both maps after reload, rendered Leaflet geometry, navigation, console errors and horizontal overflow. Tests create uniquely named maps and remove their own imports. Always use a dedicated data directory or volume nonetheless.

## Isolated HA simulator

Terminal 1, from the repository root:

```sh
.venv/bin/python -m uvicorn tests.mock_ha:app --host 0.0.0.0 --port 8129 --no-access-log
```

The simulator is **test-only**, uses the public canary `mock-ha-secret-canary`, and has no connection to a physical mower. Do not expose its port beyond your test host/network. It is excluded from the production Docker build.

Terminal 2, native console with a separate test directory:

```sh
DATA_DIR=.test-data HA_BASE_URL=http://127.0.0.1:8129 HA_TOKEN=mock-ha-secret-canary AUTO_CAPTURE=false \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 5010 --no-access-log
```

Terminal 3:

```sh
cd frontend
MOCK_HA=1 CONSOLE_TEST_URL=http://127.0.0.1:5010 npm run test:e2e
```

On PowerShell set those environment variables with `$env:NAME='value'` before each command. The live browser test waits for the battery to change from REST bootstrap 87% to WebSocket 63%, captures a real service-response fixture, sends a confirmed scoped job, checks movement denial, and verifies camera failure is isolated from the rest of the app.

## Container smoke

```sh
docker build -t mammotion-console:local .
docker run -d --name mammotion-console-smoke --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /tmp:size=64m,noexec,nosuid \
  --add-host host.docker.internal:host-gateway \
  -p 127.0.0.1:5010:5000 -v mammotion-console-smoke-data:/data \
  -e HA_BASE_URL=http://host.docker.internal:8129 \
  -e HA_TOKEN=mock-ha-secret-canary -e AUTO_CAPTURE=false mammotion-console:local
curl --fail http://127.0.0.1:5010/api/health
```

Run browser tests against port 5010. Check `docker inspect --format '{{.State.Health.Status}}' mammotion-console-smoke` and `docker logs mammotion-console-smoke`. Verify the canary is absent from diagnostics, JS bundle and logs. The canary is intentionally visible in this test command but must never be forwarded to the browser by the application.

For an actual adapter map-switch test, capture A, POST the simulator's `/testing/map/b`, wait for the 30-second capture debounce, capture B, and compare A's exported/normalized bytes. Switch `/testing/map/a`, wait and re-capture; it must deduplicate. Restart the container and verify the same two map records/files remain. The simulator's `/testing/calls` shows only the intended HA services. Remove the test container when finished; its dedicated volume can be retained for inspection.

CI-equivalent gates are lint/format, mypy, pytest, ESLint, TypeScript, Vitest, production Vite build, Playwright, Docker build and container health. Physical acceptance checks are in [deployment](DEPLOYMENT.md); they are not substitutes for these automated checks.


## Direct account expansion (2026-09-20)

Direct-provider tests use real pinned SDK models and protobuf serialization with mocked network transports. Coverage includes credential redaction, incomplete-map rejection, archive restart persistence, settings reuse, scoped camera tokens and cleanup, transport pinning, cancellation stop, stop-failure inhibition and release during telemetry refresh. Hardware camera decoding, radio reachability and actual mower motion remain installation acceptance checks. The Agora bundle is lazy-loaded; its size warning is expected.


## Runtime import correction (2026-09-20)

The deployed direct-account image failed before login because PyMammotion imports
`packaging.version` without bringing `packaging` into the production environment.
Development test dependencies had masked the missing runtime dependency. The app
now declares it explicitly (locked to 26.3), checks SDK imports during Docker build,
and reports initialization failures without exception contents or credential data.
A configured account displays Connecting while login starts. Rebuild and recreate
the container after transferring Dockerfile, pyproject.toml, requirements.lock and
backend/cloud.py. Existing .env and data remain in place.

Regression validation: 47 backend tests passed; Ruff and mypy passed. Real account
login still requires verification on the destination machine.
