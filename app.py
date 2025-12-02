import google.genai as genai
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, make_response
import requests, re, os, time, html as html_lib
import json
from bs4 import BeautifulSoup
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, template_folder="templates")
app.secret_key = "eduevo_secret_key_please_change"

# -------------------------
# Gemini API
# -------------------------
GENAI_CLIENT = genai.Client(api_key="AIzaSyD0MwOkk88fCOTZ6NAfKHJzfelIUKi11wg")
YOUTUBE_API_KEY = None
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "eduevo.db")
BASIC_QUIZ_LIMIT = 2


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
def search_semantic_scholar(topic, limit=8):
    try:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": topic,
            "limit": limit,
            "fields": "title,year,authors,url,openAccessPdf,venue"
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        data = r.json()
        results = []
        for paper in data.get("data", []):
            title = paper.get("title")
            if not title:
                continue
            authors = [a.get("name") for a in paper.get("authors", []) if a.get("name")]
            pdf = (paper.get("openAccessPdf") or {}).get("url")
            results.append({
                "title": title,
                "authors": authors,
                "year": paper.get("year"),
                "journal": paper.get("venue") or "Semantic Scholar",
                "pdf": pdf,
                "url": paper.get("url"),
                "source": "Semantic Scholar"
            })
        return results
    except Exception:
        return []


def search_crossref(topic, rows=8):
    try:
        url = "https://api.crossref.org/works"
        params = {"query": topic, "rows": rows}
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        payload = r.json().get("message", {})
        results = []
        for item in payload.get("items", []):
            title_list = item.get("title") or []
            title = title_list[0] if title_list else None
            if not title:
                continue
            authors = []
            for author in item.get("author", []):
                given = author.get("given", "")
                family = author.get("family", "")
                name = " ".join(part for part in [given, family] if part).strip()
                if name:
                    authors.append(name)
            pdf_link = ""
            for link in item.get("link", []):
                if "pdf" in (link.get("content-type") or "").lower():
                    pdf_link = link.get("URL")
                    break
            year = None
            issued = item.get("issued", {}).get("date-parts", [])
            if issued and issued[0]:
                year = issued[0][0]
            results.append({
                "title": title,
                "authors": authors[:4],
                "year": year,
                "journal": (item.get("container-title") or ["Crossref"])[0],
                "pdf": pdf_link,
                "url": item.get("URL"),
                "source": "Crossref"
            })
        return results
    except Exception:
        return []


def search_arxiv(topic, max_results=6):
    try:
        url = "https://export.arxiv.org/api/query"
        params = {"search_query": f"all:{topic}", "start": 0, "max_results": max_results}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        entries = soup.find_all("entry")
        results = []
        for entry in entries:
            title = entry.title.text.strip() if entry.title else None
            if not title:
                continue
            authors = [a.text.strip() for a in entry.find_all("name")]
            pdf_link = ""
            for link in entry.find_all("link"):
                if link.get("type") == "application/pdf":
                    pdf_link = link.get("href")
                    break
            published = entry.published.text if entry.published else ""
            year = published[:4] if published else None
            results.append({
                "title": title,
                "authors": authors[:4],
                "year": year,
                "journal": "arXiv",
                "pdf": pdf_link,
                "url": entry.id.text if entry.id else pdf_link or "",
                "source": "arXiv"
            })
        return results
    except Exception:
        return []


