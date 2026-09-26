import json
import os
import random
import re
import sqlite3
import urllib.error
import urllib.request
import time
from datetime import timedelta
from contextlib import contextmanager
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session

try:
    import psycopg
    from psycopg.rows import dict_row
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
except ImportError:  # SQLite stays dependency-free for local development.
    psycopg = None
    dict_row = None
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

BASE = Path(__file__).parent
DB_PATH = os.environ.get("PLACEMATE_DB") or str(BASE / "instance" / "placemate.db")
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
app = Flask(__name__, static_folder="public/static")
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
cross_site=bool(os.environ.get("FRONTEND_ORIGIN"))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="None" if cross_site else "Lax", SESSION_COOKIE_SECURE=cross_site or os.environ.get("COOKIE_SECURE", "0") == "1", PERMANENT_SESSION_LIFETIME=timedelta(days=7))

CURRICULUM = {
    "CSE": [
        ["Engineering Mathematics I", "Programming in C", "Engineering Physics", "Basic Electrical Engineering"],
        ["Engineering Mathematics II", "Data Structures", "Digital Logic", "Computer Organization"],
        ["Object Oriented Programming", "Discrete Mathematics", "Design and Analysis of Algorithms", "Computer Architecture"],
        ["Operating Systems", "Database Management Systems", "Theory of Computation", "Software Engineering"],
        ["Computer Networks", "Compiler Design", "Web Technologies", "Probability and Statistics"],
        ["Machine Learning", "Distributed Systems", "Information Security", "Cloud Computing"],
        ["System Design Elective", "Artificial Intelligence", "DevOps and CI/CD", "Capstone Project I"],
        ["Advanced Elective", "Industry Internship", "Capstone Project II", "Professional Practice"],
    ],
    "IT": [
        ["Engineering Mathematics I", "Programming in C", "Engineering Physics", "Basic Electrical Engineering"],
        ["Engineering Mathematics II", "Data Structures", "Digital Logic", "Web Fundamentals"],
        ["Object Oriented Programming", "Discrete Mathematics", "Algorithms", "Computer Organization"],
        ["Operating Systems", "Database Management Systems", "Software Engineering", "Data Communications"],
        ["Computer Networks", "Web Application Development", "Information Security", "Cloud Fundamentals"],
        ["Data Mining", "Distributed Systems", "Mobile Computing", "DevOps"],
        ["Big Data Analytics", "Cloud Computing", "Elective", "Capstone Project I"],
        ["Industry Internship", "Advanced Elective", "Capstone Project II", "Professional Practice"],
    ],
    "ECE": [
        ["Engineering Mathematics I", "Programming in C", "Engineering Physics", "Basic Electronics"],
        ["Engineering Mathematics II", "Digital Logic", "Circuit Theory", "Data Structures"],
        ["Signals and Systems", "Object Oriented Programming", "Microprocessors", "Probability and Statistics"],
        ["Communication Systems", "Computer Organization", "Operating Systems", "Embedded Systems"],
        ["Computer Networks", "VLSI Design", "Database Fundamentals", "Control Systems"],
        ["Internet of Things", "Digital Signal Processing", "Machine Learning", "Wireless Communications"],
        ["Cloud for IoT", "Elective", "Industry Project I", "Information Security"],
        ["Industry Internship", "Advanced Elective", "Industry Project II", "Professional Practice"],
    ],
}
BENCHMARKS = ["System design (HLD and LLD)", "Advanced SQL and NoSQL tuning", "CI/CD, Docker and deployment", "Modern framework depth (React, Node, Go)", "Cloud-native architecture and observability", "Message queues and event-driven systems"]
RELEVANT = {"Data Structures", "Algorithms", "Operating Systems", "Database Management Systems", "Computer Networks", "Object Oriented Programming", "System Design Elective", "Cloud Computing"}

