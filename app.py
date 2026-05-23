import json
import os
import re
import threading
from datetime import datetime, timedelta

import pytz
import requests
from groq import Groq
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN    = os.environ.get("SLACK_BOT_TOKEN", "")
FATHOM_API_KEY     = os.environ.get("FATHOM_API_KEY", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
ELAINE_CALENDAR_ID = os.environ.get("ELAINE_CALENDAR_ID", "")
ROSTER_CHANNEL     = os.environ.get("ROSTER_CHANNEL", "elaine-roster")
FALLBACK_CHANNEL   = os.environ.get("FALLBACK_CHANNEL", "ceo-braindump")
TIMEZONE           = os.environ.get("TIMEZONE", "America/Los_Angeles")
GHL_API_KEY        = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID    = os.environ.get("GHL_LOCATION_ID", "")
ELAINE_PHONE       = os.environ.get("ELAINE_PHONE", "2508017038")

# Minutes after call end time for each attempt: 5, 10, 20, 30, 60
RETRY_DELAYS = [5, 10, 20, 30, 60]

# ── Initialize ────────────────────────────────────────────────────────────────
app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ── Keep-Alive Ping ───────────────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200

# ── Retroactive Trigger ───────────────────────────────────────────────────────
@app.route("/run-retroactive", methods=["GET"])
def run_retroactive():
    start_date = request.args.get("start", "2026-05-21")
    end_date = request.args.get("end", "2026-05-22")

    if not GOOGLE_CREDENTIALS or not ELAINE_CALENDAR_ID:
        return jsonify({"error": "Google Calendar not configured"}), 500

    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)
        tz = pytz.timezone(TIMEZONE)

        start_dt = tz.localize(datetime.strptime(start_date, "%Y-%m-%d"))
        end_dt = tz.localize(datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1))

        events_result = service.events().list(
            calendarId=ELAINE_CALENDAR_ID,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            q="Client Check-In Call",
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = events_result.get("items", [])
        print(f"Found {len(events)} matching calendar events")

        processing = []
        for event in events:
            title = event.get("summary", "")
            if "Client Check-In Call" not in title:
                continue
            client_name = extract_client_name_from_calendar_title(title)
            print(f"Queuing: {title} -> Client: {client_name}")
            t = threading.Thread(
                target=run_post_call_automation,
                args=[client_name, title, None, 1],  # no retries for retroactive
                daemon=True
            )
            t.start()
            processing.append({"title": title, "client": client_name})

        return jsonify({"processing": processing, "count": len(processing), "status": "started in background"}), 200

    except Exception as e:
        print(f"Retroactive error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Slack Events ──────────────────────────────────────────────────────────────
@app.route("/", methods=["POST"])
def slack_events():
    data = request.json
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})
    event = data.get("event", {})
    if event.get("bot_id") or event.get("subtype"):
        return "", 200
    if event.get("type") == "app_mention":
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        post_to_thread(channel, thread_ts, "Hello, I got your message!")
    return "", 200

# ── GHL Webhook ───────────────────────────────────────────────────────────────
@app.route("/ghl-webhook", methods=["POST"])
def ghl_webhook():
    data = request.json
    print("GHL webhook received:", json.dumps(data, indent=2))

    appointment_title = data.get("calendar", {}).get("title", "")
    client_name = data.get("full_name", "") or data.get("contact", {}).get("full_name", "")
    end_time_str = data.get("calendar", {}).get("endTime", "")

    print(f"Title: {appointment_title} | Client: {client_name} | End: {end_time_str}")

    if "Client Check-In Call" not in appointment_title:
        print("Not a Client Check-In Call, skipping.")
        return "", 200

    if not end_time_str:
        print("No end time found, cannot schedule.")
        return "", 200

    try:
        end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
        end_time_iso = end_time.isoformat()
        run_at = end_time + timedelta(minutes=RETRY_DELAYS[0])  # first attempt: 5 min
        print(f"Scheduling attempt 1 at {run_at} ({RETRY_DELAYS[0]} min after call end)")

        scheduler.add_job(
            run_post_call_automation,
            "date",
            run_date=run_at,
            args=[client_name, appointment_title, end_time_iso, 1],
            id=f"call_{client_name}_{end_time_str}_attempt_1",
            replace_existing=True
        )
        return jsonify({"status": "scheduled", "run_at": run_at.isoformat(), "attempt": 1}), 200
    except Exception as e:
        print(f"Error scheduling: {e}")
        return "", 500

