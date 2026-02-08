# Combo Extractor Bot

Telegram bot: download files from links, extract user:pass lines matching your keywords, save hits per user.

## Deploy on Railway

1. **Push this project to GitHub** (if not already).

2. **Create a Railway project**
   - Go to [railway.com](https://railway.com) → New Project → Deploy from GitHub repo.
   - Select this repo.

3. **Set environment variable**
   - In the Railway project → your service → **Variables**.
   - Add: `BOT_TOKEN` = your Telegram bot token (from [@BotFather](https://t.me/BotFather)).

4. **Deploy**
   - Railway will use `Procfile` (worker: `python bot.py`) and `requirements.txt`. No need to set a start command.

5. **Optional: persist data**
   - By default, downloads/hits/keywords are **ephemeral** (lost on redeploy).
   - To keep data: add a **Volume** to the service, mount it (e.g. `/data`), then in **Variables** set:
     - `DATA_DIR=/data`

## Local run

```bash
pip install -r requirements.txt
# Set token in script or:
set BOT_TOKEN=your_token_here   # Windows
export BOT_TOKEN=your_token_here   # Linux/Mac
python bot.py
```

## Commands

- `/start`, `/help` – intro and help  
- `/kw word1, word2` – set keywords (saved in `keywords.json`)  
- `/kw` – list your keywords  
- Send links – bot downloads and extracts hits for your keywords  
- `/view`, `/send`, `/sendall` – view/send hit files  
- `/clear`, `/clearhit`, `/clearraw`, `/clearall` – clear your data  
