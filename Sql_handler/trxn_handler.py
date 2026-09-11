import logging
from typing import Optional, Dict, Any
from mysql.connector import pooling, Error as MySQLError

# --- Using the structure/pattern from db_handler.py ---
DB_CONFIG = {
    "host": "127.0.0.1",
    "database": "trxn_handler", # Using the database name from your image: trxn_handler
    "user": "root",
    "password": "0805",
}
POOL_NAME = "trxnpool"
POOL_SIZE = 5

_pool: Optional[pooling.MySQLConnectionPool] = None

def init_pool():
    """Initializes the local MySQL connection pool for trxn_handler."""
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name=POOL_NAME,
            pool_size=POOL_SIZE,
            autocommit=True, # Read operations can be autocommitted
            **DB_CONFIG
        )

def get_conn() -> pooling.PooledMySQLConnection:
    """Get a connection from the pool."""
    global _pool
    if _pool is None:
        init_pool()
    return _pool.get_connection()

logger = logging.getLogger(__name__)

# (Assumes trxn_table has order_id, transaction_id, ref_id, status, utr, etc.)

def get_transaction_status_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """
    Searches the trxn_table by order_id, ref_id, or utr and returns the 
    transaction details, including its status.
    
    :param identifier: The value to search for (could be Order ID, Ref ID, or UTR).
    :return: A dictionary containing transaction details or None if not found.
    """
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor(dictionary=True) 
        
        # NOTE: Assuming 'utr' is the column name in trxn_table
        select_sql = """
            SELECT 
                `order_id`, 
                `transaction_id`, 
                `ref_id`, 
                `utr`,
                `status`, 
                `transaction_time`
            FROM 
                trxn_table
            WHERE 
                `order_id` = %s OR 
                `ref_id` = %s OR 
                `utr` = %s
            LIMIT 1
        """
        
        cur.execute(select_sql, (identifier, identifier, identifier))
        result = cur.fetchone()
        
        if result:
            logger.info("Found transaction for identifier: %s. Status: %s", identifier, result.get('status'))
            return result 
        else:
            logger.warning("No transaction found for identifier: %s", identifier)
            return None
            
    except MySQLError as e:
        logger.exception("MySQL error in get_transaction_status_by_identifier: %s", e)
        raise
    except Exception as e:
        logger.exception("Unexpected error in get_transaction_status_by_identifier: %s", e)
        raise
    finally:
        if cur:
            try: cur.close()
            except Exception: logger.exception("Failed to close cursor")
        if conn:
            try: conn.close()
            except Exception: logger.exception("Failed to close connection (return to pool)")