@contextmanager
def db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("Install psycopg to use DATABASE_URL.")
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=8)
        postgres = True
    else:
        if os.environ.get("VERCEL"):
            raise RuntimeError("Set DATABASE_URL to a persistent PostgreSQL database on Vercel.")
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        postgres = False
    try:
        yield DatabaseConnection(conn, postgres)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class DatabaseConnection:
    def __init__(self, conn, postgres):
        self.conn = conn
        self.postgres = postgres

    def execute(self, sql, parameters=()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, parameters)

    def executescript(self, sql):
        if not self.postgres:
            return self.conn.executescript(sql)
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        cursor = None
        for statement in sql.split(";"):
            if statement.strip():
                cursor = self.conn.execute(statement)
        return cursor

    def __getattr__(self, name):
        return getattr(self.conn, name)

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(user_id TEXT PRIMARY KEY, name TEXT NOT NULL, branch TEXT NOT NULL DEFAULT 'CSE', semester INTEGER NOT NULL DEFAULT 5, domain TEXT NOT NULL DEFAULT 'Software Developer', experience TEXT NOT NULL DEFAULT 'Undergraduate', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, answers TEXT NOT NULL, strategy TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS interviews(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, role TEXT NOT NULL, difficulty TEXT NOT NULL, areas TEXT NOT NULL, state TEXT NOT NULL, turns TEXT NOT NULL, coding TEXT, report TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)

def now(): return datetime.now(timezone.utc).isoformat()
def uid():
    return session.get("user_id")
def err(message, status=400): return jsonify({"error": message}), status
def clean(value, limit=500): return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]

def gemini_json(prompt, fallback):
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return fallback
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt + "\nReturn only valid JSON, no markdown."}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=14) as resp: data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else fallback
        except urllib.error.HTTPError as exc:
            api_status="unknown"; retry_after=None
            try:
                details=json.loads(exc.read()).get("error",{})
                api_status=clean(details.get("status"),40) or api_status
                message=details.get("message","")
                match=re.search(r"retry in ([0-9.]+)s",message,re.IGNORECASE)
                if match: retry_after=float(match.group(1))
                for detail in details.get("details",[]):
                    if isinstance(detail,dict) and str(detail.get("@type","")).endswith("RetryInfo"):
                        delay=detail.get("retryDelay","")
                        match=re.fullmatch(r"([0-9.]+)s",str(delay))
                        if match: retry_after=float(match.group(1))
            except (ValueError,AttributeError,TypeError): pass
            if retry_after is None:
                try: retry_after=float(exc.headers.get("Retry-After"))
                except (TypeError,ValueError): retry_after=None
            if exc.code in (408,429,500,502,503,504) and attempt < 2:
                delay=retry_after if retry_after is not None else (0.8*(2**attempt)+random.uniform(0,0.3))
                if 0 <= delay <= 15:
                    time.sleep(delay)
                    continue
            app.logger.warning("Gemini API returned HTTP %s (%s); using local fallback.", exc.code,api_status)
            return fallback
        except (urllib.error.URLError,TimeoutError,OSError):
            if attempt<2:
                time.sleep(0.8*(2**attempt)+random.uniform(0,0.3))
                continue
            app.logger.warning("Gemini request or response parsing failed; using local fallback.")
            return fallback
        except (KeyError,ValueError,TypeError):
            app.logger.warning("Gemini request or response parsing failed; using local fallback.")
            return fallback
    return fallback

def profile_row():
    with db() as c:
        p = c.execute("SELECT * FROM profiles WHERE user_id=?", (uid(),)).fetchone()
        if not p:
            c.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?)", (uid(), "Alex Morgan", "CSE", 5, "Software Developer", "Undergraduate", now()))
            p = c.execute("SELECT * FROM profiles WHERE user_id=?", (uid(),)).fetchone()
        return dict(p)

@app.get("/")
def index(): return send_from_directory(BASE / "public" / "static", "index.html")
@app.get("/static/config.js")
def frontend_config():
    return app.response_class("window.PLACEMATE_API_BASE = " + json.dumps(os.environ.get("PUBLIC_API_BASE", "")) + ";", mimetype="application/javascript")
@app.after_request
def cors(response):
    origin=request.headers.get("Origin"); allowed=os.environ.get("FRONTEND_ORIGIN", "").rstrip("/")
    if origin and allowed and origin.rstrip("/")==allowed:
        response.headers["Access-Control-Allow-Origin"]=origin
        response.headers["Access-Control-Allow-Credentials"]="true"
        response.headers["Vary"]="Origin"
        response.headers["Access-Control-Allow-Headers"]="Content-Type"
        response.headers["Access-Control-Allow-Methods"]="GET, POST, PUT, OPTIONS"
    return response
