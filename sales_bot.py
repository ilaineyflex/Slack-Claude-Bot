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
TIMEZONE           = os.environ.get("TIMEZONE", "America/New_York")

BOOKEDSETS_CHANNEL    = os.environ.get("BOOKEDSETS_CHANNEL", "C07JLQ5UQM8")
SALES_REJECTS_CHANNEL = os.environ.get("SALES_REJECTS_CHANNEL", "C07KE1XN4TU")
CLOSED_LEADS_CHANNEL  = os.environ.get("CLOSED_LEADS_CHANNEL", "C07JPA9HZMJ")
FALLBACK_CHANNEL      = os.environ.get("FALLBACK_CHANNEL", "ceo-braindump")

SALES_CALL_KEYWORDS = [
    "sales call", "assessment call", "hormonal blueprint",
    "rescheduled sales call", "follow up assessment", "free assessment",
]

# Retry delays after call end (minutes): 5, 10, 20, 30, 60
RETRY_DELAYS = [5, 10, 20, 30, 60]

# In-memory dedup set for calendar-triggered calls
_processed_call_ids: set = set()

# ── Initialize ────────────────────────────────────────────────────────────────
app       = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.start()

# ── Keep-Alive ────────────────────────────────────────────────────────────────
@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200


# ── Manual Trigger ────────────────────────────────────────────────────────────
@app.route("/run-sales-call", methods=["GET", "POST"])
def run_sales_call_endpoint():
    """
    Params (GET or POST JSON):
      lead_name   - required, e.g. "Dorrette White"
      call_title  - optional, used to help Fathom search (defaults to lead_name)
      thread_ts   - optional, skip #bookedsets search if you know the thread ts
      end_time_iso - optional ISO datetime, used for retry scheduling
    """
    data       = request.json if request.method == "POST" and request.json else request.args
    lead_name  = (data.get("lead_name") or "").strip()
    call_title = (data.get("call_title") or "").strip() or lead_name
    thread_ts  = (data.get("thread_ts") or "").strip() or None
    end_time   = (data.get("end_time_iso") or "").strip() or None

    if not lead_name:
        return jsonify({"error": "lead_name is required"}), 400

    threading.Thread(
        target=run_sales_call_automation,
        args=[lead_name, call_title, end_time, 1, thread_ts],
        daemon=True
    ).start()
    return jsonify({"status": "started", "lead_name": lead_name}), 200


# ── Google Calendar Poller (runs every 5 min) ─────────────────────────────────
def poll_calendar_for_sales_calls():
    if not GOOGLE_CREDENTIALS or not ELAINE_CALENDAR_ID:
        return
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)
        tz  = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)
        # Window: events that ended between 3 and 20 minutes ago
        t_min = (now - timedelta(minutes=20)).isoformat()
        t_max = (now - timedelta(minutes=3)).isoformat()

        result = service.events().list(
            calendarId=ELAINE_CALENDAR_ID,
            timeMin=t_min, timeMax=t_max,
            singleEvents=True, orderBy="startTime"
        ).execute()

        for event in result.get("items", []):
            title = event.get("summary", "")
            if not any(kw in title.lower() for kw in SALES_CALL_KEYWORDS):
                continue
            eid = event.get("id", "")
            if eid in _processed_call_ids:
                continue
            _processed_call_ids.add(eid)

            end_str   = event.get("end", {}).get("dateTime", "")
            lead_name = _extract_lead_name(title)
            print(f"[Sales poll] Detected ended sales call: {title} -> {lead_name}")
            threading.Thread(
                target=run_sales_call_automation,
                args=[lead_name, title, end_str, 1, None],
                daemon=True
            ).start()
    except Exception as e:
        print(f"[Sales poll] Error: {e}")


scheduler.add_job(
    poll_calendar_for_sales_calls,
    "interval",
    minutes=5,
    id="poll_sales_calls",
    replace_existing=True
)


def _extract_lead_name(title):
    """'Dorrette White @itsmedoor - 7pm EST ...' -> 'Dorrette White'"""
    name = re.sub(r'@\S+', '', title).strip()
    if ' - ' in name:
        name = name.split(' - ')[0].strip()
    return name