# ── Main Automation ───────────────────────────────────────────────────────────
def run_post_call_automation(client_name, call_title, end_time_iso=None, attempt=1):
    print(f"\n=== Post-call automation: {client_name} (attempt {attempt}/{len(RETRY_DELAYS)}) ===")

    fathom_data = get_fathom_recording(call_title, client_name)
    if not fathom_data:
        # Schedule next retry if we have remaining attempts and a live end_time
        if end_time_iso and attempt < len(RETRY_DELAYS):
            next_attempt = attempt + 1
            delay_min = RETRY_DELAYS[next_attempt - 1]
            end_time = datetime.fromisoformat(end_time_iso)
            run_at = end_time + timedelta(minutes=delay_min)
            print(f"Fathom not found -- scheduling retry {next_attempt} at {run_at} ({delay_min} min after call end)")
            scheduler.add_job(
                run_post_call_automation,
                "date",
                run_date=run_at,
                args=[client_name, call_title, end_time_iso, next_attempt],
                id=f"call_{client_name}_{end_time_iso}_attempt_{next_attempt}",
                replace_existing=True
            )
        else:
            # All retries exhausted (or retroactive lookup) -- alert via Slack + SMS
            alert = (
                f"Could not find Fathom recording for *{client_name}*'s call after "
                f"{'all ' + str(len(RETRY_DELAYS)) + ' attempts (1 hour)' if end_time_iso else 'retroactive lookup'}.\n"
                f"Call: _{call_title}_\nPlease check Fathom manually."
            )
            notify_fallback(alert)
            if end_time_iso:
                send_ghl_sms(
                    f"Hi Elaine, the post-call summary for {client_name}'s check-in call "
                    f"could not be found in Fathom after 1 hour of retries. "
                    f"Call: {call_title}. Please check Fathom manually."
                )
        return

    # Derive call date for post headers
    tz = pytz.timezone(TIMEZONE)
    call_date = ""
    if end_time_iso:
        try:
            dt = datetime.fromisoformat(end_time_iso).astimezone(tz)
            call_date = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        except Exception:
            pass
    if not call_date:
        # Extract month/day from call title (e.g. "May 21st", "May 22")
        m = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+',
            call_title, re.IGNORECASE
        )
        if m:
            call_date = f"{m.group(0)}, {datetime.now(tz).year}"

    summary_data = generate_summary(
        fathom_data["transcript"],
        client_name,
        fathom_summary=fathom_data.get("fathom_summary", "")
    )

    thread_result = find_client_thread(client_name)
    if not thread_result:
        print(f"No thread found for {client_name}, trying fallback name extraction")
        name_from_title = extract_name_from_title(call_title)
        if name_from_title:
            thread_result = find_client_thread(name_from_title)

    video_url = fathom_data["url"]
    fathom_full = fathom_data.get("fathom_summary_full", "")
    date_str = f" {call_date}" if call_date else ""
    post_header = f"*Client Check-In Call Summary{date_str} -- {client_name}*\n*Fathom Recording:* {video_url}\n\n"

    # Post 1: summary to send to client
    post1 = post_header + f"*Summary to send to client:*\n{summary_data.get('client_summary', '')}"

    # Post 2: Elaine's post-call tasks (use - dashes for readability)
    tasks = summary_data.get("coach_tasks", [])
    tasks_text = "\n".join(f"- {t}" for t in tasks) if tasks else "No tasks identified."
    post2 = post_header + f"*Elaine's post-call tasks:*\n{tasks_text}"

    # Post 3: full Fathom summary (only when available)
    post3 = (post_header + f"*Fathom Call Summary:*\n{fathom_full}") if fathom_full else None

    if thread_result:
        channel_id, thread_ts = thread_result
        post_to_thread(channel_id, thread_ts, post1)
        post_to_thread(channel_id, thread_ts, post2)
        if post3:
            post_to_thread(channel_id, thread_ts, post3)
        print("Posted 3-part summary to client thread successfully.")
    else:
        combined = post1 + "\n\n" + post2 + ("\n\n" + post3 if post3 else "")
        alert = (
            f"Could not find a thread for *{client_name}* in #elaine-roster. "
            f"Please post this manually.\n\n{combined}"
        )
        notify_fallback(alert)
        print("No thread found, posted to fallback channel.")

    # Time-bound tasks: specific dates mentioned in the call
    if summary_data.get("tasks_with_dates"):
        create_calendar_tasks(summary_data["tasks_with_dates"])

    # Non-dated coach tasks: schedule for 6pm next weekday/Saturday
    if summary_data.get("coach_tasks"):
        schedule_coach_tasks(summary_data["coach_tasks"], client_name)

