# Zone selection and individual profiles — 2026-09-27

The original missing zone list was caused by integer precision: Mammotion area
hashes can be 64-bit integers, beyond JavaScript's exact Number range. The API
serializes `hash` as an opaque string and job commands always use the current,
scoped switch entity ID.

Real production data later showed that Mammotion can assign a different hash to
an unchanged zone. Profiles now keep the archive's stable geometry fingerprint
and canonical zone name. A current zone is reconciled in this order: exact hash,
exact geometry fingerprint, then the server-resolved name. Generic firmware names
such as `Zone 3810...` are first resolved from the current polygon area through
the configured ±2 m² mapping. Name fallback is accepted only when exactly one
zone in the archived map has that name. Ambiguous names fail closed. The browser
does not contain a duplicate area/name mapping, and launch still sends only the
current zone entity supplied by the active provider.

In Jobs / Lavori:

1. Select the archived task map. All archived zones remain visible.
2. Choose **Configure zone / Configura zona** for the zone to edit.
3. Set speed, height, direction and other fields exposed by the current provider.
4. Click **Save zone profile / Salva profilo zona**.
5. Configure the other zones separately. **Load zone profile / Carica profilo zona**
   restores one zone's settings and selects only that zone for review and launch.

Profiles are persisted in SQLite under `zone_profile`, keyed by immutable map
snapshot ID and stable archived geometry when available. Changing one profile
does not change another.
Settings do not overwrite archived geometry, and saving never sends mower
commands. Transient controls (`modify`, `start_progress`, `plan_only`) and selected
area entities are excluded from durable profiles. The server validates saved
fields against the provider schema and validates the active mower/map again at
launch. An inactive archived zone can have a local profile but cannot be launched
until its matching map is active on the mower.

The mower start-job APIs apply a single settings object to all zones in one job.
For genuinely different settings, select at least two zones with saved profiles
and choose **Start per-zone sequence / Avvia sequenza per zona**. The console
persists the reviewed order and launches one one-zone job at a time. It advances
only after the current item was observed active and then returned to idle/docked.
An unavailable/error state never advances the sequence. A delivery timeout stops
the sequence as `delivery_unknown` rather than risking a duplicate command.

The active sequence and current zone are visible in Jobs. Cancelling the sequence
leaves the current autonomous mower job unchanged and prevents later zones from
starting. A deliberate mower Stop/Dock or provider switch also cancels the pending
sequence. The existing multi-zone launch remains available when every selected
zone intentionally shares one settings object. Profiles never transfer silently
to another snapshot/version with different zone IDs.

Backend tests cover exact uint64 serialization, hash drift with stable geometry,
hash and RTK-geometry drift recovered through the area mapping, ambiguous-name
rejection, scoped launch validation, independent persistent profiles,
invalid/mismatched areas, invalid parameters and archive immutability.
Desktop/mobile tests also exercise a changed live hash and changed live geometry
with the same server-resolved zone name, reload, single-zone confirmed dispatch,
ordered two-zone sequencing, cancellation, and local configuration of an inactive
archived zone.

The same identity rules apply to **Reuse settings / Riusa impostazioni** in job
history. A changed live hash does not discard the recorded speed, height,
direction or other reported settings when exactly one current zone matches by
geometry or resolved name. Reuse only fills the job editor; it never resumes or
launches automatically. Generic or ambiguous matches require manual zone
selection.

For LUBA 3 in direct-account mode, the schema also includes **Auto-reverse mowing
direction** when the reported firmware meets PyMammotion's gate and **Edge
coverage** when the model supports it. Edge coverage is the official application's
boolean choice (`0.0` off, `0.5` on) and requires at least one perimeter lap. In
Home Assistant mode, the auto-direction field appears only when the selected mower
has one available `auto_change_direction` switch and both standard switch services;
the console writes that switch before starting the job. These values are persisted
independently in every zone profile and reusable history record.

Keep `.env` and `/data`; refresh the browser after rebuilding.
