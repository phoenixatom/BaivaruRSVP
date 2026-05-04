import asyncio
import html
import json
import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "events.json"
GROUPS_FILE = BASE_DIR / "groups.json"

SELECT_CHAT, DESCRIPTION, START, END, MIN_P, MAX_P, CONFIRM = range(7)

STEPS = [DESCRIPTION, START, END, MIN_P, MAX_P]
STEP_FIELDS = {
    DESCRIPTION: "description",
    START: "start",
    END: "end",
    MIN_P: "min",
    MAX_P: "max",
}
STEP_LABELS = {
    DESCRIPTION: "Description",
    START: "Start",
    END: "End",
    MIN_P: "participantsMin",
    MAX_P: "participantsMax",
}
STEP_HINTS = {
    DESCRIPTION: "What's the event? (e.g. Football this Saturday)",
    START: "When does it start? (e.g. Sat 3 May, 7pm)",
    END: "When does it end? (e.g. Sat 3 May, 9pm)",
    MIN_P: "Minimum to confirm. A number, e.g. 6",
    MAX_P: "Max who can join. A number, e.g. 14",
}

EDIT_THROTTLE = 1.5  # seconds: minimum gap between edits to the same event message
_edit_state: dict[str, dict] = {}  # event_id -> {"last": float, "pending": asyncio.Task | None}


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


SAVE_DEBOUNCE = 0.75  # seconds: collapse a burst of mutations into one write
_save_state: dict[Path, dict] = {}  # path -> {"task": asyncio.Task | None}


async def _flush_save(path: Path, data: dict) -> None:
    state = _save_state[path]
    try:
        await asyncio.sleep(SAVE_DEBOUNCE)
        # Serialize on the loop (fast) so we capture a consistent snapshot,
        # then push the blocking disk write off the event loop.
        text = json.dumps(data, indent=2, ensure_ascii=False)
        await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save %s: %s", path.name, e)
    finally:
        state["task"] = None


def schedule_save(path: Path, data: dict) -> None:
    """Coalesce mutations into a single trailing write per SAVE_DEBOUNCE window."""
    state = _save_state.setdefault(path, {"task": None})
    if state["task"] is not None and not state["task"].done():
        return  # a save is already pending; it'll pick up the latest state
    state["task"] = asyncio.create_task(_flush_save(path, data))


_event_locks: dict[str, asyncio.Lock] = {}


def _event_lock(event_id: str) -> asyncio.Lock:
    lock = _event_locks.get(event_id)
    if lock is None:
        lock = _event_locks[event_id] = asyncio.Lock()
    return lock


events: dict = load_json(DATA_FILE)
groups: dict = load_json(GROUPS_FILE)  # str(chat_id) -> {"id": int, "title": str}


def remember_group(chat) -> None:
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    groups[str(chat.id)] = {"id": chat.id, "title": chat.title or str(chat.id)}
    save_json(GROUPS_FILE, groups)


def display_name(user) -> str:
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += f" {user['last_name']}"
    name = name.strip()
    username = user.get("username")
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    return name or str(user["id"])


def esc(text) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


def render_event_text(event: dict) -> str:
    participants = event["participants"]
    count = len(participants)
    cap = event["max"]
    lines = []
    if event.get("ended"):
        lines.append("🔒 <b>RSVPs CLOSED</b>")
        lines.append("")
    lines.extend(
        [
            f"📅 <b>{esc(event['description'])}</b>",
            "",
            f"🕐 <b>Start:</b> {esc(event['start'])}",
            f"🕓 <b>End:</b> {esc(event['end'])}",
            f"👥 <b>Participants:</b> {count}/{cap} (min {event['min']})",
            "",
        ]
    )
    if participants:
        lines.append("<b>Joined:</b>")
        for i, p in enumerate(participants, start=1):
            lines.append(f"{i}. {esc(display_name(p))}")
    elif event.get("ended"):
        lines.append("<i>No one joined.</i>")
    else:
        lines.append("<i>No participants yet. Be the first to join!</i>")
    return "\n".join(lines)


