#!/bin/bash
#############################
# Markus Hilsenbeck
# Feb 2022
#
# entrypoint-script: 
#   - offers volume init 
#   - able to run multiple services
#   - offers a micro cli
#

# give a change to attach immediately and see startup messages
sleep 1
echo -e "\n * Starting entrypoint script."


####################################### init volume functions

init_volume_folder () {
  if [ ! -d "/volume/$1" ]; then
    echo -e " * Create volume folder and set permissions: /volume/$1."
    mkdir -pv /volume/$1
    chmod -Rfv 777 /volume/$1
  else
    echo -e " * No need to create, folder exist already: /volume/$1."
  fi
}

copy_volume_folder () {
	if [ ! "$(ls -A /volume/$1 | grep -v '.gitignore')" ]; then
    echo -e " * Empty volume folder, /volume/$1, copy files from /volume.template/$1"
    cp -fvra /volume.template/$1/* /volume/$1/
    chmod -Rfv 777 /volume/$1
  else
    echo -e " * Nothing to copy, folder not empty: /volume/$1."
	fi
}

####################################### init volume folders
 
init_volume_folder "svnrepo"
init_volume_folder "usvn"

# Ensure svnrepo has proper permissions for www-data (USVN needs to create repos)
chown -R www-data:www-data /volume/svnrepo
chmod -R 777 /volume/svnrepo
echo -e " * SVN repository folder permissions set for www-data."

# Configure svnserve to use USVN authentication files
# This function updates all repository svnserve.conf files
configure_svnserve_auth() {
  echo -e " * Configuring svnserve to use USVN authentication."
  
  # Find all repositories and configure them
  local configured_count=0
  for repo_path in /volume/svnrepo/svn/*/conf/svnserve.conf; do
    if [ -f "$repo_path" ]; then
      repo_name=$(basename $(dirname $(dirname "$repo_path")))
      
      # Check if already configured
      if grep -q "^password-db = /volume/usvn/passwd" "$repo_path"; then
        echo -e "   - Repository $repo_name: already configured"
      else
        echo -e "   - Configuring repository: $repo_name"
        /usr/local/bin/configure-repo "$(dirname $(dirname "$repo_path"))"
        configured_count=$((configured_count + 1))
      fi
    fi
  done
  
  if [ $configured_count -gt 0 ]; then
    echo -e " * Configured $configured_count repositories."
  fi
}

# Run configuration if there are repositories
if [ -d "/volume/svnrepo/svn" ]; then
  echo -e " * Checking repository configuration."
  configure_svnserve_auth
fi

# Initialize USVN data directory permissions
if [ -d "/var/www/html/usvn" ]; then
  echo -e " * Setting USVN permissions."
  
  # Set ownership (this works even on read-only mounts for process access)
  chown -R www-data:www-data /var/www/html/usvn 2>/dev/null || true
  
  # Don't try to chmod mounted read-only filesystem
  # The source code from ./usvn/src is mounted read-only, which is correct
  # We only need write access to the data directories
  
  # Create writable directories for USVN data
  mkdir -p /volume/usvn/cache /volume/usvn/files /volume/usvn/config
  chown -R www-data:www-data /volume/usvn
  chmod -R 777 /volume/usvn
  echo -e " * USVN data folders created with write access."
fi


####################################### Services START/STOP

####################################### svnserver
function START_svnserver()
{
  if [[ "$ENABLE_SVNSERVER" != "true" ]] ; then
    echo -e " * Svnserver disabled."
    return
  fi

  echo -e " * Starting svnserve."
  svnserve -d -r /volume/svnrepo/svn --listen-port 3690 
}

function STOP_svnserver()
{
  echo -e " * Stopping svnserve: done by tini."
}


####################################### apache
function START_apache()
{
  if [[ "$ENABLE_APACHE" != "true" ]] ; then
    echo -e " * Apache disabled."
    return
  fi

  echo -e " * Starting Apache web server for USVN."
  service apache2 start
}

function STOP_apache()
{
  echo -e " * Stopping Apache."
  service apache2 stop
}


################### signal handler ###################
# ignore Ctrl+C
function SIGINT_handler()
{
    echo -e "\nIgnore Ctrl+C, SIGINT. Use docker stop."
}

# docker stop sends SIGTERM
function SIGTERM_handler()
{
    echo -e "\n * Received SIGTERM/STOP: graceful shutdown services."

    STOP_svnserver
    STOP_apache

    exit 0;
}

trap SIGINT_handler SIGINT
trap SIGTERM_handler SIGTERM 


################### start services ###################
START_svnserver
START_apache


################### check if tty is connected ###################

if ! tty -s ; then
  echo "No Terminal connected. To use micro cli, use docker run -it ..."
  # wait for SIGTERM
  while true; do
    sleep 1
  done
fi

################### start micro cli, only when tty is connected  ###################
echo -e "Type help to show help.\n"

while true; do
  read -p "micro cli> " -r line

  case "$line" in
    help)
      echo -e "\nCtrl+P, Ctrl+Q : detach docker container."
      echo "stop : stop docker container." 
      echo "bash : start bash inside docker container.\n"
      echo "ps : show process tree."
      continue 
      ;;
    stop)
      echo -e "\n ****************************************************"
      echo -e " Stop docker container. Send SIGTERM to PID 1."
      echo -e " ATTENTION: might be restarted by docker restart policy."
      echo -e " ****************************************************\n"
      # send SIGTERM to PID 1 (tini)
      kill 1
      sleep 1
      ;;
    bash)   
      echo -e "\nStarting bash. Type exit when done or just detach with Ctrl+P, Ctrl+Q.\n"
      bash
      continue 
      ;;
    ps)   
      pstree -pslna
      ;;
    *)
      echo -e "Type help to show help."
      sleep 1
      ;;
  esac
done