@app.route("/api/<path:_path>", methods=["OPTIONS"])
def preflight(_path): return ("",204)
@app.get("/api/health")
def health(): return jsonify({"ok": True, "ai": bool(os.environ.get("GEMINI_API_KEY"))})
@app.before_request
def require_session():
    if request.method == "OPTIONS": return None
    if request.path.startswith("/api/") and request.path not in ("/api/health", "/api/auth", "/api/login", "/api/register", "/api/demo", "/api/logout") and not uid():
        return err("Please sign in to continue.", 401)
@app.get("/api/auth")
def auth_status(): return jsonify({"authenticated": bool(uid())})
@app.post("/api/login")
def login():
    body=request.get_json(silent=True) or {}; email=clean(body.get("email"),160).lower(); password=str(body.get("password") or "")[:200]
    with db() as c: user=c.execute("SELECT id,password_hash FROM users WHERE email=?",(email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"],password): return err("Email or password is incorrect.",401)
    session.clear(); session["user_id"]=user["id"]; session.permanent=True
    return jsonify({"ok":True})
@app.post("/api/register")
def register():
    body=request.get_json(silent=True) or {}; name=clean(body.get("name"),80); email=clean(body.get("email"),160).lower(); password=str(body.get("password") or "")
    if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return err("Enter your name and a valid email address.")
    if len(password)<10 or len(password)>200: return err("Use a password between 10 and 200 characters.")
    user_id=os.urandom(16).hex()
    try:
        with db() as c:
            c.execute("INSERT INTO users VALUES(?,?,?,?)",(user_id,email,generate_password_hash(password),now()))
            c.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?)",(user_id,name,"CSE",1,"Software Developer","Undergraduate",now()))
    except DB_INTEGRITY_ERRORS:return err("An account with this email already exists.",409)
    session.clear();session["user_id"]=user_id;session.permanent=True
    return jsonify({"ok":True}),201
@app.post("/api/demo")
def demo_session():
    session.clear();session["user_id"]="demo-"+os.urandom(16).hex();session.permanent=True
    profile_row()
    return jsonify({"ok":True})
@app.post("/api/logout")
def logout(): session.clear();return jsonify({"ok":True})
@app.get("/api/bootstrap")
def bootstrap():
    p = profile_row()
    with db() as c:
        interviews = [dict(x) for x in c.execute("SELECT * FROM interviews WHERE user_id=? ORDER BY id DESC", (uid(),)).fetchall()]
        diagnostics = c.execute("SELECT strategy,created_at FROM diagnostics WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid(),)).fetchone()
    for i in interviews:
        i["areas"] = json.loads(i["areas"]); i["turns"] = json.loads(i["turns"]); i["coding"] = json.loads(i["coding"]) if i["coding"] else None; i["report"] = json.loads(i["report"]) if i["report"] else None
    return jsonify({"profile": p, "curriculum": CURRICULUM, "benchmarks": BENCHMARKS, "interviews": interviews, "diagnostic": json.loads(diagnostics["strategy"]) if diagnostics else None, "aiEnabled": bool(os.environ.get("GEMINI_API_KEY"))})

@app.put("/api/profile")
def save_profile():
    body = request.get_json(silent=True) or {}
    name = clean(body.get("name"), 80) or "Student"
    branch = clean(body.get("branch"), 8).upper(); domain = clean(body.get("domain"), 100); exp = clean(body.get("experience"), 40)
    try: semester = int(body.get("semester"))
    except (ValueError, TypeError): return err("Choose a semester from 1 to 8.")
    if branch not in CURRICULUM or not 1 <= semester <= 8 or not domain: return err("Check your branch, semester, and target role.")
    with db() as c:
        c.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,branch=excluded.branch,semester=excluded.semester,domain=excluded.domain,experience=excluded.experience,updated_at=excluded.updated_at", (uid(), name, branch, semester, domain, exp, now()))
    return jsonify({"ok": True, "profile": profile_row()})

@app.post("/api/diagnostic")
def diagnostic():
    body = request.get_json(silent=True) or {}; answers = body.get("answers", {})
    if not isinstance(answers, dict) or len(answers) > 8: return err("Invalid diagnostic answers.")
    answers = {clean(k, 80): clean(v, 1200) for k,v in answers.items() if clean(v, 1200)}
    if len(answers) < 1: return err("Answer at least one question before generating your strategy.")
    p = profile_row(); weak=[]
    for topic, answer in answers.items():
        if len(answer) < 45 or any(w in answer.lower() for w in ["not sure", "don't know", "idk"]): weak.append(topic)
    fallback = {"refreshers": [{"title": f"{t}: rebuild the fundamentals", "detail": "Your response was brief or uncertain. Revisit the core concept, then explain it with an example."} for t in weak] or [{"title": "Keep core concepts interview-ready", "detail": "Your submitted answers show a starting point. Practice explaining trade-offs and edge cases."}], "gaps": [{"title": x, "detail": "Not typically covered deeply in a standard undergraduate syllabus. Start with a small design exercise and a deployment project."} for x in BENCHMARKS[:3]], "sync": [{"title": s, "detail": f"Connect this semester's {s} concepts to real interview scenarios."} for s in CURRICULUM[p['branch']][p['semester']-1][:2]], "evidence": f"Strategy generated from {len(answers)} submitted diagnostic answer(s)."}
    strategy = gemini_json(f"Student target role: {p['domain']}, branch {p['branch']}, current semester {p['semester']}. Answers: {json.dumps(answers)}. Return JSON with arrays refreshers, gaps, sync; each item title and detail, plus evidence string. Only cite demonstrated weaknesses.", fallback)
    for field in ("refreshers","gaps","sync"):
        if not isinstance(strategy.get(field), list): strategy[field]=fallback[field]
        strategy[field]=[{"title":clean(x.get("title"),120),"detail":clean(x.get("detail"),400)} for x in strategy[field][:5] if isinstance(x,dict)]
    strategy["evidence"] = clean(strategy.get("evidence") or fallback["evidence"],300)
    with db() as c: c.execute("INSERT INTO diagnostics(user_id,answers,strategy,created_at) VALUES(?,?,?,?)", (uid(),json.dumps(answers),json.dumps(strategy),now()))
    return jsonify({"strategy":strategy,"mode":"gemini" if os.environ.get("GEMINI_API_KEY") else "local"})

@app.post("/api/interviews")
def start_interview():
    body=request.get_json(silent=True) or {}; p=profile_row()
    role=clean(body.get("role") or p["domain"],100); difficulty=clean(body.get("difficulty"),20).lower(); areas=body.get("areas",[])
    allowed={"beginner","intermediate","advanced"}; areas=[clean(a,40) for a in areas[:8] if clean(a,40)] if isinstance(areas,list) else []
    if not role or difficulty not in allowed or not areas: return err("Choose a target role, difficulty, and at least one technical area.")
    with db() as c: previous=c.execute("SELECT turns,report FROM interviews WHERE user_id=? ORDER BY id DESC LIMIT 5",(uid(),)).fetchall()
    past_questions=[]; weaknesses=[]
    for prior in previous:
        try:
            past_questions.extend(clean(t.get("question",{}).get("question"),250) for t in json.loads(prior["turns"]) if isinstance(t,dict) and isinstance(t.get("question"),dict))
            if prior["report"]: weaknesses.extend(json.loads(prior["report"]).get("weaknesses",[])[:3])
        except (ValueError,TypeError): pass
    areas=list(dict.fromkeys(areas)); focus=areas[0]
    for weakness in weaknesses:
        match=next((a for a in areas if a.lower() in str(weakness).lower() or str(weakness).lower() in a.lower()),None)
        if match: focus=match;break
    retry_questions=[f"Let's look at {focus} from another angle. Walk me through a real situation where you would apply it, and explain the trade-offs you would consider.",f"Suppose a teammate proposes a quick fix involving {focus}. What questions would you ask before deciding whether it is safe to ship?",f"Imagine the constraints change halfway through a project involving {focus}. How would you reassess your approach and explain the change?"]
    fallback={"question": f"Let's start with {focus}. Describe a problem you solved involving this area, and walk me through how you approached it.", "topic":focus,"kind":"technical"}
    if previous:
        fallback["question"]=next((q for q in retry_questions if q not in past_questions),retry_questions[len(previous)%len(retry_questions)])
        q=fallback
    else:
        q=gemini_json(f"Act as a realistic technical placement interviewer. Role={role}, difficulty={difficulty}, areas={areas}, candidate experience={p['experience']}. Generate one open-ended first question only. Return JSON question, topic, kind. Do not include hints or answer.",fallback)
    q={"question":clean(q.get("question") or fallback["question"],700),"topic":clean(q.get("topic") or areas[0],80),"kind":"technical"}
    q["asked_at"]=now()
    with db() as c:
        sql="INSERT INTO interviews(user_id,role,difficulty,areas,state,turns,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)"
        if DATABASE_URL: sql += " RETURNING id"
        cur=c.execute(sql,(uid(),role,difficulty,json.dumps(areas),"INTRO",json.dumps([{"question":q,"answer":None,"analysis":None}]),now(),now()))
        iid=cur.fetchone()["id"] if DATABASE_URL else cur.lastrowid
    return jsonify({"id":iid,"state":"INTRO","question":q}),201

@app.post("/api/interviews/<int:iid>/begin")
def begin_interview(iid):
    item=interview(iid)
    if not item:return err("Interview not found.",404)
    if item["state"]!="INTRO":return err("This interview has already begun.",409)
    with db() as c:c.execute("UPDATE interviews SET state='TECHNICAL',updated_at=? WHERE id=? AND user_id=?",(now(),iid,uid()))
    return jsonify({"state":"TECHNICAL"})

def interview(iid):
    with db() as c: row=c.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",(iid,uid())).fetchone()
    if not row: return None
    d=dict(row); d["areas"]=json.loads(d["areas"]);d["turns"]=json.loads(d["turns"]);d["coding"]=json.loads(d["coding"]) if d["coding"] else None;d["report"]=json.loads(d["report"]) if d["report"] else None
    return d

@app.get("/api/interviews/<int:iid>")
def get_interview(iid):
    item=interview(iid); return (jsonify(item),200) if item else err("Interview not found.",404)

@app.post("/api/interviews/<int:iid>/answer")
def answer(iid):
    item=interview(iid)
    if not item: return err("Interview not found.",404)
    if item["state"] not in ("TECHNICAL","CODING_FOLLOW_UP"): return err("This interview is not accepting an answer.",409)
    body=request.get_json(silent=True) or {}; text=clean(body.get("answer"),5000)
    if len(text)<2: return err("Write an answer before continuing.")
    turns=item["turns"]
    if turns[-1].get("answer") is not None: return err("This answer was already submitted.",409)
    turns[-1]["answer"]=text
    try: turns[-1]["answer_time_seconds"]=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(turns[-1]["question"].get("asked_at",now()))).total_seconds()))
    except (ValueError,TypeError): turns[-1]["answer_time_seconds"]=None
    evidence="specific example and reasoning" if len(text)>180 else "more detail and a concrete example"
    fallback={"feedback":"Thank you. I noted your response and will explore the reasoning behind it.","question":f"Can you explain the trade-offs in your approach, and what might change if the constraints were different?","topic":turns[-1]["question"].get("topic","Reasoning"),"done":len(turns)>=4}
    p=profile_row(); result=gemini_json(f"Analyze this interview answer briefly without giving tutoring or scores. Role {item['role']}, difficulty {item['difficulty']}, prior question: {turns[-1]['question']['question']}, student answer: {text}. Generate one adaptive interviewer follow-up probing a real gap or reasoning. After 4 total turns set done true. JSON: feedback, question, topic, done boolean.",fallback)
    result={"feedback":clean(result.get("feedback") or fallback["feedback"],350),"question":clean(result.get("question") or fallback["question"],700),"topic":clean(result.get("topic") or fallback["topic"],80),"done":bool(result.get("done",False)) or len(turns)>=4}
    turns[-1]["analysis"]={"feedback":result["feedback"],"evidence":f"Answer length {len(text)} characters; interviewer probed for {evidence}."}
    if result["done"]:
        coding_fallback={"problem":"Given a list of integers and a target value, return the indices of two different elements whose values add up to the target. If no such pair exists, return an empty list. Describe the time and space complexity of your approach and how it handles duplicate values.","language":"Python"}
        generated=gemini_json(f"Generate one practical coding interview task for role {item['role']}, level {item['difficulty']}, selected areas {item['areas']}. It should be solvable in 15 minutes in a plain text editor and include clear input/output behavior and edge cases. Do not label the hidden competency, provide a solution, or add hints. Return JSON with problem and language.",coding_fallback)
        challenge=clean(generated.get("problem") or coding_fallback["problem"],900)
        nextq={"question":challenge,"topic":"Problem solving","kind":"coding","asked_at":now()}; item["state"]="CODING"; item["coding"]={"prompt":challenge,"language":clean(generated.get("language") or coding_fallback["language"],30),"code":"","attempts":0,"hints":0,"started_at":now(),"submitted":False}
    else:
        nextq={"question":result["question"],"topic":result["topic"],"kind":"technical","asked_at":now()}; item["state"]="TECHNICAL"; turns.append({"question":nextq,"answer":None,"analysis":None})
    with db() as c:c.execute("UPDATE interviews SET state=?,turns=?,coding=?,updated_at=? WHERE id=? AND user_id=?",(item["state"],json.dumps(turns),json.dumps(item["coding"]) if item["coding"] else None,now(),iid,uid()))
    return jsonify({"feedback":result["feedback"],"state":item["state"],"question":nextq,"turns":turns})

