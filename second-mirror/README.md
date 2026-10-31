# HTTP SVN server and automatic local mirrors

Optional services for the [main SVN/USVN server](../README.md), or an existing SVN
installation with compatible repository storage and authentication files.
Run the main HTTP endpoint and each mirror on separate Docker hosts as needed.
Paths and addresses below are examples, not required Synology shared-folder names.

## Architecture

```text
USVN administration -> shared repositories and auth files <- master-http
                                                            ^ HTTPS
                                                            |
Clients -> mirror:18080 -> metadata, Lock/Unlock, Commit ------+
            |                                               |
            +-- file GETs from local repositories             |
                         ^                                  |
                         +--------- sync / svnsync -----------+
```

- `master-http` is an additional Apache transport to the main repositories, not
  another independent writable copy. USVN continues to administer users and rights.
- `sync` discovers repositories and access rules, then copies missing revisions.
- `mirror` serves file contents locally. OPTIONS / PROPFIND / REPORT and write
  operations go to the main server, including all lock-state reads.
- `SVNAllowBulkUpdates Off` on the main endpoint forces skeletal update responses;
  clients fetch the file contents separately from the mirror. Do not remove it.
- Port 18081 is an optional full proxy: all requests, including file downloads,
  go to the main server. Port 18080 is the normal client endpoint.

Lock visibility does not depend on the replication interval. Client Refresh / Check
Repository reads authoritative state, including locks created directly on the main
server. Working copies and editors can still cache previously retrieved status.

Network SVN operations require the main server. There are no offline commits or
automatic failover. During replication lag a new revision can be visible in metadata
before its files arrive locally: retry Update after the worker catches up. Metadata
and properties still cross the WAN; the bandwidth saving applies to file contents.

## 1. Main HTTP endpoint

On the main host, work in this `second-mirror` directory. Use Compose v2.
The `compose.master.yaml` project defaults to `svn-master-http`.

Create a dedicated replication account in USVN, for example `svnmirror`. Its password
is used by the mirror worker, not by employees. The replication alias gives this one
account read access to complete repository histories independently of project authz;
write methods are blocked there. Normal users retain their usual project permissions.

For a **new setup**, create `config/master.json` from the supplied example:

```sh
cp config/master.auto.example.json config/master.json
chmod 644 config/master.json
```

Its `discovery_user` must match the account created above; `export_auth: true` enables
delivery of htpasswd/authz to mirrors. Ensure Apache can traverse `config/` and read
the file. The control endpoint exports password hashes, so use authenticated HTTPS
and keep the replication account private.

Create `.env` with your actual host paths, for example:

```dotenv
MASTER_REPOS=/srv/svnserver/volume/svnrepo/svn
MASTER_AUTH_DIR=/srv/svnserver/volume/usvn
MASTER_BIND_IP=127.0.0.1
MASTER_PORT=18082
```

`MASTER_REPOS` is the directory directly containing `Example/format`, `Example/db`,
etc. `MASTER_AUTH_DIR` must contain the existing `htpasswd` and `authz` files.
Use absolute host paths. The Apache container has read/write access to repositories
because commits execute here; the auth directory is mounted read-only.

```sh
docker compose -f compose.master.yaml up -d --build
docker compose -f compose.master.yaml logs --tail=50 master-http
```

Publish this backend through your TLS reverse proxy (DSM Reverse Proxy, Nginx, or
another suitable proxy). The proxy and backend must have the same `/svn/NAME` paths.
Example public origin: `https://svn.example.com`, optionally with a custom HTTPS port.
Test it from the future mirror host with a normal SVN user:

```sh
svn info --username USER https://svn.example.com/svn/Example
```

Public `/svn/NAME` endpoints follow the user's USVN access rules. Internal
`/svn-mirror-source/NAME` and `/svn-mirror-meta` endpoints are for replication only.
The proxy must pass these paths as well, including their Authorization headers.
It must support SVN/WebDAV methods and large requests. Validate Commit and remote
Copy/Move through TLS; Destination handling can differ between reverse proxies.

