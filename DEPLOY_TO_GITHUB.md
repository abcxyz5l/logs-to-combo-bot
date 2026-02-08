# Deploy This Bot to GitHub (logs-to-combo-bot)

Replace the contents of **https://github.com/abcxyz5l/logs-to-combo-bot** with this project.

## Option A: Replace repo from this folder (recommended)

In PowerShell (run from this folder):

```powershell
# 1. Clone the repo into a temp folder
cd $env:TEMP
git clone https://github.com/abcxyz5l/logs-to-combo-bot.git repo-temp
cd repo-temp

# 2. Remove all existing files (except .git)
Get-ChildItem -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force

# 3. Copy this project's files into the repo
$src = "C:\Users\anshu\Music\working log to combo bot\cursor raly combo extrator"
Copy-Item "$src\bot.py" .
Copy-Item "$src\Procfile" .
Copy-Item "$src\requirements.txt" .
Copy-Item "$src\runtime.txt" .
Copy-Item "$src\README.md" .
Copy-Item "$src\railway.json" .
Copy-Item "$src\.env.example" .
Copy-Item "$src\.gitignore" .

# 4. Commit and push (this replaces the repo content)
git add -A
git commit -m "Replace with combo extractor bot (multi-keyword, per-user, Railway-ready)"
git push origin main
```

If the repo uses `master` instead of `main`:

```powershell
git push origin master
```

## Option B: If you already have this folder in git

```powershell
cd "C:\Users\anshu\Music\working log to combo bot\cursor raly combo extrator"

git remote add origin https://github.com/abcxyz5l/logs-to-combo-bot.git
# If origin already exists and points elsewhere:
git remote set-url origin https://github.com/abcxyz5l/logs-to-combo-bot.git

git add -A
git commit -m "Combo extractor bot - multi-keyword, per-user, Railway-ready"
git push -u origin main --force
```

`--force` overwrites the existing branch on GitHub with your local version.

## After pushing

1. On **Railway**: New Project → Deploy from GitHub → select **abcxyz5l/logs-to-combo-bot**.
2. In the service → **Variables** → add **BOT_TOKEN** (your Telegram bot token).
3. Deploy. The bot will run from the Procfile (`worker: python bot.py`).
