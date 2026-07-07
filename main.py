import asyncio
import os
import json
import base64
import logging
import itertools

import aiohttp
from aiohttp import web
from telethon import TelegramClient, events, Button

# ============================================================
# 🛡️ CRITICAL PYTHON 3.14+ ASYNCIO EVENT LOOP FIX
# ============================================================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ============================================================
# CONFIG
# ============================================================
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
PORT = int(os.environ.get("PORT", 8080))

ACCOUNTS_FILE = "github_accounts.json"

GH_API = "https://api.github.com"
GH_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("github_bot")

client = TelegramClient("github_bot_session", API_ID, API_HASH, loop=loop)

# ============================================================
# PERSISTENCE
# ============================================================
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_account(owner_id):
    return load_accounts().get(str(owner_id))

def update_account(owner_id, **kwargs):
    data = load_accounts()
    entry = data.get(str(owner_id), {})
    entry.update(kwargs)
    data[str(owner_id)] = entry
    save_accounts(data)

def get_active_repo(owner_id):
    acc = get_account(owner_id)
    if not acc:
        return None
    return acc.get("active_repo")

def get_active_path(owner_id):
    acc = get_account(owner_id)
    if not acc:
        return ""
    return acc.get("active_path", "")

def set_active_path(owner_id, path):
    update_account(owner_id, active_path=path)

# ============================================================
# CALLBACK TOKEN CACHE (fixes Telegram's 64-byte callback_data limit)
# ============================================================
CB_STORE = {}
CB_COUNTER = itertools.count()

def cb_put(value):
    token = str(next(CB_COUNTER))
    CB_STORE[token] = value
    if len(CB_STORE) > 6000:
        for k in list(CB_STORE.keys())[:3000]:
            CB_STORE.pop(k, None)
    return token

def cb_get(token):
    return CB_STORE.get(token)

def cb_data(prefix, value):
    return f"{prefix}:{cb_put(value)}".encode()

# ============================================================
# ACCESS CONTROL
# ============================================================
def owner_only(func):
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            try:
                await event.respond("⛔ Access Denied. This bot is private.")
            except Exception:
                pass
            return
        return await func(event)
    return wrapper

# ============================================================
# AIOHTTP HEALTH SERVER (Render port binding)
# ============================================================
async def health(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server bound on 0.0.0.0:{PORT}")

# ============================================================
# GITHUB API CORE
# ============================================================
def gh_headers(token):
    h = dict(GH_HEADERS_BASE)
    h["Authorization"] = f"Bearer {token}"
    return h

async def gh_request(method, path, token, json_data=None, params=None):
    url = f"{GH_API}{path}"
    try:
        async with asyncio.timeout(10):
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url, headers=gh_headers(token), json=json_data, params=params
                ) as resp:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}
                    return resp.status, data
    except asyncio.TimeoutError:
        return 0, {"message": "Request timed out after 10s"}
    except aiohttp.ClientError as e:
        return -1, {"message": f"Network error: {e}"}

def gh_error_text(status, data):
    msg = data.get("message", "Unknown error") if isinstance(data, dict) else "Unknown error"
    if status == 0:
        return "⏱️ Timeout: GitHub did not respond within 10 seconds."
    if status == -1:
        return f"🌐 Network error: {msg}"
    mapping = {
        401: "🔑 401 Unauthorized — Invalid or expired PAT.",
        403: "🚫 403 Forbidden — Rate limited or insufficient scope.",
        404: "❓ 404 Not Found — Resource does not exist.",
        409: "⚠️ 409 Conflict — SHA mismatch, refetch and retry.",
        422: f"⚠️ 422 Unprocessable — {msg}",
    }
    return mapping.get(status, f"❌ Error {status}: {msg}")

async def gh_validate_token(token):
    status, data = await gh_request("GET", "/user", token)
    if status == 200:
        return True, data.get("login"), None
    return False, None, gh_error_text(status, data)

async def gh_list_repos(token):
    status, data = await gh_request("GET", "/user/repos", token, params={"per_page": 100, "sort": "updated"})
    if status == 200:
        return data, None
    return [], gh_error_text(status, data)

