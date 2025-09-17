#!/usr/bin/env python3
"""
MCP Server (FastMCP) — Moodle forum workflow with logging
Tools:
  - search_courses(query, page=0, perpage=50, as_json=False)
  - confirm_course_by_id(course_id, as_json=False)
  - get_forums_by_course_id(course_id, as_json=False)
  - list_forum_discussions(forum_id, as_json=False)
  - get_discussion_posts(discussion_id, as_json=True)  # clean authors+messages

Env:
  MOODLE_BASE_URL=https://your-moodle.example.com
  MOODLE_TOKEN=your_ws_token
Logging (optional):
  LOG_LEVEL=INFO|DEBUG|WARNING|ERROR
  LOG_JSON=true|false
"""

from __future__ import annotations
from typing import Any, Dict, List
import os, re, html, json, time, logging
import httpx
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

# NEW: HTTP server bits
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route, Mount

# -------------------- Logging --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = os.getenv("LOG_JSON", "false").lower() in ("1","true","yes")

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "__dict__", {}).items():
            if k not in ("levelname","name","msg","args","exc_info","exc_text",
                         "stack_info","lineno","pathname","filename","module",
                         "created","msecs","relativeCreated","thread","threadName",
                         "processName","process","asctime"):
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False)

def _setup_logging() -> None:
    handler = logging.StreamHandler()
    if LOG_JSON:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)

_setup_logging()
log = logging.getLogger("moodle_mcp")

def _redact(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    return value[:keep] + "…" if len(value) > keep else "…"


# -------------------- Config --------------------
load_dotenv()  # load .env if present

MOODLE_BASE_URL = os.getenv("MOODLE_BASE_URL", "").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "")
API_ENDPOINT = f"{MOODLE_BASE_URL}/webservice/rest/server.php" if MOODLE_BASE_URL else ""
CONDUIT_TOKEN = os.getenv("CONDUIT_TOKEN", "").strip()

mcp = FastMCP("moodle")

def _config_ok() -> bool:
    ok = bool(MOODLE_BASE_URL and MOODLE_TOKEN and API_ENDPOINT)
    if not ok:
        log.error("Missing configuration", extra={
            "MOODLE_BASE_URL_set": bool(MOODLE_BASE_URL),
            "MOODLE_TOKEN_set": bool(MOODLE_TOKEN),
            "API_ENDPOINT_set": bool(API_ENDPOINT),
        })
    return ok

# -------------------- Helpers -------------------
async def moodle_request(wsfunction: str, **params: Any) -> Any:
    if not _config_ok():
        return {"error": "Missing MOODLE_BASE_URL or MOODLE_TOKEN in environment."}
    payload = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        **params,
    }
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(API_ENDPOINT, data=payload, timeout=60.0)
            duration = round((time.perf_counter() - start) * 1000, 1)
            log.debug("Moodle request",
                      extra={"wsfunction": wsfunction,
                             "status_code": r.status_code,
                             "duration_ms": duration,
                             "endpoint": API_ENDPOINT,
                             "params_keys": list(params.keys()),
                             "token": _redact(MOODLE_TOKEN)})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("exception"):
                msg = data.get("message")
                log.warning("Moodle API exception",
                            extra={"wsfunction": wsfunction,
                                   "duration_ms": duration,
                                   "message": msg})
                return {"error": f"Moodle error: {msg}"}
            return data
    except httpx.HTTPStatusError as e:
        duration = round((time.perf_counter() - start) * 1000, 1)
        log.error("HTTP status error",
                  extra={"wsfunction": wsfunction,
                         "duration_ms": duration,
                         "status_code": getattr(e.response, "status_code", None)})
        return {"error": f"HTTP error: {getattr(e.response,'status_code',None)}"}
    except Exception as e:
        duration = round((time.perf_counter() - start) * 1000, 1)
        log.exception("Moodle request failed",
                      extra={"wsfunction": wsfunction,
                             "duration_ms": duration})
        return {"error": str(e)}