# ── Main Automation ───────────────────────────────────────────────────────────
def run_sales_call_automation(lead_name, call_title, end_time_iso=None,
                               attempt=1, thread_ts_override=None):
    try:
        _run_sales_call_automation_inner(lead_name, call_title, end_time_iso,
                                         attempt, thread_ts_override)
    except Exception as e:
        import traceback
        print(f"[FATAL] Sales call automation crashed for {lead_name}: {e}")
        traceback.print_exc()


def _run_sales_call_automation_inner(lead_name, call_title, end_time_iso=None,
                                      attempt=1, thread_ts_override=None):
    print(f"\n=== Sales call automation: {lead_name} (attempt {attempt}/{len(RETRY_DELAYS)}) ===")

    fathom_data = _get_fathom_recording(call_title, lead_name)
    if not fathom_data:
        if end_time_iso and attempt < len(RETRY_DELAYS):
            next_attempt = attempt + 1
            delay_min    = RETRY_DELAYS[next_attempt - 1]
            end_dt       = datetime.fromisoformat(end_time_iso)
            run_at       = end_dt + timedelta(minutes=delay_min)
            scheduler.add_job(
                run_sales_call_automation, "date", run_date=run_at,
                args=[lead_name, call_title, end_time_iso, next_attempt, thread_ts_override],
                id=f"sales_{lead_name}_{end_time_iso}_a{next_attempt}",
                replace_existing=True
            )
            print(f"Fathom not ready -- retry {next_attempt} at {run_at}")
        else:
            _notify_fallback(
                f"Could not find Fathom recording for sales call with *{lead_name}* after "
                f"{'all retries' if end_time_iso else 'manual lookup'}.\n"
                f"Call title: _{call_title}_\nPlease process manually."
            )
        return

    transcript  = fathom_data["transcript"]
    fathom_ai   = fathom_data.get("fathom_summary", "")
    fathom_disp = fathom_data.get("fathom_summary_full", "")
    video_url   = fathom_data["url"]
    content     = fathom_ai or fathom_disp

    outcome = _classify_outcome(content, transcript)
    print(f"Outcome for {lead_name}: {outcome}")

    # Build call date string for post headers
    tz = pytz.timezone(TIMEZONE)
    call_date_str = ""
    if end_time_iso:
        try:
            dt = datetime.fromisoformat(end_time_iso).astimezone(tz)
            call_date_str = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        except Exception:
            pass
    if not call_date_str:
        m = re.search(
            r'(January|February|March|April|May|June|July|August|September|'
            r'October|November|December)\s+\d+',
            call_title, re.IGNORECASE
        )
        if m:
            call_date_str = f"{m.group(0)}, {datetime.now(tz).year}"

    # Resolve bookedsets thread
    if thread_ts_override:
        thread_info = _build_thread_info(thread_ts_override)
    else:
        thread_info = _find_bookedsets_thread(lead_name)

    setter_id = thread_info["setter_id"] if thread_info else None
    ig_handle = thread_info["ig_handle"] if thread_info else ""
    thread_ts = thread_info["thread_ts"] if thread_info else None

    if outcome == "CLOSED":
        _handle_closed(lead_name, ig_handle, setter_id, thread_ts,
                       content, transcript, video_url, call_date_str)
    elif outcome == "FOLLOW_UP":
        _handle_follow_up(lead_name, ig_handle, setter_id, thread_ts,
                          content, transcript, video_url, call_date_str)
    else:
        _handle_lost(lead_name, ig_handle, setter_id, thread_ts,
                     content, transcript, video_url, call_date_str)


# ── Classification ────────────────────────────────────────────────────────────
def _classify_outcome(fathom_summary, transcript=""):
    content = (fathom_summary or transcript)[:3000]
    prompt = (
        "Classify this sales call outcome as exactly one word: CLOSED, FOLLOW_UP, or LOST.\n\n"
        "CLOSED = lead committed to a program on this call (deposit paid, payment plan agreed, "
        "or verbal commitment with a clear next-payment step).\n"
        "FOLLOW_UP = another call was scheduled but no commitment was made yet.\n"
        "LOST = lead declined, had unresolved objections, or no follow-up was booked.\n\n"
        f"Call content:\n{content}\n\n"
        "Reply with only one word: CLOSED, FOLLOW_UP, or LOST."
    )
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    raw = completion.choices[0].message.content.strip().upper()
    if "CLOSED" in raw:
        return "CLOSED"
    if "FOLLOW" in raw:
        return "FOLLOW_UP"
    return "LOST"