def render_summary_text(event: dict) -> str:
    participants = event["participants"]
    lines = [
        f"🏁 <b>RSVPs closed:</b> {esc(event['description'])}",
        "",
        f"👥 <b>Final count:</b> {len(participants)}/{event['max']} (min {event['min']})",
        "",
    ]
    if participants:
        lines.append("<b>Final participant list:</b>")
        for i, p in enumerate(participants, start=1):
            lines.append(f"{i}. {esc(display_name(p))}")
    else:
        lines.append("<i>No one joined this event.</i>")
    return "\n".join(lines)


def event_keyboard(event_id: str, full: bool) -> InlineKeyboardMarkup:
    join_label = "🚫 Full" if full else "✅ Join"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(join_label, callback_data=f"join:{event_id}"),
                InlineKeyboardButton("❌ Leave", callback_data=f"leave:{event_id}"),
            ]
        ]
    )


async def track_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.my_chat_member
    if not cmu:
        return
    chat = cmu.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = cmu.new_chat_member.status
    key = str(chat.id)
    if new_status in ("member", "administrator"):
        groups[key] = {"id": chat.id, "title": chat.title or str(chat.id)}
        save_json(GROUPS_FILE, groups)
    elif new_status in ("left", "kicked"):
        groups.pop(key, None)
        save_json(GROUPS_FILE, groups)


async def admin_groups_for(user_id: int, bot) -> list[dict]:
    out = []
    stale = []
    for key, g in list(groups.items()):
        try:
            member = await bot.get_chat_member(g["id"], user_id)
            if member.status in ("creator", "administrator"):
                try:
                    chat = await bot.get_chat(g["id"])
                    if chat.title:
                        g["title"] = chat.title
                        groups[key] = g
                except Exception:
                    pass
                out.append(g)
        except Exception:
            stale.append(key)
    if stale:
        for s in stale:
            groups.pop(s, None)
        save_json(GROUPS_FILE, groups)
    return out


async def is_group_admin(chat_id: int, user_id: int, bot) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! I'm a participant-noting bot.\n\n"
        "• Add me to your group.\n"
        "• Then run /newevent here in DM (or in the group) to create an event."
    )


