# Raschetka88 Telegram Bot

Production build for Railway.

Files:
- `bot.py` — Telegram bot
- `requirements.txt` — Python dependencies
- `railway.toml` — Railway start/restart config
- `Procfile` — fallback worker command

Railway variable required:
- `BOT_TOKEN`

Optional:
- `ADMIN_IDS` — comma-separated Telegram user IDs. Use `/myid` in the bot to learn your ID.
- `DB_PATH` — SQLite path. Default `bot.db`.

The bot supports:
- ИП Ж / ИП Д revenue input
- Бук / Кир / Тро turnover input
- payout calculation
- automatic approval
- manual approved/rounded payouts; rounding residue goes to Т
- calculation history
- editable calculation parameters
