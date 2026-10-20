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
If an interrupted operation left `svn:sync-lock`, stop all workers/manual operations,
run `run --rm sync recover NAME`, then resume. This is a replication lock, not a
user file lock. Do not use permanent lock stealing or disable locking.

Check free disk space, WAN reachability, TLS trust, source credentials and filesystem
permissions when sync fails. Apache/discovery diagnostics on the main host:

```sh
docker compose -f compose.master.yaml logs --tail=100 master-http
docker compose -f compose.master.yaml exec master-http tail -n 80 /var/log/apache2/error.log
```

Repository removal from the source catalog removes its published mirror URL after a
successful discovery, but keeps local data. Auth changes are delivered by discovery;
offline direct file reads may still use the last cached rules until it succeeds.
The mirror never becomes an independent master. Back up the main server and save
host-specific config, secrets, mirror state/adoption markers and a recovery plan.

## 4. Upgrades and existing deployments

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
| `SYNC_TIMEOUT_SECONDS` | Per-SVN-process timeout; default 21600 seconds |
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