# ── Fathom helpers ────────────────────────────────────────────────────────────
def _fathom_clean_display(text):
    """Prepare Fathom markdown for Slack: strip hyperlinks, convert markdown bold to Slack bold."""
    if not text:
        return text
    # [link text](url) → link text  (removes every embedded timestamp URL)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # **bold** → *bold*  (Slack uses single asterisk for bold, not double)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'*\1*', text)
    # ## Heading / ### Heading → *Heading*  (Slack bold instead of markdown headers)
    text = re.sub(r'^#{1,4}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    return text

def _fathom_clean_ai(text):
    """Prepare Fathom markdown for AI prompt: strip all markdown to save tokens."""
    if not text:
        return text
    # [link text](url) → link text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # **bold** and *italic* → plain
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    # ## headers → plain line (keep the heading text so structure is readable)
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ── Fathom ────────────────────────────────────────────────────────────────────
def get_fathom_recording(call_title, client_name=""):
    headers = {"X-Api-Key": FATHOM_API_KEY}

    response = requests.get("https://api.fathom.ai/external/v1/meetings", headers=headers)
    print("Fathom status:", response.status_code)

    if response.status_code != 200:
        print("Fathom error:", response.text[:500])
        return None

    body = response.json()
    if isinstance(body, list):
        meetings = body
    elif isinstance(body, dict):
        meetings = body.get("items") or body.get("data") or body.get("meetings") or []
    else:
        print("Unexpected Fathom response type:", type(body))
        return None

    print(f"Total meetings returned: {len(meetings)}")

    matched = None
    for meeting in meetings:
        if not isinstance(meeting, dict):
            continue
        title = meeting.get("title") or meeting.get("meeting_title") or ""
        print(f"Checking: '{title}'")
        if (call_title.lower() in title.lower() or
                title.lower() in call_title.lower() or
                (client_name and client_name.lower() in title.lower())):
            matched = meeting
            print(f"Matched: {title}")
            break

    if not matched:
        print(f"No Fathom recording matched for: {call_title} / {client_name}")
        return None

    recording_id = matched.get("recording_id") or matched.get("id")
    meeting_url = matched.get("share_url") or matched.get("url") or f"https://fathom.video/calls/{recording_id}"

    # Try to get Fathom's own AI summary from the meeting object
    fathom_summary_raw = ""
    for field in ["summary", "ai_summary", "notes", "description"]:
        val = matched.get(field)
        if val and isinstance(val, str) and len(val) > 50:
            fathom_summary_raw = val
            print(f"Fathom summary found in meeting field '{field}' ({len(val)} chars)")
            break

    # If not in meeting object, try the recording summary endpoint
    if not fathom_summary_raw:
        summary_resp = requests.get(
            f"https://api.fathom.ai/external/v1/recordings/{recording_id}/summary",
            headers=headers
        )
        print(f"Fathom summary endpoint status: {summary_resp.status_code}")
        if summary_resp.status_code == 200:
            s_body = summary_resp.json()
            if isinstance(s_body, dict):
                # Fathom API format: {"summary": {"markdown_formatted": "...", "template_name": "..."}}
                nested = s_body.get("summary")
                if isinstance(nested, dict):
                    val = nested.get("markdown_formatted") or nested.get("text") or nested.get("content")
                    if val and isinstance(val, str) and len(val) > 50:
                        fathom_summary_raw = val
                        print(f"Fathom summary found at summary.markdown_formatted ({len(val)} chars)")
                # Fallback: top-level string fields
                if not fathom_summary_raw:
                    for field in ["text", "content", "ai_summary", "notes", "markdown_formatted"]:
                        val = s_body.get(field)
                        if val and isinstance(val, str) and len(val) > 50:
                            fathom_summary_raw = val
                            print(f"Fathom summary found at top-level field '{field}'")
                            break
            if not fathom_summary_raw:
                print(f"Fathom summary response shape: {list(s_body.keys()) if isinstance(s_body, dict) else type(s_body)}")

    # Clean and prepare two versions of the Fathom summary
    fathom_summary_full = _fathom_clean_display(fathom_summary_raw)   # Slack display
    fathom_summary_for_ai = _fathom_clean_ai(fathom_summary_raw)      # AI prompt (no urls, no bold)

    print(f"Fetching transcript for recording_id: {recording_id}")
    transcript_resp = requests.get(
        f"https://api.fathom.ai/external/v1/recordings/{recording_id}/transcript",
        headers=headers
    )
    print("Transcript status:", transcript_resp.status_code)

    transcript_text = ""
    if transcript_resp.status_code == 200:
        t_body = transcript_resp.json()
        segments = t_body.get("transcript", [])
        if isinstance(segments, list):
            transcript_text = "\n".join(
                f"{s.get('speaker', {}).get('display_name', 'Unknown')}: {s.get('text', '')}"
                for s in segments
                if isinstance(s, dict)
            )

    return {
        "url": meeting_url,
        "transcript": transcript_text,
        "title": matched.get("title", ""),
        "fathom_summary": fathom_summary_for_ai,
        "fathom_summary_full": fathom_summary_full,
    }

# ── Groq Summary ──────────────────────────────────────────────────────────────
def generate_summary(transcript, client_name, fathom_summary=""):
    tz = pytz.timezone(TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    current_year = datetime.now(tz).year

    # If we have a Fathom summary it covers the full call; use less raw transcript
    max_transcript_chars = 4000 if fathom_summary else 7000
    if len(transcript) > max_transcript_chars:
        transcript = transcript[:max_transcript_chars] + "\n\n[Transcript truncated]"

    fathom_context = (
        f"\n\nFATHOM AI SUMMARY (this is the authoritative source -- use it for all task wording):\n{fathom_summary}\n"
        if fathom_summary else ""
    )

    prompt = f"""You are writing post-call notes from Elaine, a PCOS fitness coach, to her client {client_name}. Today is {today_str}.

THE FATHOM SUMMARY BELOW IS YOUR ONLY SOURCE OF TRUTH. Do not add topics, tasks, or context that do not appear in it.

TRANSCRIPT (for tone and direct quotes only):
{transcript}
{fathom_context}

====
TASK 1 — client_summary (a text message Elaine will send to {client_name}):

STRUCTURE (follow in order, no deviations):

A. OPENING — 1 sentence. Read the Fathom Key Takeaways to understand the TONE of this specific call, then write an opener that matches it:
   - Call where wins/progress were celebrated → "Glad we got to celebrate your wins today, [first name]!" or reference the specific celebration
   - Call where there was a coach transition → acknowledge it warmly ("Thanks for being so open with me today and so understanding about the transition, [first name]")
   - Call where client had a tough week or setback → open with care, not celebration ("Appreciate you being real with me today, [first name]")
   - Routine check-in → brief warm opener that references what the call was about
   NEVER reuse the same opener format for different clients. Use their first name. Do not use "Good connecting with you today" as a default.

B. WINS — 1-2 specific measurable wins from the Fathom Key Takeaways. ALWAYS include before → after numbers if a starting point appears anywhere (e.g. "249lbs → 241lbs" not "down 8lbs"). Include the real-life impact if mentioned ("fitting into your shorts easier"). A small natural reaction is fine ("woo!", ":)") but do not force it.

C. UPCOMING FOCUS — 1-2 sentences using the EXACT focus stated in the Fathom summary (Key Takeaways or New Focus section). Do not invent or generalize.

D. CLIENT ACTION STEPS — bullet list using hyphens. Find the Next Steps section in the Fathom summary. Under the subsection for {client_name} (not Elaine), copy each step VERBATIM. Do not paraphrase, shorten, or combine steps. If a link is referenced, write [INSERT LINK]. Keep all deadlines exactly as stated.

E. PROGRAM RENEWAL / PHASE 2 — ONLY include if Fathom's Topics or Key Takeaways explicitly mentions a Phase 2, renewal, or program continuation discussion for THIS client. If yes: 1-2 sentences connecting their stated goals to the next phase + the specific action ask (e.g. "review your finances before our next call"). If not explicitly in the Fathom summary, OMIT THIS SECTION ENTIRELY — do not infer or add it.

F. CLOSE — 1 warm, direct line. Not "let me know if you have any questions" or "looking forward to our next steps together."

====
TASK 2 — coach_tasks (Elaine's to-do list):

Find the "Next Steps" section in the Fathom summary. It will have a subsection for Elaine (the coach). Copy every task listed there VERBATIM.
- Include ALL tasks listed -- do not skip any.
- Do NOT rename or paraphrase resources ("Bloating Masterclass" stays "Bloating Masterclass", "hormonal health assessment forms" stays exactly that).
- If a task does not include {client_name}'s name, add it (e.g. "Schedule a follow-up call for next Friday" → "Schedule a follow-up call with {client_name} for next Friday").
- Do NOT invent tasks. If the Fathom summary has 4 coach tasks, return 4 coach tasks.

====
Return ONLY valid JSON, no markdown, no extra text. Use \\n for newlines inside strings, never literal newlines:
{{
  "client_summary": "full structured text message for {client_name}",
  "coach_tasks": ["verbatim task 1 from Fathom with {client_name} added where generic", "verbatim task 2"],
  "tasks_with_dates": [
    {{"task": "task description", "due_date": "YYYY-MM-DD", "due_time": "HH:MM"}}
  ]
}}

tasks_with_dates: ONLY tasks where a specific date was explicitly stated. Use year {current_year} unless stated otherwise. Use "09:00" if no time given. Return [] if none."""

    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    raw = completion.choices[0].message.content

    # Strip markdown code fences (re.sub on substrings, not .strip() on chars)
    text = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE).strip()

    # Groq sometimes puts literal newlines inside JSON string values, which is invalid.
    # Walk character by character and escape any newlines/tabs that occur inside strings.
    def escape_json_strings(s):
        result = []
        in_string = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == '"' and (i == 0 or s[i - 1] != '\\'):
                in_string = not in_string
                result.append(c)
            elif in_string and c == '\n':
                result.append('\\n')
            elif in_string and c == '\r':
                result.append('\\r')
            elif in_string and c == '\t':
                result.append('\\t')
            else:
                result.append(c)
            i += 1
        return ''.join(result)

    cleaned = escape_json_strings(text)

    # First attempt: parse the whole cleaned string
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Second attempt: extract the first {...} block in case there is surrounding text
    json_match = re.search(r'\{[\s\S]*\}', cleaned)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except Exception as e2:
            print(f"Groq JSON extraction failed after escaping: {e2}")

    print(f"Groq could not parse response. First 500 chars:\n{text[:500]}")
    return {"client_summary": text, "coach_tasks": [], "tasks_with_dates": []}

# ── Slack Helpers ─────────────────────────────────────────────────────────────
def get_channel_id(channel_name):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(
        "https://slack.com/api/conversations.list",
        headers=headers,
        params={"types": "private_channel,public_channel", "limit": 200}
    )
    for ch in resp.json().get("channels", []):
        if ch["name"] == channel_name:
            return ch["id"]
    return None

def find_client_thread(client_name):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    channel_id = get_channel_id(ROSTER_CHANNEL)
    if not channel_id:
        print(f"Channel not found: {ROSTER_CHANNEL}")
        return None

    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=headers,
        params={"channel": channel_id, "limit": 200}
    )
    name_lower = client_name.lower()
    for msg in resp.json().get("messages", []):
        if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
            continue
        first_line = msg.get("text", "").split("\n")[0].lower()
        if name_lower in first_line:
            return channel_id, msg["ts"]

    print(f"No thread found for: {client_name}")
    return None