@app.post("/api/interviews/<int:iid>/coding")
def coding(iid):
    item=interview(iid)
    if not item:return err("Interview not found.",404)
    if item["state"]!="CODING" or not item["coding"]:return err("Coding round is not active.",409)
    body=request.get_json(silent=True) or {}; action=body.get("action"); coding=item["coding"]
    if action=="hint":
        if coding["hints"]>=2:return err("No more hints are available.",409)
        coding["hints"]+=1; response={"hint":"Start by writing down the input and output, then solve a small example by hand. Consider boundary cases."}
    elif action=="save":
        coding["code"]=clean(body.get("code"),10000); coding["language"]=clean(body.get("language"),30) or "Python";response={"saved":True}
    elif action=="submit":
        code=clean(body.get("code"),10000)
        if len(code)<4:return err("Add your solution before submitting.")
        coding["code"]=code;coding["language"]=clean(body.get("language"),30) or "Python";coding["attempts"]+=1;coding["submitted"]=True
        try: coding["time_taken_seconds"]=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(coding["started_at"])).total_seconds()))
        except (ValueError,TypeError): coding["time_taken_seconds"]=None
        fallback={"feedback":"Your solution is saved. Automated code review is unavailable in local mode, and the code has not been run.","followup":"What edge case would you test first, and why?"}
        ev=gemini_json(f"Review code as interview evidence only, do not claim execution. Prompt: {coding['prompt']} Language: {coding['language']} Code: {code}. Return JSON feedback and one followup question.",fallback)
        coding["analysis"]={"feedback":clean(ev.get("feedback") or fallback["feedback"],450)}; coding["followup_question"]=clean(ev.get("followup") or fallback["followup"],400);item["state"]="CODING_FOLLOW_UP";item["turns"].append({"question":{"question":coding["followup_question"],"topic":"Code defense","kind":"followup","asked_at":now()},"answer":None,"analysis":None});response={"submitted":True,"feedback":coding["analysis"]["feedback"],"question":coding["followup_question"],"state":item["state"]}
    else:return err("Unknown coding action.")
    with db() as c:c.execute("UPDATE interviews SET state=?,coding=?,turns=?,updated_at=? WHERE id=?",(item["state"],json.dumps(coding),json.dumps(item["turns"]),now(),iid))
    return jsonify(response)