def render_wizard(data: dict, state: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the wizard message body and keyboard for a given step (or CONFIRM)."""
    target_chat = data.get("chat_id")
    title = groups.get(str(target_chat), {}).get("title") if target_chat else None

    lines = ["📝 <b>New Event</b>"]
    if title:
        lines.append(f"<i>for {esc(title)}</i>")
    lines.append("")

    if state == CONFIRM:
        lines.append("<b>Preview. Ready to post?</b>")
        lines.append("")
        for st in STEPS:
            label = STEP_LABELS[st]
            val = data.get(STEP_FIELDS[st], "")
            lines.append(f"✅ <b>{label}:</b> {esc(val)}")
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Create event", callback_data="wiz:create")],
                [
                    InlineKeyboardButton("⬅️ Back", callback_data="wiz:back"),
                    InlineKeyboardButton("✖️ Cancel", callback_data="wiz:cancel"),
                ],
            ]
        )
        return "\n".join(lines), markup

    idx = STEPS.index(state)
    for st in STEPS:
        label = STEP_LABELS[st]
        st_idx = STEPS.index(st)
        if st_idx < idx:
            val = data.get(STEP_FIELDS[st], "")
            lines.append(f"✅ <b>{label}:</b> {esc(val)}")
        elif st == state:
            hint = STEP_HINTS[st]
            lines.append(f"➡️ <b>{label}</b>: <i>{esc(hint)}</i>")
        else:
            lines.append(f"◽️ {label}")
    lines.append("")
    lines.append(f"<i>Step {idx + 1} of {len(STEPS)}. Reply with your answer.</i>")

    buttons = []
    if idx > 0:
        buttons.append(InlineKeyboardButton("⬅️ Back", callback_data="wiz:back"))
    buttons.append(InlineKeyboardButton("✖️ Cancel", callback_data="wiz:cancel"))
    return "\n".join(lines), InlineKeyboardMarkup([buttons])


async def show_wizard(target_chat_id: int, context: ContextTypes.DEFAULT_TYPE, state: int) -> None:
    """Delete the previous wizard message (if any) and send a fresh one at the bottom of the chat."""
    data = context.user_data["new_event"]
    text, markup = render_wizard(data, state)

    prev = context.user_data.pop("wizard_msg", None)
    if prev:
        try:
            await context.bot.delete_message(prev["chat_id"], prev["message_id"])
        except Exception:
            pass

    sent = await context.bot.send_message(
        chat_id=target_chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=markup,
    )
    context.user_data["wizard_msg"] = {
        "chat_id": sent.chat_id,
        "message_id": sent.message_id,
    }
    context.user_data["wizard_state"] = state


async def _try_delete(message) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def new_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        admin_groups = await admin_groups_for(user.id, context.bot)
        if not admin_groups:
            await update.message.reply_text(
                "I don't know any group where you're an admin.\n\n"
                "Make sure I've been added to your group (and you're an admin there), "
                "then try again. If I was added long ago, run /newevent in the group "
                "once so I can register it."
            )
            return ConversationHandler.END

        keyboard = [
            [InlineKeyboardButton(g["title"], callback_data=f"pickchat:{g['id']}")]
            for g in admin_groups
        ]
        keyboard.append(
            [InlineKeyboardButton("✖️ Cancel", callback_data="pickchat:cancel")]
        )
        context.user_data["new_event"] = {"creator_id": user.id}
        await update.message.reply_text(
            "📝 Which group should this event be posted to?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return SELECT_CHAT

    # In a group
    if not await is_group_admin(chat.id, user.id, context.bot):
        await update.message.reply_text("⛔ Only group admins can create events.")
        return ConversationHandler.END

    remember_group(chat)
    context.user_data["new_event"] = {
        "chat_id": chat.id,
        "creator_id": user.id,
    }
    await _try_delete(update.message)
    await show_wizard(chat.id, context, DESCRIPTION)
    return DESCRIPTION


async def select_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        _, payload = query.data.split(":", 1)
    except ValueError:
        return SELECT_CHAT

    if payload == "cancel":
        await query.edit_message_text("Cancelled.")
        context.user_data.pop("new_event", None)
        return ConversationHandler.END

    try:
        chat_id = int(payload)
    except ValueError:
        return SELECT_CHAT

    if not await is_group_admin(chat_id, query.from_user.id, context.bot):
        await query.edit_message_text("⛔ You're no longer an admin in that group.")
        context.user_data.pop("new_event", None)
        return ConversationHandler.END

    context.user_data["new_event"]["chat_id"] = chat_id

    # Replace the chat-picker with the wizard (in DM)
    try:
        await query.delete_message()
    except Exception:
        pass

    await show_wizard(update.effective_chat.id, context, DESCRIPTION)
    return DESCRIPTION


def _is_creator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = context.user_data.get("new_event")
    return bool(data) and update.effective_user.id == data["creator_id"]


async def _store_text_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    field: str,
    next_state: int,
) -> int:
    if not _is_creator(update, context):
        return context.user_data.get("wizard_state", ConversationHandler.END)
    text = (update.message.text or "").strip()
    if not text:
        return context.user_data.get("wizard_state", ConversationHandler.END)
    context.user_data["new_event"][field] = text
    await _try_delete(update.message)
    await show_wizard(update.effective_chat.id, context, next_state)
    return next_state


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _store_text_step(update, context, "description", START)


async def get_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _store_text_step(update, context, "start", END)


async def get_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _store_text_step(update, context, "end", MIN_P)


async def get_min(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_creator(update, context):
        return MIN_P
    raw = (update.message.text or "").strip()
    try:
        n = int(raw)
        if n < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please send a non-negative integer for min.")
        return MIN_P
    context.user_data["new_event"]["min"] = n
    await _try_delete(update.message)
    await show_wizard(update.effective_chat.id, context, MAX_P)
    return MAX_P


async def get_max(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_creator(update, context):
        return MAX_P
    data = context.user_data["new_event"]
    raw = (update.message.text or "").strip()
    try:
        n = int(raw)
        if n < 1 or n < data.get("min", 0):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"Max must be an integer ≥ 1 and ≥ min ({data.get('min', 0)})."
        )
        return MAX_P
    data["max"] = n
    await _try_delete(update.message)
    await show_wizard(update.effective_chat.id, context, CONFIRM)
    return CONFIRM


async def wizard_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    state = context.user_data.get("wizard_state")
    if state is None:
        return ConversationHandler.END

    if state == CONFIRM:
        target = STEPS[-1]
    else:
        idx = STEPS.index(state)
        if idx == 0:
            return state
        target = STEPS[idx - 1]

    # Clear values from `target` onward so the user re-enters them
    target_idx = STEPS.index(target)
    data = context.user_data["new_event"]
    for st in STEPS[target_idx:]:
        data.pop(STEP_FIELDS[st], None)

    chat_id = context.user_data["wizard_msg"]["chat_id"]
    await show_wizard(chat_id, context, target)
    return target


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wm = context.user_data.get("wizard_msg")
    if wm:
        try:
            await context.bot.edit_message_text(
                chat_id=wm["chat_id"],
                message_id=wm["message_id"],
                text="✖️ Event creation cancelled.",
                reply_markup=None,
            )
        except Exception:
            pass
    context.user_data.pop("new_event", None)
    context.user_data.pop("wizard_msg", None)
    context.user_data.pop("wizard_state", None)
    return ConversationHandler.END


async def wizard_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data.get("new_event") or {}

    event_id = uuid.uuid4().hex[:8]
    event = {
        "id": event_id,
        "chat_id": data["chat_id"],
        "description": data["description"],
        "start": data["start"],
        "end": data["end"],
        "min": data["min"],
        "max": data["max"],
        "participants": [],
        "message_id": None,
    }

    try:
        sent = await context.bot.send_message(
            chat_id=event["chat_id"],
            text=render_event_text(event),
            parse_mode="HTML",
            reply_markup=event_keyboard(event_id, full=False),
        )
    except Exception as e:
        logger.warning("Failed to post event: %s", e)
        wm = context.user_data.get("wizard_msg")
        if wm:
            try:
                await context.bot.edit_message_text(
                    chat_id=wm["chat_id"],
                    message_id=wm["message_id"],
                    text=f"⚠️ Couldn't post to the group: {e}",
                    reply_markup=None,
                )
            except Exception:
                pass
        context.user_data.pop("new_event", None)
        context.user_data.pop("wizard_msg", None)
        context.user_data.pop("wizard_state", None)
        return ConversationHandler.END

    event["message_id"] = sent.message_id
    events[event_id] = event
    save_json(DATA_FILE, events)

    title = groups.get(str(event["chat_id"]), {}).get("title", "the group")
    wm = context.user_data.get("wizard_msg")
    if wm:
        try:
            await context.bot.edit_message_text(
                chat_id=wm["chat_id"],
                message_id=wm["message_id"],
                text=f"✅ Event posted in <b>{esc(title)}</b>.",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass

    context.user_data.pop("new_event", None)
    context.user_data.pop("wizard_msg", None)
    context.user_data.pop("wizard_state", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_event", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def end_event_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        admin_chat_ids = {g["id"] for g in await admin_groups_for(user.id, context.bot)}
        active = [
            e for e in events.values()
            if not e.get("ended") and e["chat_id"] in admin_chat_ids
        ]
    else:
        if not await is_group_admin(chat.id, user.id, context.bot):
            await update.message.reply_text("⛔ Only group admins can close RSVPs.")
            return
        active = [
            e for e in events.values()
            if not e.get("ended") and e["chat_id"] == chat.id
        ]

    if not active:
        await update.message.reply_text("No events with open RSVPs.")
        return

    keyboard = []
    for e in active:
        prefix = ""
        if chat.type == ChatType.PRIVATE:
            grp_title = groups.get(str(e["chat_id"]), {}).get("title", str(e["chat_id"]))
            prefix = f"[{grp_title}] "
        desc = e["description"]
        if len(desc) > 30:
            desc = desc[:27] + "…"
        label = f"{prefix}{desc} ({len(e['participants'])}/{e['max']})"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"endev:{e['id']}")])
    keyboard.append([InlineKeyboardButton("✖️ Cancel", callback_data="endev:cancel")])

    await update.message.reply_text(
        "🏁 Close RSVPs for which event?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_end_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, payload = query.data.split(":", 1)
    except ValueError:
        return

    if payload == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    event = events.get(payload)
    if not event:
        await query.edit_message_text("Event not found.")
        return
    if event.get("ended"):
        await query.edit_message_text("RSVPs are already closed for that event.")
        return
    if not await is_group_admin(event["chat_id"], query.from_user.id, context.bot):
        await query.edit_message_text("⛔ You're not an admin in that group.")
        return

    event["ended"] = True
    save_json(DATA_FILE, events)

    try:
        await context.bot.edit_message_text(
            chat_id=event["chat_id"],
            message_id=event["message_id"],
            text=render_event_text(event),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception as e:
        logger.warning("Failed to update ended event message: %s", e)

    try:
        await context.bot.send_message(
            chat_id=event["chat_id"],
            text=render_summary_text(event),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to post summary: %s", e)

    await query.edit_message_text("✅ RSVPs closed and final list posted.")


async def _do_edit(context: ContextTypes.DEFAULT_TYPE, event_id: str) -> None:
    """Perform the actual edit_message_text call with the latest event state."""
    event = events.get(event_id)
    if not event:
        return
    full = len(event["participants"]) >= event["max"]
    try:
        await context.bot.edit_message_text(
            chat_id=event["chat_id"],
            message_id=event["message_id"],
            text=render_event_text(event),
            parse_mode="HTML",
            reply_markup=event_keyboard(event_id, full=full),
        )
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg:
            logger.warning("Failed to edit event %s: %s", event_id, e)


async def schedule_edit(context: ContextTypes.DEFAULT_TYPE, event_id: str) -> None:
    """Throttle edits per event: instant on first call, then at most one trailing
    edit per EDIT_THROTTLE window so bursts of clicks collapse to a single update."""
    state = _edit_state.setdefault(event_id, {"last": 0.0, "pending": None})
    loop = asyncio.get_event_loop()
    now = loop.time()
    elapsed = now - state["last"]

    if elapsed >= EDIT_THROTTLE:
        state["last"] = now
        await _do_edit(context, event_id)
        return

    if state["pending"] is not None and not state["pending"].done():
        return  # a trailing edit is already scheduled; it will pick up the latest state

    delay = EDIT_THROTTLE - elapsed

    async def _trailing():
        try:
            await asyncio.sleep(delay)
            state["last"] = asyncio.get_event_loop().time()
            await _do_edit(context, event_id)
        finally:
            state["pending"] = None

    state["pending"] = asyncio.create_task(_trailing())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    try:
        action, event_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        return

    if action not in ("join", "leave"):
        await query.answer()
        return

    user = query.from_user
    user_dict = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }

    answer_text = ""
    answer_alert = False
    changed = False

    async with _event_lock(event_id):
        event = events.get(event_id)
        if not event:
            answer_text = "This event no longer exists."
            answer_alert = True
        elif event.get("ended"):
            answer_text = "RSVPs are closed for this event."
            answer_alert = True
        else:
            participant_ids = [p["id"] for p in event["participants"]]
            if action == "join":
                if user.id in participant_ids:
                    answer_text = "You're already in."
                elif len(event["participants"]) >= event["max"]:
                    answer_text = "Sorry, the event is full."
                    answer_alert = True
                else:
                    event["participants"].append(user_dict)
                    changed = True
                    answer_text = "You're in! ✅"
            else:  # leave
                if user.id not in participant_ids:
                    answer_text = "You weren't in this event."
                else:
                    event["participants"] = [
                        p for p in event["participants"] if p["id"] != user.id
                    ]
                    changed = True
                    answer_text = "Removed."

    await query.answer(answer_text, show_alert=answer_alert)

    if changed:
        schedule_save(DATA_FILE, events)
        await schedule_edit(context, event_id)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN env var is required (see .env.example).")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newevent", new_event)],
        states={
            SELECT_CHAT: [CallbackQueryHandler(select_chat, pattern=r"^pickchat:")],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_description)],
            START: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start)],
            END: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_end)],
            MIN_P: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_min)],
            MAX_P: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_max)],
            CONFIRM: [CallbackQueryHandler(wizard_create, pattern=r"^wiz:create$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(wizard_back, pattern=r"^wiz:back$"),
            CallbackQueryHandler(wizard_cancel, pattern=r"^wiz:cancel$"),
        ],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("endevent", end_event_cmd))
    app.add_handler(conv)
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(join|leave):")
    )
    app.add_handler(
        CallbackQueryHandler(on_end_button, pattern=r"^endev:")
    )
    app.add_handler(
        ChatMemberHandler(track_membership, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Python 3.14 no longer auto-creates an event loop in get_event_loop();
    # ensure one exists before PTB calls into it.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
