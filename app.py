import os
import time
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

# ==========================================
# CAMADA DE BANCO DE DADOS (PG8000 / SQLITE)
# ==========================================
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

    # 1. Usuários
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

    # 2. Configuração TS3
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

    # 3. Eventos e Presenças
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

    # 4. Estoque de Itens (Estilo Estoque de Bar Acumulativo)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS inventory_items (
            id {id_col},
            item_name VARCHAR(150) UNIQUE NOT NULL,
            rarity VARCHAR(50) DEFAULT 'Raro',
            stock_qty INTEGER DEFAULT 0,
            boss_source VARCHAR(120) DEFAULT '',
            updated_at VARCHAR(40)
        )
    """)

    # 5. Leilões / Disputas de Lances
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS auctions (
            id {id_col},
            item_id INTEGER NOT NULL,
            item_name VARCHAR(150) NOT NULL,
            rarity VARCHAR(50) DEFAULT 'Raro',
            qty INTEGER DEFAULT 1,
            min_bid INTEGER DEFAULT 0,
            current_highest_bid INTEGER DEFAULT 0,
            status VARCHAR(40) DEFAULT 'Aberto',
            round_number INTEGER DEFAULT 1,
            ends_at_epoch BIGINT NOT NULL,
            ends_at_str VARCHAR(40),
            tied_user_ids TEXT DEFAULT '',
            winner_user_id INTEGER,
            winning_bid INTEGER DEFAULT 0,
            delivered_at VARCHAR(40),
            created_at VARCHAR(40)
        )
    """)

    # 6. Lances dos Players em cada Leilão
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS auction_bids (
            id {id_col},
            auction_id INTEGER NOT NULL,
            round_number INTEGER DEFAULT 1,
            user_id INTEGER NOT NULL,
            bid_amount INTEGER NOT NULL,
            bid_time VARCHAR(40)
        )
    """)

    # 7. Notificações de Vencedores e Desempates
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS notifications (
            id {id_col},
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at VARCHAR(40)
        )
    """)

    cfg = db_execute("SELECT id FROM ts_config WHERE id = 1", fetchone=True)
    if not cfg:
        db_execute("""
            INSERT INTO ts_config (id, host, voice_port, query_port, query_user, query_pass, ignore_channels)
            VALUES (1, '', 9987, 10011, 'serveradmin', '', 'AFK,Ausente')
        """)

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
# MOTOR DE APURAÇÃO DE LANCES E DESEMPATE (6 MIN)
# ==========================================
def create_notification(user_id, message):
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute(
        "INSERT INTO notifications (user_id, message, is_read, created_at) VALUES (?, ?, 0, ?)",
        (user_id, message, now_str)
    )