@app.post("/api/interviews/<int:iid>/finish")
def finish(iid):
    item=interview(iid)
    if not item:return err("Interview not found.",404)
    if item["report"]:return jsonify({"report":item["report"]})
    body=request.get_json(silent=True) or {}; final=clean(body.get("answer"),3000)
    if item["state"]=="CODING_FOLLOW_UP" and item["turns"][-1].get("answer") is None:
        item["turns"][-1]["answer"]=final or "No follow-up response submitted."
        try:item["turns"][-1]["answer_time_seconds"]=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(item["turns"][-1]["question"].get("asked_at",now()))).total_seconds()))
        except (ValueError,TypeError):item["turns"][-1]["answer_time_seconds"]=None
    answered=[t for t in item["turns"] if t.get("answer")]
    substantive=[t for t in answered if len(t["answer"])>120]
    coding=item["coding"] or {}; n=len(answered)
    short=sum(1 for t in answered if len(t["answer"])<100)
    fallback={"overall":None,"categories":[{"name":"Technical understanding","score":None,"evidence":f"{n} response(s) saved. AI scoring is unavailable without a configured Gemini API key.","improvement":"Explain a concept with an example, then describe its trade-offs."},{"name":"Reasoning and follow-up","score":None,"evidence":"Follow-up responses are saved, but semantic evaluation needs the configured AI service.","improvement":"Practice defending decisions when constraints change."},{"name":"Problem solving","score":None,"evidence":"Code was submitted; it was not executed or semantically evaluated in local mode.","improvement":"Walk through edge cases and describe a test strategy."}],"strengths":["Completed a structured technical interview"] if n else [],"weaknesses":["Several responses were brief; add concrete examples and trade-offs."] if short>=2 else [],"hidden_gap":{"title":"Making reasoning explicit","detail":"Several submitted answers were short, leaving little written evidence of examples or decision trade-offs.","evidence":f"{short} of {n} responses were under 100 characters."} if n>=3 and short>=3 else None,"summary":"AI evaluation is unavailable. Your answers and code were saved, but numerical scores are withheld until Gemini is configured. Submitted code was not executed."}
    p=profile_row(); payload=f"Evaluate these interview answers based only on evidence. Candidate role={item['role']}, difficulty={item['difficulty']}. Turns={json.dumps(item['turns'])}. Coding record={json.dumps(coding)}. Return JSON with overall integer 0-100 or null, categories array (name, score 0-100 or null, evidence, improvement), strengths string array, weaknesses string array, hidden_gap {{title,detail,evidence}} or null, summary. Use null for insufficient evidence, never claim code was executed."
    report=gemini_json(payload,fallback)
    # Clamp and normalize untrusted generated data.
    raw_categories=report.get("categories") if isinstance(report.get("categories"),list) else fallback["categories"]
    report["categories"]=[{"name":clean(x.get("name"),80),"score":max(0,min(100,int(x["score"]))) if isinstance(x.get("score"),(int,float)) and clean(x.get("evidence"),400) else None,"evidence":clean(x.get("evidence"),400),"improvement":clean(x.get("improvement"),300)} for x in raw_categories[:8] if isinstance(x,dict) and clean(x.get("name"),80)]
    report["overall"]=max(0,min(100,int(report["overall"]))) if isinstance(report.get("overall"),(int,float)) and len(answered)>=3 else None
    if not os.environ.get("GEMINI_API_KEY"):
        report=fallback
    report["strengths"]=[clean(x,160) for x in (report.get("strengths") if isinstance(report.get("strengths"),list) else fallback["strengths"])[:6] if isinstance(x,str)];report["weaknesses"]=[clean(x,160) for x in (report.get("weaknesses") if isinstance(report.get("weaknesses"),list) else fallback["weaknesses"])[:6] if isinstance(x,str)]
    gap=report.get("hidden_gap")
    report["hidden_gap"]={"title":clean(gap.get("title"),100),"detail":clean(gap.get("detail"),400),"evidence":clean(gap.get("evidence"),400)} if isinstance(gap,dict) and len(answered)>=3 and all(clean(gap.get(k),400) for k in ("title","detail","evidence")) else None
    if len(answered)<3:
        report["overall"]=None
        for category in report["categories"]: category["score"]=None
    report["summary"]=clean(report.get("summary") or fallback["summary"],600);report["evidence_count"]=len(answered);report["created_at"]=now()
    with db() as c:c.execute("UPDATE interviews SET state='REPORT',turns=?,report=?,updated_at=? WHERE id=?",(json.dumps(item["turns"]),json.dumps(report),now(),iid))
    return jsonify({"report":report})

@app.get("/api/mentor")
def mentor():
    with db() as c: rows=c.execute("SELECT id,role,report,created_at FROM interviews WHERE user_id=? AND report IS NOT NULL ORDER BY id DESC",(uid(),)).fetchall()
    vals=[]
    for row in rows:
        r=json.loads(row["report"]); vals.append({"id":row["id"],"role":row["role"],"created_at":row["created_at"],"weaknesses":r.get("weaknesses",[]),"strengths":r.get("strengths",[]),"hidden_gap":r.get("hidden_gap")})
    repeats={}
    for v in vals:
        for w in v["weaknesses"]: repeats[w]=repeats.get(w,0)+1
    return jsonify({"interviews":vals,"repeated":[{"topic":k,"count":v} for k,v in repeats.items() if v>=2],"focus":vals[0]["weaknesses"][:3] if vals else [],"message":"Your mentor uses evidence from completed interviews. Complete an interview to receive personalized focus areas." if not vals else "Focus areas below are drawn from your latest completed interview and recurring patterns in your saved history."})

@app.errorhandler(500)
def server_error(_): return jsonify({"error":"Something went wrong. Your saved work is still available; please retry."}),500

init_db()
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=os.environ.get("FLASK_DEBUG")=="1")
