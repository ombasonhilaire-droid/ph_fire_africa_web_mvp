# 1. Bibliothèques standards (Outils de base)
import os
import re
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

# 2. Bibliothèques tierces (Outils installés)
from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, session, url_for, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import google.generativeai as genai

# 3. Chargement du coffre-fort (.env)
load_dotenv()

# 4. Initialisation de l'Application
app = Flask(__name__)

# 5. Configuration sécurisée (On puise dans le coffre-fort)
app.secret_key = os.getenv('FLASK_SECRET_KEY')

# 6. Configuration de l'Intelligence Artificielle
api_key_ia = os.getenv('GEMINI_API_KEY')
if api_key_ia:
    genai.configure(api_key=api_key_ia)
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    print("⚠️ ATTENTION : La clé GEMINI_API_KEY est manquante dans le fichier .env")
APP_NAME = "PH FIRE AFRICA"
THEME_COLOR = "#ff2d8d"  # rose par défaut

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "ph_fire_africa.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{6,15}$")

@app.route('/rechercher')
def rechercher():
    if not session.get('utilisateur_id'):
        return redirect(url_for('login'))
    
    # On récupère ce que l'utilisateur a tapé dans la barre
    requete = request.args.get('q', '').strip()
    resultats = []
    if requete:
        conn = get_db()
        # On cherche dans la table users (nom_utilisateur ou display_name)
        query = "SELECT id, username, display_name FROM users WHERE username LIKE ? OR display_name LIKE ?"
        resultats = conn.execute(query, ('%' + requete + '%', '%' + requete + '%')).fetchall()
        conn.close()

    return render_template('recherche.html', resultats=resultats, mot_cle=requete)