async def gh_create_repo(token, name, private):
    payload = {"name": name, "private": private, "auto_init": True}
    status, data = await gh_request("POST", "/user/repos", token, json_data=payload)
    if status == 201:
        return data, None
    return None, gh_error_text(status, data)

async def gh_get_contents(token, full_repo, path=""):
    status, data = await gh_request("GET", f"/repos/{full_repo}/contents/{path}", token)
    if status == 200:
        return data, None
    return None, gh_error_text(status, data)

async def gh_put_file(token, full_repo, path, content_str, message, sha=None):
    encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    status, data = await gh_request("PUT", f"/repos/{full_repo}/contents/{path}", token, json_data=payload)
    if status in (200, 201):
        return data, None
    return None, gh_error_text(status, data)

# ============================================================
# /start
# ============================================================
@client.on(events.NewMessage(pattern=r"^/start$"))
@owner_only
async def start_handler(event):
    await event.respond(
        "🐙 **GitHub Workspace Engine Bot**\n\n"
        "/add_account — Link your GitHub PAT\n"
        "/new_repo — Create a new repository\n"
        "/switch_repo — Switch active repository\n"
        "/files — Browse active repo files\n"
        "/create_file — Create a new file\n"
        "/edit_file — Edit an existing file\n"
        "/append_file — Append text to a file\n"
        "/whoami — Show current account & active repo",
        parse_mode="markdown",
    )

# ============================================================
# /add_account
# ============================================================
@client.on(events.NewMessage(pattern=r"^/add_account$"))
@owner_only
async def add_account_handler(event):
    chat_id = event.chat_id
    try:
        async with client.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("👤 Send owner id:")
            resp = await conv.get_response()
            try:
                owner_id_input = int(resp.raw_text.strip())
            except ValueError:
                await conv.send_message("❌ Invalid owner id format. Aborting.")
                return

            await conv.send_message(
                "✅ Format looks valid.\n🔑 Now send GitHub Personal Access Token (PAT):"
            )
            resp = await conv.get_response()
            pat = resp.raw_text.strip()

            await conv.send_message("⏳ Checking...")
            valid, username, err = await gh_validate_token(pat)
            if not valid:
                await conv.send_message(f"❌ Validation failed:\n{err}")
                return

            update_account(owner_id_input, github_pat=pat, github_username=username)
            await conv.send_message(
                f"🎉 **Account linked successfully!**\nGitHub user: `{username}`",
                parse_mode="markdown",
            )
    except asyncio.TimeoutError:
        await event.respond("⌛ Setup timed out. Run /add_account again.")
    except Exception as e:
        await event.respond(f"⚠️ Unexpected error: `{e}`", parse_mode="markdown")

# ============================================================
# /whoami
# ============================================================
@client.on(events.NewMessage(pattern=r"^/whoami$"))
@owner_only
async def whoami_handler(event):
    acc = get_account(OWNER_ID)
    if not acc:
        await event.respond("⚠️ No account linked. Use /add_account first.")
        return
    active_repo = acc.get("active_repo", "None")
    await event.respond(
        f"👤 GitHub user: `{acc.get('github_username')}`\n📦 Active repo: `{active_repo}`",
        parse_mode="markdown",
    )

# ============================================================
# /new_repo
# ============================================================
@client.on(events.NewMessage(pattern=r"^/new_repo$"))
@owner_only
async def new_repo_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return

    chat_id = event.chat_id
    token = acc["github_pat"]
    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            await conv.send_message("📦 Send new repository name:")
            resp = await conv.get_response()
            repo_name = resp.raw_text.strip()
            if not repo_name:
                await conv.send_message("❌ Invalid repo name. Aborting.")
                return

            await conv.send_message(
                "🔒 Choose privacy:",
                buttons=[
                    [Button.inline("🌐 Public", b"privacy:public"), Button.inline("🔒 Private", b"privacy:private")]
                ],
            )
            cb_event = await conv.wait_event(events.CallbackQuery(func=lambda e: e.sender_id == OWNER_ID))
            privacy = cb_event.data.decode().split(":")[1]
            await cb_event.answer()
            is_private = privacy == "private"

            await conv.send_message(f"⏳ Creating repository `{repo_name}` ({privacy})...", parse_mode="markdown")
            data, err = await gh_create_repo(token, repo_name, is_private)
            if err:
                await conv.send_message(f"❌ Failed to create repo:\n{err}")
                return

            full_name = data.get("full_name")
            update_account(OWNER_ID, active_repo=full_name, active_path="")
            await conv.send_message(
                f"✅ Repository created: `{full_name}`\n🔄 Set as Active Repo.",
                parse_mode="markdown",
            )
    except asyncio.TimeoutError:
        await event.respond("⌛ Timed out. Run /new_repo again.")
    except Exception as e:
        await event.respond(f"⚠️ Error: `{e}`", parse_mode="markdown")