All current and future repository directories with names matching `[A-Za-z0-9_-]+`
are discovered automatically. Symlinks and directories without `format`/`db` are
excluded. The repository URL prefix defaults to `/svn`.

## 2. Configure a mirror host

Clone the repository on the mirror host and work in `second-mirror`:

```sh
docker build -t svn-second-mirror:local .
docker run --rm -it -v "$PWD:/setup" svn-second-mirror:local configure-mirror
```

The wizard asks for:

- Main **HTTPS origin**, for example `https://svn.example.com:18443`, without `/svn`.
- Replication account and password (hidden input).
- Mirror host LAN IPv4 used by clients.
- Absolute path for a dedicated state directory, such as `/srv/svn-mirror/data`.
- Existing repository parent, if reusing a copy; otherwise leave this field blank.

On Synology, choose paths under a suitable local NAS volume instead of `/srv`.
The generated `.env`, `config/repos.json`, and `secrets/source.json` belong to this
host. Existing files are never overwritten by the wizard. The password file is 0600;
the config is readable by the worker. Apache gets auth snapshots but not the source
password file. Keep state and repository storage outside live file-sync folders.

### Start with empty mirror storage

Only use this variant when a full initial history transfer is intended:

```sh
docker compose -p svn-second-mirror -f compose.auto.yaml up -d
```

The worker initializes missing repositories and publishes each one after its first
successful sync. The HTTP container can initially run without published repositories.
New repositories created later on the main host are initialized automatically.

### Start from an existing repository copy

An existing copy must be a consistent copy of the same history, with matching UUIDs
and no independent commits. Prepare it with `svnadmin hotcopy` or an appropriate
coordinated snapshot. A random copy of a live repository is not a valid seed merely
because its `db/current` reports the expected revision.

Set `EXISTING_REPOS` to the actual repository parent in `.env` (the wizard does this
when the existing-copy field is filled). Keep `MIRROR_DATA` as separate state storage.
Stop every old server and file synchronizer that could write to the copy.

```sh
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml run --rm sync discover
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml run --rm sync adopt-all
```

`discover` retrieves the catalog/auth only. `adopt-all` checks all selected local
repositories before converting them: layout/symlinks, UUID, local HEAD relative to
the source, and full `svnadmin verify`. It then archives old hooks, installs write
guards, sets worker permissions and initializes `svnsync --allow-non-empty`.
It does not download repository history. UUID equality and local verification alone
cannot prove that two independently modified histories are identical.

Successful verification is checkpointed per repository under
`MIRROR_DATA/status/adopt-verified/`. If another repository fails or the command is
interrupted, rerun `adopt-all`: unchanged verified copies report
`reusing successful local verification` instead of reading their asset contents
again. Source identity and local HEAD are still checked on each run. The checkpoint
compares the whole local tree by file/directory identity, sizes, permissions and
nanosecond mtime/ctime, and checks for changes during verification and before adoption.
Changing/replacing a copy or upgrading the reported svnadmin version invalidates it.
This requires the copies to stay offline: it is not a substitute for excluding other
writers, nor does a stat fingerprint detect silent storage corruption without
metadata changes. Use `run --rm sync adopt-all --reverify` to force full checks.
Runs predating this feature have no saved verification results and need one initial
pass. Checkpoints only record verification; final adoption markers are written after
all repositories have been validated and converted successfully.

Permission normalization grants both owner and group access, including copies whose
original Unix modes are zero. Read/write/traversal access is probed with the actual
worker UID before conversion. Checkpoints are refreshed after our own permission,
hook and revision-zero metadata changes so conversion failures can also resume.
If this probe still fails on a NAS, inspect its ACLs rather than repeatedly verifying
history or granting unrestricted access to the entire share.

