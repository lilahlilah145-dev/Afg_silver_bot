# AFG Silver Bot — Registration MVP

This starter version implements only:
- Pashto welcome screen
- Trading rules display and acceptance
- Registration: full name, Telegram contact, country
- Admin approval/rejection
- SQLite storage

It intentionally does **not** execute real-money trades, collect deposits, calculate margin,
liquidate positions, or run auctions. Those functions require verified market-data contracts,
tested accounting rules, legal review, operational controls, and a robust server.

## Deploy from a phone

A cloud Python host can run this bot. Create a Python service and upload these files.

Set environment variables in the host dashboard:
- `BOT_TOKEN`: token from BotFather (keep private; never paste into chat)
- `ADMIN_ID`: your numeric Telegram user ID (not your @username)

Install dependencies:
`pip install -r requirements.txt`

Run:
`python main.py`

The service must remain running. Use persistent storage for `afg_silver.sqlite3`;
otherwise registrations may be lost on redeploy.

## Get your Telegram numeric ID
Use a reputable Telegram ID helper bot, or add a temporary `/myid` command before deployment.
Do not share your BotFather token with anyone.

## Important
- Rotate the BotFather token immediately if it is exposed.
- Registration data includes phone numbers; restrict database access and publish a privacy notice.
- Do not use this starter for real-money trading.
- Have local counsel review whether offering leveraged precious-metals trading, margin, custody,
  or liquidation services is permitted in the countries where the operator and customers are located.
- Before live use, define a transparent mark-price source, stale-price rejection, spread/slippage,
  margin formula, liquidation waterfall, auction fallback, dispute process, audit logs, and recovery
  procedures. A liquidation auction cannot guarantee that the administrator avoids losses.
