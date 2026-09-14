import logging
import os
import sys
from datetime import datetime
from typing import Optional, Dict, Any

import pymongo
from pymongo import MongoClient
from pymongo.errors import PyMongoError, DuplicateKeyError
from bson.objectid import ObjectId
from bson.binary import Binary
from bson.errors import InvalidId

logger = logging.getLogger(__name__)

# MongoDB Client and Collections
client: Optional[MongoClient] = None
db: Optional[pymongo.database.Database] = None
tickets_collection: Optional[pymongo.collection.Collection] = None
attachments_collection: Optional[pymongo.collection.Collection] = None
counters_collection: Optional[pymongo.collection.Collection] = None
transactions_collection: Optional[pymongo.collection.Collection] = None

# Database Connection

def init_mongo():
    """
    Initializes the global MongoDB client, database, and collections.
    Reads configuration from environment variables.
    """
    global client, db, tickets_collection, attachments_collection, counters_collection, transactions_collection

    MONGO_URI = os.environ.get("MONGO_URI")
    MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME")

    if not MONGO_URI:
        logger.error("MONGO_URI environment variable not set. Exiting.")
        sys.exit(1)
    if not MONGO_DB_NAME:
        logger.error("MONGO_DB_NAME environment variable not set. Exiting.")
        sys.exit(1)

    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) # 5 sec timeout
        # Test connection
        client.admin.command('ping')
        
        db = client[MONGO_DB_NAME]
        tickets_collection = db["tickets"]
        attachments_collection = db["attachments"]
        counters_collection = db["counters"]
        transactions_collection = db["transactions"]
        
        logger.info(f"Successfully connected to MongoDB, database: '{MONGO_DB_NAME}'")
        
        # Create Indexes
        try:
            # For tickets
            tickets_collection.create_index("ticket_id", unique=True)
            tickets_collection.create_index("order_id", unique=True) # Assumes one ticket per order
            tickets_collection.create_index([
                ("order_id", 1),
                ("ref_id", 1),
                ("utr", 1)
            ], name="identifier_lookup")
            
            # For transactions (index the fields we search for)
            transactions_collection.create_index("order_id")
            transactions_collection.create_index("ref_id")
            transactions_collection.create_index("utr")
            
            # A counter if it doesn't exist
            counters_collection.update_one(
                {"_id": "ticket_id_counter"},
                {"$setOnInsert": {"sequence_value": 0}},
                upsert=True
            )
            logger.info("Database indexes and counters verified.")
        except PyMongoError as e:
            logger.warning(f"Error creating indexes (might already exist): {e}")

    except pymongo.errors.ServerSelectionTimeoutError as e:
        logger.exception(f"Failed to connect to MongoDB (timeout): {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Failed to connect to MongoDB: {e}")
        sys.exit(1)

def get_next_ticket_id() -> int:
    """
    Atomically increments and returns a new sequential ticket ID.
    This mimics an SQL auto-incrementing integer.
    """
    if counters_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
    try:
        result = counters_collection.find_one_and_update(
            {"_id": "ticket_id_counter"},
            {"$inc": {"sequence_value": 1}},
            return_document=pymongo.ReturnDocument.AFTER,
            upsert=True # Ensure it creates if missing
        )
        return result["sequence_value"]
    except PyMongoError:
        logger.exception("Failed to get next ticket ID from counters")
        raise

# Ticket Operations

def create_ticket(user_id: str, username: str, order_id: str, issue_type: str) -> Optional[int]:
    """
    Inserts a ticket document and returns the new sequential integer ticket_id.
    Returns None if a ticket with that order_id already exists.
    """
    if tickets_collection is None:
        raise ConnectionError("MongoDB is not initialized.")

    try:
        new_ticket_id = get_next_ticket_id()
        
        ticket_document = {
            "ticket_id": new_ticket_id,
            "user_id": user_id,
            "username": username,
            "order_id": order_id,
            "ref_id": None, 
            "utr": None,    
            "issue_type": issue_type,
            "status": "new",
            "remarks": [],  # as an array of objects
            "attachments": [], # attachment ObjectIDs
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }
        
        tickets_collection.insert_one(ticket_document)
        logger.info(f"create_ticket: committed new ticket_id={new_ticket_id} for user={user_id}")
        return new_ticket_id
        
    except DuplicateKeyError:
        logger.warning(f"create_ticket: Duplicate entry for user {user_id}. Order ID {order_id} already exists.")
        return None  # Return None to indicate duplicate
    except PyMongoError as e:
        logger.exception(f"PyMongo error in create_ticket: {e}")
        raise