**Recovery of an older, partially converted adoption:** if all selected copies
already passed full verification and have remained offline/unchanged since, use
`run --rm sync adopt-all --resume-conversion` after fixing the cause. This explicit
mode requires saved successful checks for every selected repository, matching source
identity/verifier, and at least one already initialized mirror. It trusts the prior
history verification despite changed filesystem metadata; it does not revalidate
asset contents. New checkpoints also check the recorded local HEAD. Use this only
for the conversion/permissions failure case, never to bypass failed history checks
or to accept externally modified copies. Normal `adopt-all` retains change detection.

After successful adoption:

```sh
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml up -d
```

The existing-copy overlay mounts repositories in place, not another data copy. It
also requires matching adoption markers before the worker can run. Use **both**
Compose files for all subsequent operations on this deployment. If a repository is
missing locally, its first history transfer will still be full.

For standalone Compose v1, replace `docker compose` with `docker-compose` in these
automatic mirror commands and keep `-p svn-second-mirror`. Do not use the v2-only
static `compose.yaml` as a substitute.

If verification fails, preserve the copy and diagnose that repository. Missing
revision files are not repaired by lowering `db/current` or rerunning init. Restore
a consistent copy or reconstruct a verified prefix separately before adoption.

## 3. Operations and monitoring

These are maintenance actions, not a sequence to run on each login. Use the same
Compose project and file set used at installation. For an existing-copy deployment:

```sh
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml ps
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml logs --tail=100 sync mirror
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml exec mirror python3 /opt/svnmirror/app.py status
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml exec mirror python3 /opt/svnmirror/app.py health
```

Healthy means recent successful discovery/sync, acceptable revision lag and no
partially merged revision, plus an HTTP readiness probe. Normally all repositories
show `ok: true`, `lag_revisions: 0`, and matching local/merged/master revision numbers.
`health` returns zero for success. Connect nonzero results and stale status to your
monitoring; Docker does not send administrator alerts or restart unhealthy containers
just because a healthcheck failed. Large revisions can take longer than one interval.

Use these command suffixes with the same Compose options:

| Action | Suffix |
|---|---|
| Start | `up -d` |
| Restart | `restart mirror sync` |
| Stop | `stop mirror sync` |
| Follow progress | `logs -f --tail=50 sync` |
| Apply changed environment/bindings | `up -d --force-recreate` |

Restart policies bring services back after a host reboot unless deliberately stopped
(subject to the configured Docker restart policy). To resume a stopped deployment,
use `up -d`. No configuration wizard or repeated adopt-all is needed for a restart.

For a manual sync pass, stop `sync` first, then run `run --rm sync sync-once` and
restart it with `up -d sync`. A shared OS lock prevents concurrent worker/admin runs.
The worker now recovers a stale `svn:sync-lock` automatically before a transfer.
It first holds both the shared worker OS lock and an additional lock inside the
repository, checks its source identity and installed write guards, then removes
and verifies only that replication property. `svn:sync-currently-copying` is kept
for SVN's interrupted-revision recovery. OS locks are inherited by writer children:
a surviving child prevents another worker from recovering its active lock even if
its Python parent dies. This also protects deployments sharing a repository with
different state directories, provided all writers use this worker version.

Stop all legacy workers/manual SVN writers before upgrading. Never run a separate
writer against the mirror or synchronize its live folders with file-sync software.
The automation cannot coordinate with unmanaged tools that bypass its OS locks.
Manual `run --rm sync recover NAME` remains available after stopping workers; it
uses the same checks and explicitly reports successful verification. This is a
replication lock, not a user file lock. No permanent lock stealing or disabled
locking is used; master-side user file locks are not removed.

File locks inherited with an existing repository copy can also block replay with
`E160039: User ... does not own lock`. Before replay, the same exclusive maintenance
checks remove these **mirror-local** locks automatically. Listed paths/tokens are
verified through local SVN metadata before mutation, the result is checked and
an audit record is kept in `status/NAME-last-file-lock-cleanup.json`. The real file
locks on the main server are preserved and remain authoritative for clients.
Ambiguous/unparseable lock listings fail safely instead of guessing paths.

