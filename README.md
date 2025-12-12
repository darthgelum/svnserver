# Docker Subversion Server with USVN Web GUI and svn:// protocol

This Docker container provides a complete SVN solution with **USVN** - a powerful web-based GUI for managing Subversion repositories, users, groups, and permissions.

**Key Features:**
- 🌐 **USVN Web Interface** - Manage everything through a user-friendly web GUI
- 🔌 **svn:// Protocol** - Direct svnserve access for fast operations
- 👥 **Centralized User Management** - Create and manage users/groups from one place
- 📦 **Repository Management** - Create, configure, and manage all repositories
- 🔐 **Permission Control** - Fine-grained access control per repository
- 🚀 **HTTP/HTTPS Access** - Optional WebDAV access to repositories

This Docker container is intended to run on **Synology DSM 7.x**, as a replacement for the SVN server package (dropped by Synology). However, it can be used on other servers as well.


---

- [Docker Subversion Server with USVN Web GUI and svn:// protocol](#docker-subversion-server-with-usvn-web-gui-and-svn-protocol)
  - [What is USVN?](#what-is-usvn)
  - [Quick start for Synology DSM 7.x users](#quick-start-for-synology-dsm-7x-users)
    - [Preconditions](#preconditions)
    - [Build and Run the container](#build-and-run-the-container)
    - [Access USVN Web Interface](#access-usvn-web-interface)
    - [Initial USVN Setup](#initial-usvn-setup)
    - [Create Users and Groups](#create-users-and-groups)
    - [Create and Manage Repositories](#create-and-manage-repositories)
    - [SVN checkout on client](#svn-checkout-on-client)
    - [SVN copy existing repository](#svn-copy-existing-repository)
  - [Docker configurations](#docker-configurations)
    - [Volumes](#volumes)
    - [Ports](#ports)
    - [Environment Variables](#environment-variables)
  - [Image Components](#image-components)
    - [Ubuntu 22.04](#ubuntu-2204)
    - [Tini-Init process](#tini-init-process)
    - [Entrypoint-Script](#entrypoint-script)
    - [Apache and USVN](#apache-and-usvn)
    - [SVN server](#svn-server)
  - [Docker build (force cache invalidation)](#docker-build-force-cache-invalidation)
  - [Links](#links)

---

## What is USVN?

**USVN (User-friendly SVN)** is a web-based administration tool for Subversion that provides:

- **Web Interface**: Intuitive GUI accessible from any browser
- **User Management**: Create, edit, and delete users with passwords
- **Group Management**: Organize users into groups for easier permission management
- **Repository Management**: Create and configure SVN repositories through the web
- **Permission Control**: Set read/write permissions per user/group per repository
- **Multi-Protocol Access**: Supports both svn:// protocol and HTTP/WebDAV access
- **Centralized Administration**: Manage all repositories and users from one place

USVN eliminates the need to manually edit `passwd`, `authz`, and `svnserve.conf` files!

---

## Quick start for Synology DSM 7.x users

Quick start instructions for users not interested in details.

### Preconditions

Following is assumed:

- You run Synology DSM 7.x on your NAS (can be tested with 6.2 before update)
- Docker package is installed
- SVN repos are stored in `/volume1/svn/`
- Optional: Git server package is installed (for cloning from github)

### Build and Run the container

To run the USVN server, first ssh into your NAS and execute:

```bash
cd /volume1/svn/
git clone https://github.com/MarkusH1975/svnserver.svn.mh.git
cd svnserver.svn.mh/
# Clone USVN if not already present
git clone https://github.com/usvn/usvn.git
sudo ./start.sh
```

### Access USVN Web Interface

Once the container is running, open your web browser and navigate to:

```
http://localhost:8080/usvn/
```

Or replace `localhost` with your server's IP address: `http://your-server-ip:8080/usvn/`

### Initial USVN Setup

On first access, USVN will guide you through the initial setup:

1. **Language Selection**: Choose your preferred language
2. **Database Configuration**: USVN uses SQLite by default (no configuration needed)
3. **SVN Configuration**:
   - SVN binary path: `/usr/bin/svn`
   - SVN repositories path: `/volume/svnrepo`
4. **Admin Account**: Create the administrator account
   - Username: admin (or your choice)
   - Password: (set a strong password)
   - Email: your email address

5. Click "Install" to complete the setup

### Create Users and Groups

After installation, log in with your admin account:

1. **Create Users**:
   - Go to `Users` → `Add User`
   - Enter username, password, and email
   - Click `Create`

2. **Create Groups**:
   - Go to `Groups` → `Add Group`
   - Enter group name and description
   - Add users to the group
   - Click `Create`

### Create and Manage Repositories

Create new SVN repositories through the USVN web interface:

1. Go to `Repositories` → `Create Repository`
2. Enter repository name (e.g., `myProject`)
3. Click `Create`
4. Set permissions:
   - Go to `Repositories` → Select your repository
   - Click `Permissions`
   - Assign read/write permissions to users or groups
   - Click `Save`

The repository is now accessible via:
- **svn:// protocol**: `svn://your-server-ip/myProject`
- **HTTP protocol**: `http://your-server-ip:8080/svn/myProject`

### SVN checkout on client

Now you can checkout your repository on the client:

```bash
# Via svn:// protocol (faster)
svn co --username youruser svn://serverip/myProject/

# Or via HTTP
svn co --username youruser http://serverip:8080/svn/myProject/
```

### SVN copy existing repository

**Create a Backup!** If you have existing SVN repositories, you can import them into USVN:

```bash
# Copy your existing repository
sudo cp -Rv /volume1/svn/myRepo1 /volume1/svn/svnserver.svn.mh/volume/svnrepo/
sudo chmod 777 -Rv /volume1/svn/svnserver.svn.mh/volume/svnrepo/myRepo1

# Import into USVN
# Go to USVN web interface → Repositories → Import existing repository
# Select the repository folder and follow the wizard
```

After import, configure permissions through the USVN web interface.

---

## Docker configurations

### Volumes

| Mountpoint | Container Folder | Description |
| - | - | - |
| `./volume/svnrepo/` | `/volume/svnrepo/` | Folder for SVN repositories. |
| `./volume/usvn/` | `/volume/usvn/` | USVN data directory (database, cache, files). |
| `./usvn/src/` | `/var/www/html/usvn/` | USVN application source code (read-only). |

### Ports

| Host Port | Container Port | Description |
| - | - | - |
`0.0.0.0:8080 TCP` | `80 TCP` | USVN web interface and HTTP SVN access, http://serverip:8080/usvn/
`0.0.0.0:3690 TCP` | `3690 TCP` | svnserve port for svn:// protocol

### Environment Variables

Environment variables to control `entrypoint.sh` script. Already set by default.

| Env var | Description |
| ------- | ----------- |
| `ENABLE_SVNSERVER=true`  |  Start svnserve for svn:// protocol access  |
| `ENABLE_APACHE=true`  |  Start Apache web server for USVN and HTTP SVN access  |

---

## Image Components

### Ubuntu 22.04

Was chosen as the latest Ubuntu release.

### Tini-Init process

Tini is added to have a valid init process, running as PID1. Read more information on the project page. <https://github.com/krallin/tini>.
Tini init process together with the provided entrypoint-script, is able to **run multiple services**, including graceful shutdown. It can be used as a template for other docker projects. If you attach to the container, the entrypoint-script offers a micro CLI. Type `help` for help.

### Entrypoint-Script

The `entrypoint.sh` is the central bash script, which is started from tini. It can start multiple services and offers graceful shutdown of the started services. (Tini jumps in for unhandled processes.)
Furthermore the script will **initialize** the defined **volume folder**.

Since this script is the main docker process, it cannot end and needs to run in an endless loop. To make something useful, it offers a **micro command line interface**, which can be accessed via **docker attach**. Please attach to it and type `help` for more information.

### Apache and USVN

**Apache** web server hosts the USVN web application and provides HTTP/WebDAV access to SVN repositories.

**USVN (User-friendly SVN)** is a comprehensive web-based administration interface for Subversion. It provides:
- Repository creation and management
- User and group administration
- Permission management with granular access control
- Multi-protocol support (svn://, http://, https://)
- Centralized configuration for all repositories

USVN is accessible at `http://serverip:8080/usvn/` when the container is running.

**Project:** <https://github.com/usvn/usvn>

### SVN server

SVN server `svnserve` is started and listens on port 3690, providing fast direct access via the svn:// protocol. This works in parallel with HTTP access provided by Apache.

---

## Docker build (force cache invalidation)

Sometimes docker build has problems to recognize that the build cache should be invalidated at some certain point. For example, if the `entrypoint.sh` script has changed, docker build is probably still using the cache and does not add the new version of the file. To force cache invalidation at a certain point the argument `CACHE_DATE` is used. Have a look at the Dockerfile and `start.sh`, how it is used.

---

## Links

This project was inspired by different Github projects and other sources, see some links below.

**USVN Project:**<br>
<https://github.com/usvn/usvn><br>

**Docker Components:**<br>
<https://github.com/krallin/tini><br>
<https://github.com/phusion/baseimage-docker><br>

**SVN Docker Projects:**<br>
<https://github.com/elleFlorio/svn-docker><br>
<https://github.com/smezger/svn-server-ubuntu><br>
<https://github.com/jocor87/docker-svn-ifsvnadmin><br>
<https://github.com/MarvAmBass/docker-subversion><br>
<https://github.com/ZevenFang/docker-svn-ifsvnadmin><br>
<https://github.com/garethflowers/docker-svn-server><br>

<https://github.com/mfreiholz/iF.SVNAdmin><br>

<https://kb.synology.com/en-sg/DSM/tutorial/How_to_launch_an_SVN_server_based_on_Docker_on_your_Synology_NAS><br>
<https://goinbigdata.com/docker-run-vs-cmd-vs-entrypoint/><br>
<https://docs.docker.com/config/containers/multi-service_container/><br>
<https://github.com/docker-library/official-images#init><br>
<https://www.cyberciti.biz/faq/howto-regenerate-openssh-host-keys/><br>
<https://svnbook.red-bean.com/en/1.7/svn.serverconfig.choosing.html><br>

<https://serverfault.com/questions/156470/testing-for-a-script-that-is-waiting-on-stdin><br>
<https://stackoverflow.com/a/42599638><br>
<https://stackoverflow.com/a/39150040><br>
<https://stackoverflow.com/q/70637123><br>

<https://serverfault.com/questions/23644/how-to-use-linux-username-and-password-with-subversion><br>
<https://stackoverflow.com/questions/27131309/difference-between-svnrdump-dump-svnadmin-dump><br>
<https://stackoverflow.com/a/69081169><br>

<https://www.monitorix.org/><br>
<https://www.monitorix.org/faq.html><br>

