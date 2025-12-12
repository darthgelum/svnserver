#!/usr/bin/env php
<?php















































echo "User can now commit using: svn commit --username $username --password $password\n";echo "Password set successfully for user: $username\n";chmod($passwdPath, 0666);file_put_contents($passwdPath, implode("\n", $newLines) . "\n");// Write back to file}    $newLines[] = "$username = $password";if (!$found) {// If user not found, add them}    }        $newLines[] = $line;    } else {        $found = true;        $newLines[] = "$username = $password";    if (preg_match('/^' . preg_quote($username, '/') . '\s*=/', $line)) {    // Check if this line is for the target userforeach ($lines as $line) {$newLines = [];$found = false;$lines = file($passwdPath, FILE_IGNORE_NEW_LINES);// Read current passwd file}    exit(1);    echo "Error: passwd file not found at $passwdPath\n";if (!file_exists($passwdPath)) {$passwdPath = '/volume/usvn/passwd';$password = $argv[2];$username = $argv[1];}    exit(1);    echo "Example: $argv[0] admin mypassword123\n";    echo "Usage: $argv[0] <username> <password>\n";if ($argc !== 3) { */ * Usage: set-svn-password <username> <password> * Set svn:// protocol password for a user/**/**
 * Sync USVN users to svnserve passwd format
 * This script reads USVN's database and creates a passwd file for svnserve
 */

// Path to USVN config
$configPath = '/var/www/html/usvn/config/config.ini';
$passwdPath = '/volume/usvn/passwd';

if (!file_exists($configPath)) {
    echo "USVN not configured yet. Creating default passwd file.\n";
    createDefaultPasswd($passwdPath);
    exit(0);
}

// Read USVN config - don't parse sections since keys have dots
$config = parse_ini_file($configPath, false);

// Get database path from flat config
$dbPath = null;
if (isset($config['database.options.dbname'])) {
    $dbPath = $config['database.options.dbname'];
} elseif (isset($config['database.path'])) {
    $dbPath = $config['database.path'];
}

if (!$dbPath) {
    echo "Database path not found in config.\n";
    createDefaultPasswd($passwdPath);
    exit(0);
}

if (!file_exists($dbPath)) {
    echo "Database file not found: $dbPath\n";
    createDefaultPasswd($passwdPath);
    exit(0);
}

try {
    // Connect to USVN's SQLite database
    $db = new PDO('sqlite:' . $dbPath);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    
    // Get all active users
    $stmt = $db->query("SELECT users_login FROM usvn_users");
    $users = $stmt->fetchAll(PDO::FETCH_COLUMN);
    
    // Read existing passwd file to preserve manually set passwords
    $existingPasswords = [];
    if (file_exists($passwdPath)) {
        $lines = file($passwdPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        foreach ($lines as $line) {
            if (preg_match('/^([^#\s=]+)\s*=\s*(.+)$/', trim($line), $matches)) {
                $existingPasswords[$matches[1]] = $matches[2];
            }
        }
    }
    
    // Create passwd file content
    $passwdContent = "[users]\n";
    $passwdContent .= "# IMPORTANT: Passwords for svn:// protocol access\n";
    $passwdContent .= "# These are separate from USVN web passwords!\n";
    $passwdContent .= "# Format: username = password (plain text)\n";
    $passwdContent .= "# \n";
    $passwdContent .= "# To set a user's svn:// password, edit this file:\n";
    $passwdContent .= "# docker exec -it usvn.svn nano /volume/usvn/passwd\n";
    $passwdContent .= "# OR run: docker exec usvn.svn bash -c 'echo \"username = newpassword\" >> /volume/usvn/passwd'\n\n";
    
    foreach ($users as $username) {
        // Use existing password if available, otherwise prompt to set it
        if (isset($existingPasswords[$username])) {
            $password = $existingPasswords[$username];
        } else {
            $password = "CHANGE_ME_" . $username;
        }
        $passwdContent .= "$username = $password\n";
    }
    
    // Write passwd file
    file_put_contents($passwdPath, $passwdContent);
    chmod($passwdPath, 0666);
    
    echo "Synced " . count($users) . " users to passwd file.\n";
    echo "Location: $passwdPath\n";
    echo "\nNOTE: svn:// passwords are separate from USVN web passwords.\n";
    echo "Users with 'CHANGE_ME_' passwords need to have their passwords set manually.\n";
    echo "Edit: docker exec -it usvn.svn nano /volume/usvn/passwd\n";
    
} catch (Exception $e) {
    echo "Error: " . $e->getMessage() . "\n";
    createDefaultPasswd($passwdPath);
    exit(1);
}

function createDefaultPasswd($passwdPath) {
    $content = "[users]\n";
    $content .= "# Add users here in format: username = password\n";
    $content .= "# Passwords must be in plain text for svnserve\n";
    $content .= "admin = admin\n";
    
    file_put_contents($passwdPath, $content);
    chmod($passwdPath, 0666);
    echo "Created default passwd file with admin user.\n";
}
