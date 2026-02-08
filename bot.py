import os
import asyncio
import aiohttp
import re
import json
from datetime import timedelta

from telegram import Update, InputFile
from telegram.error import TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


class _StopRequested(BaseException):
    """Raised when user sends /stop during download or processing."""


# ==== CONFIG ====
# On Railway: set BOT_TOKEN in Environment Variables (never commit real token)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Base dir for all data (downloads, hits, keywords.json). On Railway set DATA_DIR to a volume path to persist.
_ROOT_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
BASE_DOWNLOAD_DIR = os.path.join(_ROOT_DIR, "downloads")
BASE_RESULTS_DIR = os.path.join(_ROOT_DIR, "results")
BASE_HITS_DIR = os.path.join(_ROOT_DIR, "hits")
DEFAULT_KEYWORD = "savastan0"  # Fallback when user has no keywords set
KEYWORDS_JSON = os.path.join(_ROOT_DIR, "keywords.json")


def _load_keywords_data() -> dict:
    """Load { "user_id": ["kw1", "kw2"], ... } from JSON."""
    try:
        with open(KEYWORDS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_keywords_data(data: dict) -> None:
    with open(KEYWORDS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_keywords(user_id: int) -> list:
    """Return list of keywords for this user. Uses DEFAULT_KEYWORD if none set."""
    data = _load_keywords_data()
    key = str(user_id)
    if key not in data or not data[key]:
        return [DEFAULT_KEYWORD]
    return data[key]


def set_keywords(user_id: int, keywords: list) -> None:
    """Save keyword list for this user (persisted in JSON)."""
    data = _load_keywords_data()
    data[str(user_id)] = [k.strip() for k in keywords if k and k.strip()]
    _save_keywords_data(data)


def get_user_dirs(user_id: int):
    """Return (download_dir, results_dir, hits_dir) for this user. Creates dirs if needed."""
    uid = str(user_id)
    download_dir = os.path.join(BASE_DOWNLOAD_DIR, uid)
    results_dir = os.path.join(BASE_RESULTS_DIR, uid)
    hits_dir = os.path.join(BASE_HITS_DIR, uid)
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(hits_dir, exist_ok=True)
    return download_dir, results_dir, hits_dir


# ==== HELPERS ====
def extract_links(text: str) -> list:
    """Extract all URLs from text using regex."""
    url_pattern = r'https?://[^\s]+'
    links = re.findall(url_pattern, text)
    return [link.rstrip('.,;:)\'"') for link in links]  # Remove trailing punctuation


def format_timedelta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:  # NaN
        return "unknown"
    return str(timedelta(seconds=int(seconds)))


from telegram.error import BadRequest


async def safe_edit(message, text):
    """Edit a Telegram message and ignore 'Message is not modified' errors."""
    try:
        await message.edit_text(text)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise


async def try_curl_fallback(url: str, dest_path: str, progress_message) -> bool:
    """Try to download using the system `curl` command as a fallback.

    Returns True on success, False on failure.
    """
    tmp_path = dest_path + ".part"
    try:
        await safe_edit(progress_message, "Attempting fallback download with system 'curl' (may bypass Python SSL issues)...")
        proc = await asyncio.create_subprocess_exec(
            'curl', '-L', '-o', tmp_path, url, '--fail', stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(tmp_path):
            try:
                os.replace(tmp_path, dest_path)
            except Exception:
                os.rename(tmp_path, dest_path)
            await safe_edit(progress_message, f"Download complete ✅ (via curl)\nSaved as: `{dest_path}`")
            return True
        else:
            err = stderr.decode().strip() if stderr else f"curl exit {proc.returncode}"
            await safe_edit(progress_message, f"curl fallback failed: {err}")
            return False
    except FileNotFoundError:
        await safe_edit(progress_message, "curl not found on system.")
        return False
    except Exception as e:
        await safe_edit(progress_message, f"curl fallback error: {e}")
        return False


async def download_file(
    url: str,
    dest_path: str,
    progress_message,
    context: ContextTypes.DEFAULT_TYPE,
    retries: int = 3,
):
    """Stream download with progress updates to Telegram.

    Tuned for Railway Hobby (8 vCPU / 8 GB RAM): 64 MB chunks, long timeout, minimal progress overhead.
    """
    # Railway Hobby: 8 GB RAM — use 64 MB chunks for max throughput (fewer syscalls, less Python overhead)
    chunk_size = 64 * 1024 * 1024  # 64 MB
    progress_interval = 3  # Update Telegram every 3s to reduce API overhead

    ssl_setting = None

    for attempt in range(1, retries + 1):
        start = asyncio.get_running_loop().time()
        downloaded = 0
        last_update = start
        try:
            connector = aiohttp.TCPConnector(
                limit=0,
                limit_per_host=0,
                force_close=False,
            )
            async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
                # No timeout - run until done or /stop
                async with session.get(url, ssl=ssl_setting) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")

                    total = int(resp.headers.get("Content-Length", 0))

                    tmp_path = dest_path + ".part"
                    with open(tmp_path, "wb") as f:
                        while True:
                            if context.user_data.get("stop_requested"):
                                f.close()
                                try:
                                    os.remove(tmp_path)
                                except Exception:
                                    pass
                                raise _StopRequested()

                            chunk = await resp.content.read(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)

                            now = asyncio.get_running_loop().time()
                            if now - last_update >= progress_interval or (total > 0 and downloaded == total):
                                elapsed = now - start
                                speed = downloaded / elapsed if elapsed > 0 else 0  # bytes/s
                                speed_mb = speed / 1024 / 1024

                                if total > 0 and speed > 0:
                                    eta = (total - downloaded) / speed
                                    eta_text = format_timedelta(eta)
                                    total_mb = total / 1024 / 1024
                                else:
                                    eta_text = "unknown"
                                    total_mb = 0

                                downloaded_mb = downloaded / 1024 / 1024

                                text_lines = [
                                    "📥 Downloading file...",
                                    f"Downloaded: {downloaded_mb:.2f} MB"
                                    + (f" / {total_mb:.2f} MB" if total > 0 else ""),
                                    f"Speed: {speed_mb:.2f} MB/s",
                                    f"ETA: {eta_text}",
                                ]

                                try:
                                    await progress_message.edit_text("\n".join(text_lines))
                                except Exception:
                                    # Ignore edit errors (e.g., message too old)
                                    pass

                                last_update = now

                    # move tmp to final path
                    try:
                        os.replace(tmp_path, dest_path)
                    except Exception:
                        # fallback to rename
                        os.rename(tmp_path, dest_path)

            # success
            try:
                await progress_message.edit_text(
                    f"Download complete ✅\nSaved as: `{dest_path}`"
                )
            except Exception:
                pass
            return

        except aiohttp.ClientSSLError as e:
            err = f"SSL error: {e}"
            # On first SSL error attempt, retry with verification disabled
            if ssl_setting is not False:
                try:
                    await progress_message.edit_text(
                        "SSL error encountered. Retrying with SSL verification disabled..."
                    )
                except Exception:
                    pass
                ssl_setting = False
                await asyncio.sleep(1)
                continue

        except aiohttp.ClientConnectorError as e:
            err = f"Connection error: {e}"
        except asyncio.TimeoutError:
            err = "Timeout"
        except PermissionError as e:
            err = f"Permission denied: {e}"
        except Exception as e:
            err = str(e)

        # retry / final failure handling
        if attempt < retries:
            try:
                await progress_message.edit_text(f"Download failed ({err}), retrying {attempt}/{retries}...")
            except Exception:
                pass
            await asyncio.sleep(2 ** attempt)
        else:
            try:
                await progress_message.edit_text(f"Download fail ho gaya ❌: {err}")
            except Exception:
                pass
            raise RuntimeError(err)


def extract_user_pass(
    source_path: str, keyword: str, result_path: str
) -> int:
    """Scan file for lines with keyword, extract last two colon-separated fields as user:pass."""
    return extract_user_pass_multi(source_path, [keyword], result_path)


def extract_user_pass_multi(
    source_path: str, keywords: list, result_path: str
) -> int:
    """Scan file for lines containing ANY of the keywords, extract user:pass (last two colon fields).

    Intended to be run in a thread via loop.run_in_executor(...).
    """
    if not keywords:
        return 0
    count = 0
    with open(source_path, "r", encoding="utf-8", errors="ignore") as src:
        with open(result_path, "w", encoding="utf-8") as out:
            for line in src:
                if any(kw in line for kw in keywords):
                    parts = line.strip().split(":")
                    if len(parts) >= 3:
                        user_pass = ":".join(parts[-2:])
                        out.write(user_pass + "\n")
                        count += 1
    return count


# ==== HANDLERS ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["hits_files"] = []  # List of (file_path, hit_count) tuples
    await update.message.reply_text(
        "Hello! 👋\n"
        "Send download links (http:// or https://) directly or forward messages containing links.\n"
        "I will download and extract user:pass for **your keywords** (set with /kw).\n\n"
        "📝 **Commands:**\n"
        "/kw - Set or list keywords (e.g. /kw word1, word2, word3)\n"
        "/view - See all hit files available\n"
        "/send {num} - Send specific hit file\n"
        "/sendall - Merge all hits and send\n"
        "/stop - Stop all ongoing downloads and filtering\n"
        "/clear - Clear **your** data (options below)\n"
        "/clearhit - Clear your hit files only\n"
        "/clearraw - Clear your downloaded files only\n"
        "/clearall - Clear all **your** data\n\n"
        "📁 Your downloads & hits are saved **per user** — /clear only affects **your** data.\n"
        "You can send multiple links at once! 🚀"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Available commands:**\n\n"
        "/start - Reset and start a new session\n"
        "/kw - Set or list keywords (multi: /kw word1, word2, word3)\n"
        "/view - See all hit files currently available\n"
        "/send {num} - Send specific hit file by number\n"
        "/sendall - Merge all hits into ONE file and send\n"
        "/stop - Stop all downloads/filtering immediately\n"
        "/clear - Options to clear **your** data\n"
        "/clearhit - Clear your hit files only\n"
        "/clearraw - Delete your downloaded raw files only\n"
        "/clearall - Clear all **your** data (hits + raw)\n"
        "/help - Show this help message\n\n"
        f"🔑 **Keywords:** /kw to set or list (multi-keyword supported)\n"
        "Just send links or forward messages with links!"
    )


async def kw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set or list keywords. Usage: /kw keyword1, keyword2, keyword3 ..."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    # Support both /kw and /kw@BotName; take everything after first word
    parts = text.split()
    args_str = " ".join(parts[1:]).strip() if parts and parts[0].lower().startswith("/kw") else ""

    if not args_str:
        # List current keywords
        keywords = get_keywords(user_id)
        data = _load_keywords_data()
        if str(user_id) not in data or not data[str(user_id)]:
            msg = (
                f"🔑 **Your keywords:** (using default)\n"
                f"`{DEFAULT_KEYWORD}`\n\n"
                "To set your own list, send:\n"
                "`/kw word1, word2, word3`"
            )
        else:
            kw_list = ", ".join(f"`{k}`" for k in keywords)
            msg = f"🔑 **Your keywords ({len(keywords)}):**\n{kw_list}\n\nExtraction will match lines containing **any** of these."
        await update.message.reply_text(msg)
        return

    # Parse comma-separated keywords
    parts = [p.strip() for p in args_str.split(",") if p.strip()]
    if not parts:
        await update.message.reply_text("❌ Give at least one keyword. Example: `/kw savastan0, netflix, spotify`", parse_mode="Markdown")
        return

    set_keywords(user_id, parts)
    kw_list = ", ".join(f"`{k}`" for k in parts)
    await update.message.reply_text(f"✅ **Keywords set ({len(parts)}):**\n{kw_list}\n\nHits will be saved for lines matching **any** of these.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def dir_info(path):
        try:
            files = os.listdir(path)
        except Exception:
            return 0, 0
        total_size = 0
        for f in files:
            p = os.path.join(path, f)
            try:
                total_size += os.path.getsize(p)
            except Exception:
                pass
        return len(files), total_size

    download_dir, results_dir, hits_dir = get_user_dirs(update.effective_user.id)
    d_count, d_size = dir_info(download_dir)
    h_count, h_size = dir_info(hits_dir)
    r_count, r_size = dir_info(results_dir)

    def fmt_size(b):
        return f"{b/1024/1024:.2f} MB"

    hits_files = context.user_data.get("hits_files", [])
    total_hits = sum(count for _, count in hits_files)

    await update.message.reply_text(
        f"📊 **Session Status:**\n\n"
        f"💾 Downloads: {d_count} files ({fmt_size(d_size)})\n"
        f"📋 Hit Files: {h_count} files ({fmt_size(h_size)}) | Total Hits: {total_hits}\n"
        f"🗂️ Results: {r_count} files ({fmt_size(r_size)})"
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["clear_pending"] = True
    await update.message.reply_text(
        "❓ Clear **your** data only (other users are not affected):\n\n"
        "/clearhit - Clear your hit files only\n"
        "/clearraw - Clear your downloaded raw files only\n"
        "/clearall - Clear ALL your data (hits + downloads + results)"
    )


async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Delete files in THIS user's download and results folders only
    download_dir, results_dir, _ = get_user_dirs(update.effective_user.id)
    deleted = 0
    for folder in (download_dir, results_dir):
        try:
            for fname in os.listdir(folder):
                fpath = os.path.join(folder, fname)
                try:
                    os.remove(fpath)
                    deleted += 1
                except Exception:
                    pass
        except Exception:
            pass

    context.user_data.clear()
    context.user_data["hits_files"] = []
    await update.message.reply_text(f"✅ Cleared {deleted} file(s) for your account. Storage freed.")


async def clearhit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all hit files from THIS user's folder only."""
    try:
        _, _, hits_dir = get_user_dirs(update.effective_user.id)
        deleted = 0
        for fname in os.listdir(hits_dir):
            fpath = os.path.join(hits_dir, fname)
            try:
                os.remove(fpath)
                deleted += 1
            except Exception:
                pass
        
        context.user_data["hits_files"] = []
        await update.message.reply_text(f"✅ Cleared {deleted} hit file(s) for your account.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error clearing hits: {e}")


async def clearraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear THIS user's downloaded raw files only."""
    try:
        download_dir, _, _ = get_user_dirs(update.effective_user.id)
        deleted = 0
        for fname in os.listdir(download_dir):
            fpath = os.path.join(download_dir, fname)
            try:
                os.remove(fpath)
                deleted += 1
            except Exception:
                pass
        await update.message.reply_text(f"✅ Deleted {deleted} raw download file(s) for your account.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def clearall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear everything for THIS user only: hits, raw downloads, and results."""
    try:
        download_dir, results_dir, hits_dir = get_user_dirs(update.effective_user.id)
        deleted = 0
        for folder in (download_dir, hits_dir, results_dir):
            try:
                for fname in os.listdir(folder):
                    fpath = os.path.join(folder, fname)
                    try:
                        os.remove(fpath)
                        deleted += 1
                    except Exception:
                        pass
            except Exception:
                pass
        
        context.user_data["hits_files"] = []
        await update.message.reply_text(f"🗑️ Cleared all your data: {deleted} file(s) deleted. Fresh start!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all ongoing downloads and processing for this user immediately."""
    context.user_data["stop_requested"] = True
    task = context.user_data.get("_background_task")
    if task and not task.done():
        task.cancel()
    await update.message.reply_text("⏹ **Stopped.** All downloads and filtering cancelled.")


async def download_and_extract(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, link_num: int, total_links: int, user_id: int):
    """Download a single file and extract user:pass for all user keywords - CONCURRENT. Respects /stop."""
    if context.user_data.get("stop_requested"):
        return

    keywords = get_keywords(user_id)
    download_dir, _, hits_dir = get_user_dirs(user_id)
    file_name = os.path.basename(url.split("?")[0]) or f"file_{link_num}.txt"
    base, ext = os.path.splitext(file_name)
    file_name = f"{base}_{link_num}{ext}"
    dest_path = os.path.join(download_dir, file_name)

    progress_message = None
    try:
        progress_message = await update.message.reply_text(f"⬇️ **[{link_num}/{total_links}]** Downloading: `{file_name}`\n⏳ Please wait...")

        try:
            await download_file(url, dest_path, progress_message, context)
        except _StopRequested:
            if progress_message:
                await safe_edit(progress_message, f"⏹ **[{link_num}/{total_links}]** Stopped by user.")
            return

        if context.user_data.get("stop_requested"):
            if progress_message:
                await safe_edit(progress_message, f"⏹ **[{link_num}/{total_links}]** Stopped by user.")
            return

        kw_preview = ", ".join(keywords[:5]) + ("..." if len(keywords) > 5 else "")
        await safe_edit(progress_message, f"⬇️ **[{link_num}/{total_links}]** ✅ Downloaded\n🔍 Extracting keywords: {kw_preview}...")

        if context.user_data.get("stop_requested"):
            if progress_message:
                await safe_edit(progress_message, f"⏹ **[{link_num}/{total_links}]** Stopped by user.")
            return

        base_name = os.path.splitext(os.path.basename(dest_path))[0]
        result_path = os.path.join(hits_dir, f"{base_name}_{link_num}.txt")

        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, extract_user_pass_multi, dest_path, keywords, result_path)

        if count > 0:
            # Rename file to include hit count
            final_name = f"{base_name}_{link_num}_{count}_hits.txt"
            final_path = os.path.join(hits_dir, final_name)
            try:
                os.rename(result_path, final_path)
            except Exception:
                final_path = result_path
            
            await safe_edit(progress_message, f"✅ **[{link_num}/{total_links}]** Found {count} hits!")
            
            # Store in memory
            if "hits_files" not in context.user_data:
                context.user_data["hits_files"] = []
            context.user_data["hits_files"].append((final_path, count))
        else:
            await safe_edit(progress_message, f"⚠️ **[{link_num}/{total_links}]** No hits for your keywords")
            # Delete empty file
            try:
                os.remove(result_path)
            except Exception:
                pass

    except Exception as e:
        # Try curl fallback
        if progress_message:
            await safe_edit(progress_message, f"🔄 **[{link_num}/{total_links}]** Retrying with curl...")
        try:
            ok = await try_curl_fallback(url, dest_path, progress_message)
            if ok:
                # Retry extraction after curl download
                base_name = os.path.splitext(os.path.basename(dest_path))[0]
                result_path = os.path.join(hits_dir, f"{base_name}_{link_num}.txt")
                loop = asyncio.get_running_loop()
                count = await loop.run_in_executor(None, extract_user_pass_multi, dest_path, keywords, result_path)
                if count > 0:
                    final_name = f"{base_name}_{link_num}_{count}_hits.txt"
                    final_path = os.path.join(hits_dir, final_name)
                    try:
                        os.rename(result_path, final_path)
                    except Exception:
                        final_path = result_path
                    
                    if progress_message:
                        await safe_edit(progress_message, f"✅ **[{link_num}/{total_links}]** Found {count} hits (curl)!")
                    if "hits_files" not in context.user_data:
                        context.user_data["hits_files"] = []
                    context.user_data["hits_files"].append((final_path, count))
                else:
                    if progress_message:
                        await safe_edit(progress_message, f"⚠️ **[{link_num}/{total_links}]** No hits for your keywords")
                    try:
                        os.remove(result_path)
                    except Exception:
                        pass
            else:
                if progress_message:
                    await safe_edit(progress_message, f"❌ **[{link_num}/{total_links}]** Download failed")
        except Exception as e2:
            if progress_message:
                await safe_edit(progress_message, f"❌ **[{link_num}/{total_links}]** Error: {str(e)[:30]}")


async def process_links_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, links: list):
    """Download multiple links and extract user:pass concurrently - RUNS IN BACKGROUND."""
    if not links:
        await update.message.reply_text("❌ No valid links found.")
        return

    if "hits_files" not in context.user_data:
        context.user_data["hits_files"] = []

    context.user_data["stop_requested"] = False

    await update.message.reply_text(
        f"🚀 **{len(links)} link(s) detected!**\n\n"
        f"⚡ Starting parallel downloads...\n"
        f"(Use /view, /send while downloading — /stop to cancel all.)"
    )

    user_id = update.effective_user.id
    download_tasks = []
    for idx, url in enumerate(links, 1):
        task = download_and_extract(update, context, url, idx, len(links), user_id)
        download_tasks.append(task)

    async def background_download():
        """Run in background. Stops when user sends /stop (task cancelled)."""
        try:
            results = await asyncio.gather(*download_tasks, return_exceptions=True)

            if context.user_data.get("stop_requested"):
                return

            hits_files = context.user_data.get("hits_files", [])
            if hits_files:
                total_hits = sum(count for _, count in hits_files)
                try:
                    await update.message.reply_text(
                        f"\n✅ **All {len(download_tasks)} Downloads Complete!**\n\n"
                        f"📊 Total Hits Found: **{total_hits}**\n"
                        f"📁 Files: **{len(hits_files)}**\n\n"
                        f"Use /view to see all\nUse /sendall to merge & send"
                    )
                except Exception:
                    pass
            else:
                try:
                    await update.message.reply_text(f"⚠️ No results. No hits for your keywords.")
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    bg = asyncio.create_task(background_download())
    context.user_data["_background_task"] = bg


async def view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available hit files."""
    hits_files = context.user_data.get("hits_files", [])
    
    if not hits_files:
        await update.message.reply_text("❌ No hit files available.\n\nSend links to start extracting!")
        return
    
    total_hits = sum(count for _, count in hits_files)
    
    msg = "📊 **Available Hit Files:**\n\n"
    for idx, (file_path, count) in enumerate(hits_files, 1):
        file_name = os.path.basename(file_path)
        msg += f"{idx}️⃣ {file_name}\n   💾 {count} hits\n\n"
    
    msg += f"📈 **Total Hits: {total_hits}**\n\n"
    msg += "Use: /send {num} to send specific file\nUse: /sendall to merge all"
    
    await update.message.reply_text(msg)


async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_num: int):
    """Send a specific hit file by number."""
    hits_files = context.user_data.get("hits_files", [])
    
    if not hits_files:
        await update.message.reply_text("❌ No hit files available.")
        return
    
    if file_num < 1 or file_num > len(hits_files):
        await update.message.reply_text(f"❌ Invalid file number. Use /view to see available files (1-{len(hits_files)})")
        return
    
    file_path, count = hits_files[file_num - 1]
    
    if not os.path.exists(file_path):
        await update.message.reply_text(f"❌ File not found: {os.path.basename(file_path)}")
        return
    
    try:
        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=os.path.basename(file_path)),
                caption=f"📋 File #{file_num}\n💾 {count} hits",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error sending file: {e}")


async def sendall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Merge all hits into one file and send."""
    hits_files = context.user_data.get("hits_files", [])
    
    if not hits_files:
        await update.message.reply_text("❌ No hit files to merge.")
        return
    
    total_hits = sum(count for _, count in hits_files)
    
    _, _, hits_dir = get_user_dirs(update.effective_user.id)
    merged_path = os.path.join(hits_dir, f"merged_{total_hits}_hits.txt")
    
    try:
        with open(merged_path, "w", encoding="utf-8") as out:
            for idx, (file_path, _) in enumerate(hits_files, 1):
                if os.path.exists(file_path):
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        out.write(f.read())
        
        # Send merged file
        with open(merged_path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=os.path.basename(merged_path)),
                caption=f"✨ **Merged All Hits**\n💾 Total: {total_hits} entries\n📁 Files: {len(hits_files)}",
            )
        
        await update.message.reply_text("✅ Merge complete! File sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error merging files: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Extract all links from the message
    links = extract_links(text)

    if links:
        # Process detected links
        await process_links_batch(update, context, links)
    else:
        # No links found
        await update.message.reply_text(
            "❌ No links detected.\n\n"
            "Send download links (http:// or https://) or forward messages with links.\n"
            f"Use /help for commands"
        )



def main():
    async def post_init(app_arg) -> None:
        """Confirm bot is connected on startup."""
        me = await app_arg.bot.get_me()
        print(f"Connected as @{me.username}. Bot is responding to messages.")

    # Longer timeouts to avoid ReadTimeout when stopping or on slow networks
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("kw", kw_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("clear_confirm", clear_confirm))
    app.add_handler(CommandHandler("clearhit", clearhit))
    app.add_handler(CommandHandler("clearraw", clearraw))
    app.add_handler(CommandHandler("clearall", clearall))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("view", view))
    app.add_handler(CommandHandler("sendall", sendall))
    
    # Handle /send {num} command
    async def handle_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args or len(context.args) == 0:
            await update.message.reply_text("❌ Usage: /send {file_number}\n\nUse /view to see available files")
            return
        try:
            file_num = int(context.args[0])
            await send_file(update, context, file_num)
        except ValueError:
            await update.message.reply_text("❌ File number must be a number!\n\nUse /view to see available files")
    
    app.add_handler(CommandHandler("send", handle_send))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log errors and tell the user so the bot doesn't appear dead."""
        import traceback
        err = context.error
        traceback.print_exception(type(err), err, err.__traceback__)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Something went wrong. Try again or /start."
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)

    print("Bot starting... Press Ctrl+C to stop.")
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except KeyboardInterrupt:
        print("Bot stopped (Ctrl+C).")
    except (TimedOut, RuntimeError) as e:
        # Graceful shutdown: avoid traceback on API timeout or library shutdown race
        if "not properly initialized" in str(e) or isinstance(e, TimedOut):
            print("Bot stopped.")
        else:
            raise


if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise SystemExit(
            "Set BOT_TOKEN: in Railway → Variables, or in script. "
            "Never commit a real token to git."
        )
    main()