def resolve_expired_auctions(force_auction_id=None):
    """
    Verifica todos os leilões ativos cujo tempo limite já expirou.
    - Se houver 1 único maior lance: define vencedor, muda para 'Aguardando Entrega' e envia notificação.
    - Se houver empate no maior lance: abre rodada de desempate de 6 minutos ('Em Desempate')
      restrita aos empatantes, iniciando a partir do último valor empatado.
    """
    now_epoch = int(time.time())
    if force_auction_id:
        active_auctions = db_execute(
            "SELECT * FROM auctions WHERE id = ? AND status IN ('Aberto', 'Em Desempate')",
            (force_auction_id,), fetch=True
        )
    else:
        active_auctions = db_execute(
            "SELECT * FROM auctions WHERE status IN ('Aberto', 'Em Desempate') AND ends_at_epoch <= ?",
            (now_epoch,), fetch=True
        )

    for auc in active_auctions:
        aid = auc["id"]
        rnd = int(auc["round_number"] or 1)

        # Busca lances da rodada atual
        bids = db_execute("""
            SELECT b.*, u.username, u.ts_nick
            FROM auction_bids b
            JOIN users u ON b.user_id = u.id
            WHERE b.auction_id = ? AND b.round_number = ?
            ORDER BY b.bid_amount DESC, b.id ASC
        """, (aid, rnd), fetch=True)

        # Caso ninguém tenha dado lance na rodada
        if not bids:
            if auc["status"] == "Em Desempate" and auc["tied_user_ids"]:
                # Se ninguém aumentou o lance durante os 6 min de desempate, renova mais 6 min para os empatados
                new_end_epoch = int(time.time()) + 360
                new_end_str = datetime.fromtimestamp(new_end_epoch).strftime("%d/%m %H:%M:%S")
                db_execute(
                    "UPDATE auctions SET ends_at_epoch = ?, ends_at_str = ? WHERE id = ?",
                    (new_end_epoch, new_end_str, aid)
                )
            else:
                # Nenhum lance na rodada 1: devolve a quantidade ao estoque do bar
                db_execute(
                    "UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?",
                    (auc["qty"], auc["item_id"])
                )
                db_execute(
                    "UPDATE auctions SET status = 'Sem Lances (Devolvido)' WHERE id = ?",
                    (aid,)
                )
            continue

        # Pega o maior lance de cada jogador na rodada atual
        best_per_user = {}
        for b in bids:
            uid = b["user_id"]
            amt = int(b["bid_amount"])
            if uid not in best_per_user or amt > best_per_user[uid]["bid_amount"]:
                best_per_user[uid] = b

        max_bid = max(item["bid_amount"] for item in best_per_user.values())
        top_bidders = [item for item in best_per_user.values() if item["bid_amount"] == max_bid]

        if len(top_bidders) == 1:
            # VENCEDOR ÚNICO!
            winner = top_bidders[0]
            db_execute("""
                UPDATE auctions
                SET status = 'Aguardando Entrega',
                    winner_user_id = ?,
                    winning_bid = ?,
                    current_highest_bid = ?
                WHERE id = ?
            """, (winner["user_id"], max_bid, max_bid, aid))

            create_notification(
                winner["user_id"],
                f"🏆 Parabéns! Você venceu o lance do item '{auc['item_name']} (x{auc['qty']})' com a oferta de {max_bid}! Procure o Admin in-game para receber seu item."
            )
        else:
            # EMPATE NO MAIOR LANCE -> ABRE NOVO LANCE DE 6 MINUTOS PARA OS EMPATANTES!
            tied_ids_str = ",".join(str(b["user_id"]) for b in top_bidders)
            tied_names = ", ".join(b["username"] for b in top_bidders)
            new_round = rnd + 1
            new_end_epoch = int(time.time()) + 360  # 6 minutos (360 segundos)
            new_end_str = datetime.fromtimestamp(new_end_epoch).strftime("%d/%m %H:%M:%S")

            db_execute("""
                UPDATE auctions
                SET status = 'Em Desempate',
                    round_number = ?,
                    min_bid = ?,
                    current_highest_bid = ?,
                    ends_at_epoch = ?,
                    ends_at_str = ?,
                    tied_user_ids = ?
                WHERE id = ?
            """, (new_round, max_bid, max_bid, new_end_epoch, new_end_str, tied_ids_str, aid))

            for b in top_bidders:
                create_notification(
                    b["user_id"],
                    f"⚡ EMPATE no item '{auc['item_name']}' no valor de {max_bid} (Empatados: {tied_names})! Uma nova rodada exclusiva de 6 minutos foi aberta a partir de {max_bid}."
                )

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
# ROTAS PRINCIPAIS
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

@app.route("/notifications/read", methods=["POST"])
@login_required
def read_notifications():
    db_execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/")
@login_required
def dashboard():
    # Sempre verifica se algum leilão acabou de expirar antes de carregar a tela
    resolve_expired_auctions()

    tab = request.args.get("tab", "drops")
    selected_event_id = request.args.get("event_id", type=int)

    users = db_execute("SELECT * FROM users ORDER BY username ASC", fetch=True)
    users_map = {u["id"]: u for u in users}
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

    # Conta itens ganhos e já entregues (ou aguardando entrega)
    won_rows = db_execute("""
        SELECT winner_user_id, SUM(qty) as cnt
        FROM auctions
        WHERE winner_user_id IS NOT NULL AND status IN ('Aguardando Entrega', 'Entregue (OK)')
        GROUP BY winner_user_id
    """, fetch=True)
    drop_counts = {r["winner_user_id"]: int(r["cnt"]) for r in won_rows}

    for u in users:
        presences = att_counts.get(u["id"], 0)
        u["presences"] = presences
        u["pct"] = round((presences / total_events) * 100, 1) if total_events > 0 else 0.0
        u["drops_won"] = drop_counts.get(u["id"], 0)

    # Estoque de itens (estilo bar)
    inventory = db_execute("SELECT * FROM inventory_items ORDER BY stock_qty DESC, item_name ASC", fetch=True)

    # Leilões ativos e histórico
    auctions = db_execute("""
        SELECT a.*, u.username as winner_name, u.ts_nick as winner_nick
        FROM auctions a
        LEFT JOIN users u ON a.winner_user_id = u.id
        ORDER BY a.id DESC
    """, fetch=True)

    # Mapeia lances de cada leilão para mostrar no card
    all_bids = db_execute("""
        SELECT b.*, u.username, u.ts_nick
        FROM auction_bids b
        JOIN users u ON b.user_id = u.id
        ORDER BY b.bid_amount DESC, b.id DESC
    """, fetch=True)

    bids_by_auction = {}
    for b in all_bids:
        bids_by_auction.setdefault(b["auction_id"], []).append(b)

    now_epoch = int(time.time())
    uid_str = str(session["user_id"])

    for a in auctions:
        a["bids_list"] = [b for b in bids_by_auction.get(a["id"], []) if int(b["round_number"]) == int(a["round_number"])]
        a["seconds_left"] = max(0, int(a["ends_at_epoch"]) - now_epoch)
        tied_list = [x.strip() for x in (a.get("tied_user_ids") or "").split(",") if x.strip()]
        a["tied_names"] = [users_map[int(tid)]["username"] for tid in tied_list if tid.isdigit() and int(tid) in users_map]
        if a["status"] == "Aberto":
            a["can_user_bid"] = True
        elif a["status"] == "Em Desempate":
            a["can_user_bid"] = (uid_str in tied_list)
        else:
            a["can_user_bid"] = False

    # Notificações não lidas do usuário logado
    my_notifications = db_execute(
        "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY id DESC",
        (session["user_id"],), fetch=True
    )

    return render_template_string(
        MAIN_HTML,
        tab=tab,
        users=users,
        events=events,
        selected_event=selected_event,
        event_attendance=event_attendance,
        ts_cfg=ts_cfg,
        inventory=inventory,
        auctions=auctions,
        my_notifications=my_notifications,
        total_events=total_events,
        db_mode="PostgreSQL (Railway)" if is_postgres() else "SQLite Local"
    )