def strip_html(s: str) -> str:
    if not isinstance(s, str): return ""
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p\s*>", "\n\n", s)
    s = re.sub(r"(?is)<.*?>", "", s)
    return html.unescape(s).strip()

def shorten(text: str, limit: int = 320) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"

def fmt_err(resp: Any) -> str | None:
    if isinstance(resp, dict) and "error" in resp:
        return f"Request failed: {resp['error']}"
    return None

# ---- Conduit helpers (reuses httpx + existing logger) ----

def _require_conduit_config() -> None:
    if not MOODLE_BASE_URL:
        raise EnvironmentError("Missing MOODLE_BASE_URL.")
    if not CONDUIT_TOKEN:
        raise EnvironmentError("Missing environment variable 'CONDUIT_TOKEN'.")

def _conduit_create_user_xml(
    username: str,
    email: str,
    password: str,
    firstname: str,
    lastname: str,
    suspended: int = 0,
) -> str:
    e = html.escape
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<data>
  <datum action="create">
    <mapping name="username">{e(username)}</mapping>
    <mapping name="email">{e(email)}</mapping>
    <mapping name="suspended">{int(bool(suspended))}</mapping>
    <mapping name="password">{e(password)}</mapping>
    <mapping name="firstname">{e(firstname)}</mapping>
    <mapping name="lastname">{e(lastname)}</mapping>
  </datum>
