<?php
/**
 * Model for users table
 *
 * @author Team USVN <contact@usvn.info>
 * @link http://www.usvn.info
 * @license http://www.cecill.info/licences/Licence_CeCILL_V2-en.txt CeCILL V2
 * @copyright Copyright 2007, Team USVN
 * @since 0.5
 * @package USVN_Db
 * @subpackage Table
 *
 * This software has been written at EPITECH <http://www.epitech.net>
 * EPITECH, European Institute of Technology, Paris - FRANCE -
 * This project has been realised as part of
 * end of studies project.
 *
 * $Id: Users.php 1398 2007-12-11 18:33:50Z duponc_j $
 */

/**
 * Model for users table
 *
 * Extends USVN_Db_Table for magic configuration and methods
 *
 */
class USVN_Db_Table_Users extends USVN_Db_Table {
	/**
	 * The primary key column (underscore format).
	 *
	 * Without our prefixe...
	 *
	 * @var string
	 */
	protected $_primary = "users_id";

	/**
	 * The field's prefix for this table.
	 *
	 * @var string
	 */
	protected $_fieldPrefix = "users_";

	/**
	 * The table name derived from the class name (underscore format).
	 *
	 * @var array
	 */
	protected $_name = "users";

	/**
	 * Name of the Row object to instantiate when needed.
	 *
	 * @var string
	 */
	protected $_rowClass = "USVN_Db_Table_Row_User";

	/**
	 * Simple array of class names of tables that are "children" of the current
	 * table, in other words tables that contain a foreign key to this one.
	 * Array elements are not table names; they are class names of classes that
	 * extend Zend_Db_Table_Abstract.
	 *
	 * @var array
	 */
	protected $_dependentTables = array("USVN_Db_Table_UsersToGroups");

	/**
	 * Inserts a new row
	 *
	 * @param array Column-value pairs.
	 * @return integer The last insert ID.
	 */
	public function insert(array $data)
	{
		$user = $this->fetchRow(array("users_login = ?" => $data['users_login']));
		if ($user !== null) {
			throw new USVN_Exception(sprintf(T_("Login %s already exist."), $user->login));
		}
		if (!isset($data['users_is_admin'])) {
			$data['users_is_admin'] = false;
		}
		$data['users_secret_id'] = md5(time().mt_rand());
		
		// Store plain text password before row encryption happens
		$plainPassword = isset($data['users_password']) ? $data['users_password'] : null;
		$username = $data['users_login'];
		
		$res = parent::insert($data);
		
		// Pass plain password to updateHtpasswd
		if ($plainPassword !== null) {
			$this->updateHtpasswd(array($username => $plainPassword));
		} else {
			$this->updateHtpasswd();
		}
		return $res;
	}

	/**
	 * Delete existing rows.
	 *
	 * @param string An SQL WHERE clause.
	 * @return the number of rows deleted.
	 */
	public function delete($where)
	{
		$res = parent::delete($where);
		$this->updateHtpasswd();
		return $res;
	}

	/**
	 * Updates existing rows.
	 *
	 * @param array Column-value pairs.
	 * @param string An SQL WHERE clause.
	 * @return int The number of rows updated.
	 */
	public function update(array $data, $where)
	{
		// Store plain text password if being updated
		$plainPassword = isset($data['users_password']) ? $data['users_password'] : null;
		$username = isset($data['users_login']) ? $data['users_login'] : null;
		
		// If no username in data, try to extract from where clause
		if ($username === null && is_array($where)) {
			foreach ($where as $key => $value) {
				if (strpos($key, 'users_login') !== false) {
					$username = $value;
					break;
				}
			}
		}
		
		$res = parent::update($data, $where);
		
		// Pass plain password to updateHtpasswd if available
		if ($plainPassword !== null && $username !== null) {
			$this->updateHtpasswd(array($username => $plainPassword));
		} else {
			$this->updateHtpasswd();
		}
		return $res;
	}