# ==========================================
# ROTAS DO ESTOQUE (ESTILO BAR) & LEILÕES
# ==========================================
@app.route("/inventory/add", methods=["POST"])
@admin_required
def add_inventory():
    item_name = request.form.get("item_name", "").strip()
    rarity = request.form.get("rarity", "Raro").strip()
    qty = int(request.form.get("quantity") or 1)
    boss_source = request.form.get("boss_source", "").strip()
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    if not item_name or qty <= 0:
        flash("Informe o nome do item e uma quantidade válida.", "warning")
        return redirect(url_for("dashboard", tab="drops"))

    existing = db_execute(
        "SELECT * FROM inventory_items WHERE LOWER(item_name) = LOWER(?)",
        (item_name,), fetchone=True
    )
    if existing:
        new_qty = int(existing["stock_qty"]) + qty
        db_execute("""
            UPDATE inventory_items
            SET stock_qty = ?, rarity = ?, boss_source = ?, updated_at = ?
            WHERE id = ?
        """, (new_qty, rarity, boss_source or existing["boss_source"], now_str, existing["id"]))
        flash(f"Estoque acumulado! '{existing['item_name']}' recebeu +{qty} e agora possui {new_qty} un. no estoque.", "success")
    else:
        db_execute("""
            INSERT INTO inventory_items (item_name, rarity, stock_qty, boss_source, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (item_name, rarity, qty, boss_source, now_str))
        flash(f"Novo item '{item_name}' cadastrado no estoque com {qty} un.!", "success")

    return redirect(url_for("dashboard", tab="drops"))

@app.route("/inventory/<int:item_id>/adjust", methods=["POST"])
@admin_required
def adjust_inventory(item_id):
    delta = int(request.form.get("delta") or 0)
    item = db_execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,), fetchone=True)
    if item:
        new_qty = max(0, int(item["stock_qty"]) + delta)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        db_execute("UPDATE inventory_items SET stock_qty = ?, updated_at = ? WHERE id = ?", (new_qty, now_str, item_id))
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/inventory/<int:item_id>/delete", methods=["POST"])
@admin_required
def delete_inventory(item_id):
    db_execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
    flash("Item removido do estoque.", "info")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/auctions/start", methods=["POST"])
@admin_required
def start_auction():
    item_id = int(request.form.get("item_id") or 0)
    qty_to_auction = int(request.form.get("qty") or 1)
    min_bid = int(request.form.get("min_bid") or 0)
    duration_minutes = int(request.form.get("duration_minutes") or 10)

    item = db_execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,), fetchone=True)
    if not item or int(item["stock_qty"]) < qty_to_auction or qty_to_auction <= 0:
        flash("Quantidade insuficiente no estoque para liberar este lance.", "danger")
        return redirect(url_for("dashboard", tab="drops"))

    # Abate do estoque imediatamente
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute(
        "UPDATE inventory_items SET stock_qty = stock_qty - ?, updated_at = ? WHERE id = ?",
        (qty_to_auction, now_str, item_id)
    )

    ends_epoch = int(time.time()) + (duration_minutes * 60)
    ends_str = datetime.fromtimestamp(ends_epoch).strftime("%d/%m %H:%M:%S")

    db_execute("""
        INSERT INTO auctions (
            item_id, item_name, rarity, qty, min_bid, current_highest_bid,
            status, round_number, ends_at_epoch, ends_at_str, tied_user_ids, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'Aberto', 1, ?, ?, '', ?)
    """, (item_id, item["item_name"], item["rarity"], qty_to_auction, min_bid, min_bid, ends_epoch, ends_str, now_str))

    flash(f"Lance liberado para '{item['item_name']} (x{qty_to_auction})'! Tempo limite: {duration_minutes} min.", "success")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/auctions/<int:auction_id>/bid", methods=["POST"])
@login_required
def place_bid(auction_id):
    resolve_expired_auctions()

    auc = db_execute("SELECT * FROM auctions WHERE id = ?", (auction_id,), fetchone=True)
    if not auc or auc["status"] not in ("Aberto", "Em Desempate"):
        flash("Este leilão já foi encerrado.", "warning")
        return redirect(url_for("dashboard", tab="drops"))

    uid = session["user_id"]
    if auc["status"] == "Em Desempate":
        allowed = [x.strip() for x in (auc.get("tied_user_ids") or "").split(",") if x.strip()]
        if str(uid) not in allowed:
            flash("Apenas os jogadores que empataram podem dar lances nesta rodada de desempate de 6 minutos!", "danger")
            return redirect(url_for("dashboard", tab="drops"))

    try:
        bid_amount = int(request.form.get("bid_amount") or 0)
    except ValueError:
        flash("Valor de lance inválido.", "danger")
        return redirect(url_for("dashboard", tab="drops"))

    min_allowed = int(auc["min_bid"])
    if auc["status"] == "Em Desempate" and bid_amount <= min_allowed:
        flash(f"No desempate, o seu lance precisa ser maior que o valor empatado ({min_allowed})!", "warning")
        return redirect(url_for("dashboard", tab="drops"))
    elif bid_amount < min_allowed:
        flash(f"O lance mínimo inicial é {min_allowed}.", "warning")
        return redirect(url_for("dashboard", tab="drops"))

    rnd = int(auc["round_number"] or 1)
    now_str = datetime.now().strftime("%H:%M:%S")

    existing_bid = db_execute("""
        SELECT * FROM auction_bids
        WHERE auction_id = ? AND round_number = ? AND user_id = ?
    """, (auction_id, rnd, uid), fetchone=True)

    if existing_bid:
        if bid_amount <= int(existing_bid["bid_amount"]):
            flash(f"Você já tem um lance de {existing_bid['bid_amount']} nesta rodada. Digite um valor maior para atualizar.", "warning")
            return redirect(url_for("dashboard", tab="drops"))
        db_execute(
            "UPDATE auction_bids SET bid_amount = ?, bid_time = ? WHERE id = ?",
            (bid_amount, now_str, existing_bid["id"])
        )
    else:
        db_execute("""
            INSERT INTO auction_bids (auction_id, round_number, user_id, bid_amount, bid_time)
            VALUES (?, ?, ?, ?, ?)
        """, (auction_id, rnd, uid, bid_amount, now_str))

    if bid_amount > int(auc["current_highest_bid"]):
        db_execute("UPDATE auctions SET current_highest_bid = ? WHERE id = ?", (bid_amount, auction_id))

    flash(f"Lance de {bid_amount} registrado com sucesso em '{auc['item_name']}'!", "success")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/auctions/<int:auction_id>/force_end", methods=["POST"])
@admin_required
def force_end_auction(auction_id):
    resolve_expired_auctions(force_auction_id=auction_id)
    flash("Tempo do lance encerrado e apurado pelo Admin!", "info")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/auctions/<int:auction_id>/confirm_delivery", methods=["POST"])
@admin_required
def confirm_delivery(auction_id):
    auc = db_execute("SELECT * FROM auctions WHERE id = ?", (auction_id,), fetchone=True)
    if auc and auc["status"] == "Aguardando Entrega":
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        db_execute(
            "UPDATE auctions SET status = 'Entregue (OK)', delivered_at = ? WHERE id = ?",
            (now_str, auction_id)
        )
        if auc.get("winner_user_id"):
            create_notification(
                auc["winner_user_id"],
                f"✅ Entrega Confirmada (OK): O item '{auc['item_name']} (x{auc['qty']})' foi entregue a você in-game pelo Admin!"
            )
        flash(f"Entrega do item '{auc['item_name']}' confirmada com OK!", "success")
    return redirect(url_for("dashboard", tab="drops"))

@app.route("/auctions/<int:auction_id>/delete", methods=["POST"])
@admin_required
def delete_auction(auction_id):
    auc = db_execute("SELECT * FROM auctions WHERE id = ?", (auction_id,), fetchone=True)
    if auc and auc["status"] in ("Aberto", "Em Desempate"):
        # Se cancelar um leilão aberto, devolve o item ao estoque
        db_execute("UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?", (auc["qty"], auc["item_id"]))
    db_execute("DELETE FROM auction_bids WHERE auction_id = ?", (auction_id,))
    db_execute("DELETE FROM auctions WHERE id = ?", (auction_id,))
    flash("Registro de lance removido.", "info")
    return redirect(url_for("dashboard", tab="drops"))

# ==========================================
# ROTAS DE EVENTOS, PLAYERS E TS3
# ==========================================
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
    msg = f"Varredura TS3 concluída! {len(online_nicks)} online no TS3 | {matched_count} jogadores marcados com presença."
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
    db_execute("DELETE FROM auction_bids WHERE user_id = ?", (user_id,))
    db_execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
    db_execute("UPDATE auctions SET winner_user_id = NULL WHERE winner_user_id = ?", (user_id,))
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
        <p class="text-center text-secondary small mb-4">Presença TeamSpeak 3, Estoque & Leilão de Drops</p>

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
    <title>TS3 Guild Tracker - Estoque & Leilões</title>
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
        .table-dark-custom th { background-color: #1a2234; color: #94a3b8; font-size: 0.80rem; text-transform: uppercase; }
        .auction-active-card { border: 1px solid #3b82f6; box-shadow: 0 0 20px rgba(59, 130, 246, 0.12); }
        .auction-tie-card { border: 2px solid #f59e0b; box-shadow: 0 0 22px rgba(245, 158, 11, 0.22); }
        .timer-box { font-family: monospace; font-size: 1.25rem; font-weight: 700; letter-spacing: 1px; }
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

    <!-- PAINEL DE NOTIFICAÇÕES DE LANCES VENCIDOS / DESEMPATES -->
    {% if my_notifications %}
    <div class="card border-warning bg-warning bg-opacity-10 p-3 mb-4">
        <div class="d-flex justify-content-between align-items-center mb-2">
            <h6 class="fw-bold text-warning mb-0">
                <i class="fa-solid fa-bell fa-shake me-1"></i> Suas Notificações de Lances & Entregas
            </h6>
            <form method="POST" action="{{ url_for('read_notifications') }}">
                <button type="submit" class="btn btn-sm btn-outline-warning py-0">Marcar como lidas</button>
            </form>
        </div>
        <ul class="mb-0 small">
            {% for n in my_notifications %}
            <li class="mb-1"><strong>{{ n.created_at }}:</strong> {{ n.message }}</li>
            {% endfor %}
        </ul>
    </div>
    {% endif %}

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

    <!-- NAVEGAÇÃO PRINCIPAL -->
    <ul class="nav nav-pills gap-2 mb-4">
        <li class="nav-item">
            <a class="nav-link {% if tab == 'drops' %}active{% endif %}" href="{{ url_for('dashboard', tab='drops') }}">
                <i class="fa-solid fa-gavel me-1"></i> Estoque & Lances de Drops
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'eventos' %}active{% endif %}" href="{{ url_for('dashboard', tab='eventos') }}">
                <i class="fa-solid fa-tower-broadcast me-1"></i> Eventos & Chamada TS3
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

    <!-- ==========================================
         ABA 1: ESTOQUE (ESTILO BAR) & LEILÕES DE DROPS
         ========================================== -->
    {% if tab == 'drops' %}
    <div class="row g-4">

        <!-- COLUNA ESQUERDA: ESTOQUE ACUMULATIVO DO CLÃ -->
        <div class="col-lg-5">
            {% if session['role'] == 'admin' %}
            <div class="card p-3 mb-4">
                <h6 class="fw-bold mb-2">
                    <i class="fa-solid fa-boxes-stacked text-warning me-1"></i> Entrada de Estoque (Acumulativo)
                </h6>
                <p class="text-secondary small mb-3">
                    Se o item já existir no estoque, a quantidade digitada será <strong>somada</strong> ao saldo atual automaticamente.
                </p>
                <form method="POST" action="{{ url_for('add_inventory') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nome do Item (Drop)</label>
                        <input type="text" name="item_name" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Joia da Criação / Espada +15" required>
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
                            <label class="form-label small text-secondary mb-1">Qtd a Somar</label>
                            <input type="number" name="quantity" value="1" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                        </div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Boss / Origem (Opcional)</label>
                        <input type="text" name="boss_source" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Boss Kundun">
                    </div>
                    <button type="submit" class="btn btn-warning btn-sm w-100 fw-bold text-dark">
                        <i class="fa-solid fa-cart-plus me-1"></i> Somar ao Estoque
                    </button>
                </form>
            </div>
            {% endif %}

            <!-- TABELA DE ESTOQUE ATUAL -->
            <div class="card p-3">
                <h6 class="fw-bold mb-3">
                    <i class="fa-solid fa-warehouse text-info me-1"></i> Estoque Disponível no Clã ({{ inventory|length }} itens)
                </h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Item</th>
                                <th class="text-center">Saldo</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Liberar Lance / Ajuste</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in inventory %}
                            <tr>
                                <td>
                                    <span class="badge badge-raridade-{{ item.rarity }} me-1">{{ item.rarity }}</span>
                                    <strong>{{ item.item_name }}</strong>
                                    {% if item.boss_source %}
                                        <div class="text-secondary" style="font-size: 0.72rem;">Origem: {{ item.boss_source }}</div>
                                    {% endif %}
                                </td>
                                <td class="text-center">
                                    <span class="badge {% if item.stock_qty > 0 %}bg-success{% else %}bg-secondary{% endif %} fs-6">
                                        {{ item.stock_qty }}
                                    </span>
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <div class="d-flex justify-content-end align-items-center gap-1">
                                        {% if item.stock_qty > 0 %}
                                        <button class="btn btn-sm btn-primary py-0 px-2" type="button" data-bs-toggle="collapse" data-bs-target="#releaseBox{{ item.id }}">
                                            <i class="fa-solid fa-gavel me-1"></i> Liberar
                                        </button>
                                        {% endif %}
                                        <form method="POST" action="{{ url_for('adjust_inventory', item_id=item.id) }}" class="d-inline">
                                            <input type="hidden" name="delta" value="1">
                                            <button type="submit" class="btn btn-sm btn-outline-success py-0 px-2" title="Somar +1">+1</button>
                                        </form>
                                        <form method="POST" action="{{ url_for('adjust_inventory', item_id=item.id) }}" class="d-inline">
                                            <input type="hidden" name="delta" value="-1">
                                            <button type="submit" class="btn btn-sm btn-outline-warning py-0 px-2" title="Reduzir -1">-1</button>
                                        </form>
                                        <form method="POST" action="{{ url_for('delete_inventory', item_id=item.id) }}" class="d-inline" onsubmit="return confirm('Excluir item do estoque?');">
                                            <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                                                <i class="fa-solid fa-trash"></i>
                                            </button>
                                        </form>
                                    </div>
                                </td>
                                {% endif %}
                            </tr>
                            {% if session['role'] == 'admin' and item.stock_qty > 0 %}
                            <tr class="collapse" id="releaseBox{{ item.id }}">
                                <td colspan="3" class="bg-black p-2 border-secondary">
                                    <form method="POST" action="{{ url_for('start_auction') }}" class="row g-2 align-items-end">
                                        <input type="hidden" name="item_id" value="{{ item.id }}">
                                        <div class="col-3">
                                            <label class="small text-secondary" style="font-size:0.7rem;">Qtd Lote</label>
                                            <input type="number" name="qty" value="1" min="1" max="{{ item.stock_qty }}" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                        </div>
                                        <div class="col-4">
                                            <label class="small text-secondary" style="font-size:0.7rem;">Lance Inicial</label>
                                            <input type="number" name="min_bid" value="100" min="0" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                        </div>
                                        <div class="col-3">
                                            <label class="small text-secondary" style="font-size:0.7rem;">Tempo (Min)</label>
                                            <input type="number" name="duration_minutes" value="15" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                        </div>
                                        <div class="col-2">
                                            <button type="submit" class="btn btn-sm btn-success w-100" title="Iniciar Disputa">OK</button>
                                        </div>
                                    </form>
                                </td>
                            </tr>
                            {% endif %}
                            {% else %}
                            <tr>
                                <td colspan="3" class="text-center text-secondary py-4">Estoque vazio. Adicione itens acima!</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- COLUNA DIREITA: LANCES ATIVOS E ENTREGAS IN-GAME -->
        <div class="col-lg-7">

            <!-- 1. LANCES LIBERADOS / EM ANDAMENTO -->
            <div class="card p-3 mb-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h6 class="fw-bold mb-0">
                        <i class="fa-solid fa-fire text-danger me-1"></i> Lances Liberados pelo Admin (Ao Vivo)
                    </h6>
                    <a href="{{ url_for('dashboard', tab='drops') }}" class="btn btn-sm btn-outline-secondary">
                        <i class="fa-solid fa-rotate me-1"></i> Atualizar Lances
                    </a>
                </div>

                {% set active_list = auctions | selectattr('status', 'in', ['Aberto', 'Em Desempate']) | list %}
                {% if active_list %}
                    {% for a in active_list %}
                    <div class="card p-3 mb-3 bg-dark {% if a.status == 'Em Desempate' %}auction-tie-card{% else %}auction-active-card{% endif %}">
                        <div class="d-flex flex-wrap justify-content-between align-items-start gap-2 mb-2">
                            <div>
                                <span class="badge badge-raridade-{{ a.rarity }} me-1">{{ a.rarity }}</span>
                                <span class="fs-5 fw-bold">{{ a.item_name }}</span>
                                <span class="badge bg-secondary ms-1">Lote x{{ a.qty }}</span>
                                {% if a.status == 'Em Desempate' %}
                                    <span class="badge bg-warning text-dark ms-1">
                                        <i class="fa-solid fa-bolt me-1"></i> DESEMPATE 6 MIN (Rodada {{ a.round_number }})
                                    </span>
                                {% else %}
                                    <span class="badge bg-primary ms-1">Lances Abertos</span>
                                {% endif %}
                            </div>

                            <div class="text-end">
                                <div class="small text-secondary">Tempo Restante:</div>
                                <div class="timer-box {% if a.status == 'Em Desempate' %}text-warning{% else %}text-info{% endif %}"
                                     data-seconds="{{ a.seconds_left }}">
                                    --:--
                                </div>
                            </div>
                        </div>

                        {% if a.status == 'Em Desempate' %}
                        <div class="alert alert-warning py-2 px-3 small mb-2">
                            <i class="fa-solid fa-triangle-exclamation me-1"></i>
                            <strong>Empate detectado no valor de {{ a.min_bid }}!</strong>
                            Prorrogação automática de <strong>6 minutos</strong> liberada exclusivamente para os empatantes:
                            <strong>{{ a.tied_names | join(', ') }}</strong>.
                        </div>
                        {% endif %}

                        <div class="row g-2 align-items-center bg-black bg-opacity-50 rounded p-2 mb-2">
                            <div class="col-sm-4">
                                <small class="text-secondary d-block">Lance Mínimo / Base:</small>
                                <strong class="text-light">{{ a.min_bid }}</strong>
                            </div>
                            <div class="col-sm-4">
                                <small class="text-secondary d-block">Maior Lance Atual:</small>
                                <strong class="text-success fs-5">{{ a.current_highest_bid }}</strong>
                            </div>
                            <div class="col-sm-4">
                                {% if a.can_user_bid %}
                                <form method="POST" action="{{ url_for('place_bid', auction_id=a.id) }}" class="d-flex gap-1">
                                    <input type="number" name="bid_amount"
                                           min="{% if a.status == 'Em Desempate' %}{{ a.min_bid + 1 }}{% else %}{{ a.min_bid }}{% endif %}"
                                           placeholder="{% if a.status == 'Em Desempate' %}> {{ a.min_bid }}{% else %}Valor{% endif %}"
                                           class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                    <button type="submit" class="btn btn-sm btn-success fw-bold text-nowrap">
                                        Dar Lance
                                    </button>
                                </form>
                                {% else %}
                                <span class="badge bg-secondary w-100 py-2">
                                    <i class="fa-solid fa-lock me-1"></i> Restrito aos Empatantes
                                </span>
                                {% endif %}
                            </div>
                        </div>

                        <!-- Lista de lances dados nesta rodada -->
                        <div class="d-flex flex-wrap justify-content-between align-items-center mt-1">
                            <div class="small text-secondary">
                                <strong>Lances na rodada ({{ a.bids_list|length }}):</strong>
                                {% for b in a.bids_list %}
                                    <span class="badge bg-secondary bg-opacity-50 text-light me-1">
                                        {{ b.username }}: <strong>{{ b.bid_amount }}</strong> ({{ b.bid_time }})
                                    </span>
                                {% else %}
                                    <span>Nenhum lance registrado ainda.</span>
                                {% endfor %}
                            </div>

                            {% if session['role'] == 'admin' %}
                            <div class="d-flex gap-1 mt-2 mt-sm-0">
                                <form method="POST" action="{{ url_for('force_end_auction', auction_id=a.id) }}">
                                    <button type="submit" class="btn btn-sm btn-outline-warning py-0 px-2" title="Encerrar o tempo agora e apurar vencedor/empate">
                                        <i class="fa-solid fa-flag-checkered me-1"></i> Encerrar Tempo Agora
                                    </button>
                                </form>
                                <form method="POST" action="{{ url_for('delete_auction', auction_id=a.id) }}" onsubmit="return confirm('Cancelar este leilão e devolver o item ao estoque?');">
                                    <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                                        <i class="fa-solid fa-xmark"></i>
                                    </button>
                                </form>
                            </div>
                            {% endif %}
                        </div>
                    </div>
                    {% endfor %}
                {% else %}
                    <p class="text-secondary small text-center py-3 mb-0">
                        Nenhum lance aberto no momento. O Admin pode liberar itens do estoque ao lado.
                    </p>
                {% endif %}
            </div>

            <!-- 2. VENCEDORES AGUARDANDO ENTREGA IN-GAME E HISTÓRICO (OK DO ADMIN) -->
            <div class="card p-3">
                <h6 class="fw-bold mb-3">
                    <i class="fa-solid fa-handshake-simple text-success me-1"></i> Vencedores & Confirmação de Entrega In-Game (OK)
                </h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Item</th>
                                <th>Vencedor (Nick TS3)</th>
                                <th>Lance Final</th>
                                <th>Status Entrega</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Ação Admin</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for a in auctions if a.status not in ['Aberto', 'Em Desempate'] %}
                            <tr>
                                <td>
                                    <span class="badge badge-raridade-{{ a.rarity }} me-1">{{ a.rarity }}</span>
                                    <strong>{{ a.item_name }}</strong>
                                    <span class="badge bg-secondary ms-1">x{{ a.qty }}</span>
                                </td>
                                <td>
                                    {% if a.winner_name %}
                                        <strong class="text-warning"><i class="fa-solid fa-crown me-1"></i>{{ a.winner_name }}</strong>
                                        <small class="text-secondary d-block">TS3: {{ a.winner_nick }}</small>
                                    {% else %}
                                        <span class="text-secondary small">—</span>
                                    {% endif %}
                                </td>
                                <td class="fw-bold text-info">{{ a.winning_bid or '—' }}</td>
                                <td>
                                    {% if a.status == 'Aguardando Entrega' %}
                                        <span class="badge bg-warning text-dark">
                                            <i class="fa-solid fa-hourglass-half me-1"></i> Aguardando Entrega In-Game
                                        </span>
                                    {% elif a.status == 'Entregue (OK)' %}
                                        <span class="badge bg-success">
                                            <i class="fa-solid fa-check-double me-1"></i> Entregue ({{ a.delivered_at }})
                                        </span>
                                    {% else %}
                                        <span class="badge bg-secondary">{{ a.status }}</span>
                                    {% endif %}
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <div class="d-flex justify-content-end gap-1">
                                        {% if a.status == 'Aguardando Entrega' %}
                                        <form method="POST" action="{{ url_for('confirm_delivery', auction_id=a.id) }}">
                                            <button type="submit" class="btn btn-sm btn-success fw-bold py-0 px-2">
                                                <i class="fa-solid fa-check me-1"></i> OK (Entregue)
                                            </button>
                                        </form>
                                        {% endif %}
                                        <form method="POST" action="{{ url_for('delete_auction', auction_id=a.id) }}" onsubmit="return confirm('Remover este registro do histórico?');">
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
                                <td colspan="5" class="text-center text-secondary py-3">Nenhum lance finalizado ainda.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

        </div>
    </div>
    {% endif %}

    <!-- ==========================================
         ABA 2: EVENTOS & CHAMADA TS3
         ========================================== -->
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
                            <small class="text-secondary mb-1">Se o host do seu TS3 estiver bloqueando conexão externa, cole aqui os nicks copiados da sala:</small>
                            <textarea name="raw_nicks" rows="2" class="form-control form-control-sm bg-black text-light border-secondary mb-2"></textarea>
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

    <!-- ==========================================
         ABA 3: PLAYERS & RANKING
         ========================================== -->
    {% if tab == 'players' %}
    <div class="row g-4">
        {% if session['role'] == 'admin' %}
        <div class="col-lg-4">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-user-plus text-success me-1"></i> Cadastrar Novo Player / Admin</h6>
                <form method="POST" action="{{ url_for('create_user') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Login de Acesso ao Site</label>
                        <input type="text" name="username" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Senha do Player</label>
                        <input type="text" name="password" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nick Exato no TeamSpeak 3</label>
                        <input type="text" name="ts_nick" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Permissão no Site</label>
                        <select name="role" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="player" selected>Player</option>
                            <option value="admin">Administrador</option>
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
                                <th>Itens Arrematados</th>
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

    <!-- ==========================================
         ABA 4: CONFIGURAR TS3
         ========================================== -->
    {% if tab == 'tsconfig' and session['role'] == 'admin' %}
    <div class="row justify-content-center">
        <div class="col-lg-7">
            <div class="card p-4">
                <h5 class="fw-bold mb-1"><i class="fa-solid fa-server text-primary me-2"></i> Credenciais do Servidor TeamSpeak 3 (ServerQuery)</h5>
                <p class="text-secondary small mb-4">
                    Preencha os dados do seu servidor TS3 para puxar os nicks presentes nas salas em tempo real.
                </p>

                <form method="POST" action="{{ url_for('save_tsconfig') }}">
                    <div class="row g-3 mb-3">
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Endereço IP ou Domínio do TS3</label>
                            <input type="text" name="host" value="{{ ts_cfg.host }}" class="form-control bg-dark text-light border-secondary" required>
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
                            <input type="text" name="query_user" value="{{ ts_cfg.query_user }}" class="form-control bg-dark text-light border-secondary">
                        </div>
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Senha ServerQuery</label>
                            <input type="password" name="query_pass" value="{{ ts_cfg.query_pass }}" class="form-control bg-dark text-light border-secondary">
                        </div>
                    </div>

                    <div class="mb-4">
                        <label class="form-label small text-secondary">Ignorar Salas com estes Nomes (separados por vírgula)</label>
                        <input type="text" name="ignore_channels" value="{{ ts_cfg.ignore_channels }}" class="form-control bg-dark text-light border-secondary">
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

    <!-- ==========================================
         ABA 5: MEU PERFIL
         ========================================== -->
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
                        <input type="password" name="new_password" class="form-control bg-dark text-light border-secondary">
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
<script>
    // Cronômetro regressivo em tempo real para os lances abertos e desempates de 6 min
    function initTimers() {
        const timers = document.querySelectorAll('.timer-box');
        if (!timers.length) return;

        setInterval(() => {
            let shouldReload = false;
            timers.forEach(el => {
                let sec = parseInt(el.getAttribute('data-seconds'), 10);
                if (isNaN(sec)) return;
                if (sec <= 0) {
                    el.textContent = "00:00";
                    shouldReload = true;
                } else {
                    sec -= 1;
                    el.setAttribute('data-seconds', sec);
                    const m = String(Math.floor(sec / 60)).padStart(2, '0');
                    const s = String(sec % 60).padStart(2, '0');
                    el.textContent = m + ":" + s;
                }
            });
            if (shouldReload) {
                window.location.reload();
            }
        }, 1000);
    }
    document.addEventListener('DOMContentLoaded', initTimers);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
