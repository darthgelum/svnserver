# SVN Server with USVN for Docker and Synology

A self-hosted Subversion server with a web administration panel, persistent storage,
and an optional local mirror for remote teams.

[USVN](https://github.com/usvn/usvn) manages repositories, users, groups, and access
rules. The main container serves `svn://`; the optional Apache services add HTTP(S)
access and automatic replication to another Docker host.

Repository: [darthgelum/svnserver](https://github.com/darthgelum/svnserver).

## Deployment options

| Deployment | Components |
|---|---|
| One SVN server with a web panel | Root `docker-compose.yaml` |
| HTTP(S) access to the same repositories | Add `second-mirror/compose.master.yaml` and a TLS reverse proxy |
| Remote team downloading assets over its LAN | Add a mirror host using `second-mirror/compose.auto.yaml` |
| Mirror bootstrapped from an existing repository copy | Add `second-mirror/compose.existing.yaml` during setup and operation |

The main server remains the only place where user commits and locks are created.
The mirror serves file contents locally and forwards metadata and write operations
to the main server, so clients see authoritative file locks.

## Requirements

- Docker Engine and Docker Compose; on supported Synology models, Container Manager
  or the older Docker package.
- Git for obtaining the source, or an extracted checkout of this repository.
- Local persistent storage writable by the containers.
- For a remote mirror: authenticated HTTPS connectivity to the main HTTP endpoint.

Examples use `docker compose` on a Linux shell. Add `sudo` where required by your
Docker installation. The automatic mirror Compose files also support standalone
`docker-compose` v1. The master and static mirror Compose files require Compose v2.
Builds and integration tests have been exercised on Linux amd64. Check binary/image
architecture compatibility before deploying on a different NAS architecture.

## Quick start: SVN and web administration

Choose any suitable directory on your host. On Synology this is normally a shared
folder on a local volume; no particular shared-folder or volume name is required.

```sh
git clone https://github.com/darthgelum/svnserver.git
cd svnserver
mkdir -p volume/svnrepo volume/usvn/config
```

USVN source is included under `usvn/src`; no additional USVN clone is needed.

### Persist the USVN installation configuration

The main Compose file mounts repository and USVN data directories. The application
also reads its installation configuration from `/var/www/html/usvn/config` inside
the container. For a **new installation**, create `compose.local.yaml` to persist it:

```yaml
services:
  usvn:
    volumes:
      - ./volume/usvn/config:/var/www/html/usvn/config
```

Then build and start:

```sh
docker compose -f docker-compose.yaml -f compose.local.yaml up -d --build
```

For an **existing installation**, copy the current application config directory to
persistent storage before adding this mount. Mounting an empty directory over an
installed config hides the existing files. Back up the database and configuration
before rebuilding or recreating an existing server.

Open `http://HOST:8080/usvn/` and complete the USVN installer. Use container paths:

| Setting | Value |
|---|---|
| SVN storage root | `/volume/svnrepo` |
| Actual repository directories | `/volume/svnrepo/svn/REPOSITORY` |
| HTTP password file | `/volume/usvn/htpasswd` |
| Access rules | `/volume/usvn/authz` |
| SQLite database, if selected | A file under `/volume/usvn/`, such as `/volume/usvn/usvn.sqlite` |
| Repository URL for the initial svnserve setup | `svn://HOST/` |

Create the administrator account, then add users/groups and a test repository.
Set its access permissions in USVN. Test with a normal user:

```sh
svn checkout svn://HOST/Example/trunk Example
```

Use the repository root instead of `/trunk` if standard directories have not been
created. `Example`, `HOST`, ports, and host storage paths are placeholders.

### Storage and ports

| Default host binding | Purpose |
|---|---|
| `8080:80` | USVN web administration and Apache |
| `3690:3690` | Native `svn://` access |

| Host directory | Container directory | Contents |
|---|---|---|
| `./volume/svnrepo` | `/volume/svnrepo` | Repository storage root |
| `./volume/usvn` | `/volume/usvn` | Auth files, database and other configured USVN data |
| `./volume/usvn/config` with the override above | `/var/www/html/usvn/config` | USVN installation configuration |

The root Compose file binds its ports on all interfaces. Adjust bindings, firewall
rules, and your reverse proxy to fit the intended network. HTTPS terminates at a
configured reverse proxy; the base container does not issue certificates.

The bundled Apache template has `SVNParentPath /volume/svnrepo`, while the normal
USVN/svnserve layout places repositories under `/volume/svnrepo/svn`. Before using
that container's `/svn` endpoint, align its Apache configuration with the actual
repository parent. Alternatively, use the separate HTTP service below, whose
repository mount is explicitly configured. Do not assume that the panel URL is also
a correctly configured repository URL.

The main image uses Ubuntu 22.04, Subversion, Apache/PHP, SQLite support, and Tini.
`ENABLE_SVNSERVER=true` and `ENABLE_APACHE=true` control which services are started.
The entrypoint configures repositories for the shared USVN auth files and adjusts
filesystem permissions. Its current permission policy is broad: review host ACLs
and storage isolation for deployments shared with other untrusted local users.

## Optional HTTP access and remote mirrors

See **[second-mirror/README.md](second-mirror/README.md)** for setup, existing-copy
bootstrap, TLS, upgrades, and monitoring.

The addon uses separate containers and shares the main repository storage. It can
also be adapted to an existing SVN server supplying compatible htpasswd/authz
files. It does not require another writable SVN server at the mirror site.

| Default addon port | Purpose |
|---|---|
| `127.0.0.1:18082` on the main host | Main HTTP backend, normally behind HTTPS |
| `18080` on the configured mirror interface | Local file downloads, authoritative metadata/locks, proxied commits |
| `18081` on the configured mirror interface | Full proxy to the main server; file downloads also cross the WAN |

These ports are configurable. Mirror bindings default to loopback until a client
interface is configured.

### How locks and local downloads work

On the mirror's normal endpoint, OPTIONS, PROPFIND, and REPORT go to the main
server. The main server uses `SVNAllowBulkUpdates Off`: Checkout/Update return a
skeletal change description, and the client fetches file contents with separate
GET requests from the local mirror. LOCK, UNLOCK, and Commit execute on the main
server. Lock information is therefore read from the authority rather than copied
periodically by `svnsync`.

This has two practical limits:

- Network SVN operations need connectivity to the main server. Files already in a
  working copy remain available during an outage; cached status icons may be stale.
- A new revision must reach the mirror before its files can be downloaded locally.
  An Update attempted during replication lag can fail; retry after synchronization.

Properties, logs, and other metadata still cross the WAN. There is no offline commit
queue, automatic promotion to master, or multi-master repository merging.

## TortoiseSVN and Unreal Engine

Use the mirror's **18080** endpoint for local downloads and authoritative locks:
`http://MIRROR_HOST:18080/svn/Example/trunk` or the required branch.

For an existing working copy, close Unreal, preserve local changes, release your
active locks through its old endpoint, then use **TortoiseSVN → Relocate** to change
the repository root while preserving the branch path. Relocate keeps the same
repository UUID; it does not switch branches or require downloading everything.

In Unreal, select the **Subversion** source-control provider and use the developer's
personal account. The working copy must already point at the correct endpoint:
changing a Repository field in the editor does not relocate it.

- **Check Out / Get Lock** before editing an asset; **Check In / Commit** to submit.
- Refresh source-control status to see other users' locks. In TortoiseSVN, use
  **Check for Modifications → Check Repository**.
- Use `svn:needs-lock = *` on binary assets such as `.uasset` and `.umap` to make
  working files read-only until locked. This is a client workflow aid, not a
  server-side requirement to lock every otherwise-unlocked file before committing.

Validate with two users: the second sees the first user's lock and cannot acquire
it; Commit without Keep locks releases it. Also test a lock created directly on
the main server and observed through the mirror without running replication.

## Routine administration

For the main server, use the same Compose files as during installation:

```sh
docker compose -f docker-compose.yaml -f compose.local.yaml ps
docker compose -f docker-compose.yaml -f compose.local.yaml logs --tail=100 usvn
```

Choose the operation needed from this table:

| Operation | Command suffix with the same Compose files |
|---|---|
| Start | `up -d` |
| Restart existing containers | `restart` |
| Stop for maintenance | `stop` |
| Apply source changes | `up -d --build` |
| Apply changed environment or bindings | `up -d --force-recreate` |

Back up a running repository using `svnadmin hotcopy` or an appropriately coordinated
snapshot. Also save the USVN database, installation config, and authentication files.
An ordinary live folder sync is not a consistent SVN backup or replication method.
Existing repositories must be registered in USVN as well as present on disk; use
USVN's import facilities and validate access before switching users.

Before upgrading an existing deployment, preserve its local configuration and data,
then fetch code with `git pull --ff-only`. See the addon guide before updating a
mirror installation: update the main HTTP service first and preserve its Compose
project identity. Do not rerun the installer or overwrite `.env` to restart services.

## Repository layout

| Path | Purpose |
|---|---|
| `docker-compose.yaml` | Main USVN/svnserve deployment |
| `docker-context/` | Main image, Apache config, and startup/auth helpers |
| `usvn/src/` | Included USVN application source |
| `volume/` | Default host persistence directories; runtime data is not source code |
| `second-mirror/` | Optional HTTP master endpoint, automatic mirror, TLS and tests |

The repository's Docker/addon code is covered by [LICENSE](LICENSE). Included USVN
and bundled dependencies retain their respective licenses and copyright notices.
The project builds on the original work by Markus Hilsenbeck and the USVN project.

Further reading: [Subversion](https://subversion.apache.org/),
[USVN](https://github.com/usvn/usvn),
[TortoiseSVN locks](https://tortoisesvn.net/docs/release/TortoiseSVN_en/tsvn-dug-locking.html),
[SVN skelta updates](https://subversion.apache.org/docs/release-notes/1.8.html).