History transfers stream redacted SVN output and report local/merged/copying
revisions, transaction size, elapsed time and inactivity every 30 seconds. Active
transfers have no total-duration cap by default, even if `SYNC_TIMEOUT_SECONDS`
still contains the old 21600 value. An inactivity watchdog samples writer I/O
and active transaction file changes; after one hour without activity it stops the writer.
CPU usage alone does not reset this timer: a disconnected client can spin without
making progress. Transaction size excludes abandoned transactions from older attempts.
The next pass automatically recovers and retries from committed revisions. A
partially transferred, uncommitted revision may need to be transferred again;
this does not repeat adoption or download all earlier history. Discovery/auth
polling continues independently. Missing permissions, corruption or an unreachable
source remain real errors and are shown rather than silently bypassed.
Container startup also clears stale Apache PID/CGI socket artifacts only when no
Apache process is alive; it never removes repository or worker lock files.

Check free disk space, WAN reachability, TLS trust, source credentials and filesystem
permissions when sync fails. Apache/discovery diagnostics on the main host:

```sh
docker compose -f compose.master.yaml logs --tail=100 master-http
docker compose -f compose.master.yaml exec master-http tail -n 80 /var/log/apache2/error.log
```

For mirror-side `500 Proxy Error`, inspect both container logs and
`docker exec MIRROR_CONTAINER tail -n 80 /var/log/apache2/error.log`.
`AH02040: Certificate Chain too long` means the Apache upstream verification-depth
limit was exceeded. The generated configuration allows depth 5 while retaining
required CA trust and hostname verification. Update/rebuild and recreate `mirror`
to apply it; do not disable certificate verification. An authentication error such
as `user ... not found` or `Password Mismatch` is a separate login problem.

Repository removal from the source catalog removes its published mirror URL after a
successful worker pass, but keeps local data. During `sync-loop`, discovery refreshes
accounts, password hashes, groups and repository/path access rules independently of
history transfers, every 30 seconds by default (`DISCOVERY_INTERVAL_SECONDS`). This
is a polling delay plus request time, not an immediate push. A slow checkout history
transfer does not postpone this refresh. Failed discovery keeps the last cached
rules; inspect discovery health when changing permissions. USVN interface settings,
repository hooks and `svnserve.conf` are not copied to mirrors.
The mirror never becomes an independent master. Back up the main server and save
host-specific config, secrets, mirror state/adoption markers and a recovery plan.

## 4. Upgrades and existing deployments

### Administrator status page

Open `http://MIRROR_HOST:MIRROR_PORT/svn-mirror-status/` on the mirror's normal LAN
port. No additional port is needed. Set `STATUS_USERS` in the mirror `.env` to a
comma-separated allowlist of **existing SVN logins**, for example
`STATUS_USERS=svnmirror,admin`. The default permits only `svnmirror`; do not give
its replication password to ordinary users. Use a personal administrator account
in the allowlist for routine operations. Recreate `mirror` after changing this
setting. Authorization protects both static assets and the read-only JSON API.

The page auto-refreshes every five seconds and shows:

- Repository publication, local and master revisions, confirmed sync revision,
  revision lag, last success/error and copying revision. URL buttons copy the
  LAN URL and preserve the required `/svn/` prefix.
- Current transaction size, elapsed/idle time, worker heartbeat, automatic recovery
  events, source contact and free space on the repository filesystem. Revision
  progress is **not a byte percentage or a remaining-time estimate**.
- Known SVN account names and groups plus the latest successful auth check. This
  is not a list of active sessions. Passwords, hashes and raw auth files are never
  returned. USVN UI settings are not replicated.
- Bounded worker history (three files of about 2 MiB each), recent Apache/TLS/auth
  errors (two files of about 2 MiB each), repository/level filters and log download.
  The page has no remote restart/recover or repository-write controls.