# ── Outcome Handlers ──────────────────────────────────────────────────────────
def _handle_closed(lead_name, ig_handle, setter_id, thread_ts,
                   content, transcript, video_url, call_date_str):
    data       = _extract_closed_data(content, transcript, lead_name)
    setter_tag = f"<@{setter_id}>" if setter_id else "Setter not identified"
    ig_str     = f" / {ig_handle}" if ig_handle else ""

    # --- #bookedsets thread post ---
    bookedsets_post = (
        f"*CLOSED -- {lead_name}{ig_str} -- Sales Call {call_date_str}*\n"
        f"*Setter (S):* {setter_tag}\n"
        f"*Outcome:* CLOSED -- {data.get('program_name','')} "
        f"{data.get('program_length','')}\n\n"
        f"*Program:* {data.get('program_name','')} | "
        f"{data.get('program_length','')} | {data.get('total_price','')}\n"
        f"*Pillars:* {data.get('pillars','')}\n\n"
        f"*Deposit:* {data.get('deposit_info','')}\n"
        f"*Payment Plan:* {data.get('payment_plan','')}\n"
        f"*Start:* {data.get('start_date','')}   "
        f"*End:* {data.get('end_date','')}   "
        f"*Backend Call:* {data.get('backend_call_date','')}\n\n"
        f"*Fathom Recording:* {video_url}\n\n"
        f"*Call Deep Dive:*\n{data.get('call_summary','')}"
    )
    if thread_ts:
        _post(BOOKEDSETS_CHANNEL, thread_ts, bookedsets_post)
    else:
        _notify_fallback(
            f"Could not find #bookedsets thread for *{lead_name}*. "
            f"Closed lead summary below:\n\n{bookedsets_post}"
        )

    # --- #closed-leads post ---
    ig_display = f" ({ig_handle})" if ig_handle else ""
    closed_post = (
        f"*CLOSED: {lead_name}{ig_display}*\n"
        f"*Setter:* {setter_tag}\n"
        f"*Coach:* {data.get('coach','Elaine')}\n"
        f"*Program:* {data.get('program_name','')} | "
        f"Pillars: {data.get('pillars','')} | "
        f"{data.get('program_length','')} | {data.get('total_price','')}\n"
        f"*Deposit received:* {data.get('deposit_info','')}\n"
        f"*Payment Plan:* {data.get('payment_plan','')}\n"
        f"*Start date/onboarding:* {data.get('start_date','')}\n"
        f"*End date:* {data.get('end_date','')}\n"
        f"*Backend call date:* {data.get('backend_call_date','')}\n"
        f"*Fathom:* {video_url}"
    )
    _post(CLOSED_LEADS_CHANNEL, None, closed_post)

    # --- Google Calendar ---
    if data.get("start_date") and data.get("end_date") and data.get("backend_call_date"):
        _create_sales_calendar_events(
            lead_name, data["start_date"], data["end_date"], data["backend_call_date"]
        )
    print(f"Closed call handled for {lead_name}")


def _handle_follow_up(lead_name, ig_handle, setter_id, thread_ts,
                      content, transcript, video_url, call_date_str):
    data       = _extract_follow_up_data(content, transcript, lead_name)
    setter_tag = f"<@{setter_id}>" if setter_id else "Setter not identified"
    ig_str     = f" / {ig_handle}" if ig_handle else ""

    thread_post = (
        f"*FOLLOW-UP BOOKED -- {lead_name}{ig_str} -- Sales Call {call_date_str}*\n"
        f"*Setter (S):* {setter_tag}\n"
        f"*Outcome:* Follow-up call booked\n\n"
        f"{data.get('call_summary','')}\n\n"
        f"*Fathom Recording:* {video_url}"
    )
    if thread_ts:
        _post(BOOKEDSETS_CHANNEL, thread_ts, thread_post)

    # Channel alert + DM setter
    channel_alert = (
        f"<!channel> *FOLLOW-UP NEEDED -- {lead_name}{ig_str}*\n"
        f"{setter_tag} action needed:\n"
        f"{data.get('booking_instructions','')}\n"
        f"*Follow-up details:* {data.get('followup_call_details','')}"
    )
    _post(BOOKEDSETS_CHANNEL, None, channel_alert)

    if setter_id:
        dm_msg = (
            f"*Follow-up booking needed for {lead_name}{ig_str}*\n"
            f"{data.get('booking_instructions','')}\n"
            f"*When:* {data.get('followup_call_details','')}\n"
            f"*Fathom:* {video_url}"
        )
        _post(setter_id, None, dm_msg)
    print(f"Follow-up call handled for {lead_name}")


