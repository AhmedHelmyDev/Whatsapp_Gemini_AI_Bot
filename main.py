import os
import requests
import fitz
from flask import Flask, request, jsonify
from google import genai
from google.genai.types import (
    Tool,
    GenerateContentConfig,
    UrlContext,
    GoogleSearch,
    Content,
    Part,
    Blob,
)

# ─── Environment & Identity ───────────────────────────────────────────────────
WA_TOKEN    = os.environ["WA_TOKEN"]
PHONE_ID    = os.environ["PHONE_ID"]
PHONE       = os.environ["PHONE_NUMBER"]
API_KEY     = os.environ["GEN_API"]
CREATOR     = "Eng. Ahmed Helmy Eletr"
BOT_NAME    = "MindBot-1.7-mini"
MODEL_ID    = "gemini-2.5-flash-preview-05-20"

# ─── Initialize Gemini Client ──────────────────────────────────────────────────
client = genai.Client(api_key=API_KEY)

# Pre-prompt to lock in identity & tone
init_prompt = (
    f"You are \"{BOT_NAME}\", a direct and concise AI assistant developed by {CREATOR}. "
    "Do not respond to this setup message."
)
# Send once on startup (no reply expected)
client.models.generate_content(
    model=MODEL_ID,
    contents=init_prompt
)

# ─── Flask App ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

def send_whatsapp(text: str):
    url = f"https://graph.facebook.com/v18.0/{PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": PHONE,
        "type": "text",
        "text": {"body": text},
    }
    return requests.post(url, headers=headers, json=payload)

def cleanup(*paths):
    for p in paths:
        try: os.remove(p)
        except: pass
    # Also delete any uploaded files in Gemini
    for f in client.list_files(): f.delete()

def is_search_request(text: str) -> bool:
    lower = text.lower()
    return lower.startswith("/search") or "http://" in text or "https://" in text

@app.route("/", methods=["GET","POST"])
def index():
    return f"{BOT_NAME} is running."

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        # Verification handshake
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == "BOT":
            return challenge, 200
        return "Forbidden", 403

    data_entry = request.get_json().get("entry", [{}])[0]
    msg        = data_entry.get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0]
    if not msg:
        return jsonify({"status": "no message"}), 200

    mtype = msg.get("type")
    try:
        if mtype == "text":
            user_text = msg["text"]["body"]

            # ── Decide: search-vs-default ─────────────────────
            if is_search_request(user_text):
                # Tool-enabled search
                tools = [
                    Tool(url_context=UrlContext()),
                    Tool(google_search=GoogleSearch())
                ]
                cfg = GenerateContentConfig(
                    tools=tools,
                    response_modalities=["TEXT"],
                )
                resp = client.models.generate_content(
                    model=MODEL_ID,
                    contents=user_text,
                    config=cfg
                )
            else:
                # Plain text gen
                resp = client.models.generate_content(
                    model=MODEL_ID,
                    contents=user_text
                )

            reply = "".join([part.text for part in resp.candidates[0].content.parts])
            # If search: you can inspect resp.candidates[0].url_context_metadata as needed
            send_whatsapp(reply)
            return jsonify({"status": "text handled"}), 200

        # ── Media handling ────────────────────────────────
        media = msg.get(mtype)
        media_id = media["id"]
        # Fetch the media URL
        meta = requests.get(
            f"https://graph.facebook.com/v18.0/{media_id}/",
            headers={"Authorization": f"Bearer {WA_TOKEN}"}
        ).json()
        download = requests.get(meta["url"], headers={"Authorization": f"Bearer {WA_TOKEN}"})

        # Determine temp filename
        if mtype == "image":
            tmp = "/tmp/media_image.jpg"
        elif mtype == "audio":
            tmp = "/tmp/media_audio.mp3"
        elif mtype == "video":
            tmp = "/tmp/media_video.mp4"
        elif mtype == "document":
            # Handle PDF pages → images
            doc = fitz.open(stream=download.content, filetype="pdf")
            summaries = []
            for page in doc:
                img_path = "/tmp/page.jpg"
                pix = page.get_pixmap()
                pix.save(img_path)
                uf = client.upload_file(path=img_path, display_name="pdf_page")
                r = client.models.generate_content(
                    model=MODEL_ID,
                    contents=[uf, "Analyze and summarize this page directly:"]
                )
                summaries.append(r.candidates[0].content.parts[0].text)
                cleanup(img_path)
            full = "\n\n".join(summaries)
            send_whatsapp(full)
            return jsonify({"status": "pdf handled"}), 200
        else:
            send_whatsapp("Unsupported media type.")
            return jsonify({"status": "unsupported"}), 200

        # Save to disk
        with open(tmp, "wb") as f:
            f.write(download.content)

        # Upload & prompt
        uf = client.upload_file(path=tmp, display_name="user_media")
        if mtype == "video":
            prompt = "Summarize this video in 3 sentences."
        else:
            prompt = "Analyze and reply directly based on this content."

        resp = client.models.generate_content(
            model=MODEL_ID,
            contents=[uf, prompt]
        )
        summary = resp.candidates[0].content.parts[0].text

        send_whatsapp(summary)
        cleanup(tmp)
        return jsonify({"status": "media handled"}), 200

    except Exception as e:
        send_whatsapp("❌ Error processing your request.")
        print("Error:", e)
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=8000)