# ============================================================
# /switch_repo (paginated, 6 per page)
# ============================================================
REPOS_PER_PAGE = 6

async def render_repo_page(event, repos, page, edit=False):
    total_pages = max(1, (len(repos) + REPOS_PER_PAGE - 1) // REPOS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * REPOS_PER_PAGE
    chunk = repos[start:start + REPOS_PER_PAGE]

    buttons = []
    for repo in chunk:
        full_name = repo.get("full_name")
        buttons.append([Button.inline(f"📦 {full_name}", cb_data("repo", full_name))])

    nav_row = []
    if page > 0:
        nav_row.append(Button.inline("⬅️ Prev", cb_data("repopage", page - 1)))
    if page < total_pages - 1:
        nav_row.append(Button.inline("➡️ Next", cb_data("repopage", page + 1)))
    if nav_row:
        buttons.append(nav_row)

    text = f"📂 Select repository to activate: (Page {page + 1}/{total_pages})"
    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)

@client.on(events.NewMessage(pattern=r"^/switch_repo$"))
@owner_only
async def switch_repo_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return

    msg = await event.respond("⏳ Fetching repositories...")
    repos, err = await gh_list_repos(acc["github_pat"])
    if err:
        await msg.edit(f"❌ {err}")
        return
    if not repos:
        await msg.edit("📭 No repositories found.")
        return

    REPO_CACHE["repos"] = repos
    await render_repo_page(msg, repos, 0, edit=True)

REPO_CACHE = {}

@client.on(events.CallbackQuery(pattern=rb"^repopage:"))
@owner_only
async def repo_page_callback(event):
    token = event.data.decode().split(":", 1)[1]
    page = cb_get(token)
    if page is None:
        await event.answer("⚠️ Expired. Run /switch_repo again.", alert=True)
        return
    repos = REPO_CACHE.get("repos")
    if not repos:
        await event.answer("⚠️ Session expired. Run /switch_repo again.", alert=True)
        return
    await event.answer()
    await render_repo_page(event, repos, page, edit=True)

@client.on(events.CallbackQuery(pattern=rb"^repo:"))
@owner_only
async def repo_switch_callback(event):
    token = event.data.decode().split(":", 1)[1]
    full_name = cb_get(token)
    if not full_name:
        await event.answer("⚠️ Selection expired.", alert=True)
        return
    update_account(OWNER_ID, active_repo=full_name, active_path="")
    await event.answer()
    await event.respond(f"🔄 Workspace switched to: **{full_name}**", parse_mode="markdown")

# ============================================================
# /files - Browse active repo
# ============================================================
def build_file_buttons(items, current_path):
    buttons = []
    for item in items:
        name = item.get("name")
        path = item.get("path")
        if item.get("type") == "dir":
            buttons.append([Button.inline(f"📁 {name}", cb_data("nav", path))])
        else:
            buttons.append([Button.inline(f"📄 {name}", cb_data("file", path))])
    if current_path:
        parent = "/".join(current_path.split("/")[:-1])
        buttons.append([Button.inline("⬅️ Back", cb_data("nav", parent))])
    return buttons

async def render_directory(event_or_msg, full_repo, token, path, edit=False):
    data, err = await gh_get_contents(token, full_repo, path)
    if err:
        target = event_or_msg
        text = f"❌ {err}"
        if edit:
            await target.edit(text)
        else:
            await target.respond(text)
        return
    if not isinstance(data, list):
        data = [data]
    buttons = build_file_buttons(data, path)
    label = path if path else "/ (root)"
    text = f"📂 **{full_repo}**\nPath: `{label}`"
    if edit:
        await event_or_msg.edit(text, buttons=buttons, parse_mode="markdown")
    else:
        await event_or_msg.respond(text, buttons=buttons, parse_mode="markdown")