def _handle_lost(lead_name, ig_handle, setter_id, thread_ts,
                 content, transcript, video_url, call_date_str):
    data       = _extract_lost_data(content, transcript, lead_name)
    setter_tag = f"<@{setter_id}>" if setter_id else "Setter not identified"
    ig_str     = f" / {ig_handle}" if ig_handle else ""

    lost_post = (
        f"*SALES CALL REJECT -- {lead_name}{ig_str} -- {call_date_str}*\n"
        f"*Setter (S):* {setter_tag}\n\n"
        f"*Main Objection:*\n{data.get('objection','')}\n\n"
        f"*What Went Well:*\n{data.get('went_well','')}\n\n"
        f"*What Needed Improvement:*\n{data.get('needs_improvement','')}\n\n"
        f"*Nurture Action Steps for Setters:*\n{data.get('nurture_steps','')}\n\n"
        f"*Fathom Recording:* {video_url}"
    )
    _post(SALES_REJECTS_CHANNEL, None, lost_post)

    if thread_ts:
        brief = (
            f"*LOST -- Sales Call {call_date_str}*\n"
            f"Objection: {data.get('objection','')}\n"
            f"Full debrief in <#{SALES_REJECTS_CHANNEL}>."
        )
        _post(BOOKEDSETS_CHANNEL, thread_ts, brief)
    print(f"Lost call handled for {lead_name}")


# ── Data Extraction ────────────────────────────────────────────────────────────
def _groq_json(prompt):
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    raw  = completion.choices[0].message.content
    text = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    print(f"JSON parse failed. First 300 chars:\n{text[:300]}")
    return {}


