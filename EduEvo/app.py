import google.genai as genai
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
import requests, re, os, time, html as html_lib
import json
from bs4 import BeautifulSoup
import random

app = Flask(__name__, template_folder="templates")
app.secret_key = "eduevo_secret_key_please_change"

# -------------------------
# Gemini API
# -------------------------
GENAI_CLIENT = genai.Client(api_key="AIzaSyBpIm_8A9Oxs9rgfQfYxf6kyjLCY1foU6Q")
YOUTUBE_API_KEY = None


# ----------------- Helpers for safe HTML -----------------
def _escape_and_render_bold(text):
    if text is None:
        return ""
    text = str(text)
    esc = html_lib.escape(text)
    esc_with_bold = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc, flags=re.DOTALL)
    return esc_with_bold


# --------------------
# New Gemini wrapper
# --------------------
def generate_gemini_response(prompt_text):
    try:
        response = GENAI_CLIENT.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt_text
        )
        txt = response.text
        return {
            "reply": txt,
            "reply_html": _escape_and_render_bold(txt)
        }
    except Exception as e:
        return {
            "reply": f"Error contacting Gemini model: {str(e)}",
            "reply_html": _escape_and_render_bold(f"Error contacting Gemini model: {str(e)}")
        }


# ---------- Helpers ----------
def search_openalex(topic, per_page=20):
    try:
        url = "https://api.openalex.org/works"
        params = {"search": topic, "per_page": per_page, "filter": "open_access.is_oa:true"}
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        j = r.json()
        out = []
        for item in j.get("results", []):
            title = item.get("title")
            year = item.get("publication_year")
            authors = [a.get("author", {}).get("display_name") for a in item.get("authorships", [])][:4]
            journal = item.get("host_venue", {}).get("display_name")
            oa = item.get("open_access") or {}
            pdf = oa.get("oa_url") or oa.get("url")
            out.append({"title": title, "authors": authors, "year": year, "journal": journal, "pdf": pdf})
        random.shuffle(out)
        return out
    except Exception:
        return []


def search_youtube_links(topic, max_results=20):
    vids = []
    if YOUTUBE_API_KEY:
        try:
            url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "part": "snippet", "q": topic, "type": "video",
                "maxResults": min(max_results, 50), "key": YOUTUBE_API_KEY
            }
            r = requests.get(url, params=params, timeout=10)
            j = r.json()
            for it in j.get("items", [])[:max_results]:
                vid = it.get("id", {}).get("videoId")
                title = it.get("snippet", {}).get("title")
                channel = it.get("snippet", {}).get("channelTitle")
                if vid:
                    vids.append({
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "channel": channel
                    })
            return vids
        except Exception:
            pass

    # Fallback scraping
    try:
        headers = {
            "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        r = requests.get("https://www.youtube.com/results",
                         params={"search_query": topic},
                         headers=headers,
                         timeout=15)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script")

        for script in scripts:
            if "ytInitialData" in script.text:
                json_text = script.text.split("var ytInitialData = ")[1].split(";")[0]
                data = json.loads(json_text)
                contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"][
                    "sectionListRenderer"]["contents"][0]["itemSectionRenderer"]["contents"]

                for item in contents:
                    if "videoRenderer" in item:
                        v = item["videoRenderer"]
                        video_id = v.get("videoId")
                        title = v.get("title", {}).get("runs", [{}])[0].get("text", "No Title")
                        channel = v.get("ownerText", {}).get("runs", [{}])[0].get("text", "Unknown Channel")
                        if video_id and len(vids) < max_results:
                            vids.append({
                                "title": title,
                                "url": f"https://www.youtube.com/watch?v={video_id}",
                                "channel": channel
                            })
                break
    except Exception:
        pass

    return vids


# -------- Download proxy --------
@app.route("/download")
def download_proxy():
    url = request.args.get("url", "")
    if not url or not url.lower().startswith("http"):
        return "Invalid URL", 400
    try:
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()
        filename = url.split("/")[-1] or "file.pdf"
        mime = r.headers.get("Content-Type", "application/octet-stream")
        return Response(
            r.iter_content(chunk_size=16384),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime
            }
        )
    except Exception as e:
        return f"Error fetching file: {str(e)}", 500


# -------- Routes --------
@app.route("/")
def index():
    user = session.get("user_details")
    saved = request.args.get("saved")
    return render_template("index.html", user=user, saved=saved)


@app.route("/save-details", methods=["POST"])
def save_details():
    name = request.form.get("name", "").strip()
    cls = request.form.get("class", "").strip()
    interests = request.form.get("interests", "").strip()
    session["user_details"] = {"name": name, "class": cls, "interests": interests}
    return redirect(url_for("index", saved=1))


@app.route("/api/search", methods=["POST"])
def api_search():
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "No topic"}), 400

    videos = search_youtube_links(topic)
    articles = search_openalex(topic)
    pdfs = [a for a in articles if a.get("pdf")]

    web_search_link = f"https://www.google.com/search?q={topic.replace(' ', '+')}"
    pdf_search_link = f"https://www.google.com/search?q={topic.replace(' ', '+')}+filetype:pdf"

    return jsonify({
        "topic": topic,
        "videos": videos,
        "articles": articles,
        "pdfs": pdfs,
        "web_search_link": web_search_link,
        "pdf_search_link": pdf_search_link
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "No message"}), 400

    print("AI Request:", msg)
    res = generate_gemini_response(msg)
    return jsonify(res)


if __name__ == "__main__":
    print("Starting EduEvo server...")
    app.run(debug=True, port=5000)
