import logging
import functools
import asyncio
import os
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# DB Import
# Assuming mongo_db_handler.py is in the same directory or importable
import mongo_db_handler as db_handler 
from mongo_db_handler import get_transaction_status_by_identifier


# Bot and group config

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_FALLBACK_TOKEN_HERE")
# Using a placeholder for the target chat ID for demonstration
TARGET_CHAT_ID = -1003227550424 
ATTACHMENTS_DIR = "attachments" # Local folder for attachments

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# init DB
db_handler.init_mongo()

# Utility Functions

def get_follow_up_keyboard():
    """Returns the InlineKeyboardMarkup for follow-up actions."""
    keyboard = [
        [
            InlineKeyboardButton("✍️ Add additional remarks", callback_data="follow_up:add_remarks"),
            InlineKeyboardButton("📎 Add additional attachment", callback_data="follow_up:add_attachment")
        ],
        [
            InlineKeyboardButton("➕ Create a new ticket (New Order ID)", callback_data="follow_up:new_ticket"),
            InlineKeyboardButton("👋 End chat", callback_data="follow_up:end_chat")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_main_menu_keyboard():
    """Returns the main menu buttons for new users."""
    keyboard = [
        [
            InlineKeyboardButton("➕ Create a New Ticket", callback_data="main_menu:start_ticket")
        ],
        [
            InlineKeyboardButton("🔍 Check Order Status", callback_data="main_menu:check_status")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_search_type_keyboard():
    """Returns the InlineKeyboardMarkup for selecting the status lookup identifier type (Step 1 of status check)."""
    keyboard = [
        [
            InlineKeyboardButton("🔍 Search with Order ID", callback_data="search_type:order_id")
        ],
        [
            InlineKeyboardButton("🔍 Search with UTR", callback_data="search_type:utr"),
            InlineKeyboardButton("🔍 Search with Ref ID", callback_data="search_type:ref_id")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_status_error_keyboard(identifier: str, failed_type: str):
    """
    Returns the InlineKeyboardMarkup for status lookup failure, 
    offering to search again (for different types) or create a ticket.
    """
    display_id = identifier if len(identifier) < 30 else f"{identifier[:15]}..." 
    
    keyboard = []
    
    all_types = {"order_id": "Order ID", "utr": "UTR", "ref_id": "Ref ID"}
    
    # Add buttons for the two other search types
    other_search_buttons = []
    for key, value in all_types.items():
        if key != failed_type:
             # Callback data will be "error_menu:search_type:utr" or "error_menu:search_type:ref_id"
            other_search_buttons.append(InlineKeyboardButton(f"🔍 Search with {value}", callback_data=f"error_menu:search_type:{key}"))
    
    if other_search_buttons:
        # If we have two other buttons, group them on two rows
        keyboard.append([other_search_buttons[0]])
        if len(other_search_buttons) > 1:
            keyboard.append([other_search_buttons[1]])

    # Add the "Create Ticket" button
    keyboard.append([
        InlineKeyboardButton(f"➕ Create a Ticket for '{display_id}'", callback_data=f"error_menu:create_ticket_for_{identifier}"),
    ])
    
    return InlineKeyboardMarkup(keyboard)


async def send_follow_up_options(update: Update, context: ContextTypes.DEFAULT_TYPE, initial_text: str):
    """Sends the follow-up buttons to the user."""
    ticket_id = context.user_data.get('ticket_id')
    if not ticket_id:
        await update.message.reply_text("Error: Lost ticket context. Ending session.")
        context.user_data.clear()
        return

    context.user_data['awaiting_additional_remark'] = False 
    context.user_data['awaiting_remarks'] = False 
    context.user_data['in_follow_up_menu'] = True 

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"{initial_text}\n\n**What would you like to do next for Ticket ID: {ticket_id}?**",
        parse_mode="Markdown",
        reply_markup=get_follow_up_keyboard()
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    chat_id = update.message.chat.id
    logger.info(f"/start from {user.first_name} in chat {chat_id}")
    await update.message.reply_text(
        "Hi! This is the support bot. Please select an option:",
        reply_markup=get_main_menu_keyboard()
    )
    context.user_data.clear()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles messages in the target group.
    DMs the user with the new main menu buttons.
    """
    if not update.message: return
    chat_type = update.message.chat.type
    chat_id = update.message.chat.id
    if chat_type not in ("group", "supergroup", "channel"): return
    if not update.message.from_user: return

    user = update.message.from_user
    if chat_id == TARGET_CHAT_ID:
        logger.info("Target group message - DMing user %s", user.id)
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(f"Hey {user.first_name}, I noticed your message in the group.\n\n"
                      "How can I help you? Please select an option below:"),
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard() 
            )
            context.user_data.clear()
        except Exception as e:
            logger.warning("Could not DM user %s: %s", user.id, e)
            try:
                await update.message.reply_text(f"Hey {user.first_name}, please start a chat with me first 👉 @{context.bot.username}")
            except Exception:
                logger.exception("Could not notify user in group")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all private text messages, routing based on user_data state.
    """
    if not update.message or not update.message.from_user: return

    user = update.message.from_user
    text = update.message.text or ""
    if update.message.chat.type != "private": return
    if text.startswith('/'):
        if text.startswith("/start"):
            await start(update, context)
        return

    # All possible states
    awaiting_order = context.user_data.get('awaiting_order', False)
    # Replaced awaiting_status_lookup with awaiting_identifier and awaiting_search_type
    awaiting_identifier = context.user_data.get('awaiting_identifier', False) 
    awaiting_remarks = context.user_data.get('awaiting_remarks', False)
    awaiting_additional_remark = context.user_data.get('awaiting_additional_remark', False)

    logger.info("Private message from %s: %r", user.id, text)

    # Step 1: TICKET CREATION 
    if awaiting_order:
        order_id = text.strip()
        if not order_id or len(order_id) > 80:
            await update.message.reply_text("Please reply with a valid **Order ID**.")
            return
        
        context.user_data.clear() # here it clears states
        context.user_data['order_id'] = order_id
        logger.info("User %s provided Order ID: %s", user.id, order_id)

        issue_options = ["Payment issue", "Product issue", "Delivery issue"]
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"issue:{opt}")] for opt in issue_options]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        sent = await update.message.reply_text(
            f"Thanks — I received your Order ID: `{order_id}`.\n\n"
            f"**Please select the issue type:**",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['issue_messsw3age_id'] = sent.message_id
    
    # --- 2: STATUS LOOKUP - Awaiting Identifier Input ---
    elif awaiting_identifier:
        context.user_data['awaiting_identifier'] = False # Consume state
        identifier = text.strip()
        search_type = context.user_data.get('search_type') # Retrieve the search type
        
        if not search_type:
            await update.message.reply_text("Error: Search type was lost. Please start over.", reply_markup=get_main_menu_keyboard())
            context.user_data.clear()
            return
        
        # Prepare for logging and error messages
        search_type_display = search_type.replace('_', ' ').title()

        await update.message.reply_text(f"Searching for `{search_type_display}`: `{identifier}` in the ticket database...", parse_mode="Markdown")
        logger.info("User %s requested status for %s: %s", user.id, search_type, identifier)
        
        loop = asyncio.get_running_loop()
        status_message = ""
        
        try:
            # to check if a ticket exists in db_handler (using the identifier)
            ticket_exists = await loop.run_in_executor(
                None,
                functools.partial(db_handler.check_if_ticket_exists, identifier)
            )

            # THIS IS THE "GATE"
            if ticket_exists:
                await update.message.reply_text("Ticket found. Now checking transaction status...")
                
                # now it calls the new function from mongo_db_handler
                trxn_data = await loop.run_in_executor(
                    None,
                    functools.partial(get_transaction_status_by_identifier, identifier)
                )
                
                if trxn_data:
                    trxn_time_str = trxn_data.get('transaction_time')
                    # Handle if time is datetime obj or str
                    if isinstance(trxn_time_str, datetime):
                        trxn_time_str = trxn_time_str.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        trxn_time_str = str(trxn_time_str) # Just in case

                    status_message = (
                        f"🎉 **Transaction Status Found!** 🎉\n\n"
                        f"**Order ID:** `{trxn_data.get('order_id')}`\n"
                        f"**Ref ID:** `{trxn_data.get('ref_id')}`\n"
                        f"**UTR:** `{trxn_data.get('utr')}`\n"
                        f"**Current Status:** *{str(trxn_data.get('status')).upper()}*\n"
                        f"**Transaction ID:** `{trxn_data.get('transaction_id')}`\n"
                        f"**Timestamp:** {trxn_time_str}"
                    )
                else:
                    status_message = (
                        f"⚠️ **Status Not Found**\n\n"
                        f"We found a ticket for identifier `{identifier}`, but could not find a matching "
                        f"automated transaction. Please add remarks to your ticket if you need help."
                    )
            
            else:
                # If No ticket exists - CUSTOM ERROR MESSAGE AND BUTTONS (Per user request)
                status_message = (
                    f"❌ **Action Required**\n\n"
                    f"To check the status, a support ticket must exist for that order id.\n"
                    f"We could not find any tickets for your order/identifier: `{identifier}`.\n\n"
                    f"Please try searching with a different ID type or create a new ticket."
                )

                await update.message.reply_text(status_message, parse_mode="Markdown")
                await update.message.reply_text(
                    "What would you like to do next?",
                    # Pass the identifier and the type that failed
                    reply_markup=get_status_error_keyboard(identifier, search_type) 
                )
                return # Crucial: Exit the function here after presenting the custom error options

        except Exception:
            logger.exception("Error during gated status lookup")
            status_message = "⚠️ **System Error**\n\nSorry, a database error occurred. Please try again."

        # This part runs if the ticket was found (success/warning) OR if a System Error occurred.
        await update.message.reply_text(status_message, parse_mode="Markdown")
        await update.message.reply_text("What would you like to do next?", reply_markup=get_main_menu_keyboard())


    # 3: TICKET FOLLOW-UP 
    elif awaiting_remarks:
        remarks_text = text.strip()
        ticket_id = context.user_data.get('ticket_id')

        if not remarks_text:
            await update.message.reply_text("Please describe your issue or send a photo/document.")
            return
        if not ticket_id:
            logger.error("Remarks received but no ticket_id in user_data for user %s", user.id)
            await update.message.reply_text("Sorry, it seems like your ticket id was lost, Please go back and create a new ticket for this!")
            await start(update, context) 
            return

        logger.info("Updating ticket %s with initial remarks", ticket_id)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(db_handler.update_ticket_remarks, ticket_id, remarks_text)
            )
            if result:
                await send_follow_up_options(
                    update, context,
                    "Thank you! Your **initial remarks** have been added."
                )
            else:
                await update.message.reply_text("Sorry, couldn't find your ticket to add remarks.")
        except Exception:
            logger.exception("Failed to update ticket initial remarks")
            await update.message.reply_text("Sorry, a database error occurred while adding your remarks.")

    elif awaiting_additional_remark:
        additional_remark = text.strip()
        ticket_id = context.user_data.get('ticket_id')

        if not additional_remark:
            await update.message.reply_text("Please provide the additional remark text.")
            return
        if not ticket_id:
            logger.error("Additional remark received but no ticket_id in user_data for user %s", user.id)
            await update.message.reply_text("Sorry, it seems like your ticket id was lost, Please go back and create a new ticket for this!")
            await start(update, context)
            return

        logger.info("Appending additional remark to ticket %s", ticket_id)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(db_handler.append_ticket_remarks, ticket_id, additional_remark)
            )
            if result:
                await send_follow_up_options(
                    update, context,
                    "✅ Your **additional remark** has been successfully added with a timestamp."
                )
            else:
                await update.message.reply_text("Sorry, couldn't update your ticket with the additional remark.")
        except Exception:
            logger.exception("Failed to update ticket additional remarks")
            await update.message.reply_text("Sorry, a database error occurred while adding your additional remark.")
        
    elif context.user_data.get('in_follow_up_menu'):
        await update.message.reply_text("Please select an option from the menu buttons below to continue.")
        await send_follow_up_options(update, context, "Still waiting for your choice.")
    
    else:
        # Handle any other random text
        await update.message.reply_text(
            "Hi! Please choose an option below to start.",
            reply_markup=get_main_menu_keyboard()
        )


# Attachment Handler

async def handle_incoming_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user or msg.chat.type != "private":
        return

    ticket_id = context.user_data.get('ticket_id')
    awaiting_initial_remarks = context.user_data.get('awaiting_remarks')
    awaiting_additional_attachment = context.user_data.get('awaiting_additional_attachment')

    if not awaiting_initial_remarks and not awaiting_additional_attachment:
        logger.warning("Attachment received but not in a file-expecting state for user %s", msg.from_user.id)
        return await msg.reply_text("I'm not expecting a file right now. Please select 'Add additional attachment' from the menu first.")

    if not ticket_id:
        logger.warning("Attachment received but no ticket_id for user %s", msg.from_user.id)
        return await msg.reply_text("Error: Lost ticket context. Please start a new ticket.")
    
    context.user_data['awaiting_remarks'] = False
    context.user_data['awaiting_additional_attachment'] = False
    context.user_data.pop('in_follow_up_menu', None) 

    file_id = None
    filename = None
    mime = None

    if msg.document:
        doc = msg.document
        file_id = doc.file_id
        filename = os.path.basename(doc.file_name or f"document_{msg.message_id}.file")
        mime = doc.mime_type or "application/octet-stream"
    elif msg.photo:
        photo = msg.photo[-1]     # highest resolution
        file_id = photo.file_id
        filename = f"photo_{msg.message_id}.jpg"
        mime = "image/jpeg"
    else:
        return 

    try:
        tg_file = await context.bot.get_file(file_id)
        file_bytearray = await tg_file.download_as_bytearray()
        file_bytes = bytes(file_bytearray)
    except Exception:
        logger.exception("Failed to download file from Telegram for file_id=%s", file_id)
        await msg.reply_text("Sorry, I couldn't download the file. Try again.")
        if awaiting_initial_remarks:
             context.user_data['awaiting_remarks'] = True
        if awaiting_additional_attachment:
            context.user_data['awaiting_additional_attachment'] = True
        return

    local_path = os.path.join(ATTACHMENTS_DIR, f"{ticket_id}_{filename}")
    try:
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        logger.info("Saved file locally to: %s", local_path)
    except Exception:
        logger.exception("Failed to save file locally to %s", local_path)
        await msg.reply_text("Warning: I could not save the file to the local disk.")

    logger.info("Received file from user %s: filename=%s size=%d bytes",
                msg.from_user.id, filename, len(file_bytes))

    loop = asyncio.get_running_loop()
    try:
        attachment_id = await loop.run_in_executor(
            None,
            functools.partial(db_handler.save_attachment, ticket_id, filename, mime, file_bytes)
        )
        logger.info("Saved attachment id=%s for ticket=%s", attachment_id, ticket_id)

        attachment_type = "initial attachment" if awaiting_initial_remarks else "additional attachment"
        await send_follow_up_options(
            update, context,
            f"✅ Your **{attachment_type}** was successfully added to Ticket ID: {ticket_id}."
        )

    except Exception:
        logger.exception("Error saving attachment to DB for filename=%s", filename)
        await msg.reply_text("Sorry, I couldn't save the file to the database. The admin has been notified.")


# Callback Query Handlers

async def debug_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    logger.info("[DEBUG] Callback data: %r from %s", query.data, query.from_user.id)

async def handle_main_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    
    data = query.data
    action = data.split("main_menu:", 1)[1]
    
    try:
        await query.edit_message_text(
            text=f"You selected: *{action.replace('_', ' ').title()}*",
            parse_mode="Markdown"
        )
    except Exception:
        logger.warning("Failed to edit main menu message.")

    if action == "start_ticket":
        context.user_data.clear() 
        context.user_data['awaiting_order'] = True # <-- Simple ticket creation
        await query.message.reply_text(
            "Please reply with your **Order ID** to create a new ticket."
        )

    elif action == "check_status":
        context.user_data.clear() 
        # New state to prompt the user to choose the search type
        context.user_data['awaiting_search_type'] = True
        await query.message.reply_text(
            "Please select the identifier type you want to check the status for:",
            reply_markup=get_search_type_keyboard()
        )

async def handle_search_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the selection of Order ID, UTR, or Ref ID for status lookup."""
    query = update.callback_query
    if not query: return
    await query.answer()

    data = query.data
    search_type = data.split("search_type:", 1)[1]
    
    try:
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=f"You selected to search by: *{search_type.replace('_', ' ').title()}*.",
            parse_mode="Markdown"
        )
    except Exception:
        logger.warning("Failed to edit search type selection message.")
        
    context.user_data['awaiting_search_type'] = False # Consume state
    context.user_data['search_type'] = search_type # Store the selected type (e.g., 'order_id')
    context.user_data['awaiting_identifier'] = True # Now we wait for the text input
    
    prompt = {
        "order_id": "Please reply with the **Order ID** now.",
        "utr": "Please reply with the **UTR** now.",
        "ref_id": "Please reply with the **Ref ID** now.",
    }.get(search_type, "Please reply with the identifier now.")
    
    await query.message.reply_text(prompt)


async def handle_issue_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    
    data = query.data
    user = query.from_user

    if not data or not data.startswith("issue:"): return

    issue_type = data.split("issue:", 1)[1]
    context.user_data['issue_type'] = issue_type
    logger.info("User %s selected issue %s", user.id, issue_type)

    order_id = context.user_data.get('order_id')
    username = user.username or user.first_name

    if not order_id:
        await query.message.reply_text("Sorry, I'm missing the Order ID. Please start over.")
        await start(update, context) # Reset flow
        return

    loop = asyncio.get_running_loop()
    text_to_send = ""
    try:
        new_ticket_id = await loop.run_in_executor(
            None,
            functools.partial(db_handler.create_ticket, str(user.id), username, order_id, issue_type)
        )
        
        if new_ticket_id:
            context.user_data['ticket_id'] = new_ticket_id
            context.user_data['awaiting_remarks'] = True 
            context.user_data.pop('in_follow_up_menu', None) 
            text_to_send = (
                f"✅ Your ticket has been created! *Ticket ID: {new_ticket_id}*\n\n"
                "**Please briefly describe the issue (remarks) or send any relevant photos/documents now.**"
            )
        else:
            # This handles the duplicate entry case
            text_to_send = (
                "❌ *Ticket Creation Failed*\n\n"
                "A ticket with this Order ID might already exist. "
                "Please try checking your status instead."
            )

    except Exception:
        logger.exception("Failed to create ticket in DB")
        text_to_send = (
            "❌ *Sorry, a database connection error occurred.*\n"
            "Please try again later or contact an admin."
        )
        await query.message.reply_text(text_to_send, parse_mode="Markdown")
        await start(update, context) # Reset flow
        return
        
    try:
        issue_msg_id = context.user_data.get('issue_message_id')
        original_text = (
            f"Thanks — I received your Order ID: `{order_id}`.\n\n"
            f"Selected issue: {issue_type}\n\n"
        )
        
        if issue_msg_id:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=issue_msg_id,
                text=original_text + text_to_send, 
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(original_text + text_to_send, parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to edit/send issue selection response")
        if not issue_msg_id:
             await query.message.reply_text(text_to_send, parse_mode="Markdown")


async def handle_status_error_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles actions from the status lookup failure menu (e.g., search again, create ticket).
    """
    query = update.callback_query
    if not query: return
    await query.answer()
    
    data = query.data
    
    try:
        # Remove the keyboard after selection
        await context.bot.edit_message_reply_markup(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            reply_markup=None
        )
    except Exception:
        logger.warning("Failed to remove inline keyboard from error menu.")
        
    if data.startswith("error_menu:search_type:"):
        # Action: Search with a different ID type (e.g., UTR/Ref ID)
        new_search_type = data.split("error_menu:search_type:", 1)[1]
        
        context.user_data.clear()
        context.user_data['search_type'] = new_search_type
        context.user_data['awaiting_identifier'] = True

        type_display = new_search_type.replace('_', ' ').title()
        
        await query.message.reply_text(
            f"Starting new search. Please reply with the **{type_display}** now."
        )
        
    elif data.startswith("error_menu:create_ticket_for_"):
        # Action: Create a Ticket for the identifier
        identifier = data.split("error_menu:create_ticket_for_", 1)[1]
        
        # Start ticket creation flow by setting order_id and asking for issue type
        context.user_data.clear() 
        context.user_data['order_id'] = identifier
        logger.info("Starting ticket creation for pre-provided Order ID: %s", identifier)

        issue_options = ["Payment issue", "Product issue", "Delivery issue"]
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"issue:{opt}")] for opt in issue_options]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        sent = await query.message.reply_text(
            f"You chose to create a new ticket for Order ID: `{identifier}`.\n\n"
            f"**Please select the issue type:**",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['issue_messsw3age_id'] = sent.message_id


async def handle_follow_up_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    
    data = query.data
    action = data.split("follow_up:", 1)[1]
    ticket_id = context.user_data.get('ticket_id')

    if not ticket_id:
        await query.message.reply_text("Error: Lost ticket context. Please start a new ticket.")
        context.user_data.clear()
        await start(update, context) 
        return

    try:
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=f"Selected option: *{action.replace('_', ' ').title()}* for Ticket ID: `{ticket_id}`",
            parse_mode="Markdown"
        )
    except Exception:
        logger.warning("Failed to edit follow-up message.")


    if action == "add_remarks":
        context.user_data['in_follow_up_menu'] = False
        context.user_data['awaiting_additional_remark'] = True
        await query.message.reply_text(
            "Please send the **additional remark** text you want to add."
        )

    elif action == "add_attachment":
        context.user_data['in_follow_up_menu'] = False
        context.user_data['awaiting_additional_attachment'] = True
        await query.message.reply_text(
            "Please send the **additional screenshot/file** now."
        )

    elif action == "new_ticket":
        context.user_data.clear() 
        await query.message.reply_text(
            "Starting a **new ticket** workflow."
        )
        await start(update, context) # Show main menu

    elif action == "end_chat":
        context.user_data.clear()
        await query.message.reply_text(
            "👋 Thank you for using the support bot. This session is now **terminated**."
        )
        logger.info("Chat ended for user %s, ticket %s", query.from_user.id, ticket_id)
    
    else:
        await query.message.reply_text("Unknown action. Please try again.")

if __name__ == "__main__":
    logger.info("Starting bot")
    
    if not os.path.exists(ATTACHMENTS_DIR):
        os.makedirs(ATTACHMENTS_DIR)
        logger.info(f"Created directory: {ATTACHMENTS_DIR}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL), handle_incoming_attachment))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))
    group_filters = (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP | filters.ChatType.CHANNEL) & ~filters.COMMAND
    app.add_handler(MessageHandler(group_filters, handle_message))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(debug_callback_query), group=-1)
    app.add_handler(CallbackQueryHandler(handle_main_menu_selection, pattern=r"^main_menu:"))
    # New Handler for the search type selection menu
    app.add_handler(CallbackQueryHandler(handle_search_type_selection, pattern=r"^search_type:"))
    app.add_handler(CallbackQueryHandler(handle_issue_selection, pattern=r"^issue:"))
    app.add_handler(CallbackQueryHandler(handle_follow_up_actions, pattern=r"^follow_up:"))
    app.add_handler(CallbackQueryHandler(handle_status_error_menu, pattern=r"^error_menu:")) # Error menu handler

    app.run_polling(drop_pending_updates=True)