	/**
	 * Update Htpasswd file after an insert, an delete or an update
	 */
	public function updateHtpasswd($plainPasswords = array())
	{
		$text = null;
		foreach ($this->fetchAll(null, "users_login") as $user) {
			$text .= "{$user->login}:{$user->password}\n";
		}
		$config = Zend_Registry::get('config');
		if (@file_put_contents($config->subversion->passwd, $text) === false) {
			throw new USVN_Exception(T_('Can\'t create or write on htpasswd file %s.'), $config->subversion->passwd);
		}
		
		// Also update plain text passwd file for svnserve
		$passwdFile = dirname($config->subversion->passwd) . '/passwd';
		$existingPasswords = array();
		
		// Load existing passwords to preserve them
		if (file_exists($passwdFile)) {
			$lines = file($passwdFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
			foreach ($lines as $line) {
				if (strpos($line, '=') !== false) {
					list($username, $password) = explode('=', trim($line), 2);
					$existingPasswords[trim($username)] = trim($password);
				}
			}
		}
		
		// Merge in any new plain text passwords passed to this method
		foreach ($plainPasswords as $username => $password) {
			$existingPasswords[$username] = $password;
		}
		
		// Write passwd file
		$passwdContent = "[users]\n";
		foreach ($this->fetchAll(null, "users_login") as $user) {
			if (isset($existingPasswords[$user->login])) {
				$password = $existingPasswords[$user->login];
			} else {
				// New user without plain password set yet
				$password = "CHANGE_ME_" . $user->login;
			}
			$passwdContent .= $user->login . " = " . $password . "\n";
		}
		
		@file_put_contents($passwdFile, $passwdContent);
	}

	/**
	 * To know if the user already exists or not
	 *
	 * @param string
	 * @return boolean
	 */
	public function isAUser($login)
	{
		$user = $this->fetchRow(array('users_login = ?' => $login));
		if ($user === NULL) {
			return false;
		}
		return true;
	}


	/**
	 * Return the user by his name
	 *
	 * @param string $name
	 * @return USVN_Db_Table_Row
	 */
	public function findByName($name)
	{
		$db = $this->getAdapter();
		/* @var $db Zend_Db_Adapter */
		$where = $db->quoteInto("users_login = ?", $name);
		return $this->fetchRow($where, "users_login");
	}


	/**
	 * Return the user by his secret
	 *
	 * @param string $secret
	 * @return USVN_Db_Table_Row
	 */
	public function findBySecret($secret)
	{
		$db = $this->getAdapter();
		/* @var $db Zend_Db_Adapter */
		$where = $db->quoteInto("users_secret_id = ?", $secret);
		return $this->fetchRow($where, "users_secret_id");
	}


	/**
	 * Return all users like login
	 *
	 * @param string
	 * @return USVN_Db_Table_Row
	 */
	public function allUsersLike($match_login)
	{
		$db = $this->getAdapter();
		/* @var $db Zend_Db_Adapter_Pdo_Mysql */
		$where = $db->quoteInto("users_login like ?", $match_login."%");
		//$where .= $db->quoteInto(" and users_login != ?", $match_login);
		return $this->fetchAll($where, "users_login");
	}

	/**
	 * Return all no leaders like login in group
	 *
	 * @param string
	 * @param string
	 * @return USVN_Db_Table_Row
	 */
	public function allLeader($group_id)
	{
		//$db = $this->getAdapter();
		/* @var $db Zend_Db_Adapter_Pdo_Mysql */
		//$where = $db->quoteInto("users_login like ?", $match_login."%");
		//$where .= $db->quoteInto(" and users_login != ?", $match_login); where users_id groups_id is_leader
		//return $this->fetchAll($where, "users_login");


		// selection tool
		$select = $this->_db->select();

		// the FROM clause
		$select->from($this->_name, $this->_getCols());

		// the JOIN clause
		$users = self::$prefix . "users";
		$users_to_groups = self::$prefix . "users_to_groups";
		$select->joinLeft($users_to_groups, "$users_to_groups.users_id = $users.users_id",  array());
		//$select->joinLeft($users, "$users.users_id = $users_to_groups.users_id");

		$select->where("$users_to_groups.is_leader = 1 and $users_to_groups.groups_id = ?", $group_id);

		//usvn_users_to_groups.is_leader
        // return the results
        $stmt = $this->_db->query($select);
        $data = $stmt->fetchAll(Zend_Db::FETCH_ASSOC);

		$data  = array(
			'table'    => $this,
			'data'     => $data,
			'rowClass' => $this->_rowClass,
			'stored'   => true
		);

		Zend_Loader::loadClass($this->_rowsetClass);
		return new $this->_rowsetClass($data);
	}

	/**
	 * Return all users like login which aren't in group
	 *
	 * @param string
	 * @return USVN_Db_Table_Row
	 */
	public function allUsersInGroup($groups_id)
	{
//		$db = $this->getAdapter();
		/* @var $db Zend_Db_Adapter_Pdo_Mysql */
//		$where = $db->quoteInto("users_login like ? and ", $match_login."%");
		//$where .= $db->quoteInto(" and users_login != ?", $match_login);
//		return $this->fetchAll($where, "users_login");

		// selection tool
		$select = $this->_db->select();

		// the FROM clause
		$select->from($this->_name, $this->_getCols());

		// the JOIN clause
		$users = self::$prefix . "users";
		$users_to_groups = self::$prefix . "users_to_groups";
		$select->joinLeft($users_to_groups, "$users_to_groups.users_id = $users.users_id",  array());
		//$select->joinLeft($users, "$users.users_id = $users_to_groups.users_id");

		$select->where("$users_to_groups.groups_id = ?", $groups_id);

		//usvn_users_to_groups.is_leader
        // return the results
        $stmt = $this->_db->query($select);
        $data = $stmt->fetchAll(Zend_Db::FETCH_ASSOC);

		$data  = array(
			'table'    => $this,
			'data'     => $data,
			'rowClass' => $this->_rowClass,
			'stored'   => true
		);

		Zend_Loader::loadClass($this->_rowsetClass);
		return new $this->_rowsetClass($data);
	}
}
