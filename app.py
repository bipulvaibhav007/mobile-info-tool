from flask import Flask, render_template, request, redirect, url_for, abort
import phonenumbers
from phonenumbers import geocoder, carrier, timezone
import requests
import os
import sqlite3
import secrets
from datetime import datetime

app = Flask(__name__)

# ----------------- MOBILE NUMBER API -----------------
API_KEY = "78a799be5bbb4569a0dce405e67141f4"
API_URL = "http://apilayer.net/api/validate"


def get_api_location(number):
    params = {
        "access_key": API_KEY,
        "number": number,
        "format": 1
    }
    try:
        response = requests.get(API_URL, params=params)
        return response.json()
    except:
        return None


# ----------------- VISITOR TRACKER CONFIG -----------------
DB_PATH = os.path.join(os.path.dirname(__file__), "visitors.db")
IP_API_URL = "http://ipwho.is/"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # table for tracking links
    cur.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            target_url TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # table for visitor logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            ip TEXT,
            user_agent TEXT,
            referrer TEXT,
            country TEXT,
            region TEXT,
            city TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (link_id) REFERENCES links(id)
        )
    """)

    conn.commit()
    conn.close()


def generate_slug(length=6):
    return secrets.token_urlsafe(length)[:length]


def get_geo(ip_address):
    try:
        resp = requests.get(f"{IP_API_URL}{ip_address}", timeout=5)
        data = resp.json()
        return (
            data.get("country"),
            data.get("region"),
            data.get("city")
        )
    except:
        return None, None, None


# ----------------- ROUTES -----------------
# ---------- Mobile Number Home ----------
@app.route("/", methods=['GET', 'POST'])
def home():
    info = None
    error = None

    if request.method == "POST":
        number = request.form.get("mobile")

        try:
            parsed = phonenumbers.parse(number)

            # Basic data
            country_name = geocoder.country_name_for_number(parsed, "en")
            carrier_name = carrier.name_for_number(parsed, "en")
            timezone_data = timezone.time_zones_for_number(parsed)
            is_valid = phonenumbers.is_valid_number(parsed)
            is_possible = phonenumbers.is_possible_number(parsed)

            # Get state
            api_data = get_api_location(number)

            state = None
            line_type = None
            if api_data and api_data.get("valid"):
                state = api_data.get("location")
                line_type = api_data.get("line_type")

            info = {
                "number": number,
                "country": country_name,
                "state": state if state else "Not Available",
                "carrier": carrier_name,
                "timezone": timezone_data,
                "line_type": line_type if line_type else "Unknown",
                "valid": is_valid,
                "possible": is_possible
            }

        except Exception as e:
            error = "Invalid number. Use +91xxxxxxxxxx"

    return render_template("index.html", info=info, error=error)


# ---------- Visitor Tracker Home ----------
@app.route("/tracker", methods=["GET", "POST"])
def tracker():
    conn = get_db()
    cur = conn.cursor()

    new_link = None
    tracking_url = None

    if request.method == "POST":
        target_url = request.form.get("target_url")

        if target_url and not target_url.startswith(("http://", "https://")):
            target_url = "https://" + target_url

        slug = generate_slug()

        cur.execute(
            "INSERT INTO links (slug, target_url, created_at) VALUES (?, ?, ?)",
            (slug, target_url, datetime.utcnow().isoformat())
        )
        conn.commit()

        new_link = cur.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
        tracking_url = url_for("track_redirect", slug=new_link["slug"], _external=True)

    links = cur.execute("SELECT * FROM links ORDER BY id DESC").fetchall()
    conn.close()

    return render_template("tracker.html",
                           new_link=new_link,
                           tracking_url=tracking_url,
                           links=links)


# ---------- Tracking Link Handler ----------
@app.route("/t/<slug>")
def track_redirect(slug):
    conn = get_db()
    cur = conn.cursor()

    link = cur.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()

    if not link:
        conn.close()
        abort(404)

    # Visitor info
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    ua = request.headers.get("User-Agent", "")
    ref = request.referrer

    country, region, city = get_geo(ip)

    cur.execute("""
        INSERT INTO visits (
            link_id, ip, user_agent, referrer, country, region, city, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        link["id"], ip, ua, ref, country, region, city,
        datetime.utcnow().isoformat()
    ))

    conn.commit()
    conn.close()

    return redirect(link["target_url"])


# ---------- Stats Page ----------
@app.route("/stats/<slug>")
def stats(slug):
    conn = get_db()
    cur = conn.cursor()

    link = cur.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
    visits = cur.execute("SELECT * FROM visits WHERE link_id=? ORDER BY id DESC", (link["id"],)).fetchall()

    conn.close()

    return render_template("stats.html", link=link, visits=visits)

# ---------------- DELETE A LINK ----------------
@app.route("/delete/<slug>")
def delete_link(slug):
    conn = get_db()
    cur = conn.cursor()

    # get the link
    link = cur.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()

    if not link:
        conn.close()
        return "Link not found", 404

    # delete all visits for the link
    cur.execute("DELETE FROM visits WHERE link_id=?", (link["id"],))

    # delete the link
    cur.execute("DELETE FROM links WHERE id=?", (link["id"],))
    conn.commit()
    conn.close()

    return redirect(url_for("tracker"))


# ---------------- CLEAN LOGS FOR ONE LINK ----------------
@app.route("/clean/<slug>")
def clean_logs(slug):
    conn = get_db()
    cur = conn.cursor()

    link = cur.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()

    if not link:
        conn.close()
        return "Link not found", 404

    cur.execute("DELETE FROM visits WHERE link_id=?", (link["id"],))
    conn.commit()
    conn.close()

    return redirect(url_for("stats", slug=slug))


# ---------------- DELETE ALL DATA ----------------
@app.route("/delete_all")
def delete_all():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("DELETE FROM visits")
    cur.execute("DELETE FROM links")
    conn.commit()
    conn.close()

    return redirect(url_for("tracker"))

# Start App
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
