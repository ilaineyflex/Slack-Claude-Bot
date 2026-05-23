import json
from flask import Flask, request, jsonify
import requests

BOT_TOKEN = "xoxb-7632804253442-11179562758007-HzOSINDMaOOwnV4k2qSo6RK6"

app = Flask(__name__)

@app.route("/", methods=["POST"])
def slack_events():
    data = request.json
    print("Received event:", json.dumps(data, indent=2))

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    event = data.get("event", {})
    print("Event type:", event.get("type"))
    print("Bot ID:", event.get("bot_id"))
    print("Subtype:", event.get("subtype"))

    if event.get("bot_id") or event.get("subtype"):
        print("Skipping bot message or subtype")
        return "", 200

    if event.get("type") == "app_mention":
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        print("Sending reply to channel:", channel, "thread:", thread_ts)

        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": "Bearer " + BOT_TOKEN},
            json={
                "channel": channel,
                "thread_ts": thread_ts,
                "text": "Hello, I got your message!",
            },
        )
        print("Slack API response:", response.json())

    return "", 200

if __name__ == "__main__":
    app.run(port=5000)