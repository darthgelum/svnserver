#!/usr/bin/env php
<?php
/**
 * CLI utility to set svn:// protocol password for a user
 * 
 * Usage: set-svn-password <username> <password>
 * Example: docker exec usvn.svn set-svn-password admin mypassword123
 */

if ($argc != 3) {
    echo "Usage: set-svn-password <username> <password>\n";
    echo "Example: set-svn-password admin mypassword123\n";
    exit(1);
}

$username = $argv[1];
$password = $argv[2];
$passwdFile = '/volume/usvn/passwd';

// Read existing passwd file
$lines = file_exists($passwdFile) ? file($passwdFile, FILE_IGNORE_NEW_LINES) : [];
$newLines = [];
$found = false;

// Update the user's password or add if not exists
foreach ($lines as $line) {
    $line = trim($line);
    if (empty($line) || $line[0] === '#' || $line[0] === '[') {
        $newLines[] = $line;
        continue;
    }
    
    if (preg_match('/^' . preg_quote($username, '/') . '\s*=/', $line)) {
        $newLines[] = "$username = $password";
        $found = true;
    } else {
        $newLines[] = $line;
    }
}

if (!$found) {
    $newLines[] = "$username = $password";
}

// Write back to file
file_put_contents($passwdFile, implode("\n", $newLines) . "\n");
echo "Password updated for user '$username' in svn:// protocol passwd file\n";
