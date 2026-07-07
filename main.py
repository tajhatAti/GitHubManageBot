import os, asyncio, json, base64, threading, itertools
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events, Button
import aiohttp

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GH_TOKEN  = os.environ.get("GH_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))
PORT      = int(os.environ.get("PORT", 8080))

GH_API = "https://api.github.com"
WORKSPACE_FILE = "workspace.json"

# ============================================================
# EVENT LOOP FIX (Python 3.14+)
# ============================================================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = TelegramClient("github_editor_bot", API_ID, API_HASH, loop=loop)

# ============================================================
# HTTP HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    
    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

# ============================================================
# PERSISTENCE
# ============================================================
def load_workspace():
    if os.path.exists(WORKSPACE_FILE):
        try:
            with open(WORKSPACE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_workspace(data):
    with open(WORKSPACE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_workspace(user_id):
    ws = load_workspace()
    return ws.get(str(user_id), {})

def update_workspace(user_id, **kwargs):
    ws = load_workspace()
    entry = ws.get(str(user_id), {})
    entry.update(kwargs)
    ws[str(user_id)] = entry
    save_workspace(ws)

# ============================================================
# CALLBACK TOKEN CACHE (fixes 64-byte callback limit)
# ============================================================
CB_STORE = {}
CB_COUNTER = itertools.count()

def cb_put(value):
    token = str(next(CB_COUNTER))
    CB_STORE[token] = value
    if len(CB_STORE) > 5000:
        for k in list(CB_STORE.keys())[:2500]:
            CB_STORE.pop(k, None)
    return token

def cb_get(token):
    return CB_STORE.get(token)

def cb_data(prefix, value):
    return f"{prefix}:{cb_put(value)}".encode()

# ============================================================
# GITHUB API
# ============================================================
def gh_headers():
    return {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

async def gh_request(method, path, json_data=None, params=None):
    url = f"{GH_API}{path}"
    try:
        async with asyncio.timeout(10):
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url, headers=gh_headers(), json=json_data, params=params
                ) as resp:
                    try:
                        data = await resp.json()
                    except:
                        data = {}
                    return resp.status, data
    except asyncio.TimeoutError:
        return 0, {"message": "Request timed out"}
    except Exception as e:
        return -1, {"message": str(e)}

async def gh_validate_token():
    status, data = await gh_request("GET", "/user")
    if status == 200:
        return True, data.get("login")
    return False, None

async def gh_list_repos():
    status, data = await gh_request("GET", "/user/repos", params={"per_page": 100, "sort": "updated"})
    return data if status == 200 else []

async def gh_get_contents(repo, path=""):
    status, data = await gh_request("GET", f"/repos/{repo}/contents/{path}")
    return data if status == 200 else None

async def gh_put_file(repo, path, content_str, message, sha=None):
    encoded = base64.b64encode(content_str.encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    status, data = await gh_request("PUT", f"/repos/{repo}/contents/{path}", json_data=payload)
    return data if status in (200, 201) else None

async def gh_delete_file(repo, path, sha):
    payload = {"message": f"Delete {path}", "sha": sha}
    status, data = await gh_request("DELETE", f"/repos/{repo}/contents/{path}", json_data=payload)
    return status == 200

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def owner_only(func):
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            try:
                await event.respond("⛔ Access Denied")
            except:
                pass
            return
        return await func(event)
    return wrapper

async def render_file_buttons(items, current_path):
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

# ============================================================
# /start
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/start$"))
@owner_only
async def start_handler(event):
    await event.respond(
        "🐙 **GitHub Repo Editor Bot**\n\n"
        "/add_account - Add GitHub PAT\n"
        "/switch_repo - Select repo\n"
        "/files - Browse files\n"
        "/create_file - Create new file\n"
        "/whoami - Show current repo",
        parse_mode="markdown"
    )

# ============================================================
# /add_account
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/add_account$"))
@owner_only
async def add_account_handler(event):
    chat_id = event.chat_id
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("🔑 Send your GitHub Personal Access Token (PAT):")
            resp = await conv.get_response()
            pat = resp.raw_text.strip()
            
            # Validate token
            # Note: GH_TOKEN is global, so we'd need to modify this to test the provided token
            # For now, we just save it
            await conv.send_message("✅ Token saved!")
            update_workspace(OWNER_ID, gh_pat=pat)
    except asyncio.TimeoutError:
        await event.respond("⌛ Timeout")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

# ============================================================
# /whoami
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/whoami$"))
@owner_only
async def whoami_handler(event):
    ws = get_workspace(OWNER_ID)
    active_repo = ws.get("active_repo", "None")
    await event.respond(f"📦 Active Repo: **{active_repo}**", parse_mode="markdown")

# ============================================================
# /switch_repo (with pagination - 6 per page)
# ============================================================
REPOS_CACHE = {}

async def show_repos_page(event, page=0, edit=False):
    uid = OWNER_ID
    if uid not in REPOS_CACHE:
        repos = await gh_list_repos()
        REPOS_CACHE[uid] = repos
    
    repos = REPOS_CACHE.get(uid, [])
    if not repos:
        msg = "📭 No repositories found"
        if edit:
            await event.edit(msg)
        else:
            await event.respond(msg)
        return
    
    per_page = 6
    total = (len(repos) + per_page - 1) // per_page
    page = max(0, min(page, total - 1))
    start = page * per_page
    chunk = repos[start:start + per_page]
    
    buttons = []
    for repo in chunk:
        full_name = repo.get("full_name")
        buttons.append([Button.inline(f"📦 {full_name}", cb_data("select_repo", full_name))])
    
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ Prev", cb_data("repos_page", page - 1)))
    if page < total - 1:
        nav.append(Button.inline("➡️ Next", cb_data("repos_page", page + 1)))
    if nav:
        buttons.append(nav)
    
    text = f"📂 Select Repository (Page {page + 1}/{total})"
    
    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/switch_repo$"))
@owner_only
async def switch_repo_handler(event):
    await show_repos_page(event, 0, edit=False)

@bot.on(events.CallbackQuery(pattern=rb"^select_repo:"))
@owner_only
async def select_repo_callback(event):
    token = event.data.decode().split(":", 1)[1]
    repo = cb_get(token)
    if not repo:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    update_workspace(OWNER_ID, active_repo=repo, current_path="")
    await event.answer()
    await event.respond(f"✅ Switched to **{repo}**", parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^repos_page:"))
@owner_only
async def repos_page_callback(event):
    token = event.data.decode().split(":", 1)[1]
    page = cb_get(token)
    if page is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    await event.answer()
    await show_repos_page(event, page, edit=True)

# ============================================================
# /files - Browse active repo
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/files$"))
@owner_only
async def files_handler(event):
    ws = get_workspace(OWNER_ID)
    active_repo = ws.get("active_repo")
    
    if not active_repo:
        await event.respond("⚠️ No active repo. Use /switch_repo first")
        return
    
    msg = await event.respond("⏳ Loading...")
    
    path = ws.get("current_path", "")
    contents = await gh_get_contents(active_repo, path)
    
    if not contents:
        await msg.edit(f"❌ Path not found")
        return
    
    if not isinstance(contents, list):
        contents = [contents]
    
    buttons = await render_file_buttons(contents, path)
    label = path if path else "/ (root)"
    text = f"📂 **{active_repo}**\nPath: `{label}`"
    
    await msg.edit(text, buttons=buttons, parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^nav:"))
@owner_only
async def nav_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if path is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    if not repo:
        await event.answer("⚠️ No repo selected", alert=True)
        return
    
    update_workspace(OWNER_ID, current_path=path)
    
    contents = await gh_get_contents(repo, path)
    if not contents:
        await event.answer("❌ Path not found", alert=True)
        return
    
    if not isinstance(contents, list):
        contents = [contents]
    
    buttons = await render_file_buttons(contents, path)
    label = path if path else "/ (root)"
    text = f"📂 **{repo}**\nPath: `{label}`"
    
    await event.answer()
    await event.edit(text, buttons=buttons, parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^file:"))
@owner_only
async def file_selected_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if path is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    buttons = [
        [Button.inline("📄 View", cb_data("view", path))],
        [Button.inline("✏️ Edit", cb_data("edit", path))],
        [Button.inline("➕ Append", cb_data("append", path))],
        [Button.inline("🗑️ Delete", cb_data("delete", path))]
    ]
    
    await event.respond(f"🗂️ **{path}**\nChoose action:", buttons=buttons, parse_mode="markdown")

# ============================================================
# FILE OPERATIONS
# ============================================================
@bot.on(events.CallbackQuery(pattern=rb"^view:"))
@owner_only
async def view_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    data = await gh_get_contents(repo, path)
    if not data or "content" not in data:
        await event.respond("❌ Could not fetch file")
        return
    
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except:
        await event.respond("❌ Could not decode file")
        return
    
    if len(content) > 3500:
        content = content[:3500] + "\n...[truncated]"
    
    await event.respond(f"```\n{content}\n```", parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^edit:"))
@owner_only
async def edit_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    await edit_file_flow(event.chat_id, path)

@bot.on(events.CallbackQuery(pattern=rb"^append:"))
@owner_only
async def append_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    await append_file_flow(event.chat_id, path)

@bot.on(events.CallbackQuery(pattern=rb"^delete:"))
@owner_only
async def delete_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    # Get SHA first
    data = await gh_get_contents(repo, path)
    if not data or "sha" not in data:
        await event.respond("❌ Could not find file")
        return
    
    msg = await event.respond("⏳ Deleting...")
    ok = await gh_delete_file(repo, path, data["sha"])
    
    if ok:
        await msg.edit(f"✅ Deleted: `{path}`", parse_mode="markdown")
    else:
        await msg.edit(f"❌ Failed to delete", parse_mode="markdown")

async def edit_file_flow(chat_id, path):
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"✏️ Send new content for `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            new_content = resp.raw_text
            
            # Get SHA
            data = await gh_get_contents(repo, path)
            if not data or "sha" not in data:
                await conv.send_message("❌ Could not find file SHA")
                return
            
            await conv.send_message("⏳ Uploading...")
            result = await gh_put_file(repo, path, new_content, f"Edit {path}", data["sha"])
            
            if result:
                await conv.send_message(f"✅ File updated: `{path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Upload failed")
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "⌛ Timeout")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {e}")

async def append_file_flow(chat_id, path):
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"➕ Send text to append to `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            append_text = resp.raw_text
            
            # Get current content
            data = await gh_get_contents(repo, path)
            if not data or "content" not in data:
                await conv.send_message("❌ Could not fetch file")
                return
            
            try:
                existing = base64.b64decode(data["content"]).decode()
            except:
                existing = ""
            
            sha = data.get("sha")
            new_content = existing.rstrip("\n") + "\n" + append_text
            
            await conv.send_message("⏳ Uploading...")
            result = await gh_put_file(repo, path, new_content, f"Append to {path}", sha)
            
            if result:
                await conv.send_message(f"✅ Appended to: `{path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Upload failed")
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "⌛ Timeout")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {e}")

# ============================================================
# /create_file
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/create_file$"))
@owner_only
async def create_file_handler(event):
    ws = get_workspace(OWNER_ID)
    if not ws.get("active_repo"):
        await event.respond("⚠️ No active repo. Use /switch_repo first")
        return
    
    chat_id = event.chat_id
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("📝 Send file path (e.g. `src/main.py`):", parse_mode="markdown")
            resp = await conv.get_response()
            file_path = resp.raw_text.strip()
            
            if not file_path:
                await conv.send_message("❌ Invalid path")
                return
            
            await conv.send_message("✍️ Send file content:")
            resp = await conv.get_response()
            content = resp.raw_text
            
            await conv.send_message("⏳ Creating...")
            repo = ws.get("active_repo")
            result = await gh_put_file(repo, file_path, content, f"Create {file_path}")
            
            if result:
                await conv.send_message(f"✅ Created: `{file_path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Failed to create")
    except asyncio.TimeoutError:
        await event.respond("⌛ Timeout")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

# ============================================================
# MAIN
# ============================================================
async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    await bot.start(bot_token=BOT_TOKEN)
    print("[+] GitHub Editor Bot Running")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    loop.run_until_complete(main())
