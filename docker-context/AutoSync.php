<?php
/**





















         }             $this->_redirect("/users/index");             USVN_Translation::addMessage(sprintf(T_("User %s updated."), $login));+            +            exec('/usr/local/bin/sync-passwd > /dev/null 2>&1 &');+            // Auto-sync to svnserve passwd                          $user->save();             $user->setUsersPassword($password);@@ -80,6 +83,9 @@ class UsersController extends USVN_Controller         }             $this->_redirect("/users/index");             USVN_Translation::addMessage(sprintf(T_("User %s added."), $login));+            +            exec('/usr/local/bin/sync-passwd > /dev/null 2>&1 &');+            // Auto-sync to svnserve passwd                          $user->save();             $user->setUsersPassword($password);@@ -50,6 +50,9 @@ class UsersController extends USVN_Controller * USVN Hook - Automatically sync users and configure repositories
 * This file should be placed in USVN source to hook into user/repo operations
 */

// Hook into USVN's user save operation
function usvn_hook_user_save() {
    exec('/usr/local/bin/sync-passwd > /tmp/sync-passwd.log 2>&1');
}

// Hook into USVN's repository creation
function usvn_hook_repository_create($repoPath) {
    exec("/usr/local/bin/configure-repo '$repoPath' > /tmp/configure-repo.log 2>&1");
    exec('/usr/local/bin/sync-passwd > /tmp/sync-passwd.log 2>&1');
}

// Register hooks if USVN is loaded
if (class_exists('USVN_Db_Table_Users')) {
    // Patch the Users table class
    $originalSave = 'USVN_Db_Table_Users::save';
    
    // We'll patch by modifying the source files during container build
}