Update/rebuild the main HTTP service too to export current repository HEADs in
discovery. Older main images remain compatible; the page then labels the last
master revision sampled by a worker, which can be old during a long transfer.
Freshness timestamps are displayed. Cached successful checks do not prove current
WAN availability. A busy first synchronization can correctly remain unhealthy.

For an interactive mock without NAS access, run
`python3 tests/render_status_mock.py /absolute/output/status-mock.html`.
The resulting fragment contains clearly marked demonstration data and makes no
network requests. Production `index.html` never loads the demo data.

For a normal image upgrade, build first, stop legacy `sync` containers, then
recreate `mirror` and `sync` using the existing project name and Compose overlays.
Keep `.env`, secrets, repositories and state/adoption markers. No repeated
`adopt-all`, history verification or manual lock removal is required.

Update the main HTTP service **before** the mirrors. Preserve `.env`, configs,
credentials and data. The main Compose file is now named `compose.master.yaml`.
Its default project name is `svn-master-http`; an existing deployment must keep its
original project name to reuse containers/networks instead of creating a second stack.
Before the first update from an older checkout, inspect the current master container:

```sh
docker inspect MASTER_CONTAINER --format '{{index .Config.Labels "com.docker.compose.project"}}'
```

Save the returned project name as `COMPOSE_PROJECT_NAME` in the main host's `.env`,
or supply it with `-p` consistently. Keep the actual existing repository/auth paths.

On the main host, in `second-mirror`:

```sh
git pull --ff-only
docker compose -f compose.master.yaml up -d --build --force-recreate master-http
docker compose -f compose.master.yaml exec master-http grep SVNAllowBulkUpdates /etc/apache2/sites-enabled/svnmirror.conf
```

The public repository location must contain `SVNAllowBulkUpdates Off`.
On a mirror using the existing-copy overlay, with no admin operation still running:

```sh
git pull --ff-only
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml stop mirror sync
docker build -t svn-second-mirror:local .
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml run --rm sync sync-once
```

Only after sync-once succeeds:

```sh
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml up -d --force-recreate
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml exec mirror grep -E 'RewriteCond|RewriteRule|SVNAllowBulkUpdates' /etc/apache2/sites-enabled/svnmirror.conf
docker compose -p svn-second-mirror -f compose.auto.yaml -f compose.existing.yaml exec mirror python3 /opt/svnmirror/app.py health
```

