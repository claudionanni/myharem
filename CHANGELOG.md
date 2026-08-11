# Changelog

All notable changes to MyHarem are documented here.

## [0.3.3] - 2026-08-11

### Fixed
- Replication slaves (`deploy_replication`, `deploy_slave`) set `relay_log` to
  a bare relative basename with no `relay_log_index`, which failed at
  `START SLAVE` time — relay log files are only created lazily then, not at
  server startup, so the bad path went unnoticed until that point —
  with `ERROR 29 ... File './relay-bin.index' not found`. Both are now
  absolute paths anchored in the slave's own datadir.

## [0.3.2] - 2026-08-11

### Added
- `mh fetch-tarball URL [--name FILENAME]`: downloads a tarball into
  `<basedir>/local/` if not already staged there (idempotent — a pre-existing
  file with the same name is left untouched, no re-fetch). `--name` overrides
  the saved filename for URLs that don't end in the real filename (e.g. a
  presigned S3 URL with a query string). Lets a caller (like MSRS) resolve a
  community tarball's real download URL and stage it on demand instead of
  requiring it pre-staged by hand.

## [0.3.1] - 2026-08-11

Correctness fix: `mh cluster start` / `mh service start` could report a
Galera joiner as successfully synced before it actually was, because the
socket file becoming connectable (`is_socket_ready()`) was used as the sole
readiness signal, backed only by a blind 2s grace sleep. Under real SST
timing this let orchestration advance to the next node before the joiner had
genuinely reached WSREP sync, causing the actual SST/cluster formation to
fail downstream (observed repeatedly on AWS/Rocky 9).

### Fixed
- `Instance.wsrep_local_state_comment()` (new): queries
  `wsrep_local_state_comment` via root over the unix socket (not the
  `myharem` admin user, which may not exist yet — a joiner receives it via
  SST from the donor).
- `start_instance()`'s joiner-wait loop now requires `is_socket_ready()`
  **and** `wsrep_local_state_comment() == "Synced"` before declaring a
  joiner ready, replacing the blind 2s grace sleep. Same 5-minute timeout
  budget; raises the same clear error on timeout.

## [0.3.0] - 2026-08-07

Multi-host: myharem can now advertise a real IP and form clusters spanning
hosts (one node per VM), not just co-located loopback instances — making the
tarball install method usable in any topology, not only single-host.

### Added
- `advertise_address` config / `MYHAREM_ADVERTISE_ADDRESS` env (default
  `127.0.0.1`): the IP this host advertises to Galera/replication peers.
- Distributed primitives, one node per host:
  - `mh galera-node <tarball> <id> --members host:port,... --cluster-name NAME
    [--advertise IP] [--wsrep-provider PATH]` — deploy one local Galera node into
    a multi-host cluster (start the bootstrap node first, joiners after).
  - `mh repl-master <tarball> <id> [--advertise IP]` and `mh repl-slave <tarball>
    <id> --master-host IP --master-port N [--advertise IP]`.
- `mh deploygalera --advertise IP` for a single host reachable on a real IP.

### Changed
- Galera addresses are built from an explicit member list; when advertising a
  real IP, Galera listens on all interfaces (`gmcast.listen_addr=0.0.0.0`) and
  advertises the real address. The async-replication user is granted `@'%'`
  (instead of `@'localhost'`) for cross-host slaves; admin and SST stay local.
- **Backward compatible:** the `127.0.0.1` default is unchanged — single-host /
  colocated output is byte-for-byte identical (covered by a regression test).

## [0.2.1] - 2026-08-07

### Fixed
- Galera deploy now **fails immediately with a clear message** when the tarball
  does not bundle the Galera provider (`libgalera_smm.so`), instead of writing a
  dead `wsrep_provider` path that only failed cryptically at server start-up.
  The generic `linux-x86_64` tarballs omit the provider; `linux-systemd-x86_64`
  and Enterprise `rhel-*` tarballs bundle it.
- **Duplicate instance ids are now caught.** Ids map 1:1 to ports/sockets, but
  deploying the same id for two different tarball versions created two
  directories sharing the id, and instance lookup (`os.listdir` order) then
  resolved it arbitrarily — silently starting the wrong instance. `deploy`
  refuses an id already in use, and instance resolution raises on ambiguity
  instead of guessing.
- **`erase --purge` no longer reports false success.** It used
  `shutil.rmtree(..., ignore_errors=True)`, so a delete that removed nothing
  (e.g. run without sudo against db-user-owned files) still printed "erased".
  Purge now surfaces the real error and verifies the directory is actually
  gone; the move-to-`erased/` path likewise reports failures.

### Added
- `mh deploygalera --wsrep-provider PATH` (also `wsrep_provider` in the config
  and the `MYHAREM_WSREP_PROVIDER` env) to supply the Galera provider for
  tarballs that don't bundle it. Error output hints at a detected system
  provider when one is present.

## [0.2.0] - 2026-08-07

Professionalization pass — MyHarem becomes scriptable and safe to drive from
automation (and usable as a deployment backend, e.g. for Simulacro/MSRS), while
remaining a first-class standalone CLI.

### Added
- Global `--json` flag: `deploy*`, `list`, `service status`, `var`, and
  `cluster` emit machine-readable JSON on **stdout**; progress/diagnostics go to
  **stderr** — so tools can parse results reliably.
- Persisted **manifest** (`<basedir>/manifest.json`) recording each
  deployment's topology, nodes, ports, and roles — an authoritative state source
  instead of scraping directory names and `my.cnf`.
- **Cluster-level lifecycle**: `mh cluster start|stop|erase <cluster_id>`
  operates on a whole deployment in the correct order (Galera bootstrap node
  first), driven by the manifest.
- **N-node Galera** (`mh deploygalera --nodes N`) and **1-master-N-slaves**
  async replication (`mh deployreplication --slaves N`).
- Non-interactive teardown: `mh erase --yes`/`--purge`, `mh cluster erase
  --yes`/`--purge`.
- Configurable credentials via env/config: `MYHAREM_SST_PASSWORD` and
  `MYHAREM_ADMIN_PASSWORD` (the admin `myharem` user can now have a password,
  passed to clients via `MYSQL_PWD`, never the command line).
- MIT `LICENSE`; a pytest suite covering port math, the result model, manifest
  round-trip, deploy orchestration, and rollback.

### Changed
- Deploy commands return structured results and record them in the manifest.
- Unique per-cluster `wsrep_cluster_name` (`mh_cluster_<id>`) so multiple Galera
  clusters can coexist on one host without colliding.
- Output convention: **stdout = result, stderr = progress**.
- `setup.py`: `python_requires>=3.10` and full package metadata.

### Fixed
- `mh service start` now **fails (non-zero) on start/SST timeout** instead of
  silently succeeding.
- Partial multi-node deploys **roll back** already-created instances on failure
  instead of leaving orphans.
- Removed the broken `mh show remote` command (it referenced a non-existent
  fetch command).

## [0.1.0]

- Initial Python refactor: deploy single / async pair / 3-node Galera from
  tarballs, per-instance isolation (dir/datadir/`my.cnf`/port/socket), service
  users, and basic per-instance lifecycle.