def update_ticket_remarks(ticket_id: int, remarks: str) -> bool:
    """
    Sets the *initial* remarks for a ticket.
    """
    if tickets_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
        
    try:
        remark_object = {
            "text": remarks,
            "timestamp": datetime.now(),
            "type": "initial"
        }
        
        result = tickets_collection.update_one(
            {"ticket_id": ticket_id},
            {
                "$set": {
                    "remarks": [remark_object], # Overwrite with a new array
                    "updated_at": datetime.now()
                }
            }
        )
        
        if result.matched_count == 0:
            logger.warning(f"update_ticket_remarks: ticket_id {ticket_id} not found")
            return False
            
        logger.info(f"update_ticket_remarks: committed remarks for ticket {ticket_id}")
        return True
    except PyMongoError as e:
        logger.exception(f"PyMongo error in update_ticket_remarks: {e}")
        raise

def append_ticket_remarks(ticket_id: int, additional_remarks: str) -> bool:
    """
    Appends new remarks to the 'remarks' array for a given ticket.
    """
    if tickets_collection is None:
        raise ConnectionError("MongoDB is not initialized.")

    try:
        remark_object = {
            "text": additional_remarks,
            "timestamp": datetime.now(),
            "type": "additional"
        }

        result = tickets_collection.update_one(
            {"ticket_id": ticket_id},
            {
                "$push": {"remarks": remark_object},
                "$set": {"updated_at": datetime.now()}
            }
        )

        if result.matched_count == 0:
            logger.warning(f"append_ticket_remarks: ticket_id {ticket_id} not found")
            return False
            
        logger.info(f"append_ticket_remarks: appended remarks for ticket {ticket_id}")
        return True
    except PyMongoError as e:
        logger.exception(f"PyMongo error in append_ticket_remarks: {e}")
        raise

def check_if_ticket_exists(identifier: str) -> bool:
    """
    Checks if at least one ticket exists in the 'tickets' collection 
    for a given Order ID, Ref ID, or UTR.
    """
    if tickets_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
        
    try:
        query = {
            "$or": [
                {"order_id": identifier},
                {"ref_id": identifier},
                {"utr": identifier}
            ]
        }
        
        count = tickets_collection.count_documents(query, limit=1)
        
        if count > 0:
            logger.info(f"check_if_ticket_exists: Found ticket for identifier {identifier}")
            return True
        else:
            logger.info(f"check_if_ticket_exists: No ticket found for identifier {identifier}")
            return False
    except PyMongoError as e:
        logger.exception(f"PyMongo error in check_if_ticket_exists: {e}")
        raise

# Attachment Operations

def save_attachment(ticket_id: int, filename: str, mime_type: str, file_bytes: bytes) -> str:
    """
    Saves attachment data to the 'attachments' collection.
    Links the new attachment's ObjectId to the main ticket document.
    Returns the attachment's _id as a string.
    """
    if attachments_collection is None or tickets_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
        
    try:
        attachment_document = {
            "ticket_id": ticket_id, # Link back to the integer ID
            "filename": filename,
            "mime_type": mime_type,
            "file_data": Binary(file_bytes), # Store as BSON Binary
            "file_size": len(file_bytes),
            "created_at": datetime.now()
        }
        
        result = attachments_collection.insert_one(attachment_document)
        attachment_mongo_id = result.inserted_id
        
        tickets_collection.update_one(
            {"ticket_id": ticket_id},
            {"$push": {"attachments": attachment_mongo_id}}
        )
        
        logger.info(f"save_attachment: committed attachment id={attachment_mongo_id} for ticket={ticket_id}")
        return str(attachment_mongo_id)
        
    except PyMongoError as e:
        logger.exception(f"PyMongo error in save_attachment: {e}")
        raise

def retrieve_attachment(attachment_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves an attachment document from 'attachments' by its _id string.
    """
    if attachments_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
        
    try:
        mongo_id = ObjectId(attachment_id)
        result = attachments_collection.find_one({"_id": mongo_id})
        
        if result:
            logger.info(f"retrieve_attachment: fetched attachment id={attachment_id}")
            return result
        else:
            logger.warning(f"retrieve_attachment: attachment id {attachment_id} not found")
            return None
            
    except InvalidId:
        logger.warning(f"retrieve_attachment: invalid ObjectId format {attachment_id}")
        return None
    except PyMongoError as e:
        logger.exception(f"PyMongo error in retrieve_attachment: {e}")
        raise

# Transaction/Status Lookup Operations (Replaces trxn_handler)

def get_transaction_status_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """
    Finds a transaction in the 'transactions' collection by Order ID, Ref ID, or UTR.
    This replaces the old SQL trxn_handler.
    """
    if transactions_collection is None:
        raise ConnectionError("MongoDB is not initialized.")
        
    try:
        query = {
            "$or": [
                {"order_id": identifier},
                {"ref_id": identifier},
                {"utr": identifier}
            ]
        }
        
        # Find the first document that matches
        transaction_data = transactions_collection.find_one(query)
        
        if transaction_data:
            logger.info(f"Found transaction for identifier: {identifier}")
            return transaction_data
        else:
            logger.info(f"No transaction found for identifier: {identifier}")
            return None
            
    except PyMongoError as e:
        logger.exception(f"PyMongo error in get_transaction_status_by_identifier: {e}")
        raise