def extract_client_name_from_calendar_title(title):
    title = re.split(r'\s*-\s*Client Check-In Call', title)[0].strip()
    title = re.split(r'\s*-?\s*\d+:\d+', title)[0].strip()
    title = re.sub(r'\*[^*]+\*', '', title).strip(' -').strip()
    return title

def extract_name_from_title(call_title):
    if " - " in call_title:
        return call_title.split(" - ")[-1].strip()
    if "with " in call_title.lower():
        return call_title.lower().split("with ")[-1].strip().title()
    return None

def format_message(client_name, video_url, summary_data, fathom_summary_full=""):
    tasks = summary_data.get("coach_tasks", [])
    tasks_text = "\n".join(f"• {t}" for t in tasks) if tasks else "No tasks identified."
    fathom_section = f"\n\n*Fathom Call Summary:*\n{fathom_summary_full}" if fathom_summary_full else ""
    return (
        f"*Client Check-In Call Summary -- {client_name}*\n"
        f"*Fathom Recording:* {video_url}\n\n"
        f"*Summary to send to client:*\n{summary_data.get('client_summary', '')}\n\n"
        f"*Elaine's post-call tasks:*\n{tasks_text}"
        f"{fathom_section}"
    )

def post_to_thread(channel_id, thread_ts, text):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json={"channel": channel_id, "thread_ts": thread_ts, "text": text}
    )
    print("Slack post:", resp.json().get("ok"), resp.json().get("error", ""))

