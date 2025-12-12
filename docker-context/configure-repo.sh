#!/bin/bash
# Hook script to automatically configure new SVN repositories
# This is called by USVN after repository creation

REPO_PATH="$1"

if [ -z "$REPO_PATH" ]; then
    echo "Usage: $0 <repository-path>"
    exit 1
fi

SVNSERVE_CONF="$REPO_PATH/conf/svnserve.conf"

if [ ! -f "$SVNSERVE_CONF" ]; then
    echo "Error: svnserve.conf not found at $SVNSERVE_CONF"
    exit 1
fi

echo "Configuring svnserve for repository: $REPO_PATH"

# Update svnserve.conf
sed -i 's/^# anon-access = read/anon-access = none/' "$SVNSERVE_CONF"
sed -i 's/^# auth-access = write/auth-access = write/' "$SVNSERVE_CONF"
sed -i 's|^# password-db = passwd|password-db = /volume/usvn/passwd|' "$SVNSERVE_CONF"
sed -i 's|^# authz-db = authz|authz-db = /volume/usvn/authz|' "$SVNSERVE_CONF"

echo "Repository configured successfully"