@client.on(events.NewMessage(pattern=r"^/files$"))
@owner_only
async def files_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return
    active_repo = acc.get("active_repo")
    if not active_repo:
        await event.respond("⚠️ No active repository. Use /switch_repo or /new_repo first.")
        return
    msg = await event.respond("⏳ Loading...")
    await render_directory(msg, active_repo, acc["github_pat"], acc.get("active_path", ""), edit=True)

@client.on(events.CallbackQuery(pattern=rb"^nav:"))
@owner_only
async def nav_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    if path is None:
        await event.answer("⚠️ Expired. Run /files again.", alert=True)
        return
    acc = get_account(OWNER_ID)
    set_active_path(OWNER_ID, path)
    await event.answer()
    await render_directory(event, acc["active_repo"], acc["github_pat"], path, edit=True)

@client.on(events.CallbackQuery(pattern=rb"^file:"))
@owner_only
async def file_selected_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    if path is None:
        await event.answer("⚠️ Expired. Run /files again.", alert=True)
        return
    await event.answer()
    buttons = [
        [Button.inline("📄 View Content", cb_data("view", path))],
        [Button.inline("✏️ Edit File", cb_data("edit", path))],
        [Button.inline("➕ Append Text", cb_data("append", path))],
    ]
    await event.respond(f"🗂️ File: `{path}`\nChoose an action:", buttons=buttons, parse_mode="markdown")

@client.on(events.CallbackQuery(pattern=rb"^view:"))
@owner_only
async def view_file_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    if path is None:
        await event.answer("⚠️ Expired.", alert=True)
        return
    await event.answer()
    acc = get_account(OWNER_ID)
    data, err = await gh_get_contents(acc["github_pat"], acc["active_repo"], path)
    if err:
        await event.respond(f"❌ {err}")
        return
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as e:
        await event.respond(f"⚠️ Failed to decode file: {e}")
        return
    if len(content) > 3500:
        content = content[:3500] + "\n...[truncated]"
    await event.respond(f"📄 `{path}`\n\n```\n{content}\n```", parse_mode="markdown")

@client.on(events.CallbackQuery(pattern=rb"^edit:"))
@owner_only
async def edit_file_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    if path is None:
        await event.answer("⚠️ Expired.", alert=True)
        return
    await event.answer()
    await do_edit_flow(event.chat_id, path)

@client.on(events.CallbackQuery(pattern=rb"^append:"))
@owner_only
async def append_file_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    if path is None:
        await event.answer("⚠️ Expired.", alert=True)
        return
    await event.answer()
    await do_append_flow(event.chat_id, path)

# ============================================================
# /create_file
# ============================================================
@client.on(events.NewMessage(pattern=r"^/create_file$"))
@owner_only
async def create_file_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return
    active_repo = acc.get("active_repo")
    if not active_repo:
        await event.respond("⚠️ No active repository. Use /switch_repo or /new_repo first.")
        return

    chat_id = event.chat_id
    token = acc["github_pat"]
    try:
        async with client.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("📝 Send file path (e.g. `assets/config.json`):", parse_mode="markdown")
            resp = await conv.get_response()
            file_path = resp.raw_text.strip()
            if not file_path:
                await conv.send_message("❌ Invalid path. Aborting.")
                return

            await conv.send_message("✍️ Send file content:")
            resp = await conv.get_response()
            content = resp.raw_text

            await conv.send_message("⏳ Committing to GitHub...")
            data, err = await gh_put_file(token, active_repo, file_path, content, f"Create {file_path} via Telegram bot")
            if err:
                await conv.send_message(f"❌ {err}")
                return
            await conv.send_message(f"✅ File created: `{file_path}` in `{active_repo}`", parse_mode="markdown")
    except asyncio.TimeoutError:
        await event.respond("⌛ Timed out. Run /create_file again.")
    except Exception as e:
        await event.respond(f"⚠️ Error: `{e}`", parse_mode="markdown")

