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
                args=[client_name, title],
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
        run_at = end_time + timedelta(minutes=20)
        print(f"Scheduling automation to run at {run_at}")

        scheduler.add_job(
            run_post_call_automation,
            "date",
            run_date=run_at,
            args=[client_name, appointment_title],
            id=f"call_{client_name}_{end_time_str}",
            replace_existing=True
        )
        return jsonify({"status": "scheduled", "run_at": run_at.isoformat()}), 200
    except Exception as e:
        print(f"Error scheduling: {e}")
        return "", 500

# ── Main Automation ───────────────────────────────────────────────────────────
def run_post_call_automation(client_name, call_title):
    print(f"\n=== Post-call automation: {client_name} ===")

    fathom_data = get_fathom_recording(call_title, client_name)
    if not fathom_data:
        notify_fallback(f"Could not find Fathom recording for: *{call_title}*. Please check manually.")
        return

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

    message = format_message(client_name, fathom_data["url"], summary_data)

    if thread_result:
        channel_id, thread_ts = thread_result
        post_to_thread(channel_id, thread_ts, message)
        print("Posted to client thread successfully.")
    else:
        alert = (
            f"Could not find a thread for *{client_name}* in #elaine-roster. "
            f"Please post this manually.\n\n{message}"
        )
        notify_fallback(alert)
        print("No thread found, posted to fallback channel.")

    # Time-bound tasks: specific dates mentioned in the call
    if summary_data.get("tasks_with_dates"):
        create_calendar_tasks(summary_data["tasks_with_dates"])

    # Non-dated coach tasks: schedule for 6pm next weekday/Saturday
    if summary_data.get("coach_tasks"):
        schedule_coach_tasks(summary_data["coach_tasks"], client_name)

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
    fathom_summary = ""
    for field in ["summary", "ai_summary", "notes", "description"]:
        val = matched.get(field)
        if val and isinstance(val, str) and len(val) > 50:
            fathom_summary = val[:2000]
            print(f"Fathom summary found in meeting field '{field}' ({len(val)} chars)")
            break

    # If not in meeting object, try the recording summary endpoint
    if not fathom_summary:
        summary_resp = requests.get(
            f"https://api.fathom.ai/external/v1/recordings/{recording_id}/summary",
            headers=headers
        )
        print(f"Fathom summary endpoint status: {summary_resp.status_code}")
        if summary_resp.status_code == 200:
            s_body = summary_resp.json()
            for field in ["summary", "text", "content", "ai_summary", "notes"]:
                val = s_body.get(field) if isinstance(s_body, dict) else None
                if val and isinstance(val, str) and len(val) > 50:
                    fathom_summary = val[:2000]
                    print(f"Fathom summary found at endpoint field '{field}'")
                    break

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
        "fathom_summary": fathom_summary,
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
        f"\nFathom's auto-generated call summary (use this to fill in details cut by truncation):\n{fathom_summary}\n"
        if fathom_summary else ""
    )

    prompt = f"""You are writing a post-call text message from Elaine to her coaching client {client_name}. Today is {today_str}.

ELAINE'S BRAND VOICE (follow this exactly):
- Warm, direct, specific, slightly sassy. Like a knowledgeable friend texting over DMs -- not a hype woman, not a medical journal.
- She is authoritative and compassionate through PRECISION, not through enthusiasm announcements. The wins she calls out are specific (a number, a symptom, a change) not generic praise.
- Tone sits between clinical and cheerleader. Specific like a specialist. Conversational like a trusted friend.
- She explains the "why" in plain language: "your belly fat is an insulin problem" not "hormonal architecture issues."
- She is direct and does not hedge. She does not perform warmth with filler phrases.

WORDS AND PHRASES BANNED FOR THIS BRAND:
- "journey" (completely banned)
- "I see you", "you deserve better", "you're not lazy", "you've got this queen"
- "amazing!", excessive exclamation chains, over-the-top hype language
- Clinical terms the client has to Google: "visceral adipose tissue", "HPA axis", "phenotype"
- Generic opener praise that could apply to anyone

PREFERRED LANGUAGE (use where relevant):
- "belly fat", "energy that lasts", "period back", "feel like yourself again", "fit into", "in control"
- "your body is responding", "that's your hormones shifting", "here's what that means for next steps"
- Plain mechanism explanations: "the fatigue is dropping because your cortisol load is coming down"

Transcript from Elaine's coaching call with {client_name}:
{transcript}
{fathom_context}
Return ONLY valid JSON, no markdown, no extra text:
{{
  "client_summary": "A text message Elaine will send directly to {client_name}. Structure: (1) Name 1-2 specific wins from the call -- use the actual number, symptom, or change. Connect each win to what it means for progress in one sentence. (2) State the focus for the next 1-2 weeks and the plain-language reason why in 1-2 sentences. (3) List the client's action steps as bullet points using hyphens, with any deadlines stated clearly. (4) One-line warm close -- direct, not filler. Do NOT include small talk, rapport chat, or life details unless they directly explain a coaching recommendation. Do NOT open with a generic compliment. Start with the win.",
  "coach_tasks": ["Specific task Elaine must complete, naming the client and deliverable: e.g. Send {client_name} the [resource name] link", "Schedule follow-up call with {client_name} for [date if mentioned]"],
  "tasks_with_dates": [
    {{"task": "task description", "due_date": "YYYY-MM-DD", "due_time": "HH:MM"}}
  ]
}}

tasks_with_dates rules: ONLY include tasks where a specific date was explicitly mentioned in the call. Always use year {current_year} unless a different future year was clearly stated. Use "09:00" if no time was given. Return [] if none apply.
coach_tasks rules: List ALL tasks Elaine needs to complete that have no specific date. Be concrete -- name the client, the resource, the link, or the deliverable. These get auto-scheduled on Elaine's calendar."""

    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    raw = completion.choices[0].message.content

    # Strip markdown code fences properly (re.sub on substrings, not .strip() on chars)
    text = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE).strip()

    # First attempt: parse the whole cleaned string
    try:
        return json.loads(text)
    except Exception:
        pass

    # Second attempt: extract the first {...} block in case there is surrounding text
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except Exception as e2:
            print(f"Groq JSON extraction failed: {e2}")

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

def format_message(client_name, video_url, summary_data):
    tasks = summary_data.get("coach_tasks", [])
    tasks_text = "\n".join(f"• {t}" for t in tasks) if tasks else "No tasks identified."
    return (
        f"*Client Check-In Call Summary -- {client_name}*\n"
        f"*Fathom Recording:* {video_url}\n\n"
        f"*Summary to send to client:*\n{summary_data.get('client_summary', '')}\n\n"
        f"*Elaine's post-call tasks:*\n{tasks_text}"
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