def notify_fallback(text):
    channel_id = get_channel_id(FALLBACK_CHANNEL)
    if channel_id:
        post_to_thread(channel_id, None, text)

def send_ghl_sms(message):
    """Send an SMS to Elaine's phone via GoHighLevel API."""
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        print("GHL_API_KEY or GHL_LOCATION_ID not set -- skipping SMS alert")
        return
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }
    try:
        # Look up Elaine's contact by phone number
        search = requests.get(
            "https://services.leadconnectorhq.com/contacts/",
            headers=headers,
            params={"locationId": GHL_LOCATION_ID, "query": ELAINE_PHONE}
        )
        print(f"GHL contact search status: {search.status_code}")
        contacts = search.json().get("contacts", []) if search.status_code == 200 else []
        if not contacts:
            print(f"No GHL contact found for {ELAINE_PHONE} -- SMS not sent")
            return
        contact_id = contacts[0].get("id")
        # Send outbound SMS
        resp = requests.post(
            "https://services.leadconnectorhq.com/conversations/messages",
            headers=headers,
            json={"type": "SMS", "contactId": contact_id, "message": message}
        )
        print(f"GHL SMS sent: {resp.status_code} -- {resp.text[:200]}")
    except Exception as e:
        print(f"GHL SMS error: {e}")

