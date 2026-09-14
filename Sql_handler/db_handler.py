import logging
import functools 
import asyncio   
from typing import Optional, Tuple, Dict, Any
from datetime import datetime
import mysql.connector
from mysql.connector import pooling, Error as MySQLError
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": "********",
    "database": "bot",
    "user": "root",
    "password": "****",
}
POOL_NAME = "mypool"
POOL_SIZE = 5

_pool: Optional[pooling.MySQLConnectionPool] = None

# Database Connection and Pool Management
def init_pool():
    """Initializes the global MySQL connection pool."""
    global _pool
    if _pool is None:
        logger.info("Initializing MySQL connection pool")
        
        _pool = pooling.MySQLConnectionPool(
            pool_name=POOL_NAME,
            pool_size=POOL_SIZE,
            autocommit=False,  # We control transactions with commit()/rollback()
            **DB_CONFIG
        )

def get_conn() -> pooling.PooledMySQLConnection:
    """
    Get a connection from the pool.
    """
    if _pool is None:
        init_pool()
    return _pool.get_connection()
# Ticket Operations
# REVERTED to simple create_ticket 
def create_ticket(user_id: str, username: str, order_id: str, issue_type: str) -> Optional[int]:
    """
    Inserts a ticket record (with order_id only) 
    and returns the inserted id (lastrowid).
    
    (Assumes tickets table has order_id, ref_id, utr columns, 
    but ref_id and utr can be NULL)
    """
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        # We only insert the order_id. ref_id and utr will be NULL.
        insert_sql = """
            INSERT INTO tickets (user_id, username, order_id, issue_type, created_at)
            VALUES (%s, %s, %s, %s, NOW())
        """
        cur.execute(insert_sql, (user_id, username, order_id, issue_type))
        ticket_id = cur.lastrowid
        conn.commit()  # Commit the transaction
        logger.info("create_ticket: committed new ticket id=%s for user=%s", ticket_id, user_id)
        return int(ticket_id)
    except MySQLError as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after MySQLError")
        
        if e.errno == 1062: # Duplicate entry
            logger.warning("create_ticket: Duplicate entry for user %s. Order ID %s may already exist.", user_id, order_id)
            return None # Return None to indicate duplicate
            
        logger.exception("MySQL error in create_ticket: %s", e)
        raise
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after unexpected error")
        logger.exception("Unexpected error in create_ticket: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool)")

def update_ticket_remarks(ticket_id: int, remarks: str) -> bool:
    """
    Updates (overwrites) remarks for a ticket. Used for the initial remark.
    """
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        update_sql = "UPDATE tickets SET remarks = %s, updated_at = NOW() WHERE id = %s"
        cur.execute(update_sql, (remarks, ticket_id))
        
        if cur.rowcount == 0:
            conn.rollback()
            logger.warning("update_ticket_remarks: ticket id %s not found", ticket_id)
            return False
            
        conn.commit()
        logger.info("update_ticket_remarks: committed remarks for ticket %s", ticket_id)
        return True
    except MySQLError as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after MySQLError")
        logger.exception("MySQL error in update_ticket_remarks: %s", e)
        raise
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after unexpected error")
        logger.exception("Unexpected error in update_ticket_remarks: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool)")


def append_ticket_remarks(ticket_id: int, additional_remarks: str) -> bool:
    """
    Appends remarks to an existing ticket with a timestamp.
    """
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        select_sql = "SELECT remarks FROM tickets WHERE id = %s FOR UPDATE"
        cur.execute(select_sql, (ticket_id,))
        result = cur.fetchone()

        if not result:
            conn.rollback()
            logger.warning("append_ticket_remarks: ticket id %s not found", ticket_id)
            return False

        old_remarks = result[0] if result[0] else ""
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_remarks_content = (
            f"{old_remarks}\n\n"
            f"--- Added on {timestamp} ---\n"
            f"{additional_remarks}"
        ).strip()

        update_sql = "UPDATE tickets SET remarks = %s, updated_at = NOW() WHERE id = %s"
        cur.execute(update_sql, (new_remarks_content, ticket_id))

        if cur.rowcount == 0:
            conn.rollback()
            return False
            
        conn.commit()
        logger.info("append_ticket_remarks: appended remarks for ticket %s", ticket_id)
        return True
    except MySQLError as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after MySQLError")
        logger.exception("MySQL error in append_ticket_remarks: %s", e)
        raise
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after unexpected error")
        logger.exception("Unexpected error in append_ticket_remarks: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool)")


# KEPT ADVANCED check_if_ticket_exists
def check_if_ticket_exists(identifier: str) -> bool:
    """
    Checks if at least one ticket exists for a given Order ID, Ref ID, or UTR.
    (Assumes tickets table has order_id, ref_id, and utr columns)
    """
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        # Check against all three columns
        select_sql = """
            SELECT EXISTS(
                SELECT 1 FROM tickets 
                WHERE order_id = %s OR ref_id = %s OR utr = %s
            )
        """
        cur.execute(select_sql, (identifier, identifier, identifier))
        result = cur.fetchone()
        
        if result and result[0] == 1:
            logger.info("check_if_ticket_exists: Found ticket for identifier %s", identifier)
            return True
        else:
            logger.info("check_if_ticket_exists: No ticket found for identifier %s", identifier)
            return False
    except MySQLError as e:
        logger.exception("MySQL error in check_if_ticket_exists: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error in check_if_ticket_exists: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool)")

# Attachment Operations
def save_attachment(ticket_id: int, filename: str, mime_type: str, file_bytes: bytes) -> int:
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        insert_sql = """
            INSERT INTO attachments (ticket_id, filename, mime_type, file_data, file_size, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """
        cur.execute(insert_sql, (ticket_id, filename, mime_type, file_bytes, len(file_bytes)))
        attachment_id = cur.lastrowid
        conn.commit()
        logger.info("save_attachment: committed attachment id=%s for ticket=%s", attachment_id, ticket_id)
        return int(attachment_id)
    except MySQLError as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after MySQLError in save_attachment")
        logger.exception("MySQL error in save_attachment: %s", e)
        raise
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: logger.exception("Failed to rollback after unexpected error in save_attachment")
        logger.exception("Unexpected error in save_attachment: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor in save_attachment")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool) in save_attachment")


def retrieve_attachment(attachment_id: int) -> Optional[Dict[str, Any]]:
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True) 
        select_sql = """
            SELECT filename, mime_type, file_data 
            FROM attachments 
            WHERE id = %s
        """
        cur.execute(select_sql, (attachment_id,))
        result = cur.fetchone()
        
        if result:
            logger.info("retrieve_attachment: fetched attachment id=%s", attachment_id)
            return result
        else:
            logger.warning("retrieve_attachment: attachment id %s not found", attachment_id)
            return None
    except MySQLError as e:
        logger.exception("MySQL error in retrieve_attachment: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error in retrieve_attachment: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor in retrieve_attachment")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool) in retrieve_attachment")