def _extract_closed_data(fathom_summary, transcript, lead_name):
    tz        = pytz.timezone(TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    content   = (fathom_summary or transcript)[:6000]
    prompt = f"""Extract deal details from this closed sales call. Lead: {lead_name}. Today: {today_str}.

Content:
{content}

Return ONLY valid JSON. All dates as "Month D, YYYY" (e.g. "June 15, 2026").
end_date = start_date + (program_weeks * 7) days.
backend_call_date = end_date minus 30 days.

{{
  "coach": "Elaine",
  "program_name": "exact program name from call",
  "pillars": "Pillar 1 name, Pillar 2 name, Pillar 3 name",
  "program_length": "e.g. 8 weeks",
  "program_weeks": 8,
  "total_price": "e.g. $1,400 CAD",
  "deposit_info": "e.g. $50 CAD deposit paid on call May 27, 2026",
  "payment_plan": "complete payment plan description for invoice — all amounts, dates, schedule",
  "start_date": "e.g. June 15, 2026",
  "end_date": "start_date + program_weeks*7 days as Month D, YYYY",
  "backend_call_date": "end_date minus 30 days as Month D, YYYY",
  "call_summary": "2-3 paragraph narrative: key health concerns, what resonated, objections overcome, what closed the deal"
}}"""
    return _groq_json(prompt)


def _extract_follow_up_data(fathom_summary, transcript, lead_name):
    content = (fathom_summary or transcript)[:5000]
    prompt = f"""Extract follow-up details from this sales call. Lead: {lead_name}.

Content:
{content}

Return ONLY valid JSON:
{{
  "call_summary": "Setter-focused summary: lead name, call date, outcome, key health concerns, what was discussed, objections raised, what resonated, financial/life situation",
  "booking_instructions": "Specific steps the setter needs to take to book the follow-up call",
  "followup_call_details": "When the follow-up needs to happen and context the setter needs"
}}"""
    return _groq_json(prompt)


def _extract_lost_data(fathom_summary, transcript, lead_name):
    content = (fathom_summary or transcript)[:5000]
    prompt = f"""Extract debrief details from this lost sales call. Lead: {lead_name}.

Content:
{content}

Return ONLY valid JSON:
{{
  "objection": "Main objection that caused the loss — be specific",
  "went_well": "bullet list of what went well",
  "needs_improvement": "bullet list of what needed improvement",
  "nurture_steps": "specific action steps for setters to re-engage this lead in the future"
}}"""
    return _groq_json(prompt)


# ── Fathom ────────────────────────────────────────────────────────────────────
def _fathom_clean_display(text):
    if not text:
        return text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'*\1*', text)
    text = re.sub(r'^#{1,4}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    return text


def _fathom_clean_ai(text):
    if not text:
        return text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _get_fathom_recording(call_title, lead_name=""):
    headers  = {"X-Api-Key": FATHOM_API_KEY}
    response = requests.get("https://api.fathom.ai/external/v1/meetings",
                            headers=headers, timeout=30)
    print(f"Fathom status: {response.status_code}")
    if response.status_code != 200:
        print(f"Fathom error: {response.text[:500]}")
        return None

    body = response.json()
    if isinstance(body, list):
        meetings = body
    elif isinstance(body, dict):
        meetings = body.get("items") or body.get("data") or body.get("meetings") or []
    else:
        return None

    matched = None
    for meeting in meetings:
        if not isinstance(meeting, dict):
            continue
        title = meeting.get("title") or meeting.get("meeting_title") or ""
        if (call_title.lower() in title.lower() or
                title.lower() in call_title.lower() or
                (lead_name and lead_name.lower() in title.lower())):
            matched = meeting
            print(f"Fathom matched: {title}")
            break

    if not matched:
        print(f"No Fathom recording matched for: {call_title} / {lead_name}")
        return None

    recording_id = matched.get("recording_id") or matched.get("id")
    meeting_url  = (matched.get("share_url") or matched.get("url") or
                    f"https://fathom.video/calls/{recording_id}")

    # Summary
    fathom_summary_raw = ""
    for field in ["summary", "ai_summary", "notes", "description"]:
        val = matched.get(field)
        if val and isinstance(val, str) and len(val) > 50:
            fathom_summary_raw = val
            break

    if not fathom_summary_raw:
        summary_resp = requests.get(
            f"https://api.fathom.ai/external/v1/recordings/{recording_id}/summary",
            headers=headers, timeout=30
        )
        if summary_resp.status_code == 200:
            s_body = summary_resp.json()
            if isinstance(s_body, dict):
                nested = s_body.get("summary")
                if isinstance(nested, dict):
                    val = nested.get("markdown_formatted") or nested.get("text") or nested.get("content")
                    if val and isinstance(val, str) and len(val) > 50:
                        fathom_summary_raw = val
                if not fathom_summary_raw:
                    for field in ["text", "content", "ai_summary", "markdown_formatted"]:
                        val = s_body.get(field)
                        if val and isinstance(val, str) and len(val) > 50:
                            fathom_summary_raw = val
                            break

    # Transcript
    transcript_resp = requests.get(
        f"https://api.fathom.ai/external/v1/recordings/{recording_id}/transcript",
        headers=headers, timeout=30
    )
    transcript_text = ""
    if transcript_resp.status_code == 200:
        t_body   = transcript_resp.json()
        segments = t_body.get("transcript", [])
        if isinstance(segments, list):
            transcript_text = "\n".join(
                f"{s.get('speaker', {}).get('display_name', 'Unknown')}: {s.get('text', '')}"
                for s in segments if isinstance(s, dict)
            )

    # 502 guard: if both endpoints failed, treat as not ready
    if not transcript_text and not fathom_summary_raw:
        if transcript_resp.status_code != 200:
            print(f"Fathom returned {transcript_resp.status_code} and no summary -- not ready")
            return None

    return {
        "url":               meeting_url,
        "transcript":        transcript_text,
        "fathom_summary":    _fathom_clean_ai(fathom_summary_raw),
        "fathom_summary_full": _fathom_clean_display(fathom_summary_raw),
    }


# ── #bookedsets Thread Lookup ──────────────────────────────────────────────────
def _find_bookedsets_thread(lead_name):
    headers    = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    name_lower = lead_name.lower()
    name_parts = [p for p in name_lower.split() if len(p) > 2]
    cursor     = None

    for _ in range(5):  # up to 1,000 parent messages
        params = {"channel": BOOKEDSETS_CHANNEL, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp     = requests.get("https://slack.com/api/conversations.history",
                                headers=headers, params=params, timeout=15)
        body     = resp.json()
        messages = body.get("messages", [])

        for msg in messages:
            if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
                continue
            first_line = msg.get("text", "").split("\n")[0].lower()
            if all(part in first_line for part in name_parts):
                return _build_thread_info(msg["ts"])

        next_cursor = body.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor

    print(f"[bookedsets] Thread not found for: {lead_name}")
    return None


def _build_thread_info(thread_ts):
    headers  = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp     = requests.get(
        "https://slack.com/api/conversations.replies",
        headers=headers,
        params={"channel": BOOKEDSETS_CHANNEL, "ts": thread_ts, "limit": 50},
        timeout=15
    )
    messages           = resp.json().get("messages", [])
    setter_id, s_name  = _extract_setter(messages)
    ig_handle          = _extract_ig(messages)
    return {
        "channel_id":   BOOKEDSETS_CHANNEL,
        "thread_ts":    thread_ts,
        "setter_id":    setter_id,
        "setter_name":  s_name,
        "ig_handle":    ig_handle,
    }


def _extract_setter(messages):
    for msg in reversed(messages):
        text = msg.get("text", "")
        m = re.search(r'[Ss]etter[:\s]+<@([A-Z0-9]+)(?:\|([^>]+))?>', text)
        if m:
            return m.group(1), (m.group(2) or m.group(1))
    return None, None


def _extract_ig(messages):
    for msg in messages:
        text  = msg.get("text", "")
        clean = re.sub(r'<@[A-Z0-9]+(?:\|[^>]*)?>', '', text)
        m     = re.search(r'@([a-zA-Z]\w{2,})', clean)
        if m:
            return f"@{m.group(1).lower()}"
    return ""


# ── Slack Helpers ─────────────────────────────────────────────────────────────
def _post(channel_id, thread_ts, text):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    payload = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = requests.post("https://slack.com/api/chat.postMessage",
                         headers=headers, json=payload, timeout=15)
    result = resp.json()
    if not result.get("ok"):
        print(f"Slack post error: {result.get('error','unknown')}")
    return result


def _notify_fallback(text):
    channel_id = _get_channel_id(FALLBACK_CHANNEL)
    if channel_id:
        _post(channel_id, None, text)


def _get_channel_id(channel_name):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    resp = requests.get(
        "https://slack.com/api/conversations.list",
        headers=headers,
        params={"types": "private_channel,public_channel", "limit": 200},
        timeout=15
    )
    for ch in resp.json().get("channels", []):
        if ch["name"] == channel_name:
            return ch["id"]
    return None


# ── Google Calendar ────────────────────────────────────────────────────────────
def _parse_date_str(s):
    s = s.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        pass
    m = re.match(r'(\w+)\s+(\d{1,2}),?\s+(\d{4})', s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {int(m.group(2)):02d}, {m.group(3)}", "%B %d, %Y"
            )
        except ValueError:
            pass
    return None


def _create_sales_calendar_events(lead_name, start_date_str, end_date_str, backend_date_str):
    if not GOOGLE_CREDENTIALS or not ELAINE_CALENDAR_ID:
        print("Google Calendar not configured.")
        return
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service   = build("calendar", "v3", credentials=creds)
        tz_str    = TIMEZONE
        tz        = pytz.timezone(tz_str)
        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 1440},  # 24 hr
                {"method": "popup", "minutes": 720},   # 12 hr
                {"method": "popup", "minutes": 120},   # 2 hr
            ]
        }
        events_to_create = [
            (f"[{lead_name}] Program Start / Onboarding", start_date_str),
            (f"[{lead_name}] Program End Date",           end_date_str),
            (f"[{lead_name}] Backend Call Date",          backend_date_str),
        ]
        for title, date_str in events_to_create:
            dt = _parse_date_str(date_str)
            if not dt:
                print(f"Could not parse date '{date_str}' for '{title}', skipping.")
                continue
            start_dt = tz.localize(datetime(dt.year, dt.month, dt.day, 9, 0))
            end_dt   = start_dt + timedelta(hours=1)
            event    = {
                "summary": title,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_str},
                "end":   {"dateTime": end_dt.isoformat(),   "timeZone": tz_str},
                "reminders": reminders,
            }
            service.events().insert(calendarId=ELAINE_CALENDAR_ID, body=event).execute()
            print(f"Calendar event created: {title} on {start_dt.strftime('%Y-%m-%d')}")
    except Exception as e:
        print(f"Sales calendar events error: {e}")


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(port=5001)