# ============================================================
# /edit_file (command form asks for path)
# ============================================================
@client.on(events.NewMessage(pattern=r"^/edit_file$"))
@owner_only
async def edit_file_command(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return
    if not acc.get("active_repo"):
        await event.respond("⚠️ No active repository. Use /switch_repo or /new_repo first.")
        return

    chat_id = event.chat_id
    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            await conv.send_message("📝 Send the file path to edit:")
            resp = await conv.get_response()
            path = resp.raw_text.strip()
    except asyncio.TimeoutError:
        await event.respond("⌛ Timed out.")
        return

    await do_edit_flow(chat_id, path)

async def do_edit_flow(chat_id, path):
    acc = get_account(OWNER_ID)
    token = acc["github_pat"]
    full_repo = acc["active_repo"]
    try:
        async with client.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"✏️ Send the completely new content for `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            new_content = resp.raw_text

            await conv.send_message("⏳ Fetching current file SHA...")
            data, err = await gh_get_contents(token, full_repo, path)
            if err:
                await conv.send_message(f"❌ {err}")
                return
            sha = data.get("sha")

            await conv.send_message("⏳ Pushing update...")
            result, err = await gh_put_file(token, full_repo, path, new_content, f"Edit {path} via Telegram bot", sha=sha)
            if err:
                await conv.send_message(f"❌ {err}")
                return
            await conv.send_message(f"✅ File updated: `{path}`", parse_mode="markdown")
    except asyncio.TimeoutError:
        await client.send_message(chat_id, "⌛ Edit timed out.")
    except Exception as e:
        await client.send_message(chat_id, f"⚠️ Error: `{e}`", parse_mode="markdown")

# ============================================================
# /append_file (command form asks for path)
# ============================================================
@client.on(events.NewMessage(pattern=r"^/append_file$"))
@owner_only
async def append_file_command(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("github_pat"):
        await event.respond("⚠️ No GitHub PAT linked. Use /add_account first.")
        return
    if not acc.get("active_repo"):
        await event.respond("⚠️ No active repository. Use /switch_repo or /new_repo first.")
        return

    chat_id = event.chat_id
    try:
        async with client.conversation(chat_id, timeout=120) as conv:
            await conv.send_message("📝 Send the file path to append to:")
            resp = await conv.get_response()
            path = resp.raw_text.strip()
    except asyncio.TimeoutError:
        await event.respond("⌛ Timed out.")
        return

    await do_append_flow(chat_id, path)

async def do_append_flow(chat_id, path):
    acc = get_account(OWNER_ID)
    token = acc["github_pat"]
    full_repo = acc["active_repo"]
    try:
        async with client.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"➕ Send text to append to `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            append_text = resp.raw_text

            await conv.send_message("⏳ Fetching existing content...")
            data, err = await gh_get_contents(token, full_repo, path)
            if err:
                await conv.send_message(f"❌ {err}")
                return
            sha = data.get("sha")
            try:
                existing = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                existing = ""

            new_content = existing.rstrip("\n") + "\n" + append_text

            await conv.send_message("⏳ Pushing updated content...")
            result, err = await gh_put_file(token, full_repo, path, new_content, f"Append to {path} via Telegram bot", sha=sha)
            if err:
                await conv.send_message(f"❌ {err}")
                return
            await conv.send_message(f"✅ Appended to file: `{path}`", parse_mode="markdown")
    except asyncio.TimeoutError:
        await client.send_message(chat_id, "⌛ Append timed out.")
    except Exception as e:
        await client.send_message(chat_id, f"⚠️ Error: `{e}`", parse_mode="markdown")

# ============================================================
# GLOBAL ERROR HANDLER FOR CALLBACKS/MESSAGES
# ============================================================
@client.on(events.CallbackQuery())
async def fallback_callback_guard(event):
    if event.sender_id != OWNER_ID:
        await event.answer("⛔ Access Denied.", alert=True)

# ============================================================
# STARTUP
# ============================================================
async def main():
    await start_web_server()
    await client.start(bot_token=BOT_TOKEN)
    log.info("🤖 GitHub Workspace Engine Bot is running.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    loop.run_until_complete(main())