def utcnow_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB upload max
    app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    @app.before_request
    def _ensure_db():
        init_db_if_needed()

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def inject_globals():
        return {
            "APP_NAME": APP_NAME,
            "THEME_COLOR": THEME_COLOR,
            "me": current_user(),
            "unread_notifications": count_unread_notifications(),
        }

    # ---------- AUTH ----------

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user():
                flash("Connecte-toi d'abord.", "warn")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    @app.get("/")
    def index():
        if current_user():
            return redirect(url_for("feed"))
        return render_template("landing.html")

    @app.get("/signup")
    def signup():
        return render_template("signup.html")

    @app.post("/signup")
    def signup_post():
        username = (request.form.get("username") or "").strip()
        display_name = (request.form.get("display_name") or "").strip() or username
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""

        if not USERNAME_RE.match(username):
            flash("Nom d'utilisateur invalide (3–20 caractères, lettres/chiffres/_).", "error")
            return redirect(url_for("signup"))
        if not (EMAIL_RE.match(identifier) or PHONE_RE.match(identifier)):
            flash("Entre un email ou un numéro de téléphone (ex: +243...).", "error")
            return redirect(url_for("signup"))
        if len(password) < 6:
            flash("Mot de passe trop court (min 6).", "error")
            return redirect(url_for("signup"))

        pw_hash = generate_password_hash(password)
        try:
            db_execute(
                "INSERT INTO users(username, identifier, display_name, bio, password_hash, created_at) "
                "VALUES (?, ?, ?, '', ?, ?)",
                (username.lower(), identifier, display_name, pw_hash, utcnow_iso()),
            )
        except sqlite3.IntegrityError:
            flash("Ce nom d'utilisateur ou cet identifiant existe déjà.", "error")
            return redirect(url_for("signup"))

        user = db_one("SELECT * FROM users WHERE username = ?", (username.lower(),))
        session["user_id"] = user["id"]
        flash("Bienvenue sur PH FIRE AFRICA !", "ok")
        return redirect(url_for("feed"))

    @app.get("/login")
    def login():
        return render_template("login.html", next=request.args.get("next") or "")

    @app.post("/login")
    def login_post():
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        next_url = (request.form.get("next") or "").strip() or url_for("feed")

        user = db_one("SELECT * FROM users WHERE identifier = ? OR username = ?", (identifier, identifier.lower()))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Identifiant ou mot de passe incorrect.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        flash("Connexion réussie.", "ok")
        return redirect(next_url)

    @app.get("/logout")
    def logout():
        session.clear()
        flash("Déconnecté.", "ok")
        return redirect(url_for("index"))

    # ---------- FEED / POSTS ----------

    @app.get("/feed")
    @login_required
    def feed():
        me = current_user()
        posts = get_feed_posts(me["id"])
        suggestions = get_suggestions(me["id"])
        return render_template("feed.html", posts=posts, suggestions=suggestions)

    @app.get("/explore")
    @login_required
    def explore():
        posts = get_explore_posts()
        return render_template("explore.html", posts=posts)

    @app.post("/post")
    @login_required
    def create_post():
        me = current_user()
        content = (request.form.get("content") or "").strip()
        if not content and not request.files.get("image"):
            flash("Écris quelque chose ou ajoute une image.", "warn")
            return redirect(request.referrer or url_for("feed"))
        if len(content) > 500:
            flash("Post trop long (max 500 caractères).", "error")
            return redirect(request.referrer or url_for("feed"))

        image_filename = None
        image = request.files.get("image")
        if image and image.filename:
            safe_name = secure_filename(image.filename)
            stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            image_filename = f"{me['id']}_{stamp}_{safe_name}"
            image.save(str(UPLOAD_DIR / image_filename))

        db_execute(
            "INSERT INTO posts(user_id, content, image_filename, created_at) VALUES (?, ?, ?, ?)",
            (me["id"], content, image_filename, utcnow_iso()),
        )
        flash("Publié ✅", "ok")
        return redirect(request.referrer or url_for("feed"))

    @app.post("/like/<int:post_id>")
    @login_required
    def toggle_like(post_id: int):
        me = current_user()
        liked = db_one("SELECT 1 FROM likes WHERE user_id=? AND post_id=?", (me["id"], post_id))
        post = db_one("SELECT * FROM posts WHERE id=?", (post_id,))
        if not post:
            return ("Not found", 404)

        if liked:
            db_execute("DELETE FROM likes WHERE user_id=? AND post_id=?", (me["id"], post_id))
        else:
            db_execute("INSERT OR IGNORE INTO likes(user_id, post_id, created_at) VALUES (?, ?, ?)",
                       (me["id"], post_id, utcnow_iso()))
            if post["user_id"] != me["id"]:
                create_notification(user_id=post["user_id"], actor_id=me["id"], ntype="like", post_id=post_id)

        return redirect(request.referrer or url_for("feed"))

    @app.post("/comment/<int:post_id>")
    @login_required
    def add_comment(post_id: int):
        me = current_user()
        content = (request.form.get("content") or "").strip()
        if not content:
            flash("Commentaire vide.", "warn")
            return redirect(request.referrer or url_for("feed"))
        if len(content) > 300:
            flash("Commentaire trop long (max 300).", "error")
            return redirect(request.referrer or url_for("feed"))

        post = db_one("SELECT * FROM posts WHERE id=?", (post_id,))
        if not post:
            return ("Not found", 404)

        db_execute(
            "INSERT INTO comments(post_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (post_id, me["id"], content, utcnow_iso()),
        )
        if post["user_id"] != me["id"]:
            create_notification(user_id=post["user_id"], actor_id=me["id"], ntype="comment", post_id=post_id)

        flash("Commentaire envoyé.", "ok")
        return redirect(request.referrer or url_for("feed"))

    # ---------- PROFILES / FOLLOW ----------

    @app.get("/u/<username>")
    @login_required
    def profile(username: str):
        user = db_one("SELECT * FROM users WHERE username=?", (username.lower(),))
        if not user:
            return ("Not found", 404)

        me = current_user()
        is_me = (me["id"] == user["id"])
        is_following = bool(db_one("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?",
                                   (me["id"], user["id"]))) if not is_me else False

        stats = {
            "posts": db_one("SELECT COUNT(*) AS c FROM posts WHERE user_id=?", (user["id"],))["c"],
            "followers": db_one("SELECT COUNT(*) AS c FROM follows WHERE followed_id=?", (user["id"],))["c"],
            "following": db_one("SELECT COUNT(*) AS c FROM follows WHERE follower_id=?", (user["id"],))["c"],
        }
        posts = db_all(
            "SELECT p.*, u.username, u.display_name, "
            "(SELECT COUNT(*) FROM likes WHERE post_id=p.id) AS like_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count, "
            "(SELECT 1 FROM likes WHERE user_id=? AND post_id=p.id) AS liked_by_me "
            "FROM posts p JOIN users u ON u.id=p.user_id "
            "WHERE p.user_id=? ORDER BY p.created_at DESC LIMIT 50",
            (me["id"], user["id"]),
        )

        return render_template("profile.html", user=user, stats=stats, posts=posts,
                               is_me=is_me, is_following=is_following)

    @app.post("/follow/<username>")
    @login_required
    def toggle_follow(username: str):
        me = current_user()
        other = db_one("SELECT * FROM users WHERE username=?", (username.lower(),))
        if not other or other["id"] == me["id"]:
            return redirect(request.referrer or url_for("feed"))

        exists = db_one("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?",
                        (me["id"], other["id"]))
        if exists:
            db_execute("DELETE FROM follows WHERE follower_id=? AND followed_id=?", (me["id"], other["id"]))
        else:
            db_execute("INSERT OR IGNORE INTO follows(follower_id, followed_id, created_at) VALUES (?, ?, ?)",
                       (me["id"], other["id"], utcnow_iso()))
            create_notification(user_id=other["id"], actor_id=me["id"], ntype="follow", post_id=None)

        return redirect(request.referrer or url_for("profile", username=other["username"]))

    # ---------- MESSAGES ----------

    @app.get("/messages")
    @login_required
    def messages():
        me = current_user()
        threads = get_message_threads(me["id"])
        return render_template("messages.html", threads=threads)

    @app.route("/messages/<username>", methods=["GET", "POST"])
    @login_required
    def thread(username: str):
        me = current_user()
        other = db_one("SELECT * FROM users WHERE username=?", (username.lower(),))
        if not other or other["id"] == me["id"]:
            return redirect(url_for("messages"))

        if request.method == "POST":
            content = (request.form.get("content") or "").strip()
            if content:
                db_execute(
                    "INSERT INTO messages(sender_id, recipient_id, content, created_at, is_read) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (me["id"], other["id"], content[:1000], utcnow_iso()),
                )
                flash("Message envoyé.", "ok")
            return redirect(url_for("thread", username=other["username"]))

        # mark as read
        db_execute("UPDATE messages SET is_read=1 WHERE sender_id=? AND recipient_id=?",
                   (other["id"], me["id"]))

        msgs = db_all(
            "SELECT m.*, su.username AS sender_username, su.display_name AS sender_display_name, "
            "ru.username AS recipient_username, ru.display_name AS recipient_display_name "
            "FROM messages m "
            "JOIN users su ON su.id=m.sender_id "
            "JOIN users ru ON ru.id=m.recipient_id "
            "WHERE (m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?) "
            "ORDER BY m.created_at ASC LIMIT 200",
            (me["id"], other["id"], other["id"], me["id"]),
        )
        return render_template("thread.html", other=other, messages=msgs)

    # ---------- NOTIFICATIONS ----------

    @app.get("/notifications")
    @login_required
    def notifications():
        me = current_user()
        rows = db_all(
            "SELECT n.*, a.username AS actor_username, a.display_name AS actor_display_name, "
            "p.content AS post_content "
            "FROM notifications n "
            "JOIN users a ON a.id=n.actor_id "
            "LEFT JOIN posts p ON p.id=n.post_id "
            "WHERE n.user_id=? ORDER BY n.created_at DESC LIMIT 100",
            (me["id"],),
        )
        return render_template("notifications.html", notifications=rows)

    @app.post("/notifications/read_all")
    @login_required
    def notifications_read_all():
        me = current_user()
        db_execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (me["id"],))
        return redirect(url_for("notifications"))

    # ---------- SETTINGS ----------

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        me = current_user()
        if request.method == "POST":
            display_name = (request.form.get("display_name") or "").strip() or me["display_name"]
            bio = (request.form.get("bio") or "").strip()[:280]
            db_execute("UPDATE users SET display_name=?, bio=? WHERE id=?", (display_name, bio, me["id"]))
            flash("Profil mis à jour.", "ok")
            return redirect(url_for("settings"))
        me = current_user()
        return render_template("settings.html", user=me)

    # ---------- API (JSON) ----------
    
    @app.get("/api/me")
    def api_me():
        me = current_user()
        if not me:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return jsonify({"ok": True, "me": dict(me)})

    @app.get("/api/posts")
    def api_posts():
        limit = min(int(request.args.get("limit", 20)), 50)
        rows = db_all(
            "SELECT p.*, u.username, u.display_name, "
            "(SELECT COUNT(*) FROM likes WHERE post_id=p.id) AS like_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count "
            "FROM posts p JOIN users u ON u.id=p.user_id "
            "ORDER BY p.created_at DESC LIMIT ?",
            (limit,),
        )
        return jsonify({"ok": True, "posts": [dict(r) for r in rows]})
    

    # ---------- CLI ----------

    @app.cli.command("init-db")
    def init_db_command():
        init_db(force=True)
        print("✅ Base de données initialisée.")

    @app.cli.command("seed-demo")
    def seed_demo_command():
        seed_demo()
        print("✅ Données démo créées: comptes demo1 / demo2 (mot de passe: demo123).")

    # ---------- HELPERS ----------

    def db_conn():
        db = getattr(g, "db", None)
        if db is None:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
            g.db = db
        return db

    def db_one(sql: str, params=()):
        cur = db_conn().execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row

    def db_all(sql: str, params=()):
        cur = db_conn().execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows

    def db_execute(sql: str, params=()):
        conn = db_conn()
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(sql, params)
        conn.commit()

    def init_db(force: bool = False):
        if DB_PATH.exists() and not force:
            return
        init_db_if_needed(force=True)

    def init_db_if_needed(force: bool = False):
        if DB_PATH.exists() and not force:
            return
        DB_PATH.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON;")
        schema_path = BASE_DIR / "schema.sql"
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
        conn.close()

    def current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        return db_one("SELECT id, username, identifier, display_name, bio, created_at FROM users WHERE id=?", (uid,))

    def create_notification(user_id: int, actor_id: int, ntype: str, post_id):
        # ignore self
        if user_id == actor_id:
            return
        db_execute(
            "INSERT INTO notifications(user_id, actor_id, ntype, post_id, created_at, is_read) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (user_id, actor_id, ntype, post_id, utcnow_iso()),
        )

    def count_unread_notifications() -> int:
        me = current_user()
        if not me:
            return 0
        row = db_one("SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND is_read=0", (me["id"],))
        return int(row["c"] or 0)

    def get_feed_posts(user_id: int):
        # posts du user + ceux qu'il suit
        return db_all(
            "WITH followed AS (SELECT followed_id FROM follows WHERE follower_id=?) "
            "SELECT p.*, u.username, u.display_name, "
            "(SELECT COUNT(*) FROM likes WHERE post_id=p.id) AS like_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count, "
            "(SELECT 1 FROM likes WHERE user_id=? AND post_id=p.id) AS liked_by_me "
            "FROM posts p "
            "JOIN users u ON u.id=p.user_id "
            "WHERE p.user_id=? OR p.user_id IN (SELECT followed_id FROM followed) "
            "ORDER BY p.created_at DESC LIMIT 80",
            (user_id, user_id, user_id),
        )

    def get_explore_posts():
        me = current_user()
        my_id = me["id"]
        return db_all(
            "SELECT p.*, u.username, u.display_name, "
            "(SELECT COUNT(*) FROM likes WHERE post_id=p.id) AS like_count, "
            "(SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count, "
            "(SELECT 1 FROM likes WHERE user_id=? AND post_id=p.id) AS liked_by_me "
            "FROM posts p JOIN users u ON u.id=p.user_id "
            "ORDER BY p.created_at DESC LIMIT 80",
            (my_id,),
        )

    def get_suggestions(user_id: int):
        return db_all(
            "SELECT id, username, display_name FROM users "
            "WHERE id != ? AND id NOT IN (SELECT followed_id FROM follows WHERE follower_id=?) "
            "ORDER BY created_at DESC LIMIT 5",
            (user_id, user_id),
        )

    def get_message_threads(user_id: int):
        # Liste des interlocuteurs avec dernier message
        rows = db_all(
            "WITH allmsgs AS ("
            "  SELECT CASE WHEN sender_id=? THEN recipient_id ELSE sender_id END AS other_id, "
            "         MAX(created_at) AS last_ts "
            "  FROM messages "
            "  WHERE sender_id=? OR recipient_id=? "
            "  GROUP BY other_id"
            ") "
            "SELECT u.username, u.display_name, "
            "       (SELECT content FROM messages m "
            "        WHERE ((m.sender_id=? AND m.recipient_id=u.id) OR (m.sender_id=u.id AND m.recipient_id=?)) "
            "        ORDER BY created_at DESC LIMIT 1) AS last_content, "
            "       (SELECT COUNT(*) FROM messages m "
            "        WHERE m.sender_id=u.id AND m.recipient_id=? AND m.is_read=0) AS unread_count, "
            "       allmsgs.last_ts AS last_ts "
            "FROM allmsgs "
            "JOIN users u ON u.id=allmsgs.other_id "
            "ORDER BY allmsgs.last_ts DESC",
            (user_id, user_id, user_id, user_id, user_id, user_id),
        )
        return rows

    def seed_demo():
        # create two demo users if none
        existing = db_one("SELECT COUNT(*) AS c FROM users", ())["c"]
        if existing and int(existing) > 0:
            return

        demo_users = [
            ("demo1", "demo1@phfire.africa", "Demo 1"),
            ("demo2", "demo2@phfire.africa", "Demo 2"),
        ]
        for username, identifier, display_name in demo_users:
            try:
                db_execute(
                    "INSERT INTO users(username, identifier, display_name, bio, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, identifier, display_name, "Compte démo.", generate_password_hash("demo123"), utcnow_iso()),
                )
            except sqlite3.IntegrityError:
                pass

        u1 = db_one("SELECT * FROM users WHERE username='demo1'", ())
        u2 = db_one("SELECT * FROM users WHERE username='demo2'", ())
        if not u1 or not u2:
            return

        db_execute("INSERT OR IGNORE INTO follows(follower_id, followed_id, created_at) VALUES (?, ?, ?)",
                   (u1["id"], u2["id"], utcnow_iso()))
        db_execute("INSERT OR IGNORE INTO follows(follower_id, followed_id, created_at) VALUES (?, ?, ?)",
                   (u2["id"], u1["id"], utcnow_iso()))

        posts = [
            (u1["id"], "Bienvenue sur PH FIRE AFRICA 🇨🇩🔥 — version web MVP.", None),
            (u2["id"], "Objectif: réseau social + messagerie + notifications + profil.", None),
            (u1["id"], "Prochaine étape: paiements Mobile Money, marketplace, IA…", None),
        ]
        for uid, content, img in posts:
            db_execute("INSERT INTO posts(user_id, content, image_filename, created_at) VALUES (?, ?, ?, ?)",
                       (uid, content, img, utcnow_iso()))
    # MINE 

    @app.get("/stats-mine")
    @login_required
    def stats_mine():
        # 1. Action : Lire le nombre de bâtisseurs
        res_users = db_one("SELECT COUNT(*) as total FROM users")
        nombre_utilisateurs = res_users['total']
        
        # 2. Action : Calculer la somme totale des gains dans la mine
        # On utilise SUM(total_earnings) pour additionner tous les portefeuilles
        res_wealth = db_one("SELECT SUM(total_earnings) as richesse FROM wallets")
        
        # Si la mine est vide, richesse sera None, on force à 0.0
        richesse_totale = res_wealth['richesse'] if res_wealth['richesse'] else 0.0
        
        # 3. Réponse digne et riche
        return f"""
        <div style="font-family: sans-serif; padding: 20px; color: #f3f3f6; background: #0b0b10; border-radius: 15px;">
            <h1 style="color: #ff2d8d;">📊 Rapport de la Mine PH FIRE AFRICA</h1>
            <p style="font-size: 1.2em;">👷 <b>Bâtisseurs Debout :</b> {nombre_utilisateurs}</p>
            <p style="font-size: 1.2em; color: #2bd576;">💰 <b>Richesse Totale Créée :</b> {richesse_totale:.2f} $</p>
            <hr style="border: 0.5px solid rgba(255,255,255,0.1);">
            <p style="font-size: 0.9em; color: #a7a7b4;"><i>"Le savoir produit une valeur que personne ne peut voler."</i></p>
        </div>
        """
    
    # run demo seed once (only when empty)
    @app.before_request
    def _auto_seed_once():
        # cheap check: only seed when no users
        try:
            c = db_one("SELECT COUNT(*) AS c FROM users", ())["c"]
            if int(c) == 0:
                seed_demo()
        except Exception:
            pass

    return app


app = create_app()

if __name__ == "__main__":
    # Dev server
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
