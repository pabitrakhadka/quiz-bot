import json
import asyncio
import logging
import time
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, PollAnswerHandler, CallbackContext,
    CallbackQueryHandler
)
from telegram.error import BadRequest, NetworkError, RetryAfter
from asyncio.exceptions import TimeoutError
import random
import os
import glob
import backoff

# Enable logging to a file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("quiz_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ✅ Replace with your actual bot token
BOT_TOKEN = "7481731907:AAGA8Rmu3eC5QUyhUcl4DuaVaH08qA9O4sE"

# Store user statistics in a JSON file
STATS_FILE = "quiz_stats.json"
FILES_SELECTED = "filesselected.json"
READ_DIR = "question"  # Base directory for questions

# Global variable to track current directory state for each user
user_states = {}

def load_stats():
    """Load user statistics from a JSON file."""
    try:
        with open(STATS_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_stats(stats):
    """Save user statistics to a JSON file."""
    with open(STATS_FILE, "w") as file:
        json.dump(stats, file, indent=4)

def clear_stats():
    """Clear all data in the quiz statistics file."""
    with open(STATS_FILE, "w") as file:
        json.dump({}, file)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message with directory browsing options."""
    user_id = str(update.effective_user.id)
    
    # Reset user state
    user_states[user_id] = {"current_path": READ_DIR, "selected_files": []}
    
    # Show available directories
    await show_directory(update, context, READ_DIR)

async def show_directory(update: Update, context: ContextTypes.DEFAULT_TYPE, directory_path: str):
    """Show directories and JSON files in the given path."""
    try:
        if not os.path.exists(directory_path):
            await update.message.reply_text("❌ Directory not found.")
            return

        items = os.listdir(directory_path)
        directories = []
        json_files = []
        
        for item in items:
            item_path = os.path.join(directory_path, item)
            if os.path.isdir(item_path):
                directories.append(item)
            elif item.endswith('.json'):
                json_files.append(item)
        
        # Sort alphabetically
        directories.sort()
        json_files.sort()
        
        keyboard = []
        
        # Add directory buttons
        for dir_name in directories:
            keyboard.append([InlineKeyboardButton(f"📁 {dir_name}", callback_data=f"dir_{dir_name}")])
        
        # Add JSON file buttons
        for file_name in json_files:
            keyboard.append([InlineKeyboardButton(f"📄 {file_name}", callback_data=f"file_{file_name}")])

        # Add navigation and action buttons
        if directory_path != READ_DIR:
            parent_dir = os.path.dirname(directory_path)
            keyboard.append([InlineKeyboardButton("⬆️ Back", callback_data=f"dir_{os.path.basename(parent_dir)}")])

        if json_files:
            keyboard.append([InlineKeyboardButton("✅ Start Quiz with All Files", callback_data="start_quiz")])

        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        current_dir_display = directory_path.replace(READ_DIR, "Root")
        message_text = f"📂 Current Directory: `{current_dir_display}`\n\n"
        message_text += "📁 **Folders:**\n" + "\n".join([f"• {d}" for d in directories]) + "\n\n"
        message_text += "📄 **JSON Files:**\n" + "\n".join([f"• {f}" for f in json_files])

        if update.callback_query:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode="Markdown")
            
    except Exception as e:
        logger.error(f"Error showing directory: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Error accessing directory.")
        else:
            await update.message.reply_text("❌ Error accessing directory.")

async def handle_directory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle directory navigation callbacks."""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    
    # Initialize user state if not exists
    if user_id not in user_states:
        user_states[user_id] = {"current_path": READ_DIR, "selected_files": []}
    
    current_state = user_states[user_id]
    current_path = current_state["current_path"]
    
    if query.data == "refresh":
        await show_directory(update, context, current_path)
        return

    elif query.data == "start_quiz":
        # Select all JSON files in the current directory for quiz
        json_files = [os.path.join(current_path, f) for f in os.listdir(current_path) if f.endswith('.json')]
        if not json_files:
            await query.edit_message_text("❌ No JSON files found in this directory.")
            return
        current_state["selected_files"] = json_files
        await start_quiz_from_files(update, context, current_state["selected_files"])
        return

    elif query.data.startswith("dir_"):
        dir_name = query.data[4:]
        if dir_name == "..":
            new_path = os.path.dirname(current_path)
        else:
            new_path = os.path.join(current_path, dir_name)
        
        # Update user state
        current_state["current_path"] = new_path
        await show_directory(update, context, new_path)
    
    elif query.data.startswith("file_"):
        file_name = query.data[5:]
        file_path = os.path.join(current_path, file_name)
        
        # Toggle file selection
        if file_path in current_state["selected_files"]:
            current_state["selected_files"].remove(file_path)
            await query.answer(f"❌ Removed {file_name} from selection")
        else:
            current_state["selected_files"].append(file_path)
            await query.answer(f"✅ Added {file_name} to selection")
        
        # Show directory again with updated selection
        await show_directory(update, context, current_path)

async def start_quiz_from_files(update: Update, context: ContextTypes.DEFAULT_TYPE, selected_files: list):
    """Start quiz with selected JSON files."""
    if not selected_files:
        await update.callback_query.edit_message_text("❌ No files selected.")
        return
    
    try:
        # Create question set from selected files
        create_question_set(selected_files, output_file="set.json", questions_per_file=5)
        
        await update.callback_query.edit_message_text(
            f"✅ Quiz created with {len(selected_files)} files!\n"
            f"Use /quiz to start the quiz or /share to invite others!"
        )
        
    except Exception as e:
        logger.error(f"Error creating quiz from files: {e}")
        await update.callback_query.edit_message_text("❌ Error creating quiz.")

def is_valid_question(question):
    """Check if a question has valid options and required fields for the new format."""
    try:
        if not all(key in question for key in ["question", "options"]):
            logger.warning(f"Missing required fields in question: {question.get('question', 'NO_QUESTION')}")
            return False
        
        if not question["question"].strip():
            logger.warning("Empty question text found")
            return False

        options = question.get("options", [])
        
        if not isinstance(options, list) or len(options) != 4:
            logger.warning(f"Question must have exactly 4 options, found {len(options)}")
            return False

        correct_options_count = 0
        for idx, opt in enumerate(options):
            if not isinstance(opt, dict) or "text" not in opt or "isCorrect" not in opt:
                logger.warning(f"Option {idx + 1} missing required fields (text or isCorrect)")
                return False
            
            option_text = opt.get("text", "")
            if len(option_text) > 98:
                logger.warning(f"Option {idx + 1} too long: {len(option_text)} chars")
                return False
            if not option_text.strip():
                logger.warning(f"Empty option {idx + 1} found")
                return False
            
            if opt.get("isCorrect") is True:
                correct_options_count += 1

        if correct_options_count != 1:
            logger.warning(f"Question must have exactly one correct option, found {correct_options_count}")
            return False

        total_length = sum(len(opt.get("text", "")) for opt in options)
        if total_length > 100:
            logger.warning(f"Total options length ({total_length}) exceeds 100 chars")
            return False

        return True
    except Exception as e:
        logger.error(f"Error validating question: {str(e)}")
        return False

def get_selected_ids(filename):
    """Retrieve IDs of already selected questions."""
    selected_file = f"{filename}.selected.json"
    if os.path.exists(selected_file):
        with open(selected_file, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_selected_ids(filename, ids):
    """Save IDs of selected questions."""
    selected_file = f"{filename}.selected.json"
    with open(selected_file, "w", encoding="utf-8") as f:
        json.dump(list(ids), f, ensure_ascii=False, indent=2)

def load_and_select_questions(filename, questions_per_file=5, global_used_ids=None):
    """Load and select valid questions from a file, excluding globally used questions."""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            questions = json.load(f)
            if not isinstance(questions, list):
                logger.warning(f"{filename} does not contain a list of questions.")
                return []

            valid_questions = [q for q in questions if is_valid_question(q)]

            if not valid_questions:
                logger.warning(f"No valid questions in {filename}")
                return []

            for idx, q in enumerate(valid_questions):
                q.setdefault("id", str(idx))

            if global_used_ids is not None:
                unused_questions = [q for q in valid_questions if q["id"] not in global_used_ids]
            else:
                unused_questions = valid_questions.copy()

            if len(unused_questions) < questions_per_file:
                logger.info(f"All questions used in {filename}. Resetting selection.")
                if global_used_ids is not None:
                    unused_questions = valid_questions.copy()
                    for q in valid_questions:
                        global_used_ids.discard(q["id"])
                else:
                    unused_questions = valid_questions.copy()

            selected = random.sample(unused_questions, min(questions_per_file, len(unused_questions)))

            if global_used_ids is not None:
                for q in selected:
                    global_used_ids.add(q["id"])

            return selected

    except Exception as e:
        logger.error(f"Error reading {filename}: {str(e)}")
        return []

def get_file_key_from_path(path):
    """Return the filename (without extension) as the key."""
    return os.path.splitext(os.path.basename(path))[0]

def create_question_set(files_list, output_file="set.json", questions_per_file=5):
    """
    Create a question set from multiple files.
    """
    if os.path.exists(FILES_SELECTED):
        with open(FILES_SELECTED, "r", encoding="utf-8") as f:
            try:
                selected_data = json.load(f)
            except Exception:
                selected_data = []
    else:
        selected_data = []

    def get_selected_for_file(selected_data, file_name):
        for entry in selected_data:
            if file_name in entry:
                return set(entry[file_name])
        return set()

    def update_selected_for_file(selected_data, file_name, new_ids):
        found = False
        for entry in selected_data:
            if file_name in entry:
                entry[file_name].extend([i for i in new_ids if i not in entry[file_name]])
                found = True
                break
        if not found:
            selected_data.append({file_name: list(new_ids)})
        return selected_data

    final_questions = []

    for filename in files_list:
        file_name = os.path.basename(filename)
        try:
            with open(filename, "r", encoding="utf-8") as f:
                questions = json.load(f)
                valid_questions = [q for q in questions if is_valid_question(q)]
                if not valid_questions:
                    continue

                selected_indexes = get_selected_for_file(selected_data, file_name)
                available = [(idx, q) for idx, q in enumerate(valid_questions) if str(idx) not in selected_indexes]

                if len(available) < questions_per_file:
                    selected_indexes = set()
                    available = [(idx, q) for idx, q in enumerate(valid_questions)]

                chosen = random.sample(available, min(questions_per_file, len(available)))
                chosen_indexes = []
                for idx, q in chosen:
                    q['index'] = str(idx)
                    q['source'] = file_name
                    final_questions.append(q)
                    chosen_indexes.append(str(idx))

                selected_data = update_selected_for_file(selected_data, file_name, chosen_indexes)
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(final_questions, f, indent=3, ensure_ascii=False)
    logger.info(f"Created {output_file} with {len(final_questions)} questions.")

    with open(FILES_SELECTED, "w", encoding="utf-8") as f:
        json.dump(selected_data, f, indent=2, ensure_ascii=False)

    return True

def get_json_files_from_directory(directory):
    """Return list of .json file paths from a given directory."""
    # Recursively get all .json files from all subfolders
    return [f for f in glob.glob(os.path.join(directory, '**', '*.json'), recursive=True)]

async def load_quiz_data(questions_per_file=5):
    """
    Load quiz questions directly from set.json.
    """
    try:
        with open("set.json", "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        raise Exception("❌ Error: set.json file not found.")
    except json.JSONDecodeError:
        raise Exception("❌ Error: Invalid JSON format in set.json.")

last_quiz_end_time = {}

async def send_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the quiz, clear previous data, and send the first question."""
    chat_id = str(update.effective_chat.id)

    current_time = time.time()
    if chat_id in last_quiz_end_time and current_time - last_quiz_end_time[chat_id] < 60:
        await update.message.reply_text("⚠ The quiz just ended. Please wait at least 1 minute before starting again.")
        return

    clear_stats()
    user_stats = load_stats()
    user_stats[chat_id] = {"quiz_index": 0, "participants": {}}
    save_stats(user_stats)

    try:
        quiz_data = await load_quiz_data()
    except Exception as e:
        await update.message.reply_text(f"❌ Error loading quiz data: {str(e)}")
        return

    if not quiz_data:
        await update.message.reply_text("⚠ No quiz questions found.")
        return

    await send_next_question(update, context, chat_id, quiz_data)

@backoff.on_exception(
    backoff.expo,
    (NetworkError, TimeoutError),
    max_tries=3,
    max_time=60
)
async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str, quiz_data: list):
    """Send the next quiz question based on quiz index."""
    user_stats = load_stats()
    quiz_index = user_stats[chat_id].get("quiz_index", 0)

    if quiz_index >= len(quiz_data):
        last_quiz_end_time[chat_id] = time.time()
        await show_quiz_report(update, context)
        try:
            await update.effective_chat.send_message("Quiz ended!")
            await show_quiz_report(update, context)
        except Exception as e:
            logger.warning(f"Could not send automatic report: {e}")
        return

    question_data = quiz_data[quiz_index]
    question = question_data.get("question", "")
    options_data = question_data.get("options", [])
    
    options = [opt.get("text", "") for opt in options_data]
    
    correct_option_id = None
    for idx, opt in enumerate(options_data):
        if opt.get("isCorrect") is True:
            correct_option_id = idx
            break
    
    if correct_option_id is None:
        logger.error(f"No correct option found for question {quiz_index + 1}")
        user_stats[chat_id]["quiz_index"] += 1
        save_stats(user_stats)
        await send_next_question(update, context, chat_id, quiz_data)
        return

    total_options_length = sum(len(option) for option in options)
    if total_options_length > 100:
        logger.warning(f"Skipping question {quiz_index + 1}: Options exceed 100 characters.")
        user_stats[chat_id]["quiz_index"] += 1
        save_stats(user_stats)
        await send_next_question(update, context, chat_id, quiz_data)
        return

    formatted_question = f"📢 Question {quiz_index + 1}/{len(quiz_data)}: {question}"

    try:
        poll_message = await update.effective_chat.send_poll(
            question=formatted_question,
            options=options,
            type=Poll.QUIZ,
            correct_option_id=correct_option_id,
            is_anonymous=False,
            explanation="Think carefully!",
            open_period=20,
            protect_content=True
        )

        if "polls" not in context.bot_data:
            context.bot_data["polls"] = {}

        context.bot_data["polls"][poll_message.poll.id] = {
            "correct_option_id": correct_option_id,
            "chat_id": chat_id
        }

        user_stats[chat_id]["quiz_index"] += 1
        save_stats(user_stats)

        await asyncio.sleep(20)
        await send_next_question(update, context, chat_id, quiz_data)

    except (BadRequest, TimeoutError) as e:
        logger.error(f"Error sending poll: {str(e)}")
        await update.message.reply_text(f"❌ Error sending poll: {str(e)}")
    except Exception as e:
        logger.error(f"Error sending poll: {str(e)}")
        await update.message.reply_text("❌ Failed to send question after multiple retries.")
        return

async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tracks quiz answers and updates user progress in real-time."""
    poll_answer = update.poll_answer
    user_id = str(poll_answer.user.id)
    user_name = poll_answer.user.first_name
    selected_option = poll_answer.option_ids[0] if poll_answer.option_ids else None
    poll_id = poll_answer.poll_id

    if "polls" not in context.bot_data or poll_id not in context.bot_data["polls"]:
        return

    poll_data = context.bot_data["polls"][poll_id]
    chat_id = poll_data["chat_id"]
    correct_option_id = poll_data["correct_option_id"]

    user_stats = load_stats()

    if user_id not in user_stats[chat_id]["participants"]:
        user_stats[chat_id]["participants"][user_id] = {"name": user_name, "correct": 0, "wrong": 0}

    if selected_option == correct_option_id:
        user_stats[chat_id]["participants"][user_id]["correct"] += 1
    else:
        user_stats[chat_id]["participants"][user_id]["wrong"] += 1

    save_stats(user_stats)

import unicodedata

def get_display_width(text):
    """Returns the actual display width of text considering wide characters."""
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width

async def show_quiz_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates and sends the quiz report with correct alignment."""
    user_stats = load_stats()

    if not user_stats:
        await update.message.reply_text("⚠ No quiz data found.")
        return

    report = "📊 **Quiz Report (Sorted by Highest Score)** 📊\n\n"
    participants = {}

    chat_id = str(update.effective_chat.id)
    chat_data = user_stats.get(chat_id, {})
    participants = chat_data.get("participants", {})

    if not participants:
        await update.message.reply_text("⚠ No participants found in quiz. /report")
        return

    scores = []
    for user_id, data in participants.items():
        user_name = data.get("name", "Unknown")
        correct = data.get("correct", 0)
        wrong = data.get("wrong", 0)

        correct_score = correct * 2
        penalty = wrong * 0.4
        final_score = max(correct_score - penalty, 0)

        scores.append((final_score, user_name, correct, wrong))

    scores.sort(reverse=True, key=lambda x: x[0])

    max_name_length = max(get_display_width(user_name) for _, user_name, _, _ in scores)

    name_col_width = max(max_name_length + 2, 12)
    correct_col_width = 8
    wrong_col_width = 8
    score_col_width = 10

    report += f"{'Name':<{name_col_width}} {'Correct':<{correct_col_width}} {'Wrong':<{wrong_col_width}} {'Score':<{score_col_width}}\n"
    report += "-" * (name_col_width + correct_col_width + wrong_col_width + score_col_width + 3) + "\n"

    for final_score, user_name, correct, wrong in scores:
        padded_name = user_name.ljust(name_col_width - (get_display_width(user_name) - len(user_name)))
        report += f"{padded_name} {correct:<{correct_col_width}} {wrong:<{wrong_col_width}} {final_score:<{score_col_width}.2f}\n"

    await update.message.reply_text(f"```\n{report}\n```", parse_mode="Markdown")
    logger.info("Quiz report sent successfully.")

async def share_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and send a shareable link for the quiz."""
    bot_username = (await context.bot.get_me()).username
    share_link = f"https://t.me/{bot_username}?start=quiz"
    
    keyboard = [[InlineKeyboardButton("🔗 Share in Group", url=f"https://t.me/share/url?url={share_link}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(
            "📢 Share this quiz with others or start it now:",
            reply_markup=reply_markup
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "📢 Share this quiz with others or start it now:",
            reply_markup=reply_markup
        )

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the quiz and show the report immediately."""
    chat_id = str(update.effective_chat.id)
    last_quiz_end_time[chat_id] = time.time()
    await show_quiz_report(update, context)

async def create_new_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a fresh quiz set and clear all statistics."""
    try:
        clear_stats()
        
        if os.path.exists("set.json"):
            os.remove("set.json")
            
        mcq_files = get_json_files_from_directory(READ_DIR)
        if not mcq_files:
            await update.message.reply_text(f"❌ No quiz files found in '{READ_DIR}' directory.")
            return
        create_question_set(mcq_files, output_file="set.json", questions_per_file=5)
        
        await update.message.reply_text(
            "✅ New quiz set created successfully!\n"
            "Questions will not repeat until all questions have been used.\n"
            "Use /start or /quiz to begin the quiz."
        )
        
    except Exception as e:
        logger.error(f"Error creating new set: {e}")
        await update.message.reply_text("❌ Error creating new quiz set.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors caused by updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    if isinstance(context.error, NetworkError):
        logger.info("Network error occurred. Retrying...")
    elif isinstance(context.error, RetryAfter):
        logger.info(f"Rate limit hit. Waiting {context.error.retry_after} seconds")
        await asyncio.sleep(context.error.retry_after)
    else:
        logger.error(f"Update {update} caused error {context.error}")

def main():
    # Create base directory if it doesn't exist
    if not os.path.exists(READ_DIR):
        os.makedirs(READ_DIR)
        logger.info(f"Created directory: {READ_DIR}")

    # Generate questions dynamically before starting the bot
    mcq_files = get_json_files_from_directory(READ_DIR)
    if mcq_files:
        create_question_set(mcq_files, output_file="set.json", questions_per_file=5)
    else:
        logger.warning(f"❌ No quiz files found in '{READ_DIR}' directory.")

    # Start the bot with proper connection settings
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connection_pool_size(8)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Register handlers
    for handler in [
        CommandHandler("start", start),
        CommandHandler("quiz", send_quiz),
        CommandHandler("share", share_quiz),
        PollAnswerHandler(handle_quiz_answer),
        CommandHandler("report", show_quiz_report),
        CommandHandler("s", stop_quiz),
        CommandHandler("newset", create_new_set),
        CallbackQueryHandler(handle_directory_callback)
    ]:
        application.add_handler(handler)
    
    # Send startup message after bot is ready
    async def on_startup(app):
        await send_startup_message(app)
    application.post_init = on_startup

    # Start polling with optimized settings
    application.run_polling(
        poll_interval=1.0,
        timeout=30,
        allowed_updates=["message", "poll_answer", "callback_query"]
    )

async def send_startup_message(application):
    """Send a startup message with available commands to the owner."""
    OWNER_CHAT_ID = "5117694320"
    message = (
        "🤖 Quiz Bot Started!\n\n"
        "Available commands:\n"
        "/start - Browse directories and select quiz files\n"
        "/quiz - Start the quiz\n"
        "/share - Share the quiz\n"
        "/newset - Create a new quiz set\n"
        "/report - Show quiz report\n"
        "/s - Stop the quiz and show report\n"
    )
    if OWNER_CHAT_ID:
        try:
            await application.bot.send_message(chat_id=OWNER_CHAT_ID, text=message)
        except Exception as e:
            logger.warning(f"Could not send startup message to owner: {e}")

if __name__ == "__main__":
    main()