Expect proxy rules for OPTIONS/PROPFIND/REPORT and `SVNAllowBulkUpdates Off`.
Discovery requires `lock_metadata_mode=master-skelta-v1`; an older main service is
rejected instead of silently publishing a mirror without this capability.
Then test client locks as described in the [main README](../README.md#tortoisesvn-and-unreal-engine).

## 5. Configuration reference

Examples: [.env.example](.env.example), [automatic main config](config/master.auto.example.json),
[automatic mirror config](config/repos.auto.example.json). The wizard supplies
mirror `.env` and credentials; do not overwrite a live setup with example files.

| Variable | Purpose |
|---|---|
| `MASTER_REPOS`, `MASTER_AUTH_DIR` | Actual main repository parent and auth directory |
| `MASTER_CONFIG_FILE` | Main JSON config; defaults to `./config/master.json` |
| `MASTER_BIND_IP`, `MASTER_PORT` | Main HTTP binding; defaults to loopback:18082 |
| `MIRROR_DATA` | Dedicated local state/data directory |
| `EXISTING_REPOS` | Existing repository parent used by the existing-copy overlay |
| `BIND_IP`, `MIRROR_PORT`, `LIVE_PORT` | Mirror client interface and ports; ports default to 18080/18081 |
| `SYNC_INTERVAL_SECONDS` | Delay between full worker passes; default 30 seconds |
| `DISCOVERY_INTERVAL_SECONDS` | Delay between catalog/auth refreshes during `sync-loop`, independent of history transfers; default 30 seconds |
| `SYNC_TIMEOUT_SECONDS` | General command/adoption timeout; default 21600 seconds; does not cap `synchronize`/`copy-revprops` |
| `SYNC_TRANSFER_TIMEOUT_SECONDS` | Optional total history-transfer cap; default 0 (unlimited while active) |
| `SYNC_IDLE_TIMEOUT_SECONDS` | Stop/retry a transfer with no output, I/O or active transaction-file changes; default 3600 seconds |
| `SYNC_PROGRESS_SECONDS` | Transfer progress/heartbeat interval; default 30 seconds |
| `SYNC_METADATA_TIMEOUT_SECONDS` | Source identity/HEAD query timeout; default 120 seconds |
| `STATUS_USERS` | Comma-separated SVN logins allowed into the status page/API; default `svnmirror` |
| `MAX_SYNC_AGE_SECONDS`, `MAX_LAG_REVISIONS` | Health thresholds; auto Compose defaults to 1800 seconds and zero lag |
| `REVPROP_RESCAN_SECONDS` | Periodic old revision-property reconciliation; auto Compose default 86400 seconds |
| `COMPOSE_PROJECT_NAME` | Persistent deployment identity, particularly important during upgrades |

The static `compose.yaml` remains available for manually listed repositories.
Use `config/master.example.json`, `config/repos.example.json` and
`config/source.example.json`, with your actual server version and identical public
URL paths on both servers. Static auth is supplied through `AUTH_DIR`; source
credentials through `CREDENTIALS_FILE`. It does not auto-discover repositories or
users. The main HTTP server must also have bulk updates disabled for this mode.

## 6. Optional IP-certificate TLS service

An existing domain certificate/reverse proxy is usually the simplest HTTPS setup.
Alternatively, `compose.https.yaml` adds Nginx and automated Certbot issuance/renewal
for a permanent public IPv4 address, using a CA that supports IP certificates.
Set `PUBLIC_IP`, `ACME_EMAIL`, `TLS_DATA`, and port bindings from `.env.example`.

```sh
docker compose -f compose.master.yaml -f compose.https.yaml up -d --build
docker compose -f compose.master.yaml -f compose.https.yaml logs -f https
```

HTTP-01 validation requires public port **80** to reach the challenge handler on
`ACME_HTTP_PORT` (default 18083). An existing HTTP service can internally proxy only
`/.well-known/acme-challenge/` there. Choosing another external port does not change
the ACME validation port. Forward the chosen HTTPS port to `SVN_HTTPS_PORT` as needed.
Certificate enrollment accepts the CA terms using the configured email account.
Health checks and renewal retries are included; real CA issuance and router paths
must be validated in the target network. This service is optional: do not run it on
bindings already used by another TLS proxy.

## Development checks

Run from this directory with Docker and Python 3:

```sh
docker build -t svn-second-mirror:local .
python tests/integration.py
python tests/auto_integration.py
docker build -t svn-second-mirror-tls:local tls
python tests/tls_integration.py
```

Tests use disposable containers, networks and volumes. They cover replication,
existing-copy adoption, LAN downloads, write-through Commit/Copy/Move, permissions,
authoritative locks, stolen-token handling, offline failure, revprops and TLS.
Set `SVN_CLIENT_EXE` to a local SVN executable to add native-client checks to
`integration.py`; this has been exercised with Unreal's bundled SVN 1.9.5.
TLS tests use a local CA and a fake Certbot, not production certificate issuance.

Additional Linux checks run with Python 3 and native SVN 1.14 tools:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

They exercise actual SVN interrupted-revision recovery, competing/orphaned writer
locks, idle/stop handling, account/auth refresh, snapshot redaction and real Apache
authorization/CGI. The HTTP test skips when Apache is absent; set `APACHE_BINARY`,
`APACHE_MODULE_DIR` and `HTPASSWD_BINARY` for a private runtime. `node
tests/status_ui.cjs` adds Playwright browser checks (set `PLAYWRIGHT_MODULE` and
optionally `STATUS_TEST_BROWSER` when dependencies are outside normal lookup).
