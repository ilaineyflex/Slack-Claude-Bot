import json
import os
from datetime import datetime, timedelta

import pytz
import requests
import google.generativeai as genai
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN    = os.environ.get("SLACK_BOT_TOKEN", "")
FATHOM_API_KEY     = os.environ.get("FATHOM_API_KEY", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
ELAINE_CALENDAR_ID = os.environ.get("ELAINE_CALENDAR_ID", "")
ROSTER_CHANNEL     = os.environ.get("ROSTER_CHANNEL", "elaine-roster")
FALLBACK_CHANNEL   = os.environ.get("FALLBACK_CHANNEL", "ceo-braindump")
TIMEZONE           = os.environ.get("TIMEZONE", "America/Los_Angeles")

# ── Initialize ────────────────────────────────────────────────────────────────
app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)
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

        processed = []
        for event in events:
            title = event.get("summary", "")
            if "Client Check-In Call" not in title:
                continue
            client_name = extract_client_name_from_calendar_title(title)
            print(f"Processing: {title} -> Client: {client_name}")
            run_post_call_automation(client_name, title)
            processed.append({"title": title, "client": client_name})

        return jsonify({"processed": processed, "count": len(processed)}), 200

    except Exception as e:
        print(f"Retroactive error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Existing Slack Events ─────────────────────────────────────────────────────
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

    fathom_data = get_fathom_recording(call_title)
    if not fathom_data:
        notify_fallback(f"Could not find Fathom recording for: *{call_title}*. Please check manually.")
        return

    summary_data = generate_summary(fathom_data["transcript"], client_name)

    thread_result = find_client_thread(client_name)
    if not thread_result:
        name_from_title = extract_name_from_title(call_title)
        if name_from_title:
            print(f"Trying name from title: {name_from_title}")
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

    if summary_data.get("tasks_with_dates"):
        create_calendar_tasks(summary_data["tasks_with_dates"])

# ── Fathom ────────────────────────────────────────────────────────────────────
def get_fathom_recording(call_title):
    headers = {"Authorization": f"Bearer {FATHOM_API_KEY}"}

    response = requests.get(
        "https://api.fathom.video/v1/calls",
        headers=headers,
        params={"limit": 20}
    )
    print("Fathom status:", response.status_code)
    print("Fathom body:", response.text[:500])

    if response.status_code != 200:
        return None

    body = response.json()
    calls = body.get("data", body) if isinstance(body, dict) else body

    for call in calls:
        title = call.get("title", "")
        if call_title.lower() in title.lower() or title.lower() in call_title.lower():
            call_id = call.get("id")

            transcript_resp = requests.get(
                f"https://api.fathom.video/v1/calls/{call_id}/transcript",
                headers=headers
            )
            transcript_text = ""
            if transcript_resp.status_code == 200:
                t_body = transcript_resp.json()
                segments = t_body.get("segments", t_body.get("data", []))
                transcript_text = "\n".join(
                    f"{s.get('speaker', 'Unknown')}: {s.get('text', '')}"
                    for s in segments
                )

            return {
                "url": call.get("url") or call.get("share_url") or f"https://fathom.video/calls/{call_id}",
                "transcript": transcript_text,
                "title": title,
            }

    print(f"No Fathom recording matched: {call_title}")
    return None

# ── Gemini Summary ────────────────────────────────────────────────────────────
def generate_summary(transcript, client_name):
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""You are an assistant helping a fitness coach named Elaine organize client call notes.

Transcript from a coaching call with {client_name}:

{transcript}

Return ONLY valid JSON with no extra text or markdown:
{{
  "client_summary": "Bullet point summary Elaine can copy and send directly to the client. Include key discussion points and any action steps the CLIENT needs to take. Write as if speaking directly to the client.",
  "coach_tasks": ["Task 1 for Elaine", "Task 2 for Elaine"],
  "tasks_with_dates": [
    {{"task": "description", "due_date": "YYYY-MM-DD", "due_time": "HH:MM"}}
  ]
}}

Only include items in tasks_with_dates if a specific date was mentioned in the transcript. If no time was mentioned use 09:00. If none apply return an empty array."""

    response = model.generate_content(prompt)
    text = response.text.strip().strip("```").strip("json").strip()

    try:
        return json.loads(text)
    except Exception as e:
        print(f"Gemini parse error: {e}\nRaw: {response.text}")
        return {"client_summary": response.text, "coach_tasks": [], "tasks_with_dates": []}

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
    if " - Client Check-In Call" in title:
        return title.split(" - Client Check-In Call")[0].strip()
    if "- Client Check-In Call" in title:
        return title.split("- Client Check-In Call")[0].strip()
    if "Client Check-In Call - " in title:
        return title.split("Client Check-In Call - ")[1].strip()
    if "Client Check-In Call with " in title:
        return title.split("Client Check-In Call with ")[1].strip()
    return title.replace("Client Check-In Call", "").strip(" -").strip()

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
def create_calendar_tasks(tasks_with_dates):
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

        for task in tasks_with_dates:
            title = task.get("task", "Coaching task")
            due_date = task.get("due_date")
            due_time = task.get("due_time", "09:00")
            if not due_date:
                continue

            start = tz.localize(datetime.strptime(f"{due_date} {due_time}", "%Y-%m-%d %H:%M"))
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
            print(f"Calendar event created: {title} on {due_date}")
    except Exception as e:
        print(f"Calendar error: {e}")

# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(port=5000)
