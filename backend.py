import json
import os
import random
import sqlite3
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Vercel's filesystem is read-only except /tmp; local development retains database.db.
DATABASE = "/tmp/database.db" if os.getenv("VERCEL") else os.path.join(BASE_DIR, "database.db")
VIT_EMAIL_SUFFIX = os.getenv("VIT_EMAIL_SUFFIX", "@vitstudent.ac.in").lower()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "devjams-demo-change-me")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    connection = sqlite3.connect(DATABASE)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            github_username TEXT,
            leetcode_username TEXT,
            elo INTEGER NOT NULL DEFAULT 1000,
            matches_played INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_one_id INTEGER NOT NULL,
            player_two_id INTEGER NOT NULL,
            winner_id INTEGER,
            topic TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(player_one_id) REFERENCES users(id),
            FOREIGN KEY(player_two_id) REFERENCES users(id),
            FOREIGN KEY(winner_id) REFERENCES users(id)
        );
    """)
    connection.commit()
    connection.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    return db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def fetch_github(username):
    if not username:
        return None
    try:
        response = requests.get(f"https://api.github.com/users/{username}", timeout=5)
        response.raise_for_status()
        data = response.json()
        return {"public_repos": data.get("public_repos", 0), "followers": data.get("followers", 0), "avatar": data.get("avatar_url")}
    except requests.RequestException:
        return None


def fetch_leetcode(username):
    # Public endpoint; failure simply leaves profile stats unavailable for the offline demo.
    if not username:
        return None
    try:
        response = requests.get(f"https://leetcode-stats-api.herokuapp.com/{username}", timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            return None
        return {"solved": data.get("totalSolved", 0), "easy": data.get("easySolved", 0), "medium": data.get("mediumSolved", 0), "hard": data.get("hardSolved", 0)}
    except requests.RequestException:
        return None


def expected_score(elo_a, elo_b):
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))


def update_elo(winner, loser):
    k = 32
    winner_elo = round(winner["elo"] + k * (1 - expected_score(winner["elo"], loser["elo"])))
    loser_elo = round(loser["elo"] + k * (0 - expected_score(loser["elo"], winner["elo"])))
    connection = db()
    connection.execute("UPDATE users SET elo = ?, wins = wins + 1, matches_played = matches_played + 1 WHERE id = ?", (winner_elo, winner["id"]))
    connection.execute("UPDATE users SET elo = ?, matches_played = matches_played + 1 WHERE id = ?", (loser_elo, loser["id"]))
    connection.commit()
    return winner_elo, loser_elo


def generate_breakdown(match, me, won):
    """Use OpenAI when configured; retain a reliable offline judging experience otherwise."""
    fallback = {
        "headline": "Strong finish under pressure." if won else "A useful loss with clear next steps.",
        "right": "You committed to a solution path and kept the match moving." if won else "You stayed engaged through a challenging problem.",
        "improve": f"Practice {match['topic']} patterns and spend the first 5 minutes translating constraints into an approach.",
        "next": random.choice(["Try a rematch at the same difficulty.", "Queue one Medium problem in this topic.", "Write down the key pattern before your next match."])
    }
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return fallback, False
    prompt = (f"Give concise coaching to {me['name']} after a {match['difficulty']} "
              f"{match['topic']} coding duel. They {'won' if won else 'lost'}. "
              "Return JSON with headline, right, improve, and next; each value under 35 words.")
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}},
            timeout=15,
        )
        response.raise_for_status()
        return json.loads(response.json()["choices"][0]["message"]["content"]), True
    except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError):
        return fallback, False


@app.route("/")
def index():
    return redirect(url_for("profile") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
        else:
            session["user_id"] = user["id"]
            return redirect(url_for("profile"))
    return render_template("login.html", mode="login", vit_suffix=VIT_EMAIL_SUFFIX)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if not email.endswith(VIT_EMAIL_SUFFIX):
            flash(f"Use your VIT email ({VIT_EMAIL_SUFFIX}).", "error")
        elif len(password) < 6 or not name:
            flash("Enter your name and a password of at least 6 characters.", "error")
        else:
            try:
                cursor = db().execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)", (name, email, generate_password_hash(password)))
                db().commit()
                session["user_id"] = cursor.lastrowid
                return redirect(url_for("profile"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
    return render_template("login.html", mode="signup", vit_suffix=VIT_EMAIL_SUFFIX)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        db().execute("UPDATE users SET github_username = ?, leetcode_username = ? WHERE id = ?", (request.form.get("github_username", "").strip(), request.form.get("leetcode_username", "").strip(), user["id"]))
        db().commit()
        flash("Profile connections saved.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", user=user, github=fetch_github(user["github_username"]), leetcode=fetch_leetcode(user["leetcode_username"]))


@app.route("/match", methods=["GET", "POST"])
@login_required
def match():
    user = current_user()
    opponents = db().execute("SELECT * FROM users WHERE id != ? ORDER BY ABS(elo - ?) LIMIT 5", (user["id"], user["elo"])).fetchall()
    if request.method == "POST":
        opponent_id = request.form.get("opponent_id", type=int)
        opponent = db().execute("SELECT * FROM users WHERE id = ?", (opponent_id,)).fetchone()
        if not opponent or opponent["id"] == user["id"]:
            flash("Choose a valid opponent.", "error")
        else:
            cursor = db().execute("INSERT INTO matches (player_one_id, player_two_id, topic, difficulty, duration_minutes) VALUES (?, ?, ?, ?, ?)", (user["id"], opponent["id"], request.form["topic"], request.form["difficulty"], request.form.get("duration", 30, type=int)))
            db().commit()
            return redirect(url_for("play_match", match_id=cursor.lastrowid))
    return render_template("match.html", user=user, opponents=opponents)


@app.route("/match/<int:match_id>", methods=["GET", "POST"])
@login_required
def play_match(match_id):
    match = db().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if not match or session["user_id"] not in (match["player_one_id"], match["player_two_id"]):
        return redirect(url_for("match"))
    if request.method == "POST" and not match["winner_id"]:
        winner_id = request.form.get("winner_id", type=int)
        if winner_id not in (match["player_one_id"], match["player_two_id"]):
            flash("Choose a valid winner.", "error")
        else:
            winner = db().execute("SELECT * FROM users WHERE id = ?", (winner_id,)).fetchone()
            loser_id = match["player_two_id"] if winner_id == match["player_one_id"] else match["player_one_id"]
            loser = db().execute("SELECT * FROM users WHERE id = ?", (loser_id,)).fetchone()
            update_elo(winner, loser)
            db().execute("UPDATE matches SET winner_id = ? WHERE id = ?", (winner_id, match_id))
            db().commit()
            return redirect(url_for("breakdown", match_id=match_id))
    players = db().execute("SELECT id, name, elo FROM users WHERE id IN (?, ?)", (match["player_one_id"], match["player_two_id"])).fetchall()
    return render_template("match_play.html", match=match, players=players)


@app.route("/breakdown/<int:match_id>")
@login_required
def breakdown(match_id):
    match = db().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if not match or not match["winner_id"]:
        return redirect(url_for("match"))
    winner = db().execute("SELECT * FROM users WHERE id = ?", (match["winner_id"],)).fetchone()
    me = current_user()
    won = winner["id"] == me["id"]
    analysis, ai_used = generate_breakdown(match, me, won)
    return render_template("breakdown.html", match=match, winner=winner, me=me, analysis=analysis, ai_used=ai_used)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    # Vercel imports the module instead of executing __main__.
    init_db()