# ── Google Calendar ────────────────────────────────────────────────────────────
def get_next_deadline():
    """Returns 6pm on the next weekday or Saturday. No Sunday scheduling."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    candidate = now.date() + timedelta(days=1)
    # weekday(): 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    while candidate.weekday() == 6:  # Skip Sunday
        candidate += timedelta(days=1)
    return tz.localize(datetime(candidate.year, candidate.month, candidate.day, 18, 0))

def create_calendar_tasks(tasks_with_dates):
    """Create calendar events for tasks with explicit dates mentioned in the call."""
    if not GOOGLE_CREDENTIALS or not ELAINE_CALENDAR_ID:
        print("Google Calendar not configured, skipping.")
        return
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)
        tz = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)

        for task in tasks_with_dates:
            title = task.get("task", "Coaching task")
            due_date = task.get("due_date")
            due_time = task.get("due_time", "09:00")
            if not due_date:
                continue

            start = tz.localize(datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M"))

            # If the date is in the past (AI used wrong year), bump to current year
            if start < now - timedelta(days=1):
                start = start.replace(year=now.year)
            # If still in the past after current-year fix, move to next year
            if start < now - timedelta(days=1):
                start = start.replace(year=now.year + 1)

            end = start + timedelta(hours=1)
            event = {
                "summary": title,
                "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 1440},
                        {"method": "popup", "minutes": 720},
                    ]
                }
            }
            service.events().insert(calendarId=ELAINE_CALENDAR_ID, body=event).execute()
            print(f"Calendar event created: {title} on {start.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        print(f"Calendar error: {e}")

def schedule_coach_tasks(coach_tasks, client_name):
    """Schedule non-dated coach tasks as calendar reminders due 6pm next weekday/Saturday."""
    if not GOOGLE_CREDENTIALS or not ELAINE_CALENDAR_ID or not coach_tasks:
        return
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)
        deadline = get_next_deadline()
        end = deadline + timedelta(hours=1)

        for task in coach_tasks:
            event = {
                "summary": f"[{client_name}] {task}",
                "start": {"dateTime": deadline.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 1440},
                        {"method": "popup", "minutes": 720},
                    ]
                }
            }
            service.events().insert(calendarId=ELAINE_CALENDAR_ID, body=event).execute()
            print(f"Coach task scheduled: [{client_name}] {task} by {deadline.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        print(f"Coach task scheduling error: {e}")

# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(port=5000)
