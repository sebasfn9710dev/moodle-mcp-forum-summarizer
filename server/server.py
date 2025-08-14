#!/usr/bin/env python3
"""
MCP Server (FastMCP) — Moodle forum workflow with logging
Tools:
  - search_courses(query, page=0, perpage=50, as_json=False)
  - confirm_course_by_id(course_id, as_json=False)
  - get_forums_by_course_id(course_id, as_json=False)
  - list_forum_discussions(forum_id, as_json=False)
  - get_discussion_posts(discussion_id, as_json=True)  # clean authors+messages
  - summarize_discussion(discussion_id, focus="...")   # optional (OpenAI)

Env:
  MOODLE_BASE_URL=https://your-moodle.example.com
  MOODLE_TOKEN=your_ws_token
  (optional) OPENAI_API_KEY=...
  (optional) OPENAI_MODEL=gpt-4o-mini

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
        # Attach structured extras if present
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

# -------------------- Optional AI ----------------
USE_AI = False
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
try:
    from openai import AsyncOpenAI
    USE_AI = True
except Exception:
    USE_AI = False
    log.debug("OpenAI client not available; summarize_discussion will be disabled unless installed.")

# -------------------- Config --------------------
load_dotenv()  # load .env if present

MOODLE_BASE_URL = os.getenv("MOODLE_BASE_URL", "").rstrip("/")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "")
API_ENDPOINT = f"{MOODLE_BASE_URL}/webservice/rest/server.php" if MOODLE_BASE_URL else ""

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

# AI client (lazy)
_aiclient = None
def _ai():
    global _aiclient
    if _aiclient is None:
        _aiclient = AsyncOpenAI()
        log.info("Initialized OpenAI client", extra={"model": OPENAI_MODEL})
    return _aiclient

# -------------------- Helpers -------------------
async def moodle_request(wsfunction: str, **params: Any) -> Any:
    """POST to Moodle REST WS. Returns parsed JSON or {'error': '...'}."""
    if not _config_ok():
        return {"error": "Missing MOODLE_BASE_URL or MOODLE_TOKEN in environment."}

    payload = {
        "wstoken": MOODLE_TOKEN,  # redacted in logs
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        **params,
    }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(API_ENDPOINT, data=payload, timeout=60.0)
            duration = round((time.perf_counter() - start) * 1000, 1)
            # Log with redactions
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
async def summarize_discussion(discussion_id: int, focus: str = "") -> str:
    log.info("summarize_discussion called",
             extra={"discussion_id": discussion_id, "focus_len": len(focus or "")})
    if not USE_AI or not os.getenv("OPENAI_API_KEY"):
        log.warning("summarize_discussion unavailable (no OPENAI_API_KEY or client)")
        return "Summarization requires OPENAI_API_KEY and the 'openai' package."

    raw_json = await get_discussion_posts(discussion_id, as_json=True)
    try:
        posts = json.loads(raw_json)
    except Exception:
        log.exception("Failed to parse posts JSON for summarization",
                      extra={"discussion_id": discussion_id})
        return "Failed to load posts for summarization."

    if not posts:
        log.info("No posts to summarize", extra={"discussion_id": discussion_id})
        return "No posts to summarize."

    corpus_lines = [f"- {p['author']} @ {p['timecreated']}: {p['message']}" for p in posts]
    corpus = "\n".join(corpus_lines)[:120_000]

    prompt = (
        "Summarize this Moodle forum discussion.\n"
        f"Focus: {focus or 'themes, decisions, unresolved questions, sentiment, and action items (with owners if obvious).'}\n"
        "Be concise, use bullet points, end with 3–5 next steps."
    )

    start = time.perf_counter()
    resp = await _ai().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You are an expert forum summarizer."},
            {"role": "user", "content": prompt},
            {"role": "user", "content": corpus},
        ],
        temperature=0.1,
    )
    duration = round((time.perf_counter() - start) * 1000, 1)
    # Try to pull token usage if present (depends on SDK/model)
    usage = getattr(resp, "usage", None)
    usage_dict = {"input": None, "output": None, "total": None}
    if usage:
        # SDKs differ; keep it defensive
        usage_dict["input"] = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
        usage_dict["output"] = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
        total = getattr(usage, "total_tokens", None)
        if total is not None:
            usage_dict["total"] = total

    log.info("summarize_discussion completed",
             extra={"discussion_id": discussion_id, "duration_ms": duration,
                    "model": OPENAI_MODEL, "usage": usage_dict})
    return resp.choices[0].message.content.strip()

# -------------------- Main ----------------------
if __name__ == "__main__":
    log.info("Starting MCP server", extra={
        "transport": "stdio",
        "base_url_set": bool(MOODLE_BASE_URL),
        "token_present": bool(MOODLE_TOKEN),
        "endpoint_set": bool(API_ENDPOINT),
        "log_level": LOG_LEVEL,
        "json_logging": LOG_JSON,
    })
    mcp.run(transport="stdio")
