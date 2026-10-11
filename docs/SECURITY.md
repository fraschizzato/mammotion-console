# Security assumptions

This is a single-user trusted-LAN application. It does not add a login system. Anyone who can access its HTTP port can view maps and request supported commands. Bind/firewall it to trusted clients; require authentication at a reverse proxy before any wider exposure. Home Assistant iframe embedding can be added behind your chosen LAN/proxy access policy.

HA and Mammotion account credentials come from `.env`/server environment, not SQLite or browser preferences. `.env` is excluded from Git, Docker build context, exports and backups. Docker receives environment configuration at runtime. Do not publish `docker inspect` output for a running container because container administrators can read environment values.

Mutation APIs require `X-Console-Request: 1`, reject cross-site requests, validate origins and allowlist semantic operations. WebSocket origins are checked. Explicit confirmations gate mowing commands, schedule mutations and map deletion; confirmation is an intentional-action affordance, not authentication. The same-origin header is CSRF protection, not an API secret.

HA entity associations are resolved through the registry and scoped to the chosen mower. The browser cannot supply an arbitrary service, credential, camera entity, upstream URL or filesystem path. HA errors are sanitized, and diagnostic exports recursively omit credential-like fields and replace the configured token. Camera signaling sends only temporary WebRTC session material that the browser needs.

Map UUIDs are validated before path access. Import files are bounded, ZIP entries are not extracted, and SVG previews are generated from validated numeric geometry rather than imported SVG markup. Labels use DOM text rather than HTML. Supported control parameters are validated against discovered service schemas and backend allowlists. The read-only cache parser refuses symlinks escaping its configured root and only opens Mammotion-specific files.

Provider downtime disables upstream controls. HA mode rejects unsupported movement buttons. Direct mode supports explicit Wi-Fi/cloud or optional Bluetooth nudges, off by default; each pins the transport and sends zero speed on release or its short timer. A failed stop latches a fault. No automatic transport fallback is used for movement. Cancel-job is clearly distinguished from a hardware emergency stop.

The container runs unprivileged with a read-only root, dropped capabilities, one HTTP port and a persistent data volume. Dependencies are locked in `requirements.lock` and `frontend/package-lock.json`. Keep the image current and retain protected backups. Map coordinates and diagnostics are private property data even after credential redaction.