</data>"""

async def conduit_post_user_xml(xml: str) -> Dict[str, Any]:
    """
    POST to Conduit user endpoint. Conduit expects parameters in the query string,
    even for POST requests.
    """
    _require_conduit_config()
    url = f"{MOODLE_BASE_URL}/blocks/conduit/webservices/rest/user.php"
    params = {
        "method": "handle",
        "token": CONDUIT_TOKEN,
        "xml": xml,
    }
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, params=params, timeout=30.0)
            duration = round((time.perf_counter() - start) * 1000, 1)
            log.debug("Conduit request",
                      extra={"endpoint": url, "status_code": r.status_code,
                             "duration_ms": duration, "token": _redact(CONDUIT_TOKEN)})
            r.raise_for_status()
            text = r.text
            # Conduit often returns XML/text; we return it verbatim.
            return {"ok": True, "text": text}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"HTTP {getattr(e.response,'status_code',None)}"}
    except Exception as e:
        log.exception("Conduit request failed")
        return {"ok": False, "error": str(e)}


# -------------------- Tools ---------------------

@mcp.tool()
async def search_courses(query: str, page: int = 0, perpage: int = 50, as_json: bool = False) -> str:
    log.info("search_courses called", extra={"query": query, "page": page, "perpage": perpage})
    resp = await moodle_request(
        "core_course_search_courses",
        criterianame="search",
        criteriavalue=query,
        page=page,
        perpage=perpage,
    )
    if err := fmt_err(resp):
        log.warning("search_courses error", extra={"error": err})
        return err

    total = resp.get("total", 0) if isinstance(resp, dict) else 0
    courses: List[Dict[str, Any]] = resp.get("courses", []) if isinstance(resp, dict) else []
    log.info("search_courses result", extra={"returned": len(courses), "total": total})

    if not courses:
        return f"No matching courses. (total={total})"

    items = []
    for c in courses:
        items.append({
            "id": c.get("id"),
            "fullname": c.get("fullname") or c.get("displayname") or c.get("shortname") or "(unnamed)",
            "summary": shorten(strip_html(c.get("summary", "")))
        })

    if as_json:
        return json.dumps(items, ensure_ascii=False)

    lines = [f"Total (this page): {len(items)} / overall: {total}", "— Pick a course id —"]
    for it in items:
        line = f"[{it['id']}] {it['fullname']}"
        if it["summary"]:
            line += f" — {it['summary']}"
        lines.append(line)
    if (page + 1) * perpage < total:
        lines.append(f"(More available: call search_courses(query='{query}', page={page+1}))")
    return "\n".join(lines)

@mcp.tool()
async def confirm_course_by_id(course_id: int, as_json: bool = False) -> str:
    log.info("confirm_course_by_id called", extra={"course_id": course_id})
    resp = await moodle_request(
        "core_course_get_courses_by_field",
        field="id",
        value=str(course_id),  # Moodle expects a string
    )
    if err := fmt_err(resp):
        log.warning("confirm_course_by_id error", extra={"course_id": course_id, "error": err})
        return err

    courses = resp.get("courses", []) if isinstance(resp, dict) else []
    if not courses:
        log.info("confirm_course_by_id: not found", extra={"course_id": course_id})
        return f"No course found with id={course_id}."

    c = courses[0]
    item = {
        "id": c.get("id"),
        "fullname": c.get("fullname") or c.get("displayname") or c.get("shortname") or "(unnamed)",
        "shortname": c.get("shortname"),
        "categoryid": c.get("categoryid"),
        "categoryname": c.get("categoryname"),
        "visible": c.get("visible"),
        "startdate": c.get("startdate"),
        "enddate": c.get("enddate"),
        "summary": strip_html(c.get("summary", "")) or "",
        "format": c.get("format"),
        "lang": c.get("lang"),
        "enrollmentmethods": c.get("enrollmentmethods", []),
    }

    if as_json:
        return json.dumps(item, ensure_ascii=False)

    pretty = [
        f"[{item['id']}] {item['fullname']} ({item['shortname']})",
        f"Category: {item['categoryid']} ({item['categoryname']}) · Visible: {item['visible']}",
        f"Start: {item['startdate']} · End: {item['enddate']} · Format: {item['format']} · Lang: {item['lang']}",
    ]
    if item["enrollmentmethods"]:
        pretty.append(f"Enroll methods: {', '.join(item['enrollmentmethods'])}")
    if item["summary"]:
        pretty.append(f"Summary: {shorten(item['summary'])}")
    return "\n".join(pretty)

@mcp.tool()
async def get_forums_by_course_id(course_id: int, as_json: bool = False) -> str:
    log.info("get_forums_by_course_id called", extra={"course_id": course_id})
    check = await moodle_request("core_course_get_courses_by_field", field="id", value=str(course_id))
    if err := fmt_err(check):
        log.warning("course check failed", extra={"course_id": course_id, "error": err})
        return err
    if not isinstance(check, dict) or not check.get("courses"):
        log.info("course not found", extra={"course_id": course_id})
        return f"No course found with id={course_id}."

    resp = await moodle_request("mod_forum_get_forums_by_courses", **{"courseids[0]": course_id})
    if err := fmt_err(resp):
        log.warning("get_forums_by_course_id error", extra={"course_id": course_id, "error": err})
        return err

    forums = resp if isinstance(resp, list) else []
    log.info("forums fetched", extra={"course_id": course_id, "count": len(forums)})
    if not forums:
        return f"No forums found in course {course_id}."

    items = [{
        "forum_id": f.get("id"),
        "name": f.get("name"),
        "type": f.get("type"),
        "course": f.get("course"),
        "cmid": f.get("cmid"),
    } for f in forums]

    if as_json:
        return json.dumps(items, ensure_ascii=False)

    lines = [f"Forums in course {course_id} — pick a forum id:"]
    lines += [f"[{it['forum_id']}] {it['name']} · type={it['type']} · cmid={it['cmid']}" for it in items]
    return "\n".join(lines)

@mcp.tool()
async def list_forum_discussions(forum_id: int, as_json: bool = False) -> str:
    log.info("list_forum_discussions called", extra={"forum_id": forum_id})
    resp = await moodle_request("mod_forum_get_forum_discussions", forumid=forum_id)
    if err := fmt_err(resp):
        log.warning("list_forum_discussions error", extra={"forum_id": forum_id, "error": err})
        return err

    discussions = resp.get("discussions", []) if isinstance(resp, dict) else []
    log.info("discussions fetched", extra={"forum_id": forum_id, "count": len(discussions)})
    if not discussions:
        return f"No discussions found for forum {forum_id}."

    items = [{
        "forum_id": forum_id,
        "discussion_id": d.get("discussion"),
        "name": d.get("name"),
        "userfullname": d.get("userfullname"),
        "created": d.get("created"),
        "timesmodified": d.get("timemodified"),
        "numreplies": d.get("numreplies"),
    } for d in discussions]

    if as_json:
        return json.dumps(items, ensure_ascii=False)

    lines = [f"Discussions in forum {forum_id} — pick a discussion_id for get_discussion_posts(discussion_id=...):"]
    lines += [f"[discussion_id={it['discussion_id']}] {it['name']} · by {it['userfullname']} · replies={it['numreplies']}" for it in items]
    lines.append("\nNext: call get_discussion_posts(discussion_id=<one of the ids above>)")
    return "\n".join(lines)

@mcp.tool()
async def get_discussion_posts(discussion_id: int, as_json: bool = True) -> str:
    log.info("get_discussion_posts called", extra={"discussion_id": discussion_id})
    # SMART_ID_GUARD
    if os.getenv("SMART_ID_GUARD", "true").lower() in ("1","true","yes"):
        probe = await moodle_request("mod_forum_get_forum_discussions", forumid=discussion_id)
        if isinstance(probe, dict) and probe.get("discussions"):
            log.warning("SMART_ID_GUARD triggered (forum_id used as discussion_id)",
                        extra={"value": discussion_id})
            return (
                f"It looks like you passed a forum_id ({discussion_id}) to get_discussion_posts.\n"
                f"Please run list_forum_discussions(forum_id={discussion_id}) and choose a discussion_id, "
                "then call get_discussion_posts(discussion_id=<chosen_id>)."
            )

    resp = await moodle_request("mod_forum_get_discussion_posts", discussionid=discussion_id)
    if err := fmt_err(resp):
        if "Invalid parameter value" in err or "discussion" in err.lower():
            log.warning("get_discussion_posts invalid parameter",
                        extra={"discussion_id": discussion_id})
            return (
                f"{err}\n\n"
                "Tip: 'get_discussion_posts' needs a *discussion_id*, not a forum_id. "
                "Run 'list_forum_discussions(forum_id=...)' first and use the 'discussion_id' shown there."
            )
        log.warning("get_discussion_posts error", extra={"discussion_id": discussion_id, "error": err})
        return err

    posts = resp.get("posts", []) if isinstance(resp, dict) else (resp or [])
    log.info("posts fetched", extra={"discussion_id": discussion_id, "count": len(posts)})
    if not posts:
        return f"No posts found for discussion {discussion_id}."

    cleaned = []
    for p in posts:
        cleaned.append({
            "post_id": p.get("id"),
            "discussion_id": discussion_id,
            "author": p.get("author", {}).get("fullname", "Unknown"),
            "timecreated": p.get("timecreated"),
            "message": strip_html(p.get("message", "")),
        })

    if as_json:
        return json.dumps(cleaned, ensure_ascii=False)

    lines = [f"Posts in discussion {discussion_id}:"]
    for p in cleaned:
        preview = (p["message"][:240] + "…") if len(p["message"]) > 240 else p["message"]
        lines.append(f"- [post {p['post_id']}] {p['author']} @ {p['timecreated']}: {preview}")
    return "\n".join(lines)

@mcp.tool()
async def get_my_userid(as_json: bool = False) -> str:
    """
    Returns the current API user's Moodle user id (userid) using
    core_webservice_get_site_info.

    Args:
        as_json: If True, returns a JSON string like {"userid": 2, ...};
                 otherwise a short human-readable string.
    """
    log.info("get_my_userid called")
    resp = await moodle_request("core_webservice_get_site_info")
    if err := fmt_err(resp):
        log.warning("get_my_userid error", extra={"error": err})
        return err

    # Expecting a dict with 'userid' plus other fields
    userid = resp.get("userid")
    username = resp.get("username")
    fullname = resp.get("fullname")

    if userid is None:
        log.warning("get_my_userid missing userid in response")
        return "Could not determine userid from site info."

    if as_json:
        # return a compact JSON that also includes helpful context
        return json.dumps(
            {
                "userid": userid,
                "username": username,
                "fullname": fullname,
                "siteurl": resp.get("siteurl"),
            },
            ensure_ascii=False,
        )

    return f"userid={userid} (username={username}, fullname={fullname})"

@mcp.tool()
async def get_my_courses(as_json: bool = False) -> str:
    """
    Returns the list of courses the current API user is enrolled in,
    including id, fullname, startdate, and enddate.

    Args:
        as_json: If True, returns a JSON array of courses; otherwise
                 a human-readable list.

    Example:
        await get_my_courses()
        await get_my_courses(as_json=True)
    """
    log.info("get_my_courses called")

    # First, get my userid
    site_info = await moodle_request("core_webservice_get_site_info")
    if err := fmt_err(site_info):
        log.warning("get_my_courses site_info error", extra={"error": err})
        return err

    userid = site_info.get("userid")
    if not userid:
        return "Could not determine userid from site info."

    # Now, get my courses
    resp = await moodle_request("core_enrol_get_users_courses", userid=userid)
    if err := fmt_err(resp):
        log.warning("get_my_courses error", extra={"error": err})
        return err

    courses = resp if isinstance(resp, list) else []
    if not courses:
        return f"No courses found for userid={userid}."

    items = [
        {
            "id": c.get("id"),
            "fullname": c.get("fullname"),
            "startdate": c.get("startdate"),
            "enddate": c.get("enddate"),
        }
        for c in courses
    ]

    if as_json:
        return json.dumps(items, ensure_ascii=False)

    lines = [f"Courses for userid={userid}:"]
    for it in items:
        lines.append(
            f"[{it['id']}] {it['fullname']} "
            f"(start={it['startdate']}, end={it['enddate']})"
        )
    return "\n".join(lines)


'''
@mcp.tool()
async def conduit_create_user(
    username: str,
    email: str,
    password: str,
    firstname: str,
    lastname: str,
    suspended: int = 0,
    as_json: bool = False,
) -> str:
    """
    Create a Moodle user via the Conduit webservice.

    Args:
        username: Moodle username (unique).
        email: User email.
        password: Plaintext password (ensure policy allows this path).
        firstname: First name.
        lastname: Last name.
        suspended: 0 = active, 1 = suspended.
        as_json: If True, returns a JSON string; otherwise, human-readable text.
    """
    log.info("conduit_create_user called",
             extra={"username": username, "email": email,
                    "suspended": suspended, "base_url": MOODLE_BASE_URL})

    # Build XML and send with light retry for transient failures
    xml_payload = _conduit_create_user_xml(
        username=username,
        email=email,
        password=password,
        firstname=firstname,
        lastname=lastname,
        suspended=suspended,
    )

    last_err = None
    for attempt in range(1, 3 + 1):
        resp = await conduit_post_user_xml(xml_payload)
        if resp.get("ok"):
            text = resp.get("text", "")
            log.info("conduit_create_user success", extra={"attempt": attempt})
            if as_json:
                return json.dumps(
                    {"status": "success", "attempt": attempt, "username": username,
                     "email": email, "raw": text},
                    ensure_ascii=False
                )
            return (
                "✅ User creation submitted to Conduit.\n"
                f"Username: {username}\n"
                f"Email: {email}\n"
                f"Suspended: {int(bool(suspended))}\n"
                f"Response (first 500 chars): {text[:500]}"
            )
        last_err = resp.get("error") or "unknown error"
        log.warning("conduit_create_user transient error",
                    extra={"attempt": attempt, "error": last_err})
        await asyncio.sleep(0.6 * attempt)

    # Retries exhausted
    msg = f"Error: {last_err}"
    log.error("conduit_create_user error", extra={"error": msg})
    return json.dumps({"status": "error", "error": msg}, ensure_ascii=False) if as_json else msg
'''

# -------------------- HTTP app (fixed) ------------------
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
"""
from starlette.requests import Request
from starlette.responses import JSONResponse

