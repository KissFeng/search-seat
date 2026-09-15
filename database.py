from contextlib import contextmanager
import queue
import threading

import pymysql
from pymysql.cursors import DictCursor

import config


class ConnectionPool:
    def __init__(self, max_connections: int = 20, timeout: float = 10.0):
        self.max_connections = max_connections
        self.timeout = timeout
        self._pool = queue.Queue(maxsize=max_connections)
        self._created = 0
        self._lock = threading.Lock()

    def _create_connection(self):
        return pymysql.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            database=config.DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
        )

    def get_connection(self):
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self.max_connections:
                    self._created += 1
                    return self._create_connection()
            conn = self._pool.get(timeout=self.timeout)

        try:
            conn.ping()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._create_connection()

        return conn

    def release_connection(self, conn):
        if conn is None:
            return
        try:
            if not self._pool.full():
                self._pool.put_nowait(conn)
            else:
                conn.close()
                with self._lock:
                    self._created -= 1
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created -= 1

    def close_all(self):
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except queue.Empty:
                break
        with self._lock:
            self._created = 0


_POOL = None
_POOL_LOCK = threading.Lock()


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ConnectionPool(max_connections=25, timeout=10.0)
    return _POOL


def server_connection():
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )


@contextmanager
def connection():
    pool = get_pool()
    conn = pool.get_connection()
    try:
        yield conn
    finally:
        pool.release_connection(conn)


def init_db() -> None:
    with server_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )

    with connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    username VARCHAR(64) NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at DATETIME NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_users_username (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            ensure_column(cursor, "users", "disabled_at", "DATETIME NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chaoxing_sessions (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    cx_account VARCHAR(128) NOT NULL,
                    cookies_json MEDIUMTEXT NOT NULL,
                    session_valid TINYINT(1) NOT NULL DEFAULT 1,
                    last_error TEXT NULL,
                    cookies_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_chaoxing_sessions_user (user_id),
                    CONSTRAINT fk_chaoxing_sessions_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            ensure_column(cursor, "chaoxing_sessions", "cx_user_name", "VARCHAR(128) NULL")
            ensure_column(cursor, "chaoxing_sessions", "curriculum_synced_at", "DATETIME NULL")
            ensure_column(cursor, "chaoxing_sessions", "curriculum_sync_error", "TEXT NULL")
            ensure_column(cursor, "chaoxing_sessions", "current_reserves_json", "MEDIUMTEXT NULL")
            ensure_column(cursor, "chaoxing_sessions", "reserves_updated_at", "DATETIME NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT UNSIGNED NOT NULL,
                    default_webhook_url TEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id),
                    CONSTRAINT fk_user_settings_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS seat_query_history (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    room_id VARCHAR(64) NOT NULL,
                    fid_enc VARCHAR(128) NOT NULL,
                    day DATE NOT NULL,
                    start_time VARCHAR(5) NOT NULL,
                    end_time VARCHAR(5) NOT NULL,
                    available_count INT UNSIGNED NOT NULL,
                    occupied_count INT UNSIGNED NOT NULL,
                    pair_count INT UNSIGNED NOT NULL,
                    result_json MEDIUMTEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_seat_query_history_user_created (user_id, created_at),
                    CONSTRAINT fk_seat_query_history_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            ensure_column(cursor, "seat_query_history", "result_json", "MEDIUMTEXT NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS seat_watch_tasks (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    room_id VARCHAR(64) NOT NULL,
                    fid_enc VARCHAR(128) NOT NULL,
                    day DATE NOT NULL,
                    start_time VARCHAR(5) NOT NULL,
                    end_time VARCHAR(5) NOT NULL,
                    target_seats_json TEXT NOT NULL,
                    ignore_no_power TINYINT(1) NOT NULL DEFAULT 0,
                    ignore_sunny TINYINT(1) NOT NULL DEFAULT 0,
                    webhook_url TEXT NULL,
                    interval_seconds INT UNSIGNED NOT NULL DEFAULT 60,
                    status VARCHAR(20) NOT NULL DEFAULT 'running',
                    matched_seats_json TEXT NULL,
                    reminder_ack_at DATETIME NULL,
                    reminder_action VARCHAR(20) NULL,
                    last_checked_at DATETIME NULL,
                    next_check_at DATETIME NULL,
                    expires_at DATETIME NOT NULL,
                    last_error TEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_seat_watch_tasks_user_created (user_id, created_at),
                    KEY idx_seat_watch_tasks_due (status, next_check_at),
                    CONSTRAINT fk_seat_watch_tasks_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            ensure_column(cursor, "seat_watch_tasks", "reminder_ack_at", "DATETIME NULL")
            ensure_column(cursor, "seat_watch_tasks", "reminder_action", "VARCHAR(20) NULL")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_versions (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    platform VARCHAR(20) NOT NULL,
                    version_code INT UNSIGNED NOT NULL,
                    version_name VARCHAR(64) NOT NULL,
                    apk_filename VARCHAR(255) NOT NULL,
                    apk_size BIGINT UNSIGNED NOT NULL,
                    apk_sha256 CHAR(64) NOT NULL,
                    release_notes TEXT NULL,
                    force_update TINYINT(1) NOT NULL DEFAULT 0,
                    published TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_app_versions_platform_code (platform, version_code),
                    KEY idx_app_versions_latest (platform, published, version_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    content VARCHAR(500) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_chat_messages_created (created_at),
                    KEY idx_chat_messages_user_created (user_id, created_at),
                    CONSTRAINT fk_chat_messages_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS push_devices (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    platform VARCHAR(20) NOT NULL DEFAULT 'android',
                    cid VARCHAR(128) NOT NULL,
                    device_name VARCHAR(255) NULL,
                    sdk_version VARCHAR(64) NULL,
                    app_version_code INT UNSIGNED NULL,
                    app_version_name VARCHAR(64) NULL,
                    notifications_enabled TINYINT(1) NOT NULL DEFAULT 1,
                    enabled TINYINT(1) NOT NULL DEFAULT 1,
                    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_push_devices_cid (cid),
                    KEY idx_push_devices_user_enabled (user_id, enabled, last_seen_at),
                    CONSTRAINT fk_push_devices_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS academic_transcripts (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    student_name VARCHAR(64) NULL,
                    student_no VARCHAR(64) NULL,
                    student_class VARCHAR(128) NULL,
                    student_college VARCHAR(128) NULL,
                    student_major VARCHAR(128) NULL,
                    student_status VARCHAR(64) NULL,
                    pdf_url TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    KEY idx_academic_transcripts_user_created (user_id, created_at),
                    CONSTRAINT fk_academic_transcripts_user
                        FOREIGN KEY (user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )


def ensure_column(cursor, table: str, column: str, definition: str) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (config.DB_NAME, table, column),
    )
    if cursor.fetchone()["count"]:
        return
    cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def fetch_one(sql: str, params=None):
    with connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchone()


def fetch_all(sql: str, params=None):
    with connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.fetchall()


def execute(sql: str, params=None) -> int:
    with connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or ())
            return cursor.lastrowid or cursor.rowcount
