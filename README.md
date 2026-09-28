# MyJobMag Telegram Bot

Built by [karanja-254](https://github.com/karanja-254).

A Telegram bot for [MyJobMag Kenya](https://www.myjobmag.co.ke/) job alerts. It watches the public jobs feed and sends new listings to verified users.

## What the bot does

- Checks the MyJobMag Kenya XML feed every 5 minutes
- Sends only new job links to verified users
- Uses a short math quiz to block bots
- Notifies the admin when a new user verifies
- Lets the admin export Excel and PDF reports, broadcast a message, back up the database, and send a test job
- Sends the admin an automatic database backup every 7 days at 00:00 East Africa Time (UTC+3)

## What you need

- Python 3.10 or newer
- A Telegram bot token from [BotFather](https://t.me/BotFather)
- Your numeric Telegram user ID (this becomes the admin account)

## Setup

Open a terminal in this project folder.

Create and activate a virtual environment (recommended).

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your local environment file from the example:

Windows PowerShell:

```powershell
copy .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Open `.env` and replace the placeholders:

- `BOT_TOKEN` — the token BotFather gave you
- `ADMIN_ID` — your numeric Telegram user ID

`.env` stays on your machine. Do not commit it.

On a host such as Azure, set `BOT_TOKEN`, `ADMIN_ID`, and `PORT` in the app environment instead of a file. Existing environment variables are left unchanged.

## Run

```bash
python job.py
```

When configuration is present you should see `Bot is running locally...`. If `BOT_TOKEN` or `ADMIN_ID` is missing, the process stops with a startup error.

## Commands

Users:

- `/start` — math quiz, then job alerts
- `/stop` or `/cancel` — opt out (use `/start` to opt in again)
- `/help` or `/commands` — user command list

Admin only (the account in `ADMIN_ID`):

- `/admin` — button menu for broadcast, database backup, reports, and test job
- `/broadcast <message>` — message every verified user; you can also reply to a message with `/broadcast`
- `/database` — receive the SQLite database file
- `/restore` — replace the live database with a validated `myjobkenya.db` backup
- `/report` — choose a 3, 6, or 12 month Excel and PDF report
- `/testjob` — send the latest feed item to the admin only

The same job link is not sent again on the normal 5-minute cycle. Test jobs are tracked separately.

## Local data

The bot creates `myjobkenya.db` in this folder on first run. `/restore` keeps the previous live database in a local `backups/` folder before replacing it. Reports are written here as `report_3m`, `report_6m`, and `report_12m` Excel and PDF files. These files can contain Telegram usernames and chat IDs, so the database, backups, and reports are gitignored and should not be published.

## Files to deploy

- `job.py`
- `requirements.txt`
- `.env.example` (template only)

Keep your own database backups if you move the bot to another machine.