API_GATE_KEY = os.getenv("API_GATE_KEY")

class KeyGateMiddleware:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        # Protect only /mcp; allow /healthz for probes
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            if scope["method"] != "OPTIONS" and API_GATE_KEY:
                req = Request(scope, receive=receive)
                key = req.headers.get("x-api-key")
                if key != API_GATE_KEY:
                    return await JSONResponse({"error":"forbidden"}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
"""
def _health(_request):
    return PlainTextResponse("ok", status_code=200)

def build_app():
    # Build the MCP Streamable HTTP app (this has its own lifespan)
    app = mcp.streamable_http_app()   # serves /mcp

    # Attach /healthz directly to THIS app's router (no outer wrapper)
    app.router.routes.append(Route("/healthz", _health, methods=["GET"]))

    # Add CORS middleware on the same app
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],                 # tighten for prod if needed
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],   # important for browser MCP clients
    )
    return app

@mcp.tool()
async def get_my_forum_posts(
    as_json: bool = False,
    max_courses: int | None = None,
    max_forums_per_course: int | None = None,
    max_discussions_per_forum: int | None = None,
    max_posts_per_discussion: int | None = None,
    preview_chars: int = 240,
) -> str:
    """
    Traverse: me -> my courses -> forums -> discussions -> posts.
    Returns a human-readable digest by default, or nested JSON if as_json=True.

    Args:
        as_json: Return a JSON string if True, else formatted text.
        max_courses: Limit number of courses processed (None = all).
        max_forums_per_course: Limit forums per course (None = all).
        max_discussions_per_forum: Limit discussions per forum (None = all).
        max_posts_per_discussion: Limit posts per discussion (None = all).
        preview_chars: Max chars per message in human output (ignored for JSON).
    """
    log.info("get_my_forum_posts called")

    # 1) Who am I?
    site_info = await moodle_request("core_webservice_get_site_info")
    if err := fmt_err(site_info):
        log.warning("get_my_forum_posts site_info error", extra={"error": err})
        return err

    userid = site_info.get("userid")
    username = site_info.get("username")
    fullname = site_info.get("fullname")
    if not userid:
        return "Could not determine userid from site info."

    # 2) My courses
    courses_resp = await moodle_request("core_enrol_get_users_courses", userid=userid)
    if err := fmt_err(courses_resp):
        log.warning("get_my_forum_posts courses error", extra={"error": err})
        return err
    courses = courses_resp if isinstance(courses_resp, list) else []
    if not courses:
        return f"No courses found for userid={userid}."

    if isinstance(max_courses, int):
        courses = courses[:max_courses]

    result_nested: List[Dict[str, Any]] = []
    lines: List[str] = []
    header = f"Forum posts digest for {fullname or username} (userid={userid})"
    lines.append(header)
    lines.append("=" * len(header))

    # 3) For each course → forums → discussions → posts
    for c_idx, course in enumerate(courses, start=1):
        course_id = course.get("id")
        course_name = course.get("fullname") or course.get("shortname") or f"(course {course_id})"

        # Forums in course
        forums_resp = await moodle_request("mod_forum_get_forums_by_courses", **{"courseids[0]": course_id})
        if err := fmt_err(forums_resp):
            log.warning("forums fetch error", extra={"course_id": course_id, "error": err})
            forum_items = []
        else:
            forum_items = forums_resp if isinstance(forums_resp, list) else []

        if isinstance(max_forums_per_course, int):
            forum_items = forum_items[:max_forums_per_course]

        course_entry: Dict[str, Any] = {
            "course_id": course_id,
            "course_fullname": course_name,
            "forums": []
        }
        lines.append(f"\nCourse [{course_id}] {course_name}")
        lines.append("-" * (len(course_name) + len(str(course_id)) + 10))

        if not forum_items:
            lines.append("  (No forums)")
            result_nested.append(course_entry)
            continue

        for f_idx, forum in enumerate(forum_items, start=1):
            forum_id = forum.get("id")
            forum_name = forum.get("name") or f"(forum {forum_id})"
            lines.append(f"  Forum [{forum_id}] {forum_name}")

            # Discussions in forum
            discussions_resp = await moodle_request("mod_forum_get_forum_discussions", forumid=forum_id)
            if err := fmt_err(discussions_resp):
                log.warning("discussions fetch error", extra={"forum_id": forum_id, "error": err})
                discussions = []
            else:
                discussions = discussions_resp.get("discussions", []) if isinstance(discussions_resp, dict) else []

            if isinstance(max_discussions_per_forum, int):
                discussions = discussions[:max_discussions_per_forum]

            forum_entry = {"forum_id": forum_id, "forum_name": forum_name, "discussions": []}
            if not discussions:
                lines.append("    (No discussions)")
                course_entry["forums"].append(forum_entry)
                continue

            for d_idx, disc in enumerate(discussions, start=1):
                discussion_id = disc.get("discussion")
                discussion_name = disc.get("name") or f"(discussion {discussion_id})"
                author = disc.get("userfullname") or "Unknown"
                replies = disc.get("numreplies")

                lines.append(f"    Discussion [{discussion_id}] {discussion_name} · by {author} · replies={replies}")

                # Posts in discussion
                posts_resp = await moodle_request("mod_forum_get_discussion_posts", discussionid=discussion_id)
                if err := fmt_err(posts_resp):
                    log.warning("posts fetch error", extra={"discussion_id": discussion_id, "error": err})
                    posts = []
                else:
                    posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else (posts_resp or [])

                if isinstance(max_posts_per_discussion, int):
                    posts = posts[:max_posts_per_discussion]

                disc_entry = {
                    "discussion_id": discussion_id,
                    "discussion_name": discussion_name,
                    "author": author,
                    "numreplies": replies,
                    "posts": []
                }

                if not posts:
                    lines.append("      (No posts)")
                    forum_entry["discussions"].append(disc_entry)
                    continue

                for p_idx, p in enumerate(posts, start=1):
                    post_id = p.get("id")
                    post_author = (p.get("author") or {}).get("fullname", "Unknown")
                    ts = p.get("timecreated")
                    msg = strip_html(p.get("message", ""))
                    preview = (msg[:preview_chars] + "…") if len(msg) > preview_chars else msg

                    lines.append(f"      - [post {post_id}] {post_author} @ {ts}: {preview}")

                    disc_entry["posts"].append({
                        "post_id": post_id,
                        "author": post_author,
                        "timecreated": ts,
                        "message": msg,
                    })

                forum_entry["discussions"].append(disc_entry)

            course_entry["forums"].append(forum_entry)

        result_nested.append(course_entry)

    # 4) Output
    if as_json:
        return json.dumps(result_nested, ensure_ascii=False)

    return "\n".join(lines)


# -------------------- Main ----------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    log.info("Starting MCP server (HTTP)", extra={
        "transport": "streamable_http",
        "port": port,
        "base_url_set": bool(MOODLE_BASE_URL),
        "token_present": bool(MOODLE_TOKEN),
        "endpoint_set": bool(API_ENDPOINT),
        "log_level": LOG_LEVEL,
        "json_logging": LOG_JSON,
    })
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
