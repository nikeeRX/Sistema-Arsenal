import os
import socket
import sqlite3
from urllib.parse import urlparse
from datetime import datetime
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import pg8000.dbapi as pg_dbapi
except ImportError:
    pg_dbapi = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ts3_guild_tracker_secret_key_2026")

# Aceita DATABASE_URL padrão ou variáveis separadas do Postgres no Railway
def get_database_url():
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if url:
        return url.strip()
    pghost = os.environ.get("PGHOST")
    pguser = os.environ.get("PGUSER")
    pgpass = os.environ.get("PGPASSWORD")
    pgdb = os.environ.get("PGDATABASE")
    pgport = os.environ.get("PGPORT", "5432")
    if pghost and pguser and pgpass and pgdb:
        return f"postgresql://{pguser}:{pgpass}@{pghost}:{pgport}/{pgdb}"
    return None

# ==========================================
# CAMADA DE BANCO DE DADOS (PG8000 / SQLITE)
# ==========================================
def is_postgres():
    return bool(get_database_url() and pg_dbapi)

def get_db_connection():
    db_url = get_database_url()
    if db_url and pg_dbapi:
        parsed = urlparse(db_url)
        return pg_dbapi.connect(
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=int(parsed.port or 5432),
            database=parsed.path.lstrip("/")
        )
    else:
        conn = sqlite3.connect("guild_tracker.db")
        conn.row_factory = sqlite3.Row
        return conn

def db_execute(query, params=(), fetch=False, fetchone=False):
    use_pg = is_postgres()
    conn = get_db_connection()
    cur = conn.cursor()
    if use_pg:
        query = query.replace("?", "%s")
    try:
        cur.execute(query, tuple(params))
        result = None
        if fetchone:
            row = cur.fetchone()
            if row is not None:
                if use_pg:
                    cols = [col[0] for col in cur.description]
                    result = dict(zip(cols, row))
                else:
                    result = dict(row)
        elif fetch:
            rows = cur.fetchall()
            if use_pg:
                cols = [col[0] for col in cur.description]
                result = [dict(zip(cols, r)) for r in rows]
            else:
                result = [dict(r) for r in rows]
        conn.commit()
        return result
    finally:
        cur.close()
        conn.close()