def gather_article_sources(topic, max_results=20):
    combined = []
    seen_titles = set()
    source_lists = [
        search_semantic_scholar(topic, limit=max_results // 2 or 5),
        search_crossref(topic, rows=max_results // 2 or 5),
        search_arxiv(topic, max_results=max(4, max_results // 3)),
    ]
    for source in source_lists:
        for item in source:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen_titles:
                continue
            combined.append(item)
            seen_titles.add(key)
            if len(combined) >= max_results:
                break
        if len(combined) >= max_results:
            break
    pdfs = [a for a in combined if a.get("pdf")]
    return combined, pdfs


def search_youtube_links(topic, max_results=20):
    vids = []
    min_videos = 10  # Always ensure at least 10 videos
    
    if YOUTUBE_API_KEY:
        try:
            url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "part": "snippet", "q": topic, "type": "video",
                "maxResults": min(max(max_results, min_videos), 50), "key": YOUTUBE_API_KEY
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
            # If we got enough videos, return them
            if len(vids) >= min_videos:
                return vids[:max_results]
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

    # If we still don't have enough videos, generate related search terms and create search links
    if len(vids) < min_videos:
        # Generate related topics using AI
        try:
            prompt = f"""Generate {min_videos - len(vids)} related educational YouTube video search terms for "{topic}". Return only the search terms, one per line, without numbers or bullets."""
            response = generate_gemini_response(prompt)
            suggested_terms = [line.strip() for line in response.get("reply", "").strip().split("\n") if line.strip()][:min_videos - len(vids)]
            
            for term in suggested_terms:
                if term:
                    search_url = f"https://www.youtube.com/results?search_query={term.replace(' ', '+')}"
                    vids.append({
                        "title": f"{term} - Educational Video",
                        "url": search_url,
                        "channel": "YouTube Search"
                    })
        except Exception:
            pass
    
    # Ensure we have at least min_videos by adding generic search links with variations
    base_terms = [topic, f"{topic} tutorial", f"{topic} lecture", f"{topic} explanation", f"{topic} class", 
                  f"{topic} lesson", f"{topic} course", f"{topic} study", f"{topic} guide", f"{topic} basics",
                  f"learn {topic}", f"{topic} introduction", f"{topic} overview", f"{topic} concepts"]
    
    term_idx = 0
    while len(vids) < min_videos and term_idx < len(base_terms):
        search_term = base_terms[term_idx % len(base_terms)]
        search_url = f"https://www.youtube.com/results?search_query={search_term.replace(' ', '+')}"
        vids.append({
            "title": f"{search_term} - Educational Video",
            "url": search_url,
            "channel": "YouTube"
        })
        term_idx += 1

    return vids[:max_results] if len(vids) >= min_videos else vids


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
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                class TEXT,
                interests TEXT,
                plan TEXT DEFAULT 'free',
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                quiz_number INTEGER,
                question_number INTEGER,
                question TEXT,
                options TEXT,
                correct_answer TEXT,
                user_answer TEXT,
                is_correct INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        # Add plan column if it doesn't exist (migration for existing databases)
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN plan TEXT DEFAULT 'free'")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                pass  # Column already exists or other error
    conn.close()


def fetch_profile(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT u.name, IFNULL(p.class, '') AS class, IFNULL(p.interests, '') AS interests, IFNULL(p.plan, 'free') AS plan
            FROM users u
            LEFT JOIN profiles p ON p.user_id = u.id
            WHERE u.id = ?
            """,
            (user_id,),
        ).fetchone()
        if row:
            return {"name": row["name"], "class": row["class"], "interests": row["interests"], "plan": row["plan"]}
        return None
    finally:
        conn.close()


def sync_session_profile(user_id):
    profile = fetch_profile(user_id)
    if profile:
        session["user_details"] = profile
    else:
        session.pop("user_details", None)
    return profile


init_db()


def serve_static_template(filename):
    path = os.path.join(app.template_folder, filename)
    with open(path, "r", encoding="utf-8") as fp:
        content = fp.read()
    return Response(content, mimetype="text/html")


def disable_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/")
def landing():
    return serve_static_template("eduevo_website.html")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/app")
def dashboard():
    auth_user = session.get("auth_user")
    if not auth_user:
        return redirect(url_for("auth_page"))
    profile = session.get("user_details")
    if not profile:
        profile = sync_session_profile(auth_user["id"])
    
    # Check user plan and redirect to appropriate template
    user_plan = profile.get("plan", "free")
    if user_plan in ["basic", "plus", "max"]:
        response = make_response(render_template("app_plus.html", user=profile, auth_user=auth_user))
    else:
        response = make_response(render_template("index.html", user=profile, auth_user=auth_user))
    return disable_cache(response)


@app.route("/auth")
def auth_page():
    if "auth_user" in session:
        # Check if there's a return URL in the request
        return_url = request.args.get("return_url", "/app")
        return redirect(return_url)
    return render_template("auth.html")


def upsert_profile(conn, user_id, cls, interests, plan=None):
    if plan:
        conn.execute(
            """
            INSERT INTO profiles (user_id, class, interests, plan)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET class=excluded.class, interests=excluded.interests, plan=excluded.plan
            """,
            (user_id, cls, interests, plan),
        )
    else:
        conn.execute(
            """
            INSERT INTO profiles (user_id, class, interests)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET class=excluded.class, interests=excluded.interests
            """,
            (user_id, cls, interests),
        )


@app.route("/register", methods=["POST"])
def register():
    is_json = request.is_json
    data = request.get_json() if is_json else request.form
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    cls = (data.get("class") or data.get("class_level") or "").strip()
    interests = (data.get("interests") or "").strip()
    if not name or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (name, email, generate_password_hash(password)),
            )
            user_id = cur.lastrowid
            upsert_profile(conn, user_id, cls, interests)
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Email already registered"}), 400
    conn.close()
    session["auth_user"] = {"id": user_id, "name": name, "email": email}
    session["user_details"] = {"name": name, "class": cls, "interests": interests, "plan": "free"}
    resp = {"message": "Registration successful", "user": session["auth_user"], "profile": session["user_details"]}
    if is_json:
        return jsonify(resp)
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["POST"])
def login():
    is_json = request.is_json or request.content_type == "application/json"
    data = request.get_json() if is_json else request.form
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, name, email, password_hash FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    conn.close()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401
    session["auth_user"] = {"id": row["id"], "name": row["name"], "email": row["email"]}
    profile = sync_session_profile(row["id"])
    resp = {"message": "Login successful", "user": session["auth_user"], "profile": profile}
    # Always return JSON for API calls
    return jsonify(resp)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("auth_user", None)
    session.pop("user_details", None)
    return disable_cache(jsonify({"message": "Logged out"}))


@app.route("/api/search", methods=["POST"])
def api_search():
    if "auth_user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "No topic"}), 400

    videos = search_youtube_links(topic)
    articles, pdfs = gather_article_sources(topic)

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
    if "auth_user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json() or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "No message"}), 400

    print("AI Request:", msg)
    res = generate_gemini_response(msg)
    return jsonify(res)


@app.route("/api/check-auth", methods=["GET"])
def check_auth():
    """Check if user is authenticated"""
    authenticated = "auth_user" in session
    return jsonify({"authenticated": authenticated})


@app.route("/api/session-plan", methods=["GET"])
def session_plan():
    """Return current session plan details for landing page routing."""
    authenticated = "auth_user" in session
    plan = "free"
    if authenticated:
        profile = session.get("user_details")
        if not profile and session.get("auth_user"):
            profile = sync_session_profile(session["auth_user"]["id"])
        if profile:
            plan = profile.get("plan") or "free"
    return jsonify({"authenticated": authenticated, "plan": plan})


@app.route("/api/set-plan", methods=["POST"])
def set_plan():
    """Set user's subscription plan"""
    if "auth_user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    data = request.get_json() or {}
    plan = (data.get("plan") or "").strip().lower()
    if plan not in ["free", "basic", "plus", "max"]:
        return jsonify({"error": "Invalid plan"}), 400
    
    user_id = session["auth_user"]["id"]
    conn = get_db_connection()
    try:
        # Check if profile exists, if not create it
        profile_exists = conn.execute(
            "SELECT user_id FROM profiles WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        
        if profile_exists:
            # Update existing profile - ensure plan column exists
            try:
                conn.execute(
                    "UPDATE profiles SET plan = ? WHERE user_id = ?",
                    (plan, user_id)
                )
            except sqlite3.OperationalError:
                # Plan column doesn't exist, add it first
                try:
                    conn.execute("ALTER TABLE profiles ADD COLUMN plan TEXT DEFAULT 'free'")
                except sqlite3.OperationalError:
                    pass  # Column might already exist
                conn.execute(
                    "UPDATE profiles SET plan = ? WHERE user_id = ?",
                    (plan, user_id)
                )
        else:
            # Create new profile with plan
            try:
                conn.execute(
                    "INSERT INTO profiles (user_id, plan) VALUES (?, ?)",
                    (user_id, plan)
                )
            except sqlite3.OperationalError:
                # Plan column doesn't exist, add it first
                try:
                    conn.execute("ALTER TABLE profiles ADD COLUMN plan TEXT DEFAULT 'free'")
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "INSERT INTO profiles (user_id, plan) VALUES (?, ?)",
                    (user_id, plan)
                )
        conn.commit()
        profile = sync_session_profile(user_id)
        return jsonify({"message": f"Plan updated to {plan}", "plan": plan, "profile": profile})
    finally:
        conn.close()


@app.route("/api/generate-quiz", methods=["POST"])
def generate_quiz():
    """Generate quiz questions for a topic"""
    if "auth_user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    quiz_number = data.get("quiz_number", 1)
    num_questions = data.get("num_questions", 10)
    
    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    
    user_id = session["auth_user"]["id"]
    profile = fetch_profile(user_id)
    if not profile:
        return jsonify({"error": "Profile not found"}), 404
    
    plan = (profile.get("plan") or "free").lower()
    user_class = profile.get("class", "")
    
    # Check if user has access to quizzes
    if plan == "free":
        return jsonify({"error": "Quizzes are only available for Plus and Max plans"}), 403
    
    if plan == "basic":
        used = session.get("basic_quiz_uses", 0)
        if used >= BASIC_QUIZ_LIMIT:
            return jsonify({"error": "Basic plan quiz limit reached. Upgrade to Pro or Max for unlimited quizzes."}), 403
    else:
        session.pop("basic_quiz_uses", None)
    
    # For Max users, check if questions already exist to avoid duplicates
    if plan == "max":
        conn = get_db_connection()
        try:
            existing_questions = conn.execute(
                """
                SELECT question FROM quiz_history 
                WHERE user_id = ? AND topic = ?
                """,
                (user_id, topic)
            ).fetchall()
            existing_q_texts = [row[0] for row in existing_questions]
        finally:
            conn.close()
    else:
        existing_q_texts = []
    
    # Generate quiz prompt
    class_context = f" for {user_class}" if user_class else ""
    
    # For Max users, ensure completely different questions
    if existing_q_texts:
        prompt = f"""Generate {num_questions} COMPLETELY NEW and DIFFERENT multiple choice quiz questions about "{topic}"{class_context}. 
CRITICAL: These questions must be ENTIRELY DIFFERENT from these existing questions: {', '.join(existing_q_texts[:10])}

Each question should have:
1. A clear, unique question
2. 4 options (A, B, C, D)
3. The correct answer marked

Format as JSON array:
[
  {{
    "question": "Question text here",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct_answer": "A"
  }}
]

Make questions appropriate for the class level{class_context}. Ensure ALL questions are completely different from the existing ones."""
    else:
        prompt = f"""Generate {num_questions} multiple choice quiz questions about "{topic}"{class_context}. 
Each question should have:
1. A clear question
2. 4 options (A, B, C, D)
3. The correct answer marked

Format as JSON array:
[
  {{
    "question": "Question text here",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct_answer": "A"
  }}
]

Make questions appropriate for the class level{class_context}."""
    
    try:
        response = generate_gemini_response(prompt)
        quiz_text = response.get("reply", "")
        
        # Parse JSON from response (might need cleaning)
        import re
        
        # Try to extract JSON from the response
        json_match = re.search(r'\[.*\]', quiz_text, re.DOTALL)
        if json_match:
            quiz_data = json.loads(json_match.group())
        else:
            # Fallback: try to parse the whole response
            quiz_data = json.loads(quiz_text)
        
        # Ensure we have the right number of questions
        quiz_data = quiz_data[:num_questions]
        
        if plan == "basic":
            session["basic_quiz_uses"] = session.get("basic_quiz_uses", 0) + 1
        
        return jsonify({
            "topic": topic,
            "quiz_number": quiz_number,
            "questions": quiz_data
        })
    except Exception as e:
        return jsonify({"error": f"Failed to generate quiz: {str(e)}"}), 500


@app.route("/api/submit-quiz", methods=["POST"])
def submit_quiz():
    """Save quiz answers and return results"""
    if "auth_user" not in session:
        return jsonify({"error": "Authentication required"}), 401
    
    data = request.get_json() or {}
    topic = (data.get("topic") or "").strip()
    quiz_number = data.get("quiz_number", 1)
    answers = data.get("answers", [])  # List of {question, user_answer, correct_answer, options}
    
    if not topic or not answers:
        return jsonify({"error": "Topic and answers are required"}), 400
    
    user_id = session["auth_user"]["id"]
    conn = get_db_connection()
    
    try:
        with conn:
            for idx, answer_data in enumerate(answers):
                is_correct = 1 if answer_data.get("user_answer") == answer_data.get("correct_answer") else 0
                conn.execute(
                    """
                    INSERT INTO quiz_history (user_id, topic, quiz_number, question_number, question, options, correct_answer, user_answer, is_correct)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        topic,
                        quiz_number,
                        idx + 1,
                        answer_data.get("question", ""),
                        json.dumps(answer_data.get("options", [])),
                        answer_data.get("correct_answer", ""),
                        answer_data.get("user_answer", ""),
                        is_correct
                    )
                )
        
        # Calculate results
        total = len(answers)
        correct = sum(1 for a in answers if a.get("user_answer") == a.get("correct_answer"))
        score = (correct / total * 100) if total > 0 else 0
        
        return jsonify({
            "total": total,
            "correct": correct,
            "score": round(score, 2),
            "answers": answers
        })
    finally:
        conn.close()


if __name__ == "__main__":
    print("Starting EduEvo server...")
    app.run(debug=True, port=5000)
