# MCP Server — Moodle Tools (Windows)

Exposes Moodle Web Service functions over MCP to be consumed from **Claude Desktop**:
- `search_courses`
- `confirm_course_by_id`
- `get_forums_by_course_id`
- `list_forum_discussions`
- `get_discussion_posts`
- `summarize_discussion` *(optional, needs `OPENAI_API_KEY`)*

---

## Requirements

- **Windows 10/11**
- Python **3.10+** (3.11/3.12 recommended)
- [`uv`](https://docs.astral.sh/uv/) installed
- A Moodle site with:
  - **Web services enabled**
  - **REST protocol enabled**
  - A **Web Service token** for a user with rights to read courses and forums
- Claude Desktop (latest)

---

## 1. Install `uv` on Windows

Open **PowerShell** and run:

```powershell
iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex
uv --version
```

---

## 2. Enable Web Services in Moodle

1. **Log in as an admin**.

2. Navigate to:  
   `Site administration → Advanced features`  
   - ✅ Check **Enable web services**.  
   - Save changes.

3. Go to:  
   `Site administration → Plugins → Web services → Manage protocols`  
   - ✅ Enable **REST protocol**.

4. *(Optional, recommended)* **Create a restricted role** with only the needed capabilities:
   - `moodle/course:view`
   - `mod/forum:viewdiscussion`
   - `mod/forum:viewhiddentimedposts` *(if you want hidden/timed posts)*
   - `mod/forum:viewqandawithoutposting` *(if applicable)*

5. Assign this role to the account you’ll use for the token in each course where you need access.

6. **Create a service**:  
   `Site administration → Plugins → Web services → External services`  
   - Add a new service, e.g., **MCP Forum Tools**.  
   - Mark it as **enabled**.  
   - Add the required functions:
     - `core_course_search_courses`
     - `core_course_get_courses_by_field`
     - `mod_forum_get_forums_by_courses`
     - `mod_forum_get_forum_discussions`
     - `mod_forum_get_discussion_posts`

7. **Generate a token**:  
   `Site administration → Plugins → Web services → Manage tokens`  
   - Create a token for the user and service above.  
   - Copy this token — you’ll use it in `.env` as `MOODLE_TOKEN`.

---

## 3. Install the MCP server

```powershell
git clone <YOUR_MCP_SERVER_REPO_URL> mcp-moodle
cd mcp-moodle
cd server
uv init -p 3.11  # or your Python version
uv add mcp[server] httpx python-dotenv
# Optional (only if you'll use summarize_discussion on server):
uv add openai
```

---

## 4. Configure environment variables

Create `.env` from the example:

```powershell
cp .env.example .env
```

Edit `.env` in your text editor and set:

```env
MOODLE_BASE_URL=https://your-moodle.example.com
MOODLE_TOKEN=your_ws_token
# Optional for AI summaries:
# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o-mini
```

---

## 5. Run standalone (local test)

From the repo root:

```powershell
uv run server.py
```

Expected: logs like “Initializing MCP server…” and the process stays running.

---

## 6. Connect to Claude Desktop

### Download Claude Desktop for Windows
Go to [https://claude.ai/download](https://claude.ai/download) and download the Windows installer.  
Run the installer and open Claude Desktop.

### Enable Developer Mode & Edit Config
1. In Claude Desktop, click on **File → Settings**.  
2. Go to the **Developer** section.  
3. Click **Get Started** or **Edit Config**.  
4. A window will open showing the configuration file.

### Modify the JSON Config
Locate the `claude_desktop_config.json` file (default path):

```
%APPDATA%\Claude\claude_desktop_config.json
```

Add your MCP server entry like this:

```json
{
  "mcpServers": {
    "mcp_forum_summarizer": {
      "command": "uv",
      "args": [
        "--directory",
        "D:\\server",
        "run",
        "server.py"
      ]
    }
  }
}
```

**Important for Windows**: use double backslashes (`\\`) in paths.  
Replace `D:\\server` with the full path to your MCP server folder.

### Restart Claude Desktop
- Go to **File → Exit** to fully close Claude Desktop.  
- Open it again.

### Verify MCP Server is Active
- Click on **Search & Tools** in Claude Desktop.  
- You should see `mcp_forum_summarizer` listed.  
- There should be **6 tools** available (coming from `server.py`).

---

## 7. Using it in Claude Desktop

You can now interact with your Moodle MCP server via Claude Desktop.

Example starting prompt:
```
Hey, can you get me a summary of all forums within the X course?
```

Or run tools step-by-step:

### Search for a course
```
search_courses(query="biology", as_json=false)
```
→ Pick `course_id`

### Confirm course
```
confirm_course_by_id(course_id=42)
```

### List forums
```
get_forums_by_course_id(course_id=42)
```
→ Pick `forum_id`

### List discussions
```
list_forum_discussions(forum_id=1234)
```
→ Pick `discussion_id`

### Fetch posts
```
get_discussion_posts(discussion_id=5678, as_json=true)
```

### (Optional) Summarize
```
summarize_discussion(discussion_id=5678, focus="themes, unresolved questions, next steps")
```

---

## Endpoints used

- `core_course_search_courses`
- `core_course_get_courses_by_field`
- `mod_forum_get_forums_by_courses`
- `mod_forum_get_forum_discussions`
- `mod_forum_get_discussion_posts`

---

## Troubleshooting

- **Tool not visible in Claude** → check config path/JSON syntax, restart Claude.
- **`spawn uv ENOENT`** → install `uv` or fix the path in config.
- **Moodle error** → check token permissions and `MOODLE_BASE_URL`.
- **forum_id vs discussion_id confusion** → `get_discussion_posts` warns if you pass the wrong ID.