def init_db():
    id_col = "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    # Tabela de Usuários (Admin e Players)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            username VARCHAR(80) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            ts_nick VARCHAR(120) NOT NULL,
            role VARCHAR(20) DEFAULT 'player',
            created_at VARCHAR(40)
        )
    """)

    # Tabela de Configuração do TeamSpeak 3
    db_execute("""
        CREATE TABLE IF NOT EXISTS ts_config (
            id INTEGER PRIMARY KEY,
            host VARCHAR(150) DEFAULT '',
            voice_port INTEGER DEFAULT 9987,
            query_port INTEGER DEFAULT 10011,
            query_user VARCHAR(100) DEFAULT 'serveradmin',
            query_pass VARCHAR(150) DEFAULT '',
            ignore_channels TEXT DEFAULT 'AFK,Ausente'
        )
    """)

    # Tabela de Eventos (Chamadas)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS events (
            id {id_col},
            title VARCHAR(150) NOT NULL,
            event_type VARCHAR(80) DEFAULT 'Guerra / Boss',
            status VARCHAR(30) DEFAULT 'Aberto',
            created_at VARCHAR(40),
            last_sync VARCHAR(40) DEFAULT 'Nunca'
        )
    """)

    # Tabela de Presenças Contabilizadas
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS attendance (
            id {id_col},
            event_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            ts_nick_found VARCHAR(150),
            method VARCHAR(50) DEFAULT 'TS3 Auto',
            checked_at VARCHAR(40),
            UNIQUE(event_id, user_id)
        )
    """)

    # Tabela de Drops / Itens
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS drops (
            id {id_col},
            item_name VARCHAR(150) NOT NULL,
            rarity VARCHAR(50) DEFAULT 'Raro',
            quantity INTEGER DEFAULT 1,
            boss_source VARCHAR(120) DEFAULT '',
            event_id INTEGER,
            winner_user_id INTEGER,
            status VARCHAR(50) DEFAULT 'Banco do Clã',
            created_at VARCHAR(40)
        )
    """)

    # Garantir configuração inicial do TS3
    cfg = db_execute("SELECT id FROM ts_config WHERE id = 1", fetchone=True)
    if not cfg:
        db_execute("""
            INSERT INTO ts_config (id, host, voice_port, query_port, query_user, query_pass, ignore_channels)
            VALUES (1, '', 9987, 10011, 'serveradmin', '', 'AFK,Ausente')
        """)

    # Garantir usuário Admin padrão (login: admin / senha: admin123)
    admin = db_execute("SELECT id FROM users WHERE username = ?", ("admin",), fetchone=True)
    if not admin:
        db_execute("""
            INSERT INTO users (username, password_hash, ts_nick, role, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "admin",
            generate_password_hash("admin123"),
            "AdminTS",
            "admin",
            datetime.now().strftime("%d/%m/%Y %H:%M")
        ))

init_db()

# ==========================================
# INTEGRAÇÃO NATIVA TEAMSPEAK 3 SERVERQUERY
# ==========================================
def ts3_escape(text):
    if not text:
        return ""
    return (str(text)
            .replace("\\", "\\\\")
            .replace("/", "\\/")
            .replace(" ", "\\s")
            .replace("|", "\\p"))

def ts3_unescape(text):
    if not text:
        return ""
    return (str(text)
            .replace("\\s", " ")
            .replace("\\/", "/")
            .replace("\\p", "|")
            .replace("\\\\", "\\"))

def recv_until_prompt(sock):
    buffer = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer += chunk
        if b"error id=" in buffer:
            break
    return buffer.decode("utf-8", errors="ignore")

def fetch_ts3_online_clients(cfg):
    host = (cfg.get("host") or "").strip()
    voice_port = int(cfg.get("voice_port") or 9987)
    query_port = int(cfg.get("query_port") or 10011)
    user = (cfg.get("query_user") or "").strip()
    password = (cfg.get("query_pass") or "").strip()
    ignore_raw = (cfg.get("ignore_channels") or "").split(",")
    ignore_keywords = [k.strip().lower() for k in ignore_raw if k.strip()]

    if not host:
        return False, "Host/IP do TeamSpeak 3 não configurado no painel."

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8.0)

    try:
        sock.connect((host, query_port))
        sock.recv(1024)

        if user and password:
            cmd_login = f"login client_login_name={ts3_escape(user)} client_login_password={ts3_escape(password)}\n"
            sock.sendall(cmd_login.encode("utf-8"))
            resp_login = recv_until_prompt(sock)
            if "error id=0" not in resp_login:
                return False, f"Falha no login do ServerQuery: {resp_login.strip()}"

        sock.sendall(f"use port={voice_port}\n".encode("utf-8"))
        resp_use = recv_until_prompt(sock)
        if "error id=0" not in resp_use:
            return False, f"Erro ao selecionar porta de voz {voice_port}: {resp_use.strip()}"

        ignored_channel_ids = set()
        if ignore_keywords:
            sock.sendall(b"channellist\n")
            resp_channels = recv_until_prompt(sock)
            for line in resp_channels.splitlines():
                if "cid=" in line:
                    for item in line.split("|"):
                        props = {}
                        for part in item.split(" "):
                            if "=" in part:
                                k, v = part.split("=", 1)
                                props[k] = ts3_unescape(v)
                        cid = props.get("cid")
                        cname = (props.get("channel_name") or "").lower()
                        if cid and any(kw in cname for kw in ignore_keywords):
                            ignored_channel_ids.add(cid)

        sock.sendall(b"clientlist\n")
        resp_clients = recv_until_prompt(sock)
        if "error id=0" not in resp_clients:
            return False, f"Permissão negada ou erro ao rodar clientlist: {resp_clients.strip()}"

        online_nicks = []
        skipped_afk = []

        for line in resp_clients.splitlines():
            if "clid=" in line:
                for item in line.split("|"):
                    props = {}
                    for part in item.split(" "):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            props[k] = ts3_unescape(v)
                    if props.get("client_type") == "0":
                        nick = props.get("client_nickname", "").strip()
                        cid = props.get("cid")
                        if nick:
                            if cid in ignored_channel_ids:
                                skipped_afk.append(nick)
                            else:
                                online_nicks.append(nick)

        try:
            sock.sendall(b"quit\n")
        except Exception:
            pass

        return True, {"online": online_nicks, "afk": skipped_afk}

    except Exception as e:
        return False, f"Erro de conexão com o TS3 ({host}:{query_port}): {str(e)}"
    finally:
        sock.close()

def match_player_nick(registered_nick, ts_online_nicks):
    reg_clean = registered_nick.strip().lower()
    if not reg_clean:
        return None
    for online in ts_online_nicks:
        on_clean = online.strip().lower()
        if reg_clean == on_clean or reg_clean in on_clean:
            return online
    return None

# ==========================================
# DECORATORS DE AUTENTICAÇÃO
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("Acesso restrito para Administradores.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ==========================================
# ROTAS DO SISTEMA
# ==========================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = db_execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["ts_nick"] = user["ts_nick"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        flash("Login ou senha incorretos.", "danger")
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    tab = request.args.get("tab", "eventos")
    selected_event_id = request.args.get("event_id", type=int)

    users = db_execute("SELECT * FROM users ORDER BY username ASC", fetch=True)
    events = db_execute("SELECT * FROM events ORDER BY id DESC", fetch=True)
    ts_cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)

    if not selected_event_id and events:
        selected_event_id = events[0]["id"]

    selected_event = None
    event_attendance = {}
    if selected_event_id:
        selected_event = db_execute("SELECT * FROM events WHERE id = ?", (selected_event_id,), fetchone=True)
        rows = db_execute("SELECT * FROM attendance WHERE event_id = ?", (selected_event_id,), fetch=True)
        for r in rows:
            event_attendance[r["user_id"]] = r

    total_events = len(events)
    all_att = db_execute("SELECT user_id, COUNT(*) as cnt FROM attendance GROUP BY user_id", fetch=True)
    att_counts = {r["user_id"]: int(r["cnt"]) for r in all_att}

    all_drops_won = db_execute("SELECT winner_user_id, SUM(quantity) as cnt FROM drops WHERE winner_user_id IS NOT NULL GROUP BY winner_user_id", fetch=True)
    drop_counts = {r["winner_user_id"]: int(r["cnt"]) for r in all_drops_won}

    for u in users:
        presences = att_counts.get(u["id"], 0)
        u["presences"] = presences
        u["pct"] = round((presences / total_events) * 100, 1) if total_events > 0 else 0.0
        u["drops_won"] = drop_counts.get(u["id"], 0)

    drops = db_execute("""
        SELECT d.*, u.username as winner_name, u.ts_nick as winner_nick, e.title as event_title
        FROM drops d
        LEFT JOIN users u ON d.winner_user_id = u.id
        LEFT JOIN events e ON d.event_id = e.id
        ORDER BY d.id DESC
    """, fetch=True)

    return render_template_string(
        MAIN_HTML,
        tab=tab,
        users=users,
        events=events,
        selected_event=selected_event,
        event_attendance=event_attendance,
        ts_cfg=ts_cfg,
        drops=drops,
        total_events=total_events,
        db_mode="PostgreSQL (Railway)" if is_postgres() else "SQLite Local"
    )

@app.route("/events/create", methods=["POST"])
@admin_required
def create_event():
    title = request.form.get("title", "").strip()
    event_type = request.form.get("event_type", "Guerra / Boss").strip()
    if title:
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        db_execute(
            "INSERT INTO events (title, event_type, status, created_at) VALUES (?, ?, 'Aberto', ?)",
            (title, event_type, now_str)
        )
        flash(f"Evento '{title}' criado! Agora você já pode puxar a presença do TS3.", "success")
    return redirect(url_for("dashboard", tab="eventos"))

@app.route("/events/<int:event_id>/delete", methods=["POST"])
@admin_required
def delete_event(event_id):
    db_execute("DELETE FROM attendance WHERE event_id = ?", (event_id,))
    db_execute("UPDATE drops SET event_id = NULL WHERE event_id = ?", (event_id,))
    db_execute("DELETE FROM events WHERE id = ?", (event_id,))
    flash("Evento excluído com sucesso.", "info")
    return redirect(url_for("dashboard", tab="eventos"))

@app.route("/events/<int:event_id>/sync_ts3", methods=["POST"])
@admin_required
def sync_event_ts3(event_id):
    cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
    ok, result = fetch_ts3_online_clients(cfg)
    if not ok:
        flash(result, "danger")
        return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

    online_nicks = result["online"]
    afk_nicks = result["afk"]
    users = db_execute("SELECT * FROM users", fetch=True)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    matched_count = 0
    for u in users:
        found_nick = match_player_nick(u["ts_nick"], online_nicks)
        if found_nick:
            matched_count += 1
            existing = db_execute(
                "SELECT id FROM attendance WHERE event_id = ? AND user_id = ?",
                (event_id, u["id"]), fetchone=True
            )
            if existing:
                db_execute(
                    "UPDATE attendance SET ts_nick_found = ?, method = 'TS3 Auto', checked_at = ? WHERE id = ?",
                    (found_nick, now_str, existing["id"])
                )
            else:
                db_execute(
                    "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, 'TS3 Auto', ?)",
                    (event_id, u["id"], found_nick, now_str)
                )

    db_execute("UPDATE events SET last_sync = ? WHERE id = ?", (now_str, event_id))
    msg = f"Varredura TS3 concluída! {len(online_nicks)} online no TS3 | {matched_count} jogadores cadastrados marcados com presença."
    if afk_nicks:
        msg += f" ({len(afk_nicks)} ignorados em sala AFK)."
    flash(msg, "success")
    return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

@app.route("/events/<int:event_id>/manual_paste", methods=["POST"])
@admin_required
def manual_paste_ts3(event_id):
    raw_text = request.form.get("raw_nicks", "")
    lines = [l.strip() for l in raw_text.replace(",", "\n").splitlines() if l.strip()]
    users = db_execute("SELECT * FROM users", fetch=True)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    matched_count = 0
    for u in users:
        found_nick = match_player_nick(u["ts_nick"], lines)
        if found_nick:
            matched_count += 1
            existing = db_execute(
                "SELECT id FROM attendance WHERE event_id = ? AND user_id = ?",
                (event_id, u["id"]), fetchone=True
            )
            if not existing:
                db_execute(
                    "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, 'Lista TS3', ?)",
                    (event_id, u["id"], found_nick, now_str)
                )

    db_execute("UPDATE events SET last_sync = ? WHERE id = ?", (now_str, event_id))
    flash(f"{matched_count} presenças contabilizadas a partir da lista de nicks!", "success")
    return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

@app.route("/events/<int:event_id>/toggle_user/<int:user_id>", methods=["POST"])
@admin_required
def toggle_attendance(event_id, user_id):
    existing = db_execute(
        "SELECT id FROM attendance WHERE event_id = ? AND user_id = ?",
        (event_id, user_id), fetchone=True
    )
    if existing:
        db_execute("DELETE FROM attendance WHERE id = ?", (existing["id"],))
    else:
        u = db_execute("SELECT ts_nick FROM users WHERE id = ?", (user_id,), fetchone=True)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        db_execute(
            "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, 'Manual Admin', ?)",
            (event_id, user_id, u["ts_nick"] if u else "Manual", now_str)
        )
    return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

@app.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    ts_nick = request.form.get("ts_nick", "").strip()
    role = request.form.get("role", "player").strip()

    if not username or not password or not ts_nick:
        flash("Preencha Login, Senha e Nick do TS3.", "warning")
        return redirect(url_for("dashboard", tab="players"))

    exists = db_execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if exists:
        flash(f"O login '{username}' já está cadastrado.", "danger")
        return redirect(url_for("dashboard", tab="players"))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute(
        "INSERT INTO users (username, password_hash, ts_nick, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (username, generate_password_hash(password), ts_nick, role, now_str)
    )
    flash(f"Jogador '{username}' (Nick TS3: {ts_nick}) criado com sucesso!", "success")
    return redirect(url_for("dashboard", tab="players"))

@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("Você não pode excluir sua própria conta logada.", "warning")
        return redirect(url_for("dashboard", tab="players"))

    db_execute("DELETE FROM attendance WHERE user_id = ?", (user_id,))
    db_execute("UPDATE drops SET winner_user_id = NULL WHERE winner_user_id = ?", (user_id,))
    db_execute("DELETE FROM users WHERE id = ?", (user_id,))
    flash("Jogador excluído do sistema.", "info")
    return redirect(url_for("dashboard", tab="players"))

@app.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    new_ts_nick = request.form.get("ts_nick", "").strip()
    new_pass = request.form.get("new_password", "").strip()
    uid = session["user_id"]

    if new_ts_nick:
        db_execute("UPDATE users SET ts_nick = ? WHERE id = ?", (new_ts_nick, uid))
        session["ts_nick"] = new_ts_nick
    if new_pass:
        db_execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_pass), uid))

    flash("Seus dados foram atualizados com sucesso!", "success")
    return redirect(url_for("dashboard", tab="perfil"))

@app.route("/drops/create", methods=["POST"])
@admin_required
def create_drop():
    item_name = request.form.get("item_name", "").strip()
    rarity = request.form.get("rarity", "Raro").strip()
    quantity = int(request.form.get("quantity") or 1)
    boss_source = request.form.get("boss_source", "").strip()
    event_id = request.form.get("event_id")
    winner_user_id = request.form.get("winner_user_id")
    status = request.form.get("status", "Banco do Clã").strip()

    ev_val = int(event_id) if event_id and event_id.isdigit() else None
    win_val = int(winner_user_id) if winner_user_id and winner_user_id.isdigit() else None

    if item_name:
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        db_execute("""
            INSERT INTO drops (item_name, rarity, quantity, boss_source, event_id, winner_user_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (item_name, rarity, quantity, boss_source, ev_val, win_val, status, now_str))
        flash(f"Drop '{item_name}' (x{quantity}) registrado com sucesso!", "success")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/drops/<int:drop_id>/assign", methods=["POST"])
@admin_required
def assign_drop(drop_id):
    winner_user_id = request.form.get("winner_user_id")
    status = request.form.get("status", "Entregue ao Player")
    win_val = int(winner_user_id) if winner_user_id and winner_user_id.isdigit() else None
    db_execute(
        "UPDATE drops SET winner_user_id = ?, status = ? WHERE id = ?",
        (win_val, status, drop_id)
    )
    flash("Destino do Drop atualizado!", "success")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/drops/<int:drop_id>/delete", methods=["POST"])
@admin_required
def delete_drop(drop_id):
    db_execute("DELETE FROM drops WHERE id = ?", (drop_id,))
    flash("Item removido da lista de Drops.", "info")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/tsconfig/save", methods=["POST"])
@admin_required
def save_tsconfig():
    host = request.form.get("host", "").strip()
    voice_port = int(request.form.get("voice_port") or 9987)
    query_port = int(request.form.get("query_port") or 10011)
    query_user = request.form.get("query_user", "").strip()
    query_pass = request.form.get("query_pass", "").strip()
    ignore_channels = request.form.get("ignore_channels", "").strip()

    db_execute("""
        UPDATE ts_config
        SET host = ?, voice_port = ?, query_port = ?, query_user = ?, query_pass = ?, ignore_channels = ?
        WHERE id = 1
    """, (host, voice_port, query_port, query_user, query_pass, ignore_channels))

    action = request.form.get("action", "save")
    if action == "test":
        cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
        ok, res = fetch_ts3_online_clients(cfg)
        if ok:
            nicks_str = ", ".join(res["online"]) if res["online"] else "Nenhum cliente de voz conectado agora"
            flash(f"Conexão TS3 OK! Online nas salas válidas ({len(res['online'])}): {nicks_str}", "success")
        else:
            flash(res, "danger")
    else:
        flash("Configurações do TeamSpeak 3 salvas com sucesso!", "success")

    return redirect(url_for("dashboard", tab="tsconfig"))

# ==========================================
# TEMPLATES HTML EMBUTIDOS
# ==========================================
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Login - Painel TS3 & Drops</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
    <style>
        body {
            background: radial-gradient(circle at top, #1a2238 0%, #0b0f19 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-card {
            background: #131926;
            border: 1px solid #2a3550;
            border-radius: 14px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.6);
            width: 100%;
            max-width: 410px;
            padding: 2.2rem;
        }
        .brand-icon {
            width: 64px;
            height: 64px;
            border-radius: 16px;
            background: linear-gradient(135deg, #3b82f6, #1d4ed8);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.8rem;
            margin: 0 auto 1rem;
            color: #fff;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand-icon"><i class="fa-solid fa-headset"></i></div>
        <h4 class="text-center fw-bold mb-1">TS3 Guild Tracker</h4>
        <p class="text-center text-secondary small mb-4">Controle de Presença TeamSpeak 3 & Drops</p>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for cat, msg in messages %}
              <div class="alert alert-{{ cat }} py-2 small">{{ msg }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        <form method="POST">
            <div class="mb-3">
                <label class="form-label small text-secondary">Login do Player / Admin</label>
                <div class="input-group">
                    <span class="input-group-text bg-dark border-secondary"><i class="fa-solid fa-user"></i></span>
                    <input type="text" name="username" class="form-control bg-dark text-light border-secondary" required autofocus>
                </div>
            </div>
            <div class="mb-4">
                <label class="form-label small text-secondary">Senha</label>
                <div class="input-group">
                    <span class="input-group-text bg-dark border-secondary"><i class="fa-solid fa-lock"></i></span>
                    <input type="password" name="password" class="form-control bg-dark text-light border-secondary" required>
                </div>
            </div>
            <button type="submit" class="btn btn-primary w-100 fw-semibold py-2">
                <i class="fa-solid fa-right-to-bracket me-1"></i> Entrar no Sistema
            </button>
        </form>
    </div>
</body>
</html>
"""

MAIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>TS3 Guild Tracker - Presença & Drops</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #0b0f19; color: #e2e8f0; }
        .navbar { background-color: #131926 !important; border-bottom: 1px solid #263147; }
        .card { background-color: #131926; border: 1px solid #263147; border-radius: 12px; }
        .nav-pills .nav-link { color: #94a3b8; border-radius: 8px; font-weight: 500; }
        .nav-pills .nav-link.active { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; }
        .badge-raridade-Comum { background-color: #475569; }
        .badge-raridade-Raro { background-color: #0284c7; }
        .badge-raridade-Épico { background-color: #7c3aed; }
        .badge-raridade-Lendário { background-color: #d97706; color: #fff; }
        .table-dark-custom th { background-color: #1a2234; color: #94a3b8; font-size: 0.82rem; text-transform: uppercase; }
    </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark px-3 py-2 mb-4">
    <div class="container-fluid">
        <a class="navbar-brand fw-bold d-flex align-items-center gap-2" href="{{ url_for('dashboard') }}">
            <i class="fa-solid fa-headset text-primary"></i> TS3 Guild Tracker
            <span class="badge {% if 'PostgreSQL' in db_mode %}bg-success{% else %}bg-warning text-dark{% endif %} ms-2" style="font-size: 0.7rem;">
                <i class="fa-solid fa-database me-1"></i>{{ db_mode }}
            </span>
        </a>
        <div class="d-flex align-items-center gap-3">
            <span class="small text-secondary">
                <i class="fa-solid fa-user-shield me-1 text-info"></i>
                <strong>{{ session['username'] }}</strong>
                <span class="badge bg-secondary ms-1">TS3: {{ session['ts_nick'] }}</span>
                {% if session['role'] == 'admin' %}
                    <span class="badge bg-primary ms-1">ADMIN</span>
                {% endif %}
            </span>
            <a href="{{ url_for('logout') }}" class="btn btn-sm btn-outline-danger">
                <i class="fa-solid fa-power-off"></i> Sair
            </a>
        </div>
    </div>
</nav>

<div class="container pb-5">

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="alert alert-{{ cat }} alert-dismissible fade show shadow-sm" role="alert">
            {{ msg }}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <ul class="nav nav-pills gap-2 mb-4">
        <li class="nav-item">
            <a class="nav-link {% if tab == 'eventos' %}active{% endif %}" href="{{ url_for('dashboard', tab='eventos') }}">
                <i class="fa-solid fa-tower-broadcast me-1"></i> Eventos & Chamada TS3
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'drops' %}active{% endif %}" href="{{ url_for('dashboard', tab='drops') }}">
                <i class="fa-solid fa-gem me-1"></i> Módulo de Drops (Itens)
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'players' %}active{% endif %}" href="{{ url_for('dashboard', tab='players') }}">
                <i class="fa-solid fa-users me-1"></i> Players & Ranking
            </a>
        </li>
        {% if session['role'] == 'admin' %}
        <li class="nav-item">
            <a class="nav-link {% if tab == 'tsconfig' %}active{% endif %}" href="{{ url_for('dashboard', tab='tsconfig') }}">
                <i class="fa-solid fa-gears me-1"></i> Configurar TeamSpeak 3
            </a>
        </li>
        {% endif %}
        <li class="nav-item">
            <a class="nav-link {% if tab == 'perfil' %}active{% endif %}" href="{{ url_for('dashboard', tab='perfil') }}">
                <i class="fa-solid fa-id-card me-1"></i> Meu Perfil
            </a>
        </li>
    </ul>

    {% if tab == 'eventos' %}
    <div class="row g-4">
        <div class="col-lg-4">
            {% if session['role'] == 'admin' %}
            <div class="card p-3 mb-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-plus-circle text-primary me-1"></i> Iniciar Novo Evento</h6>
                <form method="POST" action="{{ url_for('create_event') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nome do Evento / Boss</label>
                        <input type="text" name="title" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Castle Siege 21h / Boss Raid" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Tipo</label>
                        <select name="event_type" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="Guerra / PvP">Guerra / PvP</option>
                            <option value="Boss / Raid">Boss / Raid</option>
                            <option value="Evento Diário">Evento Diário</option>
                            <option value="Reunião TS3">Reunião TS3</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-primary btn-sm w-100 fw-semibold">
                        <i class="fa-solid fa-play me-1"></i> Criar Evento
                    </button>
                </form>
            </div>
            {% endif %}

            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-calendar-check text-info me-1"></i> Histórico de Eventos ({{ total_events }})</h6>
                <div class="list-group list-group-flush">
                    {% for ev in events %}
                    <a href="{{ url_for('dashboard', tab='eventos', event_id=ev.id) }}"
                       class="list-group-item list-group-item-action bg-transparent text-light border-secondary py-2 {% if selected_event and selected_event.id == ev.id %}active rounded{% endif %}">
                        <div class="d-flex justify-content-between align-items-center">
                            <strong class="small">{{ ev.title }}</strong>
                            <span class="badge bg-dark border border-secondary">{{ ev.event_type }}</span>
                        </div>
                        <div class="small text-secondary mt-1" style="font-size: 0.75rem;">
                            <i class="fa-regular fa-clock me-1"></i> {{ ev.created_at }}
                        </div>
                    </a>
                    {% else %}
                    <p class="text-secondary small mb-0">Nenhum evento registrado ainda.</p>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="col-lg-8">
            {% if selected_event %}
            <div class="card p-4">
                <div class="d-flex flex-wrap justify-content-between align-items-center border-bottom border-secondary pb-3 mb-3 gap-2">
                    <div>
                        <span class="badge bg-primary mb-1">{{ selected_event.event_type }}</span>
                        <h4 class="fw-bold mb-0">{{ selected_event.title }}</h4>
                        <small class="text-secondary">
                            Criado em {{ selected_event.created_at }} | Última leitura TS3: <strong>{{ selected_event.last_sync }}</strong>
                        </small>
                    </div>

                    {% if session['role'] == 'admin' %}
                    <div class="d-flex gap-2">
                        <form method="POST" action="{{ url_for('sync_event_ts3', event_id=selected_event.id) }}">
                            <button type="submit" class="btn btn-success fw-semibold">
                                <i class="fa-solid fa-satellite-dish me-1"></i> Puxar Presença do TS3 Agora
                            </button>
                        </form>
                        <form method="POST" action="{{ url_for('delete_event', event_id=selected_event.id) }}" onsubmit="return confirm('Excluir este evento e todas as presenças dele?');">
                            <button type="submit" class="btn btn-outline-danger">
                                <i class="fa-solid fa-trash"></i>
                            </button>
                        </form>
                    </div>
                    {% endif %}
                </div>

                {% if session['role'] == 'admin' %}
                <div class="mb-3">
                    <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#collapsePaste">
                        <i class="fa-solid fa-paste me-1"></i> Contingência: Colar lista de Nicks do TS3 manualmente
                    </button>
                    <div class="collapse mt-2" id="collapsePaste">
                        <form method="POST" action="{{ url_for('manual_paste_ts3', event_id=selected_event.id) }}" class="card card-body bg-dark border-secondary p-2">
                            <small class="text-secondary mb-1">Se o host do seu TS3 estiver bloqueando conexão externa, cole aqui os nicks copiados da sala (um por linha ou separados por vírgula):</small>
                            <textarea name="raw_nicks" rows="2" class="form-control form-control-sm bg-black text-light border-secondary mb-2" placeholder="Ex:&#10;[CLAN] Player1&#10;Player2"></textarea>
                            <button type="submit" class="btn btn-sm btn-info align-self-end">Processar Nicks Colados</button>
                        </form>
                    </div>
                </div>
                {% endif %}

                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Login Player</th>
                                <th>Nick Cadastrado (TS3)</th>
                                <th>Status no Evento</th>
                                <th>Nick Lido / Método</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Ação Manual</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in users %}
                            {% set att = event_attendance.get(u.id) %}
                            <tr>
                                <td class="fw-semibold">{{ u.username }}</td>
                                <td><span class="badge bg-secondary">{{ u.ts_nick }}</span></td>
                                <td>
                                    {% if att %}
                                        <span class="badge bg-success"><i class="fa-solid fa-check me-1"></i> PRESENTE</span>
                                    {% else %}
                                        <span class="badge bg-danger bg-opacity-50 text-light"><i class="fa-solid fa-xmark me-1"></i> AUSENTE</span>
                                    {% endif %}
                                </td>
                                <td class="small text-secondary">
                                    {% if att %}
                                        <strong class="text-light">{{ att.ts_nick_found }}</strong> ({{ att.method }} às {{ att.checked_at }})
                                    {% else %}
                                        —
                                    {% endif %}
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <form method="POST" action="{{ url_for('toggle_attendance', event_id=selected_event.id, user_id=u.id) }}" class="d-inline">
                                        {% if att %}
                                            <button type="submit" class="btn btn-sm btn-outline-warning py-0 px-2">Remover</button>
                                        {% else %}
                                            <button type="submit" class="btn btn-sm btn-outline-success py-0 px-2">+ Presença</button>
                                        {% endif %}
                                    </form>
                                </td>
                                {% endif %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            {% else %}
            <div class="card p-5 text-center">
                <i class="fa-solid fa-tower-broadcast fa-3x text-secondary mb-3"></i>
                <h5>Nenhum Evento Selecionado</h5>
                <p class="text-secondary small">Crie um evento ao lado para puxar a lista de presença do TeamSpeak 3.</p>
            </div>
            {% endif %}
        </div>
    </div>
    {% endif %}

    {% if tab == 'drops' %}
    <div class="row g-4">
        {% if session['role'] == 'admin' %}
        <div class="col-lg-4">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-box-open text-warning me-1"></i> Cadastrar Novo Drop / Item</h6>
                <form method="POST" action="{{ url_for('create_drop') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nome do Item (Drop)</label>
                        <input type="text" name="item_name" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Espada Lendária +15 / Joia Boss" required>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-7">
                            <label class="form-label small text-secondary mb-1">Raridade</label>
                            <select name="rarity" class="form-select form-select-sm bg-dark text-light border-secondary">
                                <option value="Comum">Comum</option>
                                <option value="Raro" selected>Raro</option>
                                <option value="Épico">Épico</option>
                                <option value="Lendário">Lendário</option>
                            </select>
                        </div>
                        <div class="col-5">
                            <label class="form-label small text-secondary mb-1">Qtd</label>
                            <input type="number" name="quantity" value="1" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                        </div>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Boss / Origem</label>
                        <input type="text" name="boss_source" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Kundun / Dragão Vermelho">
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Vincular a um Evento (Opcional)</label>
                        <select name="event_id" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="">-- Sem vínculo --</option>
                            {% for ev in events %}
                            <option value="{{ ev.id }}">{{ ev.title }} ({{ ev.created_at }})</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Destinado ao Player (Opcional)</label>
                        <select name="winner_user_id" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="">-- No Banco do Clã (Sem dono) --</option>
                            {% for u in users %}
                            <option value="{{ u.id }}">{{ u.username }} (TS3: {{ u.ts_nick }})</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Status do Item</label>
                        <select name="status" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="Banco do Clã">No Banco do Clã</option>
                            <option value="Em Sorteio / Leilão">Em Sorteio / Leilão</option>
                            <option value="Entregue ao Player">Entregue ao Player</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-warning btn-sm w-100 fw-bold text-dark">
                        <i class="fa-solid fa-plus me-1"></i> Adicionar Drop
                    </button>
                </form>
            </div>
        </div>
        {% endif %}

        <div class="{% if session['role'] == 'admin' %}col-lg-8{% else %}col-lg-12{% endif %}">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-gem text-info me-1"></i> Registro Geral de Drops do Clã ({{ drops|length }})</h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Item / Drop</th>
                                <th>Boss / Evento</th>
                                <th>Status</th>
                                <th>Player Recebedor</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Gerenciar</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for d in drops %}
                            <tr>
                                <td>
                                    <span class="badge badge-raridade-{{ d.rarity }} me-1">{{ d.rarity }}</span>
                                    <strong>{{ d.item_name }}</strong>
                                    <span class="badge bg-secondary ms-1">x{{ d.quantity }}</span>
                                    <div class="text-secondary" style="font-size: 0.72rem;">{{ d.created_at }}</div>
                                </td>
                                <td class="small">
                                    <div>{{ d.boss_source or '—' }}</div>
                                    {% if d.event_title %}
                                        <span class="badge bg-dark border border-secondary">{{ d.event_title }}</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <span class="badge {% if d.status == 'Entregue ao Player' %}bg-success{% elif d.status == 'Banco do Clã' %}bg-info text-dark{% else %}bg-warning text-dark{% endif %}">
                                        {{ d.status }}
                                    </span>
                                </td>
                                <td>
                                    {% if d.winner_name %}
                                        <strong class="text-warning"><i class="fa-solid fa-crown me-1"></i>{{ d.winner_name }}</strong>
                                        <small class="text-secondary">({{ d.winner_nick }})</small>
                                    {% else %}
                                        <span class="text-secondary small">Estoque Clã</span>
                                    {% endif %}
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <div class="d-flex justify-content-end gap-1">
                                        <form method="POST" action="{{ url_for('assign_drop', drop_id=d.id) }}" class="d-flex gap-1">
                                            <select name="winner_user_id" class="form-select form-select-sm bg-dark text-light border-secondary py-0" style="width: 130px; font-size:0.75rem;">
                                                <option value="">-- Clã --</option>
                                                {% for u in users %}
                                                <option value="{{ u.id }}" {% if d.winner_user_id == u.id %}selected{% endif %}>{{ u.username }}</option>
                                                {% endfor %}
                                            </select>
                                            <input type="hidden" name="status" value="Entregue ao Player">
                                            <button type="submit" class="btn btn-sm btn-outline-info py-0 px-2" title="Atribuir a este Player">
                                                <i class="fa-solid fa-check"></i>
                                            </button>
                                        </form>
                                        <form method="POST" action="{{ url_for('delete_drop', drop_id=d.id) }}" onsubmit="return confirm('Excluir este drop?');">
                                            <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                                                <i class="fa-solid fa-trash"></i>
                                            </button>
                                        </form>
                                    </div>
                                </td>
                                {% endif %}
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="5" class="text-center text-secondary py-4">Nenhum drop cadastrado ainda.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    {% if tab == 'players' %}
    <div class="row g-4">
        {% if session['role'] == 'admin' %}
        <div class="col-lg-4">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-user-plus text-success me-1"></i> Cadastrar Novo Player / Admin</h6>
                <form method="POST" action="{{ url_for('create_user') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Login de Acesso ao Site</label>
                        <input type="text" name="username" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: jefferson" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Senha do Player</label>
                        <input type="text" name="password" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Senha inicial" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nick Exato no TeamSpeak 3</label>
                        <input type="text" name="ts_nick" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Jefferson (sem precisar da tag)" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Permissão no Site</label>
                        <select name="role" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="player" selected>Player (Visualiza Presenças e Drops)</option>
                            <option value="admin">Administrador (Controle Total)</option>
                        </select>
                    </div>
                    <button type="submit" class="btn btn-success btn-sm w-100 fw-semibold">
                        <i class="fa-solid fa-check me-1"></i> Criar Conta do Player
                    </button>
                </form>
            </div>
        </div>
        {% endif %}

        <div class="{% if session['role'] == 'admin' %}col-lg-8{% else %}col-lg-12{% endif %}">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-trophy text-warning me-1"></i> Membros Cadastrados & Taxa de Presença TS3</h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Login Site</th>
                                <th>Nick no TS3</th>
                                <th>Cargo</th>
                                <th>Presenças ({{ total_events }} Eventos)</th>
                                <th>Drops Recebidos</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Excluir</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in users %}
                            <tr>
                                <td class="fw-bold">{{ u.username }}</td>
                                <td><span class="badge bg-secondary">{{ u.ts_nick }}</span></td>
                                <td>
                                    {% if u.role == 'admin' %}
                                        <span class="badge bg-primary">Admin</span>
                                    {% else %}
                                        <span class="badge bg-dark border border-secondary">Player</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <div class="d-flex align-items-center gap-2">
                                        <div class="progress flex-grow-1 bg-dark" style="height: 8px; max-width: 110px;">
                                            <div class="progress-bar bg-success" style="width: {{ u.pct }}%;"></div>
                                        </div>
                                        <span class="small fw-semibold">{{ u.presences }} ({{ u.pct }}%)</span>
                                    </div>
                                </td>
                                <td>
                                    <span class="badge bg-warning text-dark fw-bold">{{ u.drops_won }} itens</span>
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    {% if u.id != session['user_id'] %}
                                    <form method="POST" action="{{ url_for('delete_user', user_id=u.id) }}" onsubmit="return confirm('Excluir o jogador {{ u.username }}?');">
                                        <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                                            <i class="fa-solid fa-user-xmark"></i>
                                        </button>
                                    </form>
                                    {% endif %}
                                </td>
                                {% endif %}
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    {% endif %}

    {% if tab == 'tsconfig' and session['role'] == 'admin' %}
    <div class="row justify-content-center">
        <div class="col-lg-7">
            <div class="card p-4">
                <h5 class="fw-bold mb-1"><i class="fa-solid fa-server text-primary me-2"></i> Credenciais do Servidor TeamSpeak 3 (ServerQuery)</h5>
                <p class="text-secondary small mb-4">
                    Preencha os dados do seu servidor TS3 para que o sistema conecte na porta Query e puxe os nicks presentes nas salas em tempo real.
                </p>

                <form method="POST" action="{{ url_for('save_tsconfig') }}">
                    <div class="row g-3 mb-3">
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Endereço IP ou Domínio do TS3</label>
                            <input type="text" name="host" value="{{ ts_cfg.host }}" class="form-control bg-dark text-light border-secondary" placeholder="Ex: 191.252.100.10 ou meuclan.ts3.com.br" required>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label small text-secondary">Porta de Voz (UDP)</label>
                            <input type="number" name="voice_port" value="{{ ts_cfg.voice_port }}" class="form-control bg-dark text-light border-secondary" required>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label small text-secondary">Porta ServerQuery (TCP)</label>
                            <input type="number" name="query_port" value="{{ ts_cfg.query_port }}" class="form-control bg-dark text-light border-secondary" required>
                        </div>
                    </div>

                    <div class="row g-3 mb-3">
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Usuário ServerQuery</label>
                            <input type="text" name="query_user" value="{{ ts_cfg.query_user }}" class="form-control bg-dark text-light border-secondary" placeholder="Ex: serveradmin">
                        </div>
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Senha ServerQuery</label>
                            <input type="password" name="query_pass" value="{{ ts_cfg.query_pass }}" class="form-control bg-dark text-light border-secondary" placeholder="Senha gerada no TS3">
                        </div>
                    </div>

                    <div class="mb-4">
                        <label class="form-label small text-secondary">Ignorar Salas com estes Nomes (separados por vírgula)</label>
                        <input type="text" name="ignore_channels" value="{{ ts_cfg.ignore_channels }}" class="form-control bg-dark text-light border-secondary" placeholder="Ex: AFK, Ausente, Away">
                    </div>

                    <div class="d-flex gap-2">
                        <button type="submit" name="action" value="save" class="btn btn-primary fw-semibold">
                            <i class="fa-solid fa-floppy-disk me-1"></i> Salvar Configurações
                        </button>
                        <button type="submit" name="action" value="test" class="btn btn-outline-info fw-semibold">
                            <i class="fa-solid fa-plug-circle-check me-1"></i> Salvar e Testar Conexão Agora
                        </button>
                    </div>
                </form>
            </div>
        </div>
    </div>
    {% endif %}

    {% if tab == 'perfil' %}
    <div class="row justify-content-center">
        <div class="col-lg-5">
            <div class="card p-4">
                <h5 class="fw-bold mb-3"><i class="fa-solid fa-user-gear text-info me-2"></i> Meus Dados de Jogador</h5>
                <form method="POST" action="{{ url_for('update_profile') }}">
                    <div class="mb-3">
                        <label class="form-label small text-secondary">Login</label>
                        <input type="text" value="{{ session['username'] }}" class="form-control bg-dark text-secondary border-secondary" disabled>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary">Meu Nick no TeamSpeak 3</label>
                        <input type="text" name="ts_nick" value="{{ session['ts_nick'] }}" class="form-control bg-dark text-light border-secondary" required>
                    </div>
                    <div class="mb-4">
                        <label class="form-label small text-secondary">Nova Senha (deixe em branco para manter a atual)</label>
                        <input type="password" name="new_password" class="form-control bg-dark text-light border-secondary" placeholder="••••••••">
                    </div>
                    <button type="submit" class="btn btn-primary w-100 fw-semibold">
                        <i class="fa-solid fa-save me-1"></i> Atualizar Meu Perfil
                    </button>
                </form>
            </div>
        </div>
    </div>
    {% endif %}

</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
