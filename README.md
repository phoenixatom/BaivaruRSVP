# Participant Bot

A Telegram bot for tracking event participants in groups. Admins create an event, members tap **Join** / **Leave**, and the bot maintains a single live participant list, with no message spam.

## Features

- **Wizard-style event creation** with a single live message: progress indicator, ✅/➡️/◽️ checklist, inline **Back** / **Cancel** buttons, and a final preview before posting
- Self-cleaning chat: user replies during the wizard are deleted automatically (best-effort)
- Inline **Join** / **Leave** buttons; one message edited live as people join (no spam)
- Hard cap at `participantsMax`. Extra joiners get a "full" alert
- Admin-only event creation. Works in-group **or** in DM with a group picker
- `/endevent` closes RSVPs, edits the original message with a 🔒 banner, and posts the final list
- Throttled message edits: bursts of clicks collapse to one trailing edit
- Persistent state across restarts (`events.json`, `groups.json`)

## Setup

### 1. Create the bot

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram.
2. `/newbot` → choose a name and username (must end in `bot`).
3. Copy the token BotFather gives you.
4. `/setprivacy` → pick your bot → **Disable**. (Required so the bot can read replies during the event-creation conversation in groups.)

### 2. Install

Requires Python 3.11+ (3.13 recommended; 3.14 works with the loop shim already in `bot.py`).

```bash
git clone <this-repo> participantbot   # or copy the folder
cd participantbot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and paste your token:

```
BOT_TOKEN=123456789:ABCdef-your-real-token
```

### 4. Run

```bash
python bot.py
```

You should see `Bot starting…`. Leave the terminal open. `Ctrl+C` to stop.

For always-on deployment, run it under `tmux`, `systemd`, `pm2`, or any cloud host.

## Usage

### Add the bot to your group

1. Open the group → group title → **Add members** → search for your bot's username.
2. Promote it to **admin** (Group settings → Administrators). At minimum it needs permission to send and edit messages. Granting **delete messages** permission lets the bot keep the wizard chat clean by removing user replies as they're processed.

### Create an event

**Option A: in the group**

Send `/newevent` in the group. The bot replaces it with a single wizard message that walks you through five steps:

```
📝 New Event
for Football Group

✅ Description: Football this Saturday
✅ Start: Sat 3 May, 7pm
➡️ End: When does it end? (e.g. Sat 3 May, 9pm)
◽️ participantsMin
◽️ participantsMax

Step 3 of 5. Reply with your answer.

[⬅️ Back]  [✖️ Cancel]
```

- Reply with your answer to the highlighted step. The wizard updates and re-posts at the bottom of the chat (the previous wizard message is deleted, your reply is deleted).
- Tap **⬅️ Back** any time to fix an earlier answer (later answers are cleared so you re-confirm them).
- Tap **✖️ Cancel** to abort.
- After step 5 you'll see a full preview with **✅ Create event**. Nothing is posted to the group until you confirm.

**Option B: in DM with the bot**

Send `/newevent` in DM. The bot shows inline buttons listing every group where you're an admin. Pick one, then run through the same wizard in DM. The event posts in the chosen group when you confirm.

`/cancel` also works as a fallback to abort.

### Join / leave

Anyone in the group taps **✅ Join** or **❌ Leave** on the event message. The participant list updates in place. When the event hits `participantsMax`, the join button switches to **🚫 Full**.

### Close RSVPs for an event

```
/endevent
```

- In the group: shows buttons for events with open RSVPs in this group.
- In DM: shows buttons for events with open RSVPs across all groups you admin (each labelled with its group name).

Picking one:
1. Edits the original event message: adds a 🔒 *RSVPs CLOSED* banner and removes the Join/Leave buttons.
2. Posts a **new** message in the group with the final participant list (so members get a notification).
3. Blocks any further Join/Leave taps.

## Commands

| Command | Where | Who | What |
|---|---|---|---|
| `/start` | DM | anyone | Greeting / help |
| `/newevent` | group or DM | group admins | Create a new event |
| `/endevent` | group or DM | group admins | Close RSVPs for an event and post the final list |
| `/cancel` | during creation | the creator | Abort the in-progress event creation (fallback for the wizard's ✖️ Cancel button) |

## Data storage

The bot persists state to two JSON files alongside `bot.py`:

- `events.json`: every event (description, times, min/max, participants, message_id, ended flag)
- `groups.json`: chats the bot is a member of (id, title)

Both are loaded into memory at startup and rewritten after every state change. Safe to inspect or back up. Don't edit them while the bot is running.

For larger deployments (>500 active users or hundreds of stored events), migrate to SQLite. The schema is trivial.

## Configuration tunables

In `bot.py`:

- `EDIT_THROTTLE = 1.5`: minimum gap (seconds) between edits to the same event message. Higher = smoother under load, slightly stale display. Lower = snappier, more API calls.

## License

Do whatever you want.
