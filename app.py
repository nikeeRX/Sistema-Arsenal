import os
import re
import csv
import io
import json
import time
import socket
import sqlite3
import urllib.request
import urllib.parse
import http.cookiejar
from urllib.parse import urlparse
from datetime import datetime
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import pg8000.dbapi as pg_dbapi
except ImportError:
    pg_dbapi = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ts3_guild_tracker_secret_key_2026")

DEFAULT_NEOTS_API_URL = "https://neots.com.br/wp-json/neots/v1/service/383/viewer?_neots_action=view_channels&force=1"
PW_CLASSES = ["WR", "MG", "WF", "WB", "EA", "EP", "MC", "PSY", "MS", "SK"]

# Cache leve em memória para exibir os nicks online do TS3 dentro da Planilha de TW
LAST_TS3_ONLINE_CACHE = {"nicks": [], "updated_at": "Nunca"}

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

def safe_add_column(table_name, column_def):
    try:
        db_execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")
    except Exception:
        pass

def normalize_pw_class(cls_str):
    c = (cls_str or "").strip().upper()
    if c == "ESP":
        return "PSY"
    return c if c in PW_CLASSES else "WR"

def init_db():
    id_col = "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # 1. Usuários da Guild
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_col},
            username VARCHAR(80) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            ts_nick VARCHAR(120) NOT NULL,
            player_class VARCHAR(20) DEFAULT 'WR',
            role VARCHAR(20) DEFAULT 'player',
            bonus_tkp INTEGER DEFAULT 0,
            account_status VARCHAR(30) DEFAULT 'ativo',
            reset_status VARCHAR(30) DEFAULT 'nenhum',
            created_at VARCHAR(40)
        )
    """)
    safe_add_column("users", "player_class VARCHAR(20) DEFAULT 'WR'")
    safe_add_column("users", "bonus_tkp INTEGER DEFAULT 0")
    safe_add_column("users", "account_status VARCHAR(30) DEFAULT 'ativo'")
    safe_add_column("users", "reset_status VARCHAR(30) DEFAULT 'nenhum'")

    db_execute("UPDATE users SET player_class = 'WR' WHERE player_class IS NULL OR player_class = ''")
    db_execute("UPDATE users SET account_status = 'ativo' WHERE account_status IS NULL OR account_status = ''")
    db_execute("UPDATE users SET reset_status = 'nenhum' WHERE reset_status IS NULL OR reset_status = ''")

    # 2. Configuração TS3 + Integração API NEO TEAMSPEAK
    db_execute("""
        CREATE TABLE IF NOT EXISTS ts_config (
            id INTEGER PRIMARY KEY,
            host VARCHAR(150) DEFAULT '157.151.9.204',
            voice_port INTEGER DEFAULT 9971,
            query_port INTEGER DEFAULT 10011,
            query_user VARCHAR(100) DEFAULT '',
            query_pass VARCHAR(150) DEFAULT '',
            ignore_channels TEXT DEFAULT 'AFK,Ausente',
            neots_api_url TEXT DEFAULT '',
            neots_cookie TEXT DEFAULT ''
        )
    """)
    safe_add_column("ts_config", "neots_api_url TEXT DEFAULT ''")
    safe_add_column("ts_config", "neots_cookie TEXT DEFAULT ''")

    # 3. Eventos e Presenças (1 Presença = +1 TKP)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS events (
            id {id_col},
            title VARCHAR(150) NOT NULL,
            event_type VARCHAR(80) DEFAULT 'GVG / TW',
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
            method VARCHAR(120) DEFAULT 'NeoTS Auto',
            checked_at VARCHAR(40),
            UNIQUE(event_id, user_id)
        )
    """)

    # 4. Catálogo Consolidado de Itens (Soma para Saque)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS inventory_items (
            id {id_col},
            item_name VARCHAR(150) UNIQUE NOT NULL,
            rarity VARCHAR(50) DEFAULT 'Raro',
            tkp_value INTEGER DEFAULT 1,
            stock_qty INTEGER DEFAULT 0,
            boss_source VARCHAR(120) DEFAULT '',
            updated_at VARCHAR(40)
        )
    """)
    safe_add_column("inventory_items", "tkp_value INTEGER DEFAULT 1")

    # 5. Histórico de Entradas no Estoque por Data de Chegada
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS inventory_entries (
            id {id_col},
            item_id INTEGER NOT NULL,
            item_name VARCHAR(150) NOT NULL,
            rarity VARCHAR(50) DEFAULT 'Raro',
            tkp_value INTEGER DEFAULT 1,
            qty_added INTEGER NOT NULL,
            boss_source VARCHAR(120) DEFAULT '',
            arrival_date VARCHAR(40) NOT NULL
        )
    """)

    # 6. Leilões / Disputas de Saque em TKP
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

    # 7. Lances dos Players em TKP
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

    # 8. Notificações
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS notifications (
            id {id_col},
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at VARCHAR(40)
        )
    """)

    # 9. Módulo Planilha de TW (PTs com Posição ATAQUE ou DEFESA, Slots e Nicks Extras)
    db_execute(f"""
        CREATE TABLE IF NOT EXISTS tw_parties (
            id {id_col},
            pt_name VARCHAR(120) NOT NULL,
            pt_function VARCHAR(150) DEFAULT '',
            pt_side VARCHAR(30) DEFAULT 'ATAQUE',
            created_at VARCHAR(40)
        )
    """)
    safe_add_column("tw_parties", "pt_side VARCHAR(30) DEFAULT 'ATAQUE'")
    db_execute("UPDATE tw_parties SET pt_side = 'ATAQUE' WHERE pt_side IS NULL OR pt_side = ''")

    db_execute(f"""
        CREATE TABLE IF NOT EXISTS tw_party_slots (
            id {id_col},
            pt_id INTEGER NOT NULL,
            slot_number INTEGER NOT NULL,
            player_nick VARCHAR(120) DEFAULT '',
            pilot_nick VARCHAR(120) DEFAULT '',
            player_class VARCHAR(20) DEFAULT '',
            UNIQUE(pt_id, slot_number)
        )
    """)
    safe_add_column("tw_party_slots", "pilot_nick VARCHAR(120) DEFAULT ''")

    db_execute(f"""
        CREATE TABLE IF NOT EXISTS tw_extra_players (
            id {id_col},
            nick VARCHAR(120) UNIQUE NOT NULL,
            player_class VARCHAR(20) DEFAULT 'WR',
            pilot_note VARCHAR(120) DEFAULT '',
            created_at VARCHAR(40)
        )
    """)

    existing_items = db_execute("SELECT * FROM inventory_items", fetch=True)
    for it in existing_items:
        has_entry = db_execute("SELECT id FROM inventory_entries WHERE item_id = ?", (it["id"],), fetchone=True)
        if not has_entry and int(it.get("stock_qty") or 0) > 0:
            db_execute("""
                INSERT INTO inventory_entries (item_id, item_name, rarity, tkp_value, qty_added, boss_source, arrival_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                it["id"],
                it["item_name"],
                it.get("rarity", "Raro"),
                int(it.get("tkp_value") or 1),
                int(it["stock_qty"]),
                it.get("boss_source", ""),
                it.get("updated_at") or datetime.now().strftime("%d/%m/%Y %H:%M")
            ))

    cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
    if not cfg:
        db_execute("""
            INSERT INTO ts_config (id, host, voice_port, query_port, query_user, query_pass, ignore_channels, neots_api_url, neots_cookie)
            VALUES (1, '157.151.9.204', 9971, 10011, '', '', 'AFK,Ausente', ?, '')
        """, (DEFAULT_NEOTS_API_URL,))
    elif not cfg.get("neots_api_url"):
        db_execute("UPDATE ts_config SET neots_api_url = ? WHERE id = 1", (DEFAULT_NEOTS_API_URL,))

    admin = db_execute("SELECT id FROM users WHERE username = ?", ("admin",), fetchone=True)
    if not admin:
        db_execute("""
            INSERT INTO users (username, password_hash, ts_nick, player_class, role, bonus_tkp, account_status, reset_status, created_at)
            VALUES (?, ?, ?, 'WR', ?, 0, 'ativo', 'nenhum', ?)
        """, (
            "admin",
            generate_password_hash("admin123"),
            "Sinister",
            "admin",
            datetime.now().strftime("%d/%m/%Y %H:%M")
        ))

init_db()

# ==========================================
# CARTEIRA TKP E VALIDAÇÃO DE SALDO
# ==========================================
def create_notification(user_id, message):
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute(
        "INSERT INTO notifications (user_id, message, is_read, created_at) VALUES (?, ?, 0, ?)",
        (user_id, message, now_str)
    )

def calculate_wallets(exclude_auction_id=None):
    users = db_execute("SELECT id, bonus_tkp FROM users", fetch=True)
    att_rows = db_execute("SELECT user_id, COUNT(*) as cnt FROM attendance GROUP BY user_id", fetch=True)
    att_map = {r["user_id"]: int(r["cnt"]) for r in att_rows}

    spent_rows = db_execute("""
        SELECT winner_user_id, SUM(winning_bid) as total_spent
        FROM auctions
        WHERE winner_user_id IS NOT NULL AND status IN ('Aguardando Entrega', 'Entregue (OK)')
        GROUP BY winner_user_id
    """, fetch=True)
    spent_map = {r["winner_user_id"]: int(r["total_spent"] or 0) for r in spent_rows}

    open_auctions = db_execute(
        "SELECT id, round_number FROM auctions WHERE status IN ('Aberto', 'Em Desempate')",
        fetch=True
    )
    locked_map = {}
    for oa in open_auctions:
        if exclude_auction_id and int(oa["id"]) == int(exclude_auction_id):
            continue
        bids = db_execute("""
            SELECT user_id, MAX(bid_amount) as max_b
            FROM auction_bids
            WHERE auction_id = ? AND round_number = ?
            GROUP BY user_id
        """, (oa["id"], oa["round_number"]), fetch=True)
        for b in bids:
            uid = b["user_id"]
            locked_map[uid] = locked_map.get(uid, 0) + int(b["max_b"] or 0)

    wallets = {}
    for u in users:
        uid = u["id"]
        pres = att_map.get(uid, 0)
        bonus = int(u.get("bonus_tkp") or 0)
        spent = spent_map.get(uid, 0)
        locked = locked_map.get(uid, 0)
        balance = max(0, (pres + bonus) - spent)
        available = max(0, balance - locked)
        wallets[uid] = {
            "presences": pres,
            "bonus_tkp": bonus,
            "spent_tkp": spent,
            "locked_tkp": locked,
            "tkp_balance": balance,
            "available_to_bid": available
        }
    return wallets

# ==========================================
# MOTOR DE APURAÇÃO DE LANCES (TKP) E DESEMPATE (6 MIN)
# ==========================================
def resolve_expired_auctions(force_auction_id=None):
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

        bids = db_execute("""
            SELECT b.*, u.username, u.ts_nick
            FROM auction_bids b
            JOIN users u ON b.user_id = u.id
            WHERE b.auction_id = ? AND b.round_number = ?
            ORDER BY b.bid_amount DESC, b.id ASC
        """, (aid, rnd), fetch=True)

        if not bids:
            if auc["status"] == "Em Desempate" and auc["tied_user_ids"]:
                new_end_epoch = int(time.time()) + 360
                new_end_str = datetime.fromtimestamp(new_end_epoch).strftime("%d/%m %H:%M:%S")
                db_execute(
                    "UPDATE auctions SET ends_at_epoch = ?, ends_at_str = ? WHERE id = ?",
                    (new_end_epoch, new_end_str, aid)
                )
            else:
                db_execute(
                    "UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?",
                    (auc["qty"], auc["item_id"])
                )
                db_execute(
                    "UPDATE auctions SET status = 'Sem Lances (Devolvido)' WHERE id = ?",
                    (aid,)
                )
            continue

        best_per_user = {}
        for b in bids:
            uid = b["user_id"]
            amt = int(b["bid_amount"])
            if uid not in best_per_user or amt > best_per_user[uid]["bid_amount"]:
                best_per_user[uid] = b

        max_bid = max(item["bid_amount"] for item in best_per_user.values())
        top_bidders = [item for item in best_per_user.values() if item["bid_amount"] == max_bid]

        if len(top_bidders) == 1:
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
                f"🏆 Parabéns! Você venceu o saque do item '{auc['item_name']} (x{auc['qty']})' com o lance de {max_bid} TKP! Procure o Admin in-game para receber seu item."
            )
        else:
            tied_ids_str = ",".join(str(b["user_id"]) for b in top_bidders)
            tied_names = ", ".join(b["username"] for b in top_bidders)
            new_round = rnd + 1
            new_end_epoch = int(time.time()) + 360
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
                    f"⚡ EMPATE no item '{auc['item_name']}' no valor de {max_bid} TKP (Empatados: {tied_names})! Uma nova rodada exclusiva de 6 minutos foi aberta a partir de {max_bid} TKP."
                )

# ==========================================
# INTEGRAÇÃO API NEO TEAMSPEAK (JSON) + ÁRVORE DE CANAIS
# ==========================================
def clean_channel_label(name):
    if not name:
        return ""
    cleaned = re.sub(r"\[[^\]]*spacer[^\]]*\]", "", str(name), flags=re.IGNORECASE)
    cleaned = cleaned.replace("•", "").strip()
    return cleaned or str(name).strip()

def build_channel_hierarchy(channels_list):
    by_cid = {}
    for ch in channels_list:
        cid = str(ch.get("cid") or ch.get("channel_id") or "")
        if cid:
            by_cid[cid] = ch

    hierarchy = {}
    for cid, ch in by_cid.items():
        path_parts = []
        curr = ch
        visited = set()
        while curr:
            c_id = str(curr.get("cid") or curr.get("channel_id") or "")
            if not c_id or c_id in visited:
                break
            visited.add(c_id)
            label = clean_channel_label(curr.get("channel_name", ""))
            if label:
                path_parts.insert(0, label)
            pid = str(curr.get("pid") or "0")
            if pid == "0" or pid not in by_cid:
                break
            curr = by_cid[pid]

        full_path = " > ".join(path_parts) if path_parts else clean_channel_label(ch.get("channel_name", ""))
        hierarchy[cid] = {
            "name": clean_channel_label(ch.get("channel_name", "")),
            "raw_name": ch.get("channel_name", ""),
            "full_path": full_path
        }
    return hierarchy

def parse_neots_json_payload(data, ignore_keywords, filter_mode="all"):
    if not isinstance(data, dict) or "clients" not in data:
        return False, "Formato JSON inesperado retornado pela API (chave 'clients' ausente)."

    channels_list = data.get("channels") or []
    clients_list = data.get("clients") or []
    hierarchy = build_channel_hierarchy(channels_list)

    online_clients = []
    skipped_afk = []
    skipped_filter = []

    for cl in clients_list:
        if str(cl.get("client_type", "0")) != "0":
            continue
        nick = (cl.get("client_nickname") or "").strip()
        if not nick:
            continue
        cid = str(cl.get("cid") or "")
        ch_info = hierarchy.get(cid, {"name": f"Canal {cid}", "full_path": f"Canal {cid}"})
        full_path = ch_info["full_path"]
        path_lower = full_path.lower()

        if ignore_keywords and any(kw in path_lower for kw in ignore_keywords):
            skipped_afk.append(f"{nick} ({full_path})")
            continue

        if filter_mode == "gvg_tw":
            if not ("gvg" in path_lower or "tw" in path_lower):
                skipped_filter.append(f"{nick} ({full_path})")
                continue
        elif filter_mode == "gvg":
            if "gvg" not in path_lower:
                skipped_filter.append(f"{nick} ({full_path})")
                continue
        elif filter_mode == "tw":
            if "tw" not in path_lower:
                skipped_filter.append(f"{nick} ({full_path})")
                continue
        elif filter_mode == "eventos":
            if "eventos" not in path_lower and "artes marciais" not in path_lower and "tigres" not in path_lower:
                skipped_filter.append(f"{nick} ({full_path})")
                continue

        online_clients.append({
            "nick": nick,
            "cid": cid,
            "channel_name": ch_info["name"],
            "channel_path": full_path
        })

    LAST_TS3_ONLINE_CACHE["nicks"] = [c["nick"] for c in online_clients]
    LAST_TS3_ONLINE_CACHE["updated_at"] = datetime.now().strftime("%H:%M:%S")

    return True, {
        "online": online_clients,
        "afk": skipped_afk,
        "filtered_out": skipped_filter
    }

def fetch_neots_api_clients(cfg, filter_mode="all"):
    api_url = (cfg.get("neots_api_url") or DEFAULT_NEOTS_API_URL).strip()
    neots_user = (cfg.get("query_user") or "").strip()
    neots_pass = (cfg.get("query_pass") or "").strip()
    neots_cookie = (cfg.get("neots_cookie") or "").strip()
    ignore_raw = (cfg.get("ignore_channels") or "AFK,Ausente").split(",")
    ignore_keywords = [k.strip().lower() for k in ignore_raw if k.strip()]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://neots.com.br/"
    }
    if neots_cookie:
        if "||NONCE:" in neots_cookie:
            cookie_part, nonce_part = neots_cookie.split("||NONCE:", 1)
            headers["Cookie"] = cookie_part.strip()
            headers["X-WP-Nonce"] = nonce_part.strip()
        else:
            headers["Cookie"] = neots_cookie

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    try:
        req = urllib.request.Request(api_url, headers=headers, method="GET")
        with opener.open(req, timeout=10) as resp:
            raw_body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw_body)
            if isinstance(data, dict) and "clients" in data:
                return parse_neots_json_payload(data, ignore_keywords, filter_mode=filter_mode)
    except urllib.error.HTTPError as http_err:
        if http_err.code in (401, 403) and neots_user and neots_pass:
            try:
                login_url = "https://neots.com.br/wp-login.php"
                login_data = urllib.parse.urlencode({
                    "log": neots_user,
                    "pwd": neots_pass,
                    "wp-submit": "Acessar",
                    "redirect_to": "https://neots.com.br/painel/",
                    "testcookie": "1"
                }).encode("utf-8")
                login_headers = dict(headers)
                login_headers["Content-Type"] = "application/x-www-form-urlencoded"
                login_req = urllib.request.Request(login_url, data=login_data, headers=login_headers, method="POST")
                with opener.open(login_req, timeout=10) as login_resp:
                    panel_html = login_resp.read().decode("utf-8", errors="ignore")
                    nonce_match = re.search(r'"nonce"\s*:\s*"([a-zA-Z0-9]+)"', panel_html)
                    if nonce_match:
                        headers["X-WP-Nonce"] = nonce_match.group(1)

                req2 = urllib.request.Request(api_url, headers=headers, method="GET")
                with opener.open(req2, timeout=10) as resp2:
                    raw_body2 = resp2.read().decode("utf-8", errors="ignore")
                    data2 = json.loads(raw_body2)
                    return parse_neots_json_payload(data2, ignore_keywords, filter_mode=filter_mode)
            except Exception as auth_err:
                return False, f"A API da NeoTS exigiu sessão autenticada (HTTP {http_err.code}) e o login automático retornou: {str(auth_err)}. Verifique o Cookie na aba Configurar TS3."
        else:
            return False, f"A API da NeoTS retornou HTTP {http_err.code}. Verifique o Cookie da Sessão na aba Configurar TeamSpeak 3."
    except Exception as e:
        return False, f"Erro ao consultar API da NeoTS: {str(e)}"

    return False, "Não foi possível obter a lista de canais da API da NeoTS."

def match_player_nick(registered_nick, online_clients_list):
    reg_clean = registered_nick.strip().lower()
    if not reg_clean:
        return None
    for cl in online_clients_list:
        if isinstance(cl, dict):
            on_nick = cl.get("nick", "").strip()
            ch_path = cl.get("channel_path", "")
        else:
            on_nick = str(cl).strip()
            ch_path = "Lista Manual"
        on_clean = on_nick.lower()
        if reg_clean == on_clean or reg_clean in on_clean:
            return {"nick": on_nick, "channel_path": ch_path}
    return None

def is_nick_online_in_cache(nick_str, online_nicks_list):
    clean = (nick_str or "").strip().lower()
    if not clean or not online_nicks_list:
        return False
    for on in online_nicks_list:
        on_low = on.strip().lower()
        if clean == on_low or clean in on_low or on_low in clean:
            return True
    return False

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
# ROTAS DE LOGIN, AUTO-CADASTRO E RESET DE SENHA
# ==========================================
@app.route("/api/check_user_login")
def api_check_user_login():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"exists": False, "reset_approved": False, "account_status": ""})

    user = db_execute(
        "SELECT username, account_status, reset_status FROM users WHERE LOWER(username) = LOWER(?)",
        (username,), fetchone=True
    )
    if not user:
        return jsonify({"exists": False, "reset_approved": False, "account_status": ""})

    acc_status = user.get("account_status") or "ativo"
    rst_status = user.get("reset_status") or "nenhum"
    return jsonify({
        "exists": True,
        "username": user["username"],
        "account_status": acc_status,
        "reset_status": rst_status,
        "reset_approved": (rst_status == "aprovado")
    })

@app.route("/login", methods=["GET", "POST"])
def login():
    force_reset_username = request.args.get("reset_user", "").strip()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        user = db_execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
        if not user:
            flash("Usuário não encontrado. Verifique o login ou clique em 'Criar Conta'.", "danger")
            return render_template_string(LOGIN_HTML, force_reset_username="", pw_classes=PW_CLASSES)

        acc_status = user.get("account_status") or "ativo"
        rst_status = user.get("reset_status") or "nenhum"

        if acc_status == "pendente":
            flash("⏳ Seu cadastro já foi recebido e está aguardando a aprovação de um Administrador!", "warning")
            return render_template_string(LOGIN_HTML, force_reset_username="", pw_classes=PW_CLASSES)

        if rst_status == "aprovado":
            if new_password:
                if len(new_password) < 3:
                    flash("A nova senha precisa ter pelo menos 3 caracteres.", "warning")
                    return render_template_string(LOGIN_HTML, force_reset_username=user["username"], pw_classes=PW_CLASSES)
                if confirm_password and new_password != confirm_password:
                    flash("A confirmação da nova senha não confere.", "danger")
                    return render_template_string(LOGIN_HTML, force_reset_username=user["username"], pw_classes=PW_CLASSES)

                db_execute(
                    "UPDATE users SET password_hash = ?, reset_status = 'nenhum' WHERE id = ?",
                    (generate_password_hash(new_password), user["id"])
                )
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["ts_nick"] = user["ts_nick"]
                session["player_class"] = normalize_pw_class(user.get("player_class"))
                session["role"] = user["role"]
                flash("✅ Nova senha criada com sucesso! Bem-vindo de volta.", "success")
                return redirect(url_for("dashboard"))
            else:
                flash("🔓 O Admin liberou a criação da sua nova senha! Digite sua nova senha abaixo.", "info")
                return render_template_string(LOGIN_HTML, force_reset_username=user["username"], pw_classes=PW_CLASSES)

        if check_password_hash(user["password_hash"], password):
            if rst_status != "nenhum":
                db_execute("UPDATE users SET reset_status = 'nenhum' WHERE id = ?", (user["id"],))
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["ts_nick"] = user["ts_nick"]
            session["player_class"] = normalize_pw_class(user.get("player_class"))
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))

        flash("Senha incorreta. Se esqueceu sua senha, clique em 'Reset Senha' acima.", "danger")

    return render_template_string(LOGIN_HTML, force_reset_username=force_reset_username, pw_classes=PW_CLASSES)

@app.route("/register", methods=["POST"])
def register_player():
    username = request.form.get("username", "").strip()
    ts_nick = request.form.get("ts_nick", "").strip()
    player_class = normalize_pw_class(request.form.get("player_class", "WR"))
    password = request.form.get("password", "").strip()

    if not username or not ts_nick or not password:
        flash("Preencha Login, Nick do TS3, Classe e Senha para solicitar seu cadastro.", "warning")
        return redirect(url_for("login"))

    exists = db_execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if exists:
        flash(f"O login '{username}' já existe no sistema. Escolha outro ou faça login.", "danger")
        return redirect(url_for("login"))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute("""
        INSERT INTO users (username, password_hash, ts_nick, player_class, role, bonus_tkp, account_status, reset_status, created_at)
        VALUES (?, ?, ?, ?, 'player', 0, 'pendente', 'nenhum', ?)
    """, (username, generate_password_hash(password), ts_nick, player_class, now_str))

    flash(
        f"✅ Cadastro de '{username}' (TS3: {ts_nick} | Classe: {player_class}) enviado com sucesso! "
        f"Assim que o Administrador aprovar no painel, seu acesso estará liberado.",
        "success"
    )
    return redirect(url_for("login"))

@app.route("/forgot_password", methods=["POST"])
def forgot_password_request():
    username = request.form.get("username", "").strip()
    if not username:
        flash("Digite seu login para solicitar o reset de senha ao Admin.", "warning")
        return redirect(url_for("login"))

    user = db_execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if not user:
        flash(f"Nenhuma conta encontrada com o login '{username}'.", "danger")
        return redirect(url_for("login"))

    if user.get("reset_status") == "aprovado":
        flash("🔓 O Admin já aprovou seu reset! Digite sua nova senha abaixo.", "info")
        return redirect(url_for("login", reset_user=user["username"]))

    db_execute("UPDATE users SET reset_status = 'solicitado' WHERE id = ?", (user["id"],))
    flash(
        f"🔑 Pedido de nova senha para '{user['username']}' enviado ao Admin! "
        f"Assim que o Admin aprovar, basta digitar seu login na tela de entrada para cadastrar a nova senha.",
        "info"
    )
    return redirect(url_for("login"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/notifications/read", methods=["POST"])
@login_required
def read_notifications():
    db_execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
    return redirect(request.referrer or url_for("dashboard"))

# ==========================================
# DASHBOARD PRINCIPAL
# ==========================================
@app.route("/")
@login_required
def dashboard():
    resolve_expired_auctions()

    tab = request.args.get("tab", "estoque")
    tw_view = request.args.get("tw_view", "planilha")  # 'planilha' (estilo Excel lado a lado) ou 'cards' (individual)
    selected_pt_id = request.args.get("pt_id", type=int)
    selected_event_id = request.args.get("event_id", type=int)

    all_users = db_execute("SELECT * FROM users ORDER BY username ASC", fetch=True)
    users_map = {u["id"]: u for u in all_users}
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
    wallets = calculate_wallets()

    won_rows = db_execute("""
        SELECT winner_user_id, SUM(qty) as cnt
        FROM auctions
        WHERE winner_user_id IS NOT NULL AND status IN ('Aguardando Entrega', 'Entregue (OK)')
        GROUP BY winner_user_id
    """, fetch=True)
    drop_counts = {r["winner_user_id"]: int(r["cnt"]) for r in won_rows}

    active_users = []
    pending_users = []
    reset_requests = []

    registered_class_counts = {c: 0 for c in PW_CLASSES}
    players_by_class = {c: [] for c in PW_CLASSES}

    online_ts3_nicks = LAST_TS3_ONLINE_CACHE.get("nicks") or []

    for u in all_users:
        w = wallets.get(u["id"], {})
        presences = w.get("presences", 0)
        u["presences"] = presences
        u["pct"] = round((presences / total_events) * 100, 1) if total_events > 0 else 0.0
        u["drops_won"] = drop_counts.get(u["id"], 0)
        u["bonus_tkp"] = w.get("bonus_tkp", 0)
        u["spent_tkp"] = w.get("spent_tkp", 0)
        u["locked_tkp"] = w.get("locked_tkp", 0)
        u["tkp_balance"] = w.get("tkp_balance", 0)
        u["available_to_bid"] = w.get("available_to_bid", 0)
        p_cls = normalize_pw_class(u.get("player_class"))
        u["player_class"] = p_cls
        u["is_online_ts"] = is_nick_online_in_cache(u["ts_nick"], online_ts3_nicks)
        u["is_extra"] = False

        acc_st = u.get("account_status") or "ativo"
        rst_st = u.get("reset_status") or "nenhum"

        if acc_st == "pendente":
            pending_users.append(u)
        else:
            active_users.append(u)
            registered_class_counts[p_cls] = registered_class_counts.get(p_cls, 0) + 1
            players_by_class[p_cls].append(u)

        if rst_st == "solicitado":
            reset_requests.append(u)

    # Nicks Extras / Convidados / Líderes de Fora cadastrados na Planilha de TW
    tw_extra_players = db_execute("SELECT * FROM tw_extra_players ORDER BY nick ASC", fetch=True)
    for ex in tw_extra_players:
        ex_cls = normalize_pw_class(ex.get("player_class"))
        ex["player_class"] = ex_cls
        ex["is_online_ts"] = is_nick_online_in_cache(ex["nick"], online_ts3_nicks)
        registered_class_counts[ex_cls] = registered_class_counts.get(ex_cls, 0) + 1
        players_by_class[ex_cls].append({
            "username": ex["nick"],
            "ts_nick": ex["nick"],
            "player_class": ex_cls,
            "is_extra": True,
            "pilot_note": ex.get("pilot_note") or "Externo / Avulso",
            "is_online_ts": ex["is_online_ts"]
        })

    my_wallet = wallets.get(session["user_id"], {"tkp_balance": 0, "available_to_bid": 0, "locked_tkp": 0})

    # ==========================================================
    # DADOS DO MÓDULO PLANILHA DE TW (ATAQUE vs DEFESA + CONTAGENS)
    # ==========================================================
    tw_parties = db_execute("SELECT * FROM tw_parties ORDER BY id ASC", fetch=True)
    all_tw_slots = db_execute("SELECT * FROM tw_party_slots ORDER BY pt_id ASC, slot_number ASC", fetch=True)

    slots_by_pt = {}
    for s in all_tw_slots:
        slots_by_pt.setdefault(s["pt_id"], {})[int(s["slot_number"])] = s

    tw_assigned_class_counts = {c: 0 for c in PW_CLASSES}
    tw_attack_class_counts = {c: 0 for c in PW_CLASSES}
    tw_defense_class_counts = {c: 0 for c in PW_CLASSES}
    assigned_nicks_map = {}

    attack_parties = []
    defense_parties = []

    for pt in tw_parties:
        pid = pt["id"]
        side = (pt.get("pt_side") or "ATAQUE").strip().upper()
        if side not in ("ATAQUE", "DEFESA"):
            side = "ATAQUE"
        pt["pt_side"] = side

        pt_slots = []
        filled_count = 0
        for slot_num in range(1, 11):
            s_obj = slots_by_pt.get(pid, {}).get(slot_num, {
                "slot_number": slot_num,
                "player_nick": "",
                "pilot_nick": "",
                "player_class": ""
            })
            p_nick = (s_obj.get("player_nick") or "").strip()
            pil_nick = (s_obj.get("pilot_nick") or "").strip()
            cls_val = normalize_pw_class(s_obj.get("player_class")) if p_nick and s_obj.get("player_class") else ""
            s_obj["player_class"] = cls_val

            if p_nick:
                filled_count += 1
                assigned_nicks_map[p_nick.lower()] = f"{pt['pt_name']} ({side})"
                if cls_val in PW_CLASSES:
                    tw_assigned_class_counts[cls_val] += 1
                    if side == "ATAQUE":
                        tw_attack_class_counts[cls_val] += 1
                    else:
                        tw_defense_class_counts[cls_val] += 1
            if pil_nick:
                assigned_nicks_map[pil_nick.lower()] = f"{pt['pt_name']} ({side})"

            s_obj["is_online_ts"] = (
                is_nick_online_in_cache(p_nick, online_ts3_nicks)
                or is_nick_online_in_cache(pil_nick, online_ts3_nicks)
            )
            pt_slots.append(s_obj)

        pt["slots"] = pt_slots
        pt["filled_count"] = filled_count

        if side == "DEFESA":
            defense_parties.append(pt)
        else:
            attack_parties.append(pt)

    total_attack_players = sum(tw_attack_class_counts.values())
    total_defense_players = sum(tw_defense_class_counts.values())
    total_tw_escalados = total_attack_players + total_defense_players

    for cls_key, plist in players_by_class.items():
        for pl in plist:
            pl_ts_low = (pl.get("ts_nick") or "").strip().lower()
            pl_usr_low = (pl.get("username") or "").strip().lower()
            pl["assigned_pt"] = assigned_nicks_map.get(pl_ts_low) or assigned_nicks_map.get(pl_usr_low) or ""

    # 1. Catálogo completo de itens
    catalog_items = db_execute("SELECT * FROM inventory_items ORDER BY item_name ASC", fetch=True)

    # 2. Histórico de Entradas no Estoque por Data de Chegada
    stock_entries = db_execute("SELECT * FROM inventory_entries ORDER BY id DESC", fetch=True)

    entries_count_map = {}
    for e in stock_entries:
        entries_count_map[e["item_id"]] = entries_count_map.get(e["item_id"], 0) + 1

    # 3. Drops Disponíveis para Saque
    available_drops = []
    for item in catalog_items:
        item["tkp_value"] = int(item.get("tkp_value") or 0)
        item["stock_qty"] = int(item.get("stock_qty") or 0)
        item["total_tkp_worth"] = item["stock_qty"] * item["tkp_value"]
        item["batches_count"] = entries_count_map.get(item["id"], 0)
        if item["stock_qty"] > 0:
            available_drops.append(item)

    # 4. Leilões / Saques ativos e histórico
    auctions = db_execute("""
        SELECT a.*, u.username as winner_name, u.ts_nick as winner_nick
        FROM auctions a
        LEFT JOIN users u ON a.winner_user_id = u.id
        ORDER BY a.id DESC
    """, fetch=True)

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

    my_notifications = db_execute(
        "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY id DESC",
        (session["user_id"],), fetch=True
    )

    return render_template_string(
        MAIN_HTML,
        tab=tab,
        tw_view=tw_view,
        selected_pt_id=selected_pt_id,
        pw_classes=PW_CLASSES,
        users=active_users,
        tw_extra_players=tw_extra_players,
        online_ts3_nicks=online_ts3_nicks,
        ts3_cache_time=LAST_TS3_ONLINE_CACHE.get("updated_at", "Nunca"),
        pending_users=pending_users,
        reset_requests=reset_requests,
        registered_class_counts=registered_class_counts,
        tw_assigned_class_counts=tw_assigned_class_counts,
        tw_attack_class_counts=tw_attack_class_counts,
        tw_defense_class_counts=tw_defense_class_counts,
        total_attack_players=total_attack_players,
        total_defense_players=total_defense_players,
        total_tw_escalados=total_tw_escalados,
        players_by_class=players_by_class,
        tw_parties=tw_parties,
        attack_parties=attack_parties,
        defense_parties=defense_parties,
        my_wallet=my_wallet,
        events=events,
        selected_event=selected_event,
        event_attendance=event_attendance,
        ts_cfg=ts_cfg,
        catalog_items=catalog_items,
        stock_entries=stock_entries,
        available_drops=available_drops,
        auctions=auctions,
        my_notifications=my_notifications,
        total_events=total_events,
        db_mode="PostgreSQL (Railway)" if is_postgres() else "SQLite Local"
    )

# ==========================================
# ROTAS DO MÓDULO PLANILHA DE TW (MODELO EXCEL ATAQUE & DEFESA)
# ==========================================
@app.route("/tw/add_extra_player", methods=["POST"])
@admin_required
def add_tw_extra_player():
    nick = request.form.get("nick", "").strip()
    player_class = normalize_pw_class(request.form.get("player_class", "WR"))
    pilot_note = request.form.get("pilot_note", "").strip()

    if not nick:
        flash("Informe o Nick para incluir na listagem da TW.", "warning")
        return redirect(url_for("dashboard", tab="tw"))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    existing = db_execute("SELECT id FROM tw_extra_players WHERE LOWER(nick) = LOWER(?)", (nick,), fetchone=True)
    if existing:
        db_execute(
            "UPDATE tw_extra_players SET player_class = ?, pilot_note = ? WHERE id = ?",
            (player_class, pilot_note, existing["id"])
        )
        flash(f"Nick avulso '{nick}' ({player_class}) atualizado na base da TW!", "success")
    else:
        db_execute(
            "INSERT INTO tw_extra_players (nick, player_class, pilot_note, created_at) VALUES (?, ?, ?, ?)",
            (nick, player_class, pilot_note, now_str)
        )
        flash(f"Nick '{nick}' ({player_class}) cadastrado com sucesso para a Planilha de TW!", "success")

    return redirect(request.referrer or url_for("dashboard", tab="tw"))

@app.route("/tw/extra_player/<int:extra_id>/delete", methods=["POST"])
@admin_required
def delete_tw_extra_player(extra_id):
    db_execute("DELETE FROM tw_extra_players WHERE id = ?", (extra_id,))
    flash("Nick avulso removido da listagem.", "info")
    return redirect(request.referrer or url_for("dashboard", tab="tw"))

@app.route("/tw/sync_ts3_online", methods=["POST"])
@admin_required
def sync_tw_ts3_online():
    cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
    ok, res = fetch_neots_api_clients(cfg, filter_mode="all")
    if ok:
        online_cnt = len(res["online"])
        flash(f"Lista do TS3 sincronizada! {online_cnt} jogador(es) online disponíveis na seleção das PTs.", "success")
    else:
        flash(res, "danger")
    return redirect(request.referrer or url_for("dashboard", tab="tw"))

@app.route("/tw/seed_default_pts", methods=["POST"])
@admin_required
def seed_default_tw_pts():
    """
    Cria automaticamente as 12 PTs do modelo Excel caso ainda não existam:
    - ATAQUE: CT, RECUPERAÇÃO, APOIO 1, APOIO 2, APOIO 3, APOIO 4
    - DEFESA: KILL CT 1, KILL CT 2, BACKDEF 1, BACKDEF 2, BACKDEF 3, BACKDEF 4
    """
    default_structure = [
        ("CT", "Catapulta Principal", "ATAQUE"),
        ("RECUPERAÇÃO", "Recuperação / Apoio CT", "ATAQUE"),
        ("APOIO 1", "Apoio Ataque 1", "ATAQUE"),
        ("APOIO 2", "Apoio Ataque 2", "ATAQUE"),
        ("APOIO 3", "Apoio Ataque 3", "ATAQUE"),
        ("APOIO 4", "Apoio Ataque 4", "ATAQUE"),
        ("KILL CT 1", "Anti-Catapulta 1", "DEFESA"),
        ("KILL CT 2", "Anti-Catapulta 2", "DEFESA"),
        ("BACKDEF 1", "Defesa Recuada 1", "DEFESA"),
        ("BACKDEF 2", "Defesa Recuada 2", "DEFESA"),
        ("BACKDEF 3", "Defesa Recuada 3", "DEFESA"),
        ("BACKDEF 4", "Defesa Recuada 4", "DEFESA"),
    ]
    existing_pts = db_execute("SELECT pt_name, pt_side FROM tw_parties", fetch=True)
    existing_keys = {(r["pt_name"].strip().upper(), (r.get("pt_side") or "ATAQUE").strip().upper()) for r in existing_pts}

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    added = 0
    for name, func, side in default_structure:
        if (name.upper(), side.upper()) not in existing_keys:
            db_execute(
                "INSERT INTO tw_parties (pt_name, pt_function, pt_side, created_at) VALUES (?, ?, ?, ?)",
                (name, func, side, now_str)
            )
            added += 1

    if added > 0:
        flash(f"✅ {added} PTs padrão do Modelo Excel (Ataque & Defesa) foram criadas na planilha!", "success")
    else:
        flash("As PTs padrão do modelo já estão criadas na planilha.", "info")
    return redirect(url_for("dashboard", tab="tw", tw_view="planilha"))

@app.route("/tw/create_pt", methods=["POST"])
@admin_required
def create_tw_pt():
    pt_name = request.form.get("pt_name", "").strip()
    pt_function = request.form.get("pt_function", "").strip()
    pt_side = request.form.get("pt_side", "ATAQUE").strip().upper()
    if pt_side not in ("ATAQUE", "DEFESA"):
        pt_side = "ATAQUE"

    if not pt_name:
        flash("Informe o nome da PT (ex: CT, APOIO 1, KILL CT 1).", "warning")
        return redirect(url_for("dashboard", tab="tw"))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute(
        "INSERT INTO tw_parties (pt_name, pt_function, pt_side, created_at) VALUES (?, ?, ?, ?)",
        (pt_name, pt_function, pt_side, now_str)
    )
    flash(f"PT '{pt_name}' adicionada no bloco de {pt_side}!", "success")
    return redirect(request.referrer or url_for("dashboard", tab="tw"))

def build_nick_lookup_maps():
    active_users = db_execute("SELECT username, ts_nick, player_class FROM users", fetch=True)
    extra_users = db_execute("SELECT nick, player_class, pilot_note FROM tw_extra_players", fetch=True)

    nick_to_class = {}
    known_nicks = set()
    for u in active_users:
        c = normalize_pw_class(u.get("player_class"))
        if u.get("ts_nick"):
            k = u["ts_nick"].strip().lower()
            nick_to_class[k] = c
            known_nicks.add(k)
        if u.get("username"):
            k = u["username"].strip().lower()
            nick_to_class[k] = c
            known_nicks.add(k)

    for ex in extra_users:
        c = normalize_pw_class(ex.get("player_class"))
        if ex.get("nick"):
            k = ex["nick"].strip().lower()
            nick_to_class[k] = c
            known_nicks.add(k)

    return nick_to_class, known_nicks

@app.route("/tw/<int:pt_id>/save", methods=["POST"])
@admin_required
def save_tw_pt(pt_id):
    pt_name = request.form.get("pt_name", "").strip()
    pt_function = request.form.get("pt_function", "").strip()
    pt_side = request.form.get("pt_side", "ATAQUE").strip().upper()
    if pt_side not in ("ATAQUE", "DEFESA"):
        pt_side = "ATAQUE"
    auto_register_new = request.form.get("auto_register_new") == "1"

    if pt_name:
        db_execute(
            "UPDATE tw_parties SET pt_name = ?, pt_function = ?, pt_side = ? WHERE id = ?",
            (pt_name, pt_function, pt_side, pt_id)
        )

    nick_to_class, known_nicks = build_nick_lookup_maps()
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    new_nicks_created = 0

    for slot_num in range(1, 11):
        nick_val = request.form.get(f"nick_{slot_num}", "").strip()
        pilot_val = request.form.get(f"pilot_{slot_num}", "").strip()
        raw_cls = request.form.get(f"class_{slot_num}", "").strip().upper()

        if nick_val:
            if raw_cls:
                class_val = normalize_pw_class(raw_cls)
            else:
                class_val = nick_to_class.get(nick_val.lower(), "WR")
        else:
            class_val = ""

        if nick_val and auto_register_new and nick_val.lower() not in known_nicks:
            cls_to_save = class_val if class_val in PW_CLASSES else "WR"
            note_to_save = f"Piloto/Líder: {pilot_val}" if pilot_val else f"Escalado na {pt_name} ({pt_side})"
            try:
                db_execute(
                    "INSERT INTO tw_extra_players (nick, player_class, pilot_note, created_at) VALUES (?, ?, ?, ?)",
                    (nick_val, cls_to_save, note_to_save, now_str)
                )
                known_nicks.add(nick_val.lower())
                new_nicks_created += 1
            except Exception:
                pass

        existing = db_execute(
            "SELECT id FROM tw_party_slots WHERE pt_id = ? AND slot_number = ?",
            (pt_id, slot_num), fetchone=True
        )
        if existing:
            db_execute(
                "UPDATE tw_party_slots SET player_nick = ?, pilot_nick = ?, player_class = ? WHERE id = ?",
                (nick_val, pilot_val, class_val, existing["id"])
            )
        else:
            db_execute(
                "INSERT INTO tw_party_slots (pt_id, slot_number, player_nick, pilot_nick, player_class) VALUES (?, ?, ?, ?, ?)",
                (pt_id, slot_num, nick_val, pilot_val, class_val)
            )

    msg = f"PT '{pt_name}' ({pt_side}) atualizada com sucesso!"
    if new_nicks_created > 0:
        msg += f" (+{new_nicks_created} novo(s) nick(s) incluído(s) na base)."
    flash(msg, "success")
    return redirect(request.referrer or url_for("dashboard", tab="tw"))

@app.route("/tw/save_matrix", methods=["POST"])
@admin_required
def save_tw_matrix():
    """
    Salva de uma só vez todas as PTs editadas lado a lado na Visão Planilha Completa (Ataque e Defesa).
    """
    tw_parties = db_execute("SELECT id, pt_name, pt_side FROM tw_parties", fetch=True)
    nick_to_class, known_nicks = build_nick_lookup_maps()
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    new_nicks_created = 0

    for pt in tw_parties:
        pid = pt["id"]
        new_name = request.form.get(f"pt_name_{pid}", "").strip() or pt["pt_name"]
        new_func = request.form.get(f"pt_func_{pid}", "").strip()
        new_side = request.form.get(f"pt_side_{pid}", pt.get("pt_side") or "ATAQUE").strip().upper()
        if new_side not in ("ATAQUE", "DEFESA"):
            new_side = "ATAQUE"

        db_execute(
            "UPDATE tw_parties SET pt_name = ?, pt_function = ?, pt_side = ? WHERE id = ?",
            (new_name, new_func, new_side, pid)
        )

        for slot_num in range(1, 11):
            nick_val = request.form.get(f"nick_{pid}_{slot_num}", "").strip()
            pilot_val = request.form.get(f"pilot_{pid}_{slot_num}", "").strip()
            raw_cls = request.form.get(f"class_{pid}_{slot_num}", "").strip().upper()

            if nick_val:
                if raw_cls:
                    class_val = normalize_pw_class(raw_cls)
                else:
                    class_val = nick_to_class.get(nick_val.lower(), "WR")
            else:
                class_val = ""

            if nick_val and nick_val.lower() not in known_nicks:
                cls_to_save = class_val if class_val in PW_CLASSES else "WR"
                note_to_save = f"Piloto/Líder: {pilot_val}" if pilot_val else f"Planilha {new_name} ({new_side})"
                try:
                    db_execute(
                        "INSERT INTO tw_extra_players (nick, player_class, pilot_note, created_at) VALUES (?, ?, ?, ?)",
                        (nick_val, cls_to_save, note_to_save, now_str)
                    )
                    known_nicks.add(nick_val.lower())
                    new_nicks_created += 1
                except Exception:
                    pass

            existing = db_execute(
                "SELECT id FROM tw_party_slots WHERE pt_id = ? AND slot_number = ?",
                (pid, slot_num), fetchone=True
            )
            if existing:
                db_execute(
                    "UPDATE tw_party_slots SET player_nick = ?, pilot_nick = ?, player_class = ? WHERE id = ?",
                    (nick_val, pilot_val, class_val, existing["id"])
                )
            else:
                db_execute(
                    "INSERT INTO tw_party_slots (pt_id, slot_number, player_nick, pilot_nick, player_class) VALUES (?, ?, ?, ?, ?)",
                    (pid, slot_num, nick_val, pilot_val, class_val)
                )

    msg = "✅ Planilha Completa de TW (Ataque & Defesa) salva com sucesso!"
    if new_nicks_created > 0:
        msg += f" ({new_nicks_created} novo(s) nick(s) cadastrado(s) automaticamente)."
    flash(msg, "success")
    return redirect(url_for("dashboard", tab="tw", tw_view="planilha"))

@app.route("/tw/<int:pt_id>/delete", methods=["POST"])
@admin_required
def delete_tw_pt(pt_id):
    db_execute("DELETE FROM tw_party_slots WHERE pt_id = ?", (pt_id,))
    db_execute("DELETE FROM tw_parties WHERE id = ?", (pt_id,))
    flash("PT removida da Planilha de TW.", "info")
    return redirect(request.referrer or url_for("dashboard", tab="tw"))

@app.route("/tw/export_csv")
@login_required
def export_tw_csv():
    tw_parties = db_execute("SELECT * FROM tw_parties ORDER BY id ASC", fetch=True)
    all_tw_slots = db_execute("SELECT * FROM tw_party_slots ORDER BY pt_id ASC, slot_number ASC", fetch=True)
    slots_by_pt = {}
    for s in all_tw_slots:
        slots_by_pt.setdefault(s["pt_id"], {})[int(s["slot_number"])] = s

    attack_pts = [p for p in tw_parties if (p.get("pt_side") or "ATAQUE").upper() == "ATAQUE"]
    defense_pts = [p for p in tw_parties if (p.get("pt_side") or "ATAQUE").upper() == "DEFESA"]

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")

    def write_side_block(side_title, pts_list):
        writer.writerow([f"=== BLOCO: {side_title} ==="])
        if not pts_list:
            writer.writerow(["Nenhuma PT cadastrada neste bloco."])
            writer.writerow([])
            return

        header_names = []
        sub_headers = []
        for p in pts_list:
            label = f"{p['pt_name']} ({p['pt_function']})" if p.get("pt_function") else p["pt_name"]
            header_names.extend([label, "", ""])
            sub_headers.extend(["CLASSE", "NICK", "PILOTO/LÍDER"])
        header_names.extend(["", f"QUANTIDADE {side_title}", ""])
        sub_headers.extend(["", "CLASSE", "QUANTIDADE"])

        writer.writerow(header_names)
        writer.writerow(sub_headers)

        side_cls_counts = {c: 0 for c in PW_CLASSES}
        pt_totals = [0] * len(pts_list)

        for idx, p in enumerate(pts_list):
            for s_num in range(1, 11):
                s = slots_by_pt.get(p["id"], {}).get(s_num, {})
                nk = (s.get("player_nick") or "").strip()
                cl = normalize_pw_class(s.get("player_class")) if nk and s.get("player_class") else ""
                if nk:
                    pt_totals[idx] += 1
                    if cl in side_cls_counts:
                        side_cls_counts[cl] += 1

        for row_idx in range(10):
            slot_num = row_idx + 1
            row_cells = []
            for p in pts_list:
                s = slots_by_pt.get(p["id"], {}).get(slot_num, {})
                row_cells.extend([
                    s.get("player_class") or "",
                    s.get("player_nick") or "",
                    s.get("pilot_nick") or ""
                ])
            cls_name = PW_CLASSES[row_idx]
            row_cells.extend(["", cls_name, side_cls_counts.get(cls_name, 0)])
            writer.writerow(row_cells)

        # Linha de Subtotal por PT e Total do Bloco
        footer_cells = []
        for tot in pt_totals:
            footer_cells.extend(["TOTAL PT:", tot, ""])
        footer_cells.extend(["", f"TOTAL {side_title}", sum(side_cls_counts.values())])
        writer.writerow(footer_cells)
        writer.writerow([])

    writer.writerow(["CONTROLE DE TW - PLANILHA OFICIAL (ATAQUE & DEFESA)"])
    writer.writerow([])
    write_side_block("ATAQUE", attack_pts)
    write_side_block("DEFESA", defense_pts)

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=Controle_TW_Ataque_Defesa.csv"}
    )

# ==========================================
# ROTAS DE APROVAÇÃO DE PLAYERS E RESET DE SENHA (ADMIN)
# ==========================================
@app.route("/users/<int:user_id>/approve_account", methods=["POST"])
@admin_required
def approve_user_account(user_id):
    u = db_execute("SELECT * FROM users WHERE id = ?", (user_id,), fetchone=True)
    if u:
        db_execute("UPDATE users SET account_status = 'ativo' WHERE id = ?", (user_id,))
        create_notification(user_id, "✅ Sua conta foi aprovada pelo Administrador! Bem-vindo ao sistema do clã.")
        flash(f"Cadastro do jogador '{u['username']}' (TS3: {u['ts_nick']} | Classe: {u.get('player_class', 'WR')}) APROVADO!", "success")
    return redirect(request.referrer or url_for("dashboard", tab="players"))

@app.route("/users/<int:user_id>/approve_reset", methods=["POST"])
@admin_required
def approve_password_reset(user_id):
    u = db_execute("SELECT * FROM users WHERE id = ?", (user_id,), fetchone=True)
    if u:
        db_execute("UPDATE users SET reset_status = 'aprovado' WHERE id = ?", (user_id,))
        flash(
            f"🔓 Reset de senha LIBERADO para '{u['username']}'! "
            f"Assim que ele digitar o login '{u['username']}' na tela inicial, o campo de criar nova senha abrirá automaticamente.",
            "success"
        )
    return redirect(request.referrer or url_for("dashboard", tab="players"))

@app.route("/users/<int:user_id>/cancel_reset", methods=["POST"])
@admin_required
def cancel_password_reset(user_id):
    db_execute("UPDATE users SET reset_status = 'nenhum' WHERE id = ?", (user_id,))
    flash("Solicitação de reset de senha cancelada.", "info")
    return redirect(request.referrer or url_for("dashboard", tab="players"))

@app.route("/users/<int:user_id>/update_class", methods=["POST"])
@admin_required
def update_user_class(user_id):
    new_cls = normalize_pw_class(request.form.get("player_class", "WR"))
    db_execute("UPDATE users SET player_class = ? WHERE id = ?", (new_cls, user_id))
    flash(f"Classe atualizada para {new_cls}!", "success")
    return redirect(request.referrer or url_for("dashboard", tab="players"))

# ==========================================
# ROTAS DA ABA 1: ESTOQUE (CADASTRO, LISTA SUSPENSA E ENTRADAS POR DATA)
# ==========================================
@app.route("/inventory/create_new", methods=["POST"])
@admin_required
def create_new_inventory_item():
    item_name = request.form.get("item_name", "").strip()
    rarity = request.form.get("rarity", "Raro").strip()
    tkp_value = int(request.form.get("tkp_value") or 1)
    qty = int(request.form.get("quantity") or 1)
    boss_source = request.form.get("boss_source", "").strip()
    arrival_input = request.form.get("arrival_date", "").strip()

    if arrival_input:
        try:
            dt_obj = datetime.strptime(arrival_input, "%Y-%m-%dT%H:%M")
            arrival_str = dt_obj.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            arrival_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    else:
        arrival_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    if not item_name or qty <= 0:
        flash("Informe o nome do item e uma quantidade inicial válida.", "warning")
        return redirect(url_for("dashboard", tab="estoque"))

    existing = db_execute(
        "SELECT * FROM inventory_items WHERE LOWER(item_name) = LOWER(?)",
        (item_name,), fetchone=True
    )

    if existing:
        item_id = existing["id"]
        new_qty = int(existing["stock_qty"]) + qty
        db_execute("""
            UPDATE inventory_items
            SET stock_qty = ?, rarity = ?, tkp_value = ?, boss_source = ?, updated_at = ?
            WHERE id = ?
        """, (new_qty, rarity, tkp_value, boss_source or existing["boss_source"], arrival_str, item_id))
        item_real_name = existing["item_name"]
    else:
        db_execute("""
            INSERT INTO inventory_items (item_name, rarity, tkp_value, stock_qty, boss_source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (item_name, rarity, tkp_value, qty, boss_source, arrival_str))
        created = db_execute("SELECT * FROM inventory_items WHERE LOWER(item_name) = LOWER(?)", (item_name,), fetchone=True)
        item_id = created["id"]
        item_real_name = created["item_name"]

    db_execute("""
        INSERT INTO inventory_entries (item_id, item_name, rarity, tkp_value, qty_added, boss_source, arrival_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (item_id, item_real_name, rarity, tkp_value, qty, boss_source, arrival_str))

    flash(f"Item '{item_real_name}' ({tkp_value} TKP) registrado com +{qty} un. na data {arrival_str} e incluído na Lista Suspensa!", "success")
    return redirect(url_for("dashboard", tab="estoque"))

@app.route("/inventory/add_from_dropdown", methods=["POST"])
@admin_required
def add_stock_from_dropdown():
    item_id = int(request.form.get("item_id") or 0)
    qty = int(request.form.get("quantity") or 1)
    boss_source = request.form.get("boss_source", "").strip()
    custom_tkp = request.form.get("tkp_value", "").strip()
    arrival_input = request.form.get("arrival_date", "").strip()

    item = db_execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,), fetchone=True)
    if not item or qty <= 0:
        flash("Selecione um item válido na lista suspensa e informe a quantidade.", "warning")
        return redirect(url_for("dashboard", tab="estoque"))

    if arrival_input:
        try:
            dt_obj = datetime.strptime(arrival_input, "%Y-%m-%dT%H:%M")
            arrival_str = dt_obj.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            arrival_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    else:
        arrival_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    tkp_val = int(custom_tkp) if custom_tkp.isdigit() else int(item.get("tkp_value") or 1)
    new_total_qty = int(item["stock_qty"]) + qty
    final_boss = boss_source if boss_source else (item.get("boss_source") or "")

    db_execute("""
        UPDATE inventory_items
        SET stock_qty = ?, tkp_value = ?, boss_source = ?, updated_at = ?
        WHERE id = ?
    """, (new_total_qty, tkp_val, final_boss, arrival_str, item_id))

    db_execute("""
        INSERT INTO inventory_entries (item_id, item_name, rarity, tkp_value, qty_added, boss_source, arrival_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (item_id, item["item_name"], item["rarity"], tkp_val, qty, final_boss, arrival_str))

    flash(
        f"Entrada registrada em {arrival_str}: +{qty} un. de '{item['item_name']}' ({tkp_val} TKP). "
        f"Saldo acumulado para saque: {new_total_qty} un.!",
        "success"
    )
    return redirect(url_for("dashboard", tab="estoque"))

@app.route("/inventory/entry/<int:entry_id>/delete", methods=["POST"])
@admin_required
def delete_stock_entry(entry_id):
    entry = db_execute("SELECT * FROM inventory_entries WHERE id = ?", (entry_id,), fetchone=True)
    if entry:
        item = db_execute("SELECT * FROM inventory_items WHERE id = ?", (entry["item_id"],), fetchone=True)
        if item:
            new_qty = max(0, int(item["stock_qty"]) - int(entry["qty_added"]))
            db_execute("UPDATE inventory_items SET stock_qty = ? WHERE id = ?", (new_qty, item["id"]))
        db_execute("DELETE FROM inventory_entries WHERE id = ?", (entry_id,))
        flash(f"Entrada de {entry['qty_added']}x '{entry['item_name']}' ({entry['arrival_date']}) estornada do estoque.", "info")
    return redirect(url_for("dashboard", tab="estoque"))

@app.route("/inventory/item/<int:item_id>/update_tkp", methods=["POST"])
@admin_required
def update_item_tkp(item_id):
    new_tkp = int(request.form.get("tkp_value") or 0)
    db_execute("UPDATE inventory_items SET tkp_value = ? WHERE id = ?", (new_tkp, item_id))
    flash("Valor em TKP do item atualizado!", "success")
    return redirect(url_for("dashboard", tab="saque"))

@app.route("/inventory/item/<int:item_id>/delete_catalog", methods=["POST"])
@admin_required
def delete_catalog_item(item_id):
    db_execute("DELETE FROM inventory_entries WHERE item_id = ?", (item_id,))
    db_execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
    flash("Item e seu histórico de entradas foram removidos.", "info")
    return redirect(url_for("dashboard", tab="saque"))

# ==========================================
# ROTAS DA ABA 2: SAQUES & LANCES EM TKP
# ==========================================
@app.route("/auctions/start", methods=["POST"])
@admin_required
def start_auction():
    item_id = int(request.form.get("item_id") or 0)
    qty_to_auction = int(request.form.get("qty") or 1)
    min_bid = int(request.form.get("min_bid") or 0)
    duration_minutes = int(request.form.get("duration_minutes") or 10)

    item = db_execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,), fetchone=True)
    if not item or int(item["stock_qty"]) < qty_to_auction or qty_to_auction <= 0:
        flash("Quantidade insuficiente disponível para liberar este saque.", "danger")
        return redirect(url_for("dashboard", tab="saque"))

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

    flash(f"Saque/Lance liberado para '{item['item_name']} (x{qty_to_auction})' valendo a partir de {min_bid} TKP! Tempo: {duration_minutes} min.", "success")
    return redirect(url_for("dashboard", tab="saque"))

@app.route("/auctions/<int:auction_id>/bid", methods=["POST"])
@login_required
def place_bid(auction_id):
    resolve_expired_auctions()

    auc = db_execute("SELECT * FROM auctions WHERE id = ?", (auction_id,), fetchone=True)
    if not auc or auc["status"] not in ("Aberto", "Em Desempate"):
        flash("Este lance já foi encerrado.", "warning")
        return redirect(url_for("dashboard", tab="saque"))

    uid = session["user_id"]
    if auc["status"] == "Em Desempate":
        allowed = [x.strip() for x in (auc.get("tied_user_ids") or "").split(",") if x.strip()]
        if str(uid) not in allowed:
            flash("Apenas os jogadores que empataram podem dar lances nesta rodada de desempate de 6 minutos!", "danger")
            return redirect(url_for("dashboard", tab="saque"))

    try:
        bid_amount = int(request.form.get("bid_amount") or 0)
    except ValueError:
        flash("Valor de lance em TKP inválido.", "danger")
        return redirect(url_for("dashboard", tab="saque"))

    min_allowed = int(auc["min_bid"])
    if auc["status"] == "Em Desempate" and bid_amount <= min_allowed:
        flash(f"No desempate, o seu lance precisa ser maior que o valor empatado ({min_allowed} TKP)!", "warning")
        return redirect(url_for("dashboard", tab="saque"))
    elif bid_amount < min_allowed:
        flash(f"O lance mínimo inicial é {min_allowed} TKP.", "warning")
        return redirect(url_for("dashboard", tab="saque"))

    wallets = calculate_wallets(exclude_auction_id=auction_id)
    user_wallet = wallets.get(uid, {"available_to_bid": 0, "tkp_balance": 0})
    max_can_bid = int(user_wallet["available_to_bid"])

    if bid_amount > max_can_bid:
        flash(
            f"Saldo TKP insuficiente! Você tentou dar um lance de {bid_amount} TKP, "
            f"mas seu saldo livre disponível no momento é de {max_can_bid} TKP.",
            "danger"
        )
        return redirect(url_for("dashboard", tab="saque"))

    rnd = int(auc["round_number"] or 1)
    now_str = datetime.now().strftime("%H:%M:%S")

    existing_bid = db_execute("""
        SELECT * FROM auction_bids
        WHERE auction_id = ? AND round_number = ? AND user_id = ?
    """, (auction_id, rnd, uid), fetchone=True)

    if existing_bid:
        if bid_amount <= int(existing_bid["bid_amount"]):
            flash(f"Você já tem um lance de {existing_bid['bid_amount']} TKP nesta rodada. Digite um valor maior.", "warning")
            return redirect(url_for("dashboard", tab="saque"))
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

    flash(f"Lance de {bid_amount} TKP registrado com sucesso em '{auc['item_name']}'!", "success")
    return redirect(url_for("dashboard", tab="saque"))

@app.route("/auctions/<int:auction_id>/force_end", methods=["POST"])
@admin_required
def force_end_auction(auction_id):
    resolve_expired_auctions(force_auction_id=auction_id)
    flash("Tempo do lance encerrado e apurado pelo Admin!", "info")
    return redirect(url_for("dashboard", tab="saque"))

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
                f"✅ Entrega Confirmada (OK): O item '{auc['item_name']} (x{auc['qty']})' arrematado por {auc['winning_bid']} TKP foi entregue in-game pelo Admin!"
            )
        flash(f"Entrega do item '{auc['item_name']}' confirmada com OK!", "success")
    return redirect(url_for("dashboard", tab="saque"))

@app.route("/auctions/<int:auction_id>/delete", methods=["POST"])
@admin_required
def delete_auction(auction_id):
    auc = db_execute("SELECT * FROM auctions WHERE id = ?", (auction_id,), fetchone=True)
    if auc and auc["status"] in ("Aberto", "Em Desempate"):
        db_execute("UPDATE inventory_items SET stock_qty = stock_qty + ? WHERE id = ?", (auc["qty"], auc["item_id"]))
    db_execute("DELETE FROM auction_bids WHERE auction_id = ?", (auction_id,))
    db_execute("DELETE FROM auctions WHERE id = ?", (auction_id,))
    flash("Registro de lance removido.", "info")
    return redirect(url_for("dashboard", tab="saque"))

# ==========================================
# ROTAS DE EVENTOS, PLAYERS E CHAMADA NEOTS (1 PRESENÇA = +1 TKP)
# ==========================================
@app.route("/events/create", methods=["POST"])
@admin_required
def create_event():
    title = request.form.get("title", "").strip()
    event_type = request.form.get("event_type", "GVG / TW").strip()
    if title:
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        db_execute(
            "INSERT INTO events (title, event_type, status, created_at) VALUES (?, ?, 'Aberto', ?)",
            (title, event_type, now_str)
        )
        flash(f"Evento '{title}' criado! Escolha o filtro de salas (GVG/TW ou Todas) e puxe a presença.", "success")
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
    filter_mode = request.form.get("filter_mode", "all").strip()
    cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
    ok, result = fetch_neots_api_clients(cfg, filter_mode=filter_mode)
    if not ok:
        flash(result, "danger")
        return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

    online_clients = result["online"]
    afk_list = result["afk"]
    filtered_out = result["filtered_out"]
    users = db_execute("SELECT * FROM users WHERE account_status = 'ativo' OR account_status IS NULL", fetch=True)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    matched_count = 0
    new_tkp_awarded = 0
    for u in users:
        match = match_player_nick(u["ts_nick"], online_clients)
        if match:
            matched_count += 1
            found_nick = match["nick"]
            ch_path = match["channel_path"]
            method_label = f"Sala: {ch_path}"[:115]
            existing = db_execute(
                "SELECT id FROM attendance WHERE event_id = ? AND user_id = ?",
                (event_id, u["id"]), fetchone=True
            )
            if existing:
                db_execute(
                    "UPDATE attendance SET ts_nick_found = ?, method = ?, checked_at = ? WHERE id = ?",
                    (found_nick, method_label, now_str, existing["id"])
                )
            else:
                new_tkp_awarded += 1
                db_execute(
                    "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, ?, ?)",
                    (event_id, u["id"], found_nick, method_label, now_str)
                )
                create_notification(u["id"], f"💰 Você ganhou +1 TKP por presença no TS3 ({ch_path})!")

    db_execute("UPDATE events SET last_sync = ? WHERE id = ?", (now_str, event_id))
    msg = (
        f"Leitura NeoTS concluída! {len(online_clients)} jogador(es) nas salas válidas | "
        f"{matched_count} cadastrado(s) presente(s) ({new_tkp_awarded} ganharam +1 TKP)."
    )
    if afk_list:
        msg += f" [{len(afk_list)} ignorado(s) em AFK/Ausente]."
    if filtered_out:
        msg += f" [{len(filtered_out)} fora do filtro {filter_mode.upper()}]."
    flash(msg, "success")
    return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

@app.route("/events/<int:event_id>/manual_paste", methods=["POST"])
@admin_required
def manual_paste_ts3(event_id):
    raw_text = request.form.get("raw_nicks", "").strip()
    filter_mode = request.form.get("filter_mode", "all").strip()
    cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
    ignore_raw = (cfg.get("ignore_channels") or "AFK,Ausente").split(",")
    ignore_keywords = [k.strip().lower() for k in ignore_raw if k.strip()]

    online_clients = []
    if raw_text.startswith("{") and '"clients"' in raw_text:
        try:
            data = json.loads(raw_text)
            ok, parsed = parse_neots_json_payload(data, ignore_keywords, filter_mode=filter_mode)
            if ok:
                online_clients = parsed["online"]
        except Exception as e:
            flash(f"Erro ao ler o JSON colado: {str(e)}", "danger")
            return redirect(url_for("dashboard", tab="eventos", event_id=event_id))
    else:
        lines = [l.strip() for l in raw_text.replace(",", "\n").splitlines() if l.strip()]
        online_clients = [{"nick": l, "channel_path": "Lista Manual"} for l in lines]

    users = db_execute("SELECT * FROM users WHERE account_status = 'ativo' OR account_status IS NULL", fetch=True)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    matched_count = 0
    new_tkp = 0
    for u in users:
        match = match_player_nick(u["ts_nick"], online_clients)
        if match:
            matched_count += 1
            found_nick = match["nick"]
            ch_path = match["channel_path"]
            method_label = f"Sala: {ch_path}"[:115]
            existing = db_execute(
                "SELECT id FROM attendance WHERE event_id = ? AND user_id = ?",
                (event_id, u["id"]), fetchone=True
            )
            if existing:
                db_execute(
                    "UPDATE attendance SET ts_nick_found = ?, method = ?, checked_at = ? WHERE id = ?",
                    (found_nick, method_label, now_str, existing["id"])
                )
            else:
                new_tkp += 1
                db_execute(
                    "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, ?, ?)",
                    (event_id, u["id"], found_nick, method_label, now_str)
                )
                create_notification(u["id"], f"💰 Você ganhou +1 TKP por presença registrada no TS3 ({ch_path})!")

    db_execute("UPDATE events SET last_sync = ? WHERE id = ?", (now_str, event_id))
    flash(f"{matched_count} presença(s) contabilizada(s) ({new_tkp} novas receberam +1 TKP)!", "success")
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
        flash("Presença removida (-1 TKP estornado).", "info")
    else:
        u = db_execute("SELECT ts_nick FROM users WHERE id = ?", (user_id,), fetchone=True)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        db_execute(
            "INSERT INTO attendance (event_id, user_id, ts_nick_found, method, checked_at) VALUES (?, ?, ?, 'Manual Admin', ?)",
            (event_id, user_id, u["ts_nick"] if u else "Manual", now_str)
        )
        create_notification(user_id, "💰 Você ganhou +1 TKP por presença confirmada pelo Admin!")
        flash("Presença adicionada (+1 TKP)!", "success")
    return redirect(url_for("dashboard", tab="eventos", event_id=event_id))

@app.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    ts_nick = request.form.get("ts_nick", "").strip()
    player_class = normalize_pw_class(request.form.get("player_class", "WR"))
    role = request.form.get("role", "player").strip()
    initial_tkp = int(request.form.get("bonus_tkp") or 0)

    if not username or not password or not ts_nick:
        flash("Preencha Login, Senha, Classe e Nick do TS3.", "warning")
        return redirect(url_for("dashboard", tab="players"))

    exists = db_execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,), fetchone=True)
    if exists:
        flash(f"O login '{username}' já está cadastrado.", "danger")
        return redirect(url_for("dashboard", tab="players"))

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    db_execute("""
        INSERT INTO users (username, password_hash, ts_nick, player_class, role, bonus_tkp, account_status, reset_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'ativo', 'nenhum', ?)
    """, (username, generate_password_hash(password), ts_nick, player_class, role, initial_tkp, now_str))
    flash(f"Jogador '{username}' (Nick TS3: {ts_nick} | Classe: {player_class}) criado e já ativo!", "success")
    return redirect(url_for("dashboard", tab="players"))

@app.route("/users/<int:user_id>/adjust_tkp", methods=["POST"])
@admin_required
def adjust_user_tkp(user_id):
    delta = int(request.form.get("delta") or 0)
    u = db_execute("SELECT * FROM users WHERE id = ?", (user_id,), fetchone=True)
    if u and delta != 0:
        new_bonus = int(u.get("bonus_tkp") or 0) + delta
        db_execute("UPDATE users SET bonus_tkp = ? WHERE id = ?", (new_bonus, user_id))
        sinal = f"+{delta}" if delta > 0 else str(delta)
        create_notification(user_id, f"⚖️ O Admin realizou um ajuste manual de {sinal} TKP na sua carteira.")
        flash(f"Saldo de '{u['username']}' ajustado em {sinal} TKP.", "success")
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
    flash("Registro do jogador removido do sistema.", "info")
    return redirect(request.referrer or url_for("dashboard", tab="players"))

@app.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    new_ts_nick = request.form.get("ts_nick", "").strip()
    new_class = normalize_pw_class(request.form.get("player_class", ""))
    new_pass = request.form.get("new_password", "").strip()
    uid = session["user_id"]

    if new_ts_nick:
        db_execute("UPDATE users SET ts_nick = ? WHERE id = ?", (new_ts_nick, uid))
        session["ts_nick"] = new_ts_nick
    if new_class in PW_CLASSES:
        db_execute("UPDATE users SET player_class = ? WHERE id = ?", (new_class, uid))
        session["player_class"] = new_class
    if new_pass:
        db_execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_pass), uid))

    flash("Seus dados foram atualizados com sucesso!", "success")
    return redirect(url_for("dashboard", tab="perfil"))

@app.route("/tsconfig/save", methods=["POST"])
@admin_required
def save_tsconfig():
    neots_api_url = request.form.get("neots_api_url", "").strip() or DEFAULT_NEOTS_API_URL
    query_user = request.form.get("query_user", "").strip()
    query_pass = request.form.get("query_pass", "").strip()
    neots_cookie = request.form.get("neots_cookie", "").strip()
    ignore_channels = request.form.get("ignore_channels", "").strip()
    host = request.form.get("host", "157.151.9.204").strip()
    voice_port = int(request.form.get("voice_port") or 9971)

    db_execute("""
        UPDATE ts_config
        SET neots_api_url = ?, query_user = ?, query_pass = ?, neots_cookie = ?,
            ignore_channels = ?, host = ?, voice_port = ?
        WHERE id = 1
    """, (neots_api_url, query_user, query_pass, neots_cookie, ignore_channels, host, voice_port))

    action = request.form.get("action", "save")
    if action == "test":
        cfg = db_execute("SELECT * FROM ts_config WHERE id = 1", fetchone=True)
        ok, res = fetch_neots_api_clients(cfg, filter_mode="all")
        if ok:
            online_list = [f"{c['nick']} [{c['channel_path']}]" for c in res["online"]]
            nicks_str = ", ".join(online_list) if online_list else "Nenhum jogador nas salas válidas agora"
            flash(f"API NeoTS Conectada com Sucesso! Online ({len(online_list)}): {nicks_str}", "success")
        else:
            flash(res, "danger")
    else:
        flash("Configurações da API NEO TEAMSPEAK salvas com sucesso!", "success")

    return redirect(url_for("dashboard", tab="tsconfig"))

# ==========================================
# TEMPLATES HTML EMBUTIDOS (COMPLETOS)
# ==========================================
LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Acesso - TS3 Guild Tracker</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
    <style>
        body {
            background: radial-gradient(circle at top, #1a2238 0%, #0b0f19 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }
        .login-card {
            background: #131926;
            border: 1px solid #2a3550;
            border-radius: 14px;
            box-shadow: 0 15px 35px rgba(0,0,0,0.6);
            width: 100%;
            max-width: 450px;
            padding: 2rem;
        }
        .brand-icon {
            width: 60px;
            height: 60px;
            border-radius: 16px;
            background: linear-gradient(135deg, #3b82f6, #1d4ed8);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.7rem;
            margin: 0 auto 0.8rem;
            color: #fff;
        }
        .nav-pills .nav-link {
            color: #94a3b8;
            font-size: 0.85rem;
            font-weight: 600;
            border-radius: 8px;
        }
        .nav-pills .nav-link.active {
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
            color: #fff;
        }
        .reset-unlocked-box {
            border: 1px solid #10b981;
            background: rgba(16, 185, 129, 0.1);
            border-radius: 10px;
            padding: 12px;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand-icon"><i class="fa-solid fa-headset"></i></div>
        <h4 class="text-center fw-bold mb-1">TS3 Guild Tracker</h4>
        <p class="text-center text-secondary small mb-3">Presença TS3, Saque TKP & Controle de TW</p>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for cat, msg in messages %}
              <div class="alert alert-{{ cat }} py-2 small">{{ msg }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        <ul class="nav nav-pills nav-fill gap-1 mb-4 bg-dark p-1 rounded-3 border border-secondary" role="tablist">
            <li class="nav-item" role="presentation">
                <button class="nav-link active py-1" id="tab-login-btn" data-bs-toggle="pill" data-bs-target="#pane-login" type="button" role="tab">
                    <i class="fa-solid fa-right-to-bracket me-1"></i> Entrar
                </button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link py-1" id="tab-register-btn" data-bs-toggle="pill" data-bs-target="#pane-register" type="button" role="tab">
                    <i class="fa-solid fa-user-plus me-1"></i> Criar Conta
                </button>
            </li>
            <li class="nav-item" role="presentation">
                <button class="nav-link py-1" id="tab-forgot-btn" data-bs-toggle="pill" data-bs-target="#pane-forgot" type="button" role="tab">
                    <i class="fa-solid fa-key me-1"></i> Reset Senha
                </button>
            </li>
        </ul>

        <div class="tab-content">
            <!-- ABA 1: LOGIN -->
            <div class="tab-pane fade show active" id="pane-login" role="tabpanel">
                <form method="POST" action="{{ url_for('login') }}">
                    <div class="mb-3">
                        <label class="form-label small text-secondary">Login do Player / Admin</label>
                        <div class="input-group">
                            <span class="input-group-text bg-dark border-secondary"><i class="fa-solid fa-user"></i></span>
                            <input type="text" name="username" id="loginUsernameInput"
                                   value="{{ force_reset_username }}"
                                   class="form-control bg-dark text-light border-secondary"
                                   placeholder="Digite seu login..." required autofocus
                                   oninput="scheduleUserCheck(this.value)"
                                   onblur="checkUserStatusNow(this.value)">
                        </div>
                        <div id="userStatusHint" class="small mt-1" style="display:none;"></div>
                    </div>

                    <div class="mb-4" id="normalPasswordGroup" {% if force_reset_username %}style="display:none;"{% endif %}>
                        <label class="form-label small text-secondary">Senha</label>
                        <div class="input-group">
                            <span class="input-group-text bg-dark border-secondary"><i class="fa-solid fa-lock"></i></span>
                            <input type="password" name="password" id="normalPasswordInput"
                                   class="form-control bg-dark text-light border-secondary"
                                   {% if not force_reset_username %}required{% endif %}>
                        </div>
                    </div>

                    <div class="mb-4 reset-unlocked-box" id="approvedResetGroup" {% if not force_reset_username %}style="display:none;"{% endif %}>
                        <div class="small fw-bold text-success mb-2">
                            <i class="fa-solid fa-unlock-keyhole me-1"></i>
                            O Admin liberou a criação de uma nova senha para sua conta!
                        </div>
                        <div class="mb-2">
                            <label class="form-label small text-light mb-1">Digite sua Nova Senha</label>
                            <input type="password" name="new_password" id="newPasswordInput"
                                   class="form-control form-control-sm bg-dark text-warning border-success"
                                   placeholder="Nova senha..."
                                   {% if force_reset_username %}required{% endif %}>
                        </div>
                        <div>
                            <label class="form-label small text-light mb-1">Confirmar Nova Senha</label>
                            <input type="password" name="confirm_password" id="confirmPasswordInput"
                                   class="form-control form-control-sm bg-dark text-warning border-success"
                                   placeholder="Repita a nova senha...">
                        </div>
                    </div>

                    <button type="submit" id="loginSubmitBtn" class="btn {% if force_reset_username %}btn-success{% else %}btn-primary{% endif %} w-100 fw-semibold py-2">
                        {% if force_reset_username %}
                            <i class="fa-solid fa-check-double me-1"></i> Salvar Nova Senha e Entrar
                        {% else %}
                            <i class="fa-solid fa-right-to-bracket me-1"></i> Entrar no Sistema
                        {% endif %}
                    </button>
                </form>
            </div>

            <!-- ABA 2: AUTO-CADASTRO DO PLAYER -->
            <div class="tab-pane fade" id="pane-register" role="tabpanel">
                <form method="POST" action="{{ url_for('register_player') }}">
                    <div class="alert alert-info py-2 px-3 small mb-3">
                        <i class="fa-solid fa-user-check me-1"></i>
                        Crie sua conta escolhendo sua <strong>Classe</strong>. Seu acesso será liberado assim que o <strong>Admin aprovar</strong>.
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Escolha seu Login de Acesso</label>
                        <input type="text" name="username" class="form-control bg-dark text-light border-secondary" placeholder="Ex: sinister" required>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-8">
                            <label class="form-label small text-secondary mb-1">Seu Nick no TeamSpeak 3</label>
                            <input type="text" name="ts_nick" class="form-control bg-dark text-light border-secondary" placeholder="Ex: Sinister" required>
                        </div>
                        <div class="col-4">
                            <label class="form-label small text-warning fw-bold mb-1">Sua Classe</label>
                            <select name="player_class" class="form-select bg-dark text-warning fw-bold border-secondary" required>
                                {% for cls in pw_classes %}
                                <option value="{{ cls }}">{{ cls }}</option>
                                {% endfor %}
                            </select>
                        </div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Crie sua Senha</label>
                        <input type="password" name="password" class="form-control bg-dark text-light border-secondary" placeholder="Sua senha de acesso" required>
                    </div>
                    <button type="submit" class="btn btn-success w-100 fw-semibold py-2">
                        <i class="fa-solid fa-paper-plane me-1"></i> Solicitar Cadastro ao Admin
                    </button>
                </form>
            </div>

            <!-- ABA 3: ESQUECI MINHA SENHA -->
            <div class="tab-pane fade" id="pane-forgot" role="tabpanel">
                <form method="POST" action="{{ url_for('forgot_password_request') }}">
                    <div class="alert alert-warning py-2 px-3 small mb-3">
                        <i class="fa-solid fa-key me-1"></i>
                        Esqueceu a senha? Informe seu login abaixo para avisar o Admin. Assim que o Admin liberar, basta digitar seu login na aba <strong>Entrar</strong> que o campo de criar nova senha abrirá na hora!
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary">Qual é o seu Login?</label>
                        <input type="text" name="username" class="form-control bg-dark text-light border-secondary" placeholder="Digite seu login cadastrado..." required>
                    </div>
                    <button type="submit" class="btn btn-warning text-dark w-100 fw-bold py-2">
                        <i class="fa-solid fa-unlock me-1"></i> Pedir Liberação de Nova Senha
                    </button>
                </form>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let checkTimer = null;

        function scheduleUserCheck(val) {
            clearTimeout(checkTimer);
            checkTimer = setTimeout(() => checkUserStatusNow(val), 350);
        }

        async function checkUserStatusNow(username) {
            const clean = (username || "").trim();
            const normalGroup = document.getElementById('normalPasswordGroup');
            const normalInput = document.getElementById('normalPasswordInput');
            const resetGroup = document.getElementById('approvedResetGroup');
            const newPassInput = document.getElementById('newPasswordInput');
            const submitBtn = document.getElementById('loginSubmitBtn');
            const hint = document.getElementById('userStatusHint');

            if (!clean) {
                hint.style.display = 'none';
                return;
            }

            try {
                const resp = await fetch(`/api/check_user_login?username=${encodeURIComponent(clean)}`);
                const data = await resp.json();

                if (data.exists && data.account_status === 'pendente') {
                    hint.style.display = 'block';
                    hint.className = 'small mt-1 text-warning';
                    hint.innerHTML = '<i class="fa-solid fa-hourglass-half me-1"></i>Conta aguardando aprovação do Admin.';
                } else if (data.exists && data.reset_status === 'solicitado') {
                    hint.style.display = 'block';
                    hint.className = 'small mt-1 text-info';
                    hint.innerHTML = '<i class="fa-solid fa-clock me-1"></i>Pedido de reset enviado. Aguardando o Admin clicar em Liberar.';
                } else {
                    hint.style.display = 'none';
                }

                if (data.exists && data.reset_approved) {
                    normalGroup.style.display = 'none';
                    normalInput.removeAttribute('required');
                    resetGroup.style.display = 'block';
                    newPassInput.setAttribute('required', 'required');
                    submitBtn.className = 'btn btn-success w-100 fw-semibold py-2';
                    submitBtn.innerHTML = '<i class="fa-solid fa-check-double me-1"></i> Salvar Nova Senha e Entrar';
                } else {
                    resetGroup.style.display = 'none';
                    newPassInput.removeAttribute('required');
                    normalGroup.style.display = 'block';
                    normalInput.setAttribute('required', 'required');
                    submitBtn.className = 'btn btn-primary w-100 fw-semibold py-2';
                    submitBtn.innerHTML = '<i class="fa-solid fa-right-to-bracket me-1"></i> Entrar no Sistema';
                }
            } catch (e) {}
        }
    </script>
</body>
</html>
"""

MAIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>TS3 Guild Tracker - Estoque, Saque TKP & Controle de TW</title>
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
        .badge-tkp { background: linear-gradient(135deg, #f59e0b, #b45309); color: #fff; font-weight: 700; }
        .table-dark-custom th { background-color: #1a2234; color: #94a3b8; font-size: 0.78rem; text-transform: uppercase; }
        .auction-active-card { border: 1px solid #3b82f6; box-shadow: 0 0 20px rgba(59, 130, 246, 0.12); }
        .auction-tie-card { border: 2px solid #f59e0b; box-shadow: 0 0 22px rgba(245, 158, 11, 0.22); }
        .timer-box { font-family: monospace; font-size: 1.25rem; font-weight: 700; letter-spacing: 1px; }

        /* Badges de Classes PW */
        .cls-badge { font-weight: 700; min-width: 42px; display: inline-block; text-align: center; }
        .cls-WR { background-color: #b45309; color: #fff; }
        .cls-MG { background-color: #1d4ed8; color: #fff; }
        .cls-WF { background-color: #15803d; color: #fff; }
        .cls-WB { background-color: #854d0e; color: #fff; }
        .cls-EA { background-color: #0369a1; color: #fff; }
        .cls-EP { background-color: #0ea5e9; color: #000; }
        .cls-MC { background-color: #be123c; color: #fff; }
        .cls-PSY { background-color: #6d28d9; color: #fff; }
        .cls-MS { background-color: #10b981; color: #000; }
        .cls-SK { background-color: #dc2626; color: #fff; }

        .leader-slot-row {
            background: rgba(245, 158, 11, 0.14);
            border: 1px solid #f59e0b;
            border-radius: 8px;
            padding: 6px;
        }
        .member-slot-row {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid #263147;
            border-radius: 8px;
            padding: 5px;
        }

        /* Estilos da Visão Planilha Excel (CONTROLE DE TW) */
        .tw-excel-banner-attack {
            background: linear-gradient(90deg, #991b1b 0%, #dc2626 50%, #991b1b 100%);
            color: #fff;
            font-weight: 800;
            letter-spacing: 1px;
            text-transform: uppercase;
            padding: 8px 14px;
            border-radius: 8px 8px 0 0;
            border: 1px solid #ef4444;
        }
        .tw-excel-banner-defense {
            background: linear-gradient(90deg, #1e3a8a 0%, #2563eb 50%, #1e3a8a 100%);
            color: #fff;
            font-weight: 800;
            letter-spacing: 1px;
            text-transform: uppercase;
            padding: 8px 14px;
            border-radius: 8px 8px 0 0;
            border: 1px solid #3b82f6;
        }
        .tw-matrix-col {
            min-width: 225px;
            max-width: 245px;
            background: #0f1522;
            border: 1px solid #2a364f;
            border-radius: 8px;
            flex: 0 0 auto;
        }
        .tw-matrix-header-attack {
            background: #451a1c;
            border-bottom: 2px solid #ef4444;
            padding: 6px 8px;
            border-radius: 7px 7px 0 0;
        }
        .tw-matrix-header-defense {
            background: #172554;
            border-bottom: 2px solid #3b82f6;
            padding: 6px 8px;
            border-radius: 7px 7px 0 0;
        }
        .tw-matrix-row-leader {
            background: rgba(245, 158, 11, 0.16);
            border-bottom: 1px solid rgba(245, 158, 11, 0.4);
        }
        .tw-matrix-footer {
            background: #1a2234;
            border-top: 2px solid #334155;
            font-weight: 800;
            padding: 6px;
            text-align: center;
            border-radius: 0 0 7px 7px;
        }
        .tw-qty-sidebar {
            background: #111827;
            border: 1px solid #374151;
            border-radius: 8px;
        }
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
        <div class="d-flex flex-wrap align-items-center gap-2">
            <span class="small text-secondary">
                <i class="fa-solid fa-user-shield me-1 text-info"></i>
                <strong>{{ session['username'] }}</strong>
                <span class="badge cls-badge cls-{{ session.get('player_class', 'WR') }} ms-1">{{ session.get('player_class', 'WR') }}</span>
                <span class="badge bg-secondary ms-1">TS3: {{ session['ts_nick'] }}</span>
                <span class="badge badge-tkp ms-1" title="Saldo Total na Carteira (Livre para lance: {{ my_wallet.available_to_bid }} TKP)">
                    <i class="fa-solid fa-coins me-1"></i>Carteira: {{ my_wallet.tkp_balance }} TKP (Livre: {{ my_wallet.available_to_bid }})
                </span>
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

<div class="container-fluid px-3 px-lg-4 pb-5">

    <!-- CENTRAL DE APROVAÇÕES DO ADMIN -->
    {% if session['role'] == 'admin' and (pending_users or reset_requests) %}
    <div class="card border-info bg-info bg-opacity-10 p-3 mb-4">
        <h6 class="fw-bold text-info mb-2">
            <i class="fa-solid fa-user-check me-1"></i> Solicitações Pendentes de Players (Aprovação do Admin)
        </h6>

        {% if pending_users %}
        <div class="mb-2">
            <div class="small fw-bold text-light mb-1">Novos Cadastros Aguardando Aprovação ({{ pending_users|length }}):</div>
            <div class="d-flex flex-wrap gap-2">
                {% for pu in pending_users %}
                <div class="bg-dark border border-secondary rounded px-3 py-2 d-flex align-items-center gap-2">
                    <div>
                        <strong class="text-warning">{{ pu.username }}</strong>
                        <span class="badge cls-badge cls-{{ pu.player_class }} ms-1">{{ pu.player_class }}</span>
                        <span class="badge bg-secondary ms-1">TS3: {{ pu.ts_nick }}</span>
                        <small class="text-secondary d-block" style="font-size:0.7rem;">Solicitado em {{ pu.created_at }}</small>
                    </div>
                    <form method="POST" action="{{ url_for('approve_user_account', user_id=pu.id) }}" class="d-inline ms-2">
                        <button type="submit" class="btn btn-sm btn-success fw-bold py-0 px-2">
                            <i class="fa-solid fa-check me-1"></i> Aprovar
                        </button>
                    </form>
                    <form method="POST" action="{{ url_for('delete_user', user_id=pu.id) }}" class="d-inline" onsubmit="return confirm('Recusar e excluir este cadastro?');">
                        <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                            <i class="fa-solid fa-xmark"></i>
                        </button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}

        {% if reset_requests %}
        <div>
            <div class="small fw-bold text-warning mb-1">Players que Esqueceram a Senha e Pediram Liberação ({{ reset_requests|length }}):</div>
            <div class="d-flex flex-wrap gap-2">
                {% for ru in reset_requests %}
                <div class="bg-dark border border-warning rounded px-3 py-2 d-flex align-items-center gap-2">
                    <div>
                        <strong class="text-light">{{ ru.username }}</strong>
                        <span class="badge cls-badge cls-{{ ru.player_class }} ms-1">{{ ru.player_class }}</span>
                        <span class="badge bg-secondary ms-1">TS3: {{ ru.ts_nick }}</span>
                    </div>
                    <form method="POST" action="{{ url_for('approve_password_reset', user_id=ru.id) }}" class="d-inline ms-2">
                        <button type="submit" class="btn btn-sm btn-warning text-dark fw-bold py-0 px-2">
                            <i class="fa-solid fa-unlock me-1"></i> Aprovar Criar Nova Senha
                        </button>
                    </form>
                    <form method="POST" action="{{ url_for('cancel_password_reset', user_id=ru.id) }}" class="d-inline">
                        <button type="submit" class="btn btn-sm btn-outline-secondary py-0 px-2">Ignorar</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}
    </div>
    {% endif %}

    <!-- PAINEL DE NOTIFICAÇÕES -->
    {% if my_notifications %}
    <div class="card border-warning bg-warning bg-opacity-10 p-3 mb-4">
        <div class="d-flex justify-content-between align-items-center mb-2">
            <h6 class="fw-bold text-warning mb-0">
                <i class="fa-solid fa-bell fa-shake me-1"></i> Suas Notificações de Presença, Saques & Lances (TKP)
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

    <!-- NAVEGAÇÃO PRINCIPAL POR ABAS -->
    <ul class="nav nav-pills flex-wrap gap-2 mb-4">
        <li class="nav-item">
            <a class="nav-link {% if tab == 'estoque' %}active{% endif %}" href="{{ url_for('dashboard', tab='estoque') }}">
                <i class="fa-solid fa-boxes-stacked me-1"></i> Estoque (Entradas por Data)
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'saque' %}active{% endif %}" href="{{ url_for('dashboard', tab='saque') }}">
                <i class="fa-solid fa-hand-holding-dollar me-1"></i> Drops Disponíveis para Saque
                {% if available_drops %}
                    <span class="badge bg-warning text-dark ms-1">{{ available_drops|length }}</span>
                {% endif %}
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'tw' %}active{% endif %}" href="{{ url_for('dashboard', tab='tw') }}">
                <i class="fa-solid fa-chess-rook me-1"></i> Controle de TW (Ataque & Defesa)
                <span class="badge bg-warning text-dark ms-1">{{ total_tw_escalados }} Escalados</span>
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'eventos' %}active{% endif %}" href="{{ url_for('dashboard', tab='eventos') }}">
                <i class="fa-solid fa-tower-broadcast me-1"></i> Eventos & Chamada TS3 (+1 TKP)
            </a>
        </li>
        <li class="nav-item">
            <a class="nav-link {% if tab == 'players' %}active{% endif %}" href="{{ url_for('dashboard', tab='players') }}">
                <i class="fa-solid fa-users me-1"></i> Players & Carteira TKP
                {% if session['role'] == 'admin' and (pending_users or reset_requests) %}
                    <span class="badge bg-danger ms-1">{{ (pending_users|length) + (reset_requests|length) }}</span>
                {% endif %}
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

    <!-- ==========================================================
         ABA CONTROLE DE TW (MODELO EXCEL: ATAQUE & DEFESA + CONTAGENS + VISUALIZAR/EDITAR)
         ========================================================== -->
    {% if tab == 'tw' %}

    <!-- Datalist Unificada: Membros da Guild + Nicks Extras TW + Nicks Online no TS3 agora -->
    <datalist id="allTwPlayersDatalist">
        {% for u in users %}
        <option value="{{ u.ts_nick }}">{% if u.is_online_ts %}🟢 [ON TS3] {% endif %}Guild: {{ u.username }} ({{ u.player_class }})</option>
        {% endfor %}
        {% for ex in tw_extra_players %}
        <option value="{{ ex.nick }}">{% if ex.is_online_ts %}🟢 [ON TS3] {% endif %}Extra/Fora: {{ ex.pilot_note or 'Convidado' }} ({{ ex.player_class }})</option>
        {% endfor %}
        {% for on_nick in online_ts3_nicks %}
        <option value="{{ on_nick }}">🟢 [Online no TS3 Agora]</option>
        {% endfor %}
    </datalist>

    <!-- 1. BARRA SUPERIOR: TOTAL GERAL DA TW + CONTABILIZADOR DE CLASSES + BOTÕES DE AÇÃO -->
    <div class="card p-3 mb-3 border-primary">
        <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-3">
            <div class="d-flex align-items-center gap-3">
                <div class="bg-warning text-dark rounded-3 px-3 py-2 text-center shadow-sm">
                    <div class="small fw-bold text-uppercase" style="font-size: 0.68rem;">Total Escalado TW</div>
                    <div class="fs-3 fw-black lh-1" id="grandTotalTwBadge">{{ total_tw_escalados }}</div>
                </div>
                <div>
                    <h5 class="fw-bold mb-1">
                        <i class="fa-solid fa-table-columns text-info me-1"></i> Controle de TW — Ataque & Defesa
                    </h5>
                    <div class="small text-secondary">
                        <span class="badge bg-danger me-1">ATAQUE: <span id="topAttackTotal">{{ total_attack_players }}</span> ({{ attack_parties|length }} PTs)</span>
                        <span class="badge bg-primary me-1">DEFESA: <span id="topDefenseTotal">{{ total_defense_players }}</span> ({{ defense_parties|length }} PTs)</span>
                        <span class="badge bg-dark border border-secondary">Online TS3: <strong class="text-success">{{ online_ts3_nicks|length }}</strong> ({{ ts3_cache_time }})</span>
                    </div>
                </div>
            </div>

            <!-- SELETOR DE MODO DE VISUALIZAÇÃO / EDIÇÃO E BOTÕES RÁPIDOS -->
            <div class="d-flex flex-wrap align-items-center gap-2">
                <div class="btn-group btn-group-sm" role="group">
                    <a href="{{ url_for('dashboard', tab='tw', tw_view='planilha') }}"
                       class="btn {% if tw_view == 'planilha' %}btn-primary fw-bold{% else %}btn-outline-secondary{% endif %}">
                        <i class="fa-solid fa-table me-1"></i> Visão Planilha Geral (Ataque & Defesa)
                    </a>
                    <a href="{{ url_for('dashboard', tab='tw', tw_view='cards') }}"
                       class="btn {% if tw_view == 'cards' %}btn-primary fw-bold{% else %}btn-outline-secondary{% endif %}">
                        <i class="fa-solid fa-sliders me-1"></i> Visualizar / Editar PT Individual
                    </a>
                </div>

                {% if session['role'] == 'admin' %}
                <form method="POST" action="{{ url_for('seed_default_tw_pts') }}" class="d-inline">
                    <button type="submit" class="btn btn-sm btn-outline-warning fw-semibold" title="Cria CT, RECUPERAÇÃO, APOIO 1..4 (Ataque) e KILL CT 1..2, BACKDEF 1..4 (Defesa)">
                        <i class="fa-solid fa-wand-magic-sparkles me-1"></i> Gerar 12 PTs do Modelo Excel
                    </button>
                </form>
                <form method="POST" action="{{ url_for('sync_tw_ts3_online') }}" class="d-inline">
                    <button type="submit" class="btn btn-sm btn-outline-success fw-semibold">
                        <i class="fa-solid fa-satellite-dish me-1"></i> Puxar Online TS3
                    </button>
                </form>
                {% endif %}

                <button class="btn btn-sm btn-outline-info" type="button" data-bs-toggle="collapse" data-bs-target="#collapsePlayersByClass">
                    <i class="fa-solid fa-users-viewfinder me-1"></i> Base de Contas ({{ (users|length) + (tw_extra_players|length) }})
                </button>
                <a href="{{ url_for('export_tw_csv') }}" class="btn btn-sm btn-success fw-semibold">
                    <i class="fa-solid fa-file-excel me-1"></i> Exportar Excel (.CSV)
                </a>
            </div>
        </div>

        <!-- RESUMO DE CLASSES CADASTRADAS VS ESCALADAS -->
        <div class="row g-2 text-center">
            {% for cls in pw_classes %}
            {% set cad_cnt = registered_class_counts.get(cls, 0) %}
            {% set esc_cnt = tw_assigned_class_counts.get(cls, 0) %}
            <div class="col-6 col-sm-4 col-md-3 col-lg">
                <div class="p-2 rounded bg-dark border border-secondary h-100">
                    <span class="badge cls-badge cls-{{ cls }} mb-1">{{ cls }}</span>
                    <div class="fw-bold text-light">{{ cad_cnt }} <small class="text-secondary fw-normal" style="font-size:0.7rem;">base</small></div>
                    <div class="small {% if esc_cnt > 0 %}text-warning fw-bold{% else %}text-secondary{% endif %}" style="font-size:0.72rem;">
                        Escalados: <span class="live-total-cls-{{ cls }}">{{ esc_cnt }}</span>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>

        <!-- Gaveta da Base de Contas por Classe -->
        <div class="collapse mt-3" id="collapsePlayersByClass">
            <div class="card card-body bg-black border-secondary p-3">
                <h6 class="small fw-bold text-info mb-2">
                    Base de Contas por Classe (🟢 = Online no TS3 | Verde = Já Escalado em PT | Cinza = Disponível):
                </h6>
                <div class="row g-2">
                    {% for cls in pw_classes %}
                    <div class="col-md-6 col-lg-4">
                        <div class="p-2 border border-secondary rounded bg-dark">
                            <span class="badge cls-badge cls-{{ cls }} me-1">{{ cls }}</span>
                            <strong class="small">({{ players_by_class[cls]|length }}):</strong>
                            <div class="mt-1 d-flex flex-wrap gap-1">
                                {% for pl in players_by_class[cls] %}
                                    <span class="badge {% if pl.assigned_pt %}bg-success{% else %}bg-secondary bg-opacity-50 text-light{% endif %}">
                                        {% if pl.is_online_ts %}🟢 {% endif %}
                                        {{ pl.ts_nick }}
                                        {% if pl.is_extra %}<span class="text-warning">[Extra]</span>{% endif %}
                                        {% if pl.assigned_pt %}({{ pl.assigned_pt }}){% endif %}
                                    </span>
                                {% else %}
                                    <span class="text-secondary small">Nenhum cadastrado</span>
                                {% endfor %}
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>

    <!-- 2. PAINEL DE INCLUSÃO RÁPIDA (NOVO NICK / CONVIDADO + CRIAR NOVA PT COM POSIÇÃO ATAQUE/DEFESA) -->
    {% if session['role'] == 'admin' %}
    <div class="row g-3 mb-4">
        <div class="col-lg-6">
            <div class="card p-3 h-100 border-info">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <h6 class="fw-bold text-info mb-0">
                        <i class="fa-solid fa-user-plus me-1"></i> Incluir Nick na Base da TW (Player / Firma / Líder de Fora)
                    </h6>
                    {% if tw_extra_players %}
                    <button class="btn btn-sm btn-outline-secondary py-0 px-2" type="button" data-bs-toggle="collapse" data-bs-target="#collapseExtraList">
                        Ver Extras ({{ tw_extra_players|length }})
                    </button>
                    {% endif %}
                </div>
                <form method="POST" action="{{ url_for('add_tw_extra_player') }}" class="row g-2 align-items-end mt-1">
                    <div class="col-sm-4">
                        <label class="small text-secondary" style="font-size:0.7rem;">Nick do Char</label>
                        <input type="text" name="nick" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: nikeeMG" required>
                    </div>
                    <div class="col-sm-3">
                        <label class="small text-warning fw-bold" style="font-size:0.7rem;">Classe</label>
                        <select name="player_class" class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary">
                            {% for cls in pw_classes %}
                            <option value="{{ cls }}">{{ cls }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="col-sm-3">
                        <label class="small text-secondary" style="font-size:0.7rem;">Quem vai jogar (Opcional)</label>
                        <input type="text" name="pilot_note" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Líder / Firma">
                    </div>
                    <div class="col-sm-2">
                        <button type="submit" class="btn btn-info btn-sm w-100 fw-bold text-dark">+ Base</button>
                    </div>
                </form>

                {% if tw_extra_players %}
                <div class="collapse mt-2" id="collapseExtraList">
                    <div class="bg-black rounded p-2 border border-secondary d-flex flex-wrap gap-2">
                        {% for ex in tw_extra_players %}
                        <div class="badge bg-dark border border-secondary d-flex align-items-center gap-1 p-1">
                            <span class="badge cls-badge cls-{{ ex.player_class }}">{{ ex.player_class }}</span>
                            <strong>{{ ex.nick }}</strong>
                            {% if ex.pilot_note %}<small class="text-info">({{ ex.pilot_note }})</small>{% endif %}
                            <form method="POST" action="{{ url_for('delete_tw_extra_player', extra_id=ex.id) }}" class="d-inline ms-1">
                                <button type="submit" class="btn btn-sm text-danger p-0 border-0"><i class="fa-solid fa-xmark"></i></button>
                            </form>
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
            </div>
        </div>

        <div class="col-lg-6">
            <div class="card p-3 h-100 border-warning">
                <h6 class="fw-bold text-warning mb-1">
                    <i class="fa-solid fa-flag me-1"></i> Criar Nova PT (Escolher Posição: ATAQUE ou DEFESA)
                </h6>
                <form method="POST" action="{{ url_for('create_tw_pt') }}" class="row g-2 align-items-end mt-1">
                    <div class="col-sm-3">
                        <label class="small text-warning fw-bold" style="font-size:0.7rem;">Posição na TW</label>
                        <select name="pt_side" class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary">
                            <option value="ATAQUE">⚔️ ATAQUE</option>
                            <option value="DEFESA">🛡️ DEFESA</option>
                        </select>
                    </div>
                    <div class="col-sm-3">
                        <label class="small text-secondary" style="font-size:0.7rem;">Nome da PT</label>
                        <input type="text" name="pt_name" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: CT / APOIO 1 / BACKDEF 1" required>
                    </div>
                    <div class="col-sm-4">
                        <label class="small text-secondary" style="font-size:0.7rem;">Função / Detalhe</label>
                        <input type="text" name="pt_function" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Catapulta / Anti-CT">
                    </div>
                    <div class="col-sm-2">
                        <button type="submit" class="btn btn-warning btn-sm w-100 fw-bold text-dark">
                            + Criar PT
                        </button>
                    </div>
                </form>
            </div>
        </div>
    </div>
    {% endif %}

    <!-- ==========================================================
         MODO 1: VISÃO PLANILHA COMPLETA LADO A LADO (IGUAL AO EXCEL: ATAQUE & DEFESA + TABELAS DE QUANTIDADE)
         ========================================================== -->
    {% if tw_view == 'planilha' %}
        {% if tw_parties %}
        <form method="POST" action="{{ url_for('save_tw_matrix') }}" id="twMatrixForm">

            {% if session['role'] == 'admin' %}
            <div class="d-flex flex-wrap justify-content-between align-items-center bg-dark border border-success rounded-3 p-2 px-3 mb-3">
                <div class="small text-light">
                    <i class="fa-solid fa-pen-to-square text-success me-1"></i>
                    <strong>Edição Direta na Planilha Ativa:</strong> Digite ou escolha o <strong>NICK</strong> em qualquer coluna — a <strong>CLASSE</strong> e as tabelas de <strong>QUANTIDADE ATAQUE / DEFESA</strong> atualizam sozinhas na hora!
                </div>
                <button type="submit" class="btn btn-success btn-sm fw-bold px-4">
                    <i class="fa-solid fa-floppy-disk me-1"></i> Salvar Toda a Planilha de TW
                </button>
            </div>
            {% endif %}

            <!-- ==========================================
                 BLOCO 1 DA PLANILHA: ATAQUE + QUANTIDADE ATAQUE
                 ========================================== -->
            <div class="mb-4">
                <div class="tw-excel-banner-attack d-flex justify-content-between align-items-center">
                    <span><i class="fa-solid fa-burst me-2"></i> BLOCO DE ATAQUE ({{ attack_parties|length }} PTs)</span>
                    <span class="badge bg-black text-warning fs-6">TOTAL ATAQUE: <span id="bannerAttackTotal">{{ total_attack_players }}</span></span>
                </div>
                <div class="card rounded-top-0 border-danger p-3">
                    <div class="row g-3">
                        <!-- Colunas das PTs de ATAQUE Lado a Lado -->
                        <div class="col-xl-9 col-lg-8">
                            {% if attack_parties %}
                            <div class="d-flex gap-2 overflow-auto pb-2">
                                {% for pt in attack_parties %}
                                <div class="tw-matrix-col" data-pt-id="{{ pt.id }}" data-pt-side="ATAQUE">
                                    <div class="tw-matrix-header-attack">
                                        {% if session['role'] == 'admin' %}
                                        <div class="d-flex justify-content-between align-items-center gap-1 mb-1">
                                            <input type="text" name="pt_name_{{ pt.id }}" value="{{ pt.pt_name }}"
                                                   class="form-control form-control-sm bg-black text-warning fw-bold border-secondary py-0 px-1"
                                                   style="font-size:0.8rem;" required>
                                            <select name="pt_side_{{ pt.id }}" class="form-select form-select-sm bg-black text-light border-secondary py-0 px-1" style="width:82px; font-size:0.7rem;" title="Mover para Ataque ou Defesa">
                                                <option value="ATAQUE" selected>ATAQUE</option>
                                                <option value="DEFESA">DEFESA</option>
                                            </select>
                                            <a href="{{ url_for('dashboard', tab='tw', tw_view='cards', pt_id=pt.id) }}" class="btn btn-sm btn-outline-light py-0 px-1" title="Abrir PT isolada">
                                                <i class="fa-solid fa-up-right-from-square" style="font-size:0.7rem;"></i>
                                            </a>
                                        </div>
                                        <input type="text" name="pt_func_{{ pt.id }}" value="{{ pt.pt_function }}"
                                               class="form-control form-control-sm bg-dark text-info border-0 py-0 px-1"
                                               style="font-size:0.7rem;" placeholder="Função da PT...">
                                        {% else %}
                                        <div class="fw-bold text-warning text-center">{{ pt.pt_name }}</div>
                                        <div class="small text-info text-center" style="font-size:0.72rem;">{{ pt.pt_function or 'Ataque' }}</div>
                                        {% endif %}
                                    </div>

                                    <!-- Subcabeçalho CLASSE | NICK igual ao Excel -->
                                    <div class="d-flex bg-black text-secondary fw-bold border-bottom border-secondary px-2 py-1" style="font-size:0.68rem;">
                                        <div style="width: 68px;">CLASSE</div>
                                        <div class="flex-grow-1">NICK (1º = LÍDER)</div>
                                    </div>

                                    <!-- 10 Linhas da PT -->
                                    {% for s in pt.slots %}
                                    {% set is_ldr = (s.slot_number == 1) %}
                                    <div class="p-1 border-bottom border-secondary {% if is_ldr %}tw-matrix-row-leader{% endif %}">
                                        {% if session['role'] == 'admin' %}
                                        <div class="d-flex align-items-center gap-1">
                                            <select name="class_{{ pt.id }}_{{ s.slot_number }}"
                                                    id="mClass_{{ pt.id }}_{{ s.slot_number }}"
                                                    onchange="recalcTwMatrix()"
                                                    class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary py-0 px-1 matrix-cls-select"
                                                    data-side="ATAQUE"
                                                    data-pt-id="{{ pt.id }}"
                                                    style="width: 66px; font-size:0.74rem;">
                                                <option value="">--</option>
                                                {% for cls in pw_classes %}
                                                <option value="{{ cls }}" {% if s.player_class == cls %}selected{% endif %}>{{ cls }}</option>
                                                {% endfor %}
                                            </select>
                                            <div class="flex-grow-1 position-relative">
                                                <input type="text"
                                                       name="nick_{{ pt.id }}_{{ s.slot_number }}"
                                                       id="mNick_{{ pt.id }}_{{ s.slot_number }}"
                                                       value="{{ s.player_nick }}"
                                                       list="allTwPlayersDatalist"
                                                       oninput="onMatrixNickInput(this, 'mClass_{{ pt.id }}_{{ s.slot_number }}', 'mPilot_{{ pt.id }}_{{ s.slot_number }}')"
                                                       class="form-control form-control-sm bg-dark {% if is_ldr %}text-warning fw-bold border-warning{% else %}text-light border-secondary{% endif %} py-0 px-1 matrix-nick-input"
                                                       data-side="ATAQUE"
                                                       data-pt-id="{{ pt.id }}"
                                                       style="font-size:0.78rem;"
                                                       placeholder="{% if is_ldr %}👑 Líder...{% else %}{{ s.slot_number }}. Nick...{% endif %}">
                                            </div>
                                        </div>
                                        <input type="text"
                                               name="pilot_{{ pt.id }}_{{ s.slot_number }}"
                                               id="mPilot_{{ pt.id }}_{{ s.slot_number }}"
                                               value="{{ s.pilot_nick or '' }}"
                                               class="form-control form-control-sm bg-black text-info border-0 py-0 px-1 mt-1"
                                               style="font-size:0.68rem;"
                                               placeholder="Opcional: Quem vai jogar...">
                                        {% else %}
                                        <div class="d-flex align-items-center justify-content-between px-1 py-1">
                                            <span class="badge cls-badge cls-{{ s.player_class }}" style="font-size:0.68rem;">{{ s.player_class or '—' }}</span>
                                            <div class="text-truncate ms-2 flex-grow-1 {% if is_ldr %}fw-bold text-warning{% else %}small{% endif %}">
                                                {% if is_ldr %}👑 {% endif %}
                                                {% if s.is_online_ts %}<span class="text-success">●</span> {% endif %}
                                                {{ s.player_nick or '—' }}
                                                {% if s.pilot_nick %}<small class="text-info d-block" style="font-size:0.65rem;">({{ s.pilot_nick }})</small>{% endif %}
                                            </div>
                                        </div>
                                        {% endif %}
                                    </div>
                                    {% endfor %}

                                    <!-- Rodapé com Contagem da PT (Igual à linha 19 do Excel) -->
                                    <div class="tw-matrix-footer text-warning">
                                        TOTAL PT: <span id="ptCount_{{ pt.id }}" class="fs-6">{{ pt.filled_count }}</span> / 10
                                    </div>
                                </div>
                                {% endfor %}
                            </div>
                            {% else %}
                            <div class="text-center text-secondary py-4">
                                Nenhuma PT de <strong>ATAQUE</strong> criada ainda. Crie acima ou clique em <strong>Gerar 12 PTs do Modelo Excel</strong>.
                            </div>
                            {% endif %}
                        </div>

                        <!-- Tabela Lateral Direita: QUANTIDADE ATAQUE (Igual às colunas O e P do Excel) -->
                        <div class="col-xl-3 col-lg-4">
                            <div class="tw-qty-sidebar p-2 h-100">
                                <div class="bg-danger text-white fw-bold text-center py-1 rounded mb-2" style="font-size:0.82rem;">
                                    QUANTIDADE ATAQUE
                                </div>
                                <table class="table table-dark table-sm table-bordered border-secondary align-middle mb-0 text-center">
                                    <thead>
                                        <tr class="text-secondary" style="font-size:0.72rem;">
                                            <th>CLASSE</th>
                                            <th>QUANTIDADE</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for cls in pw_classes %}
                                        <tr>
                                            <td><span class="badge cls-badge cls-{{ cls }}">{{ cls }}</span></td>
                                            <td class="fw-bold fs-6" id="qtyAttack_{{ cls }}">{{ tw_attack_class_counts.get(cls, 0) }}</td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                    <tfoot>
                                        <tr class="table-warning text-dark fw-black">
                                            <td>TOTAL</td>
                                            <td class="fs-5" id="qtyAttackTotal">{{ total_attack_players }}</td>
                                        </tr>
                                    </tfoot>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ==========================================
                 BLOCO 2 DA PLANILHA: DEFESA + QUANTIDADE DEFESA
                 ========================================== -->
            <div class="mb-4">
                <div class="tw-excel-banner-defense d-flex justify-content-between align-items-center">
                    <span><i class="fa-solid fa-shield-halved me-2"></i> BLOCO DE DEFESA ({{ defense_parties|length }} PTs)</span>
                    <span class="badge bg-black text-info fs-6">TOTAL DEFESA: <span id="bannerDefenseTotal">{{ total_defense_players }}</span></span>
                </div>
                <div class="card rounded-top-0 border-primary p-3">
                    <div class="row g-3">
                        <!-- Colunas das PTs de DEFESA Lado a Lado -->
                        <div class="col-xl-9 col-lg-8">
                            {% if defense_parties %}
                            <div class="d-flex gap-2 overflow-auto pb-2">
                                {% for pt in defense_parties %}
                                <div class="tw-matrix-col" data-pt-id="{{ pt.id }}" data-pt-side="DEFESA">
                                    <div class="tw-matrix-header-defense">
                                        {% if session['role'] == 'admin' %}
                                        <div class="d-flex justify-content-between align-items-center gap-1 mb-1">
                                            <input type="text" name="pt_name_{{ pt.id }}" value="{{ pt.pt_name }}"
                                                   class="form-control form-control-sm bg-black text-warning fw-bold border-secondary py-0 px-1"
                                                   style="font-size:0.8rem;" required>
                                            <select name="pt_side_{{ pt.id }}" class="form-select form-select-sm bg-black text-light border-secondary py-0 px-1" style="width:82px; font-size:0.7rem;" title="Mover para Ataque ou Defesa">
                                                <option value="ATAQUE">ATAQUE</option>
                                                <option value="DEFESA" selected>DEFESA</option>
                                            </select>
                                            <a href="{{ url_for('dashboard', tab='tw', tw_view='cards', pt_id=pt.id) }}" class="btn btn-sm btn-outline-light py-0 px-1" title="Abrir PT isolada">
                                                <i class="fa-solid fa-up-right-from-square" style="font-size:0.7rem;"></i>
                                            </a>
                                        </div>
                                        <input type="text" name="pt_func_{{ pt.id }}" value="{{ pt.pt_function }}"
                                               class="form-control form-control-sm bg-dark text-info border-0 py-0 px-1"
                                               style="font-size:0.7rem;" placeholder="Função da PT...">
                                        {% else %}
                                        <div class="fw-bold text-warning text-center">{{ pt.pt_name }}</div>
                                        <div class="small text-info text-center" style="font-size:0.72rem;">{{ pt.pt_function or 'Defesa' }}</div>
                                        {% endif %}
                                    </div>

                                    <div class="d-flex bg-black text-secondary fw-bold border-bottom border-secondary px-2 py-1" style="font-size:0.68rem;">
                                        <div style="width: 68px;">CLASSE</div>
                                        <div class="flex-grow-1">NICK (1º = LÍDER)</div>
                                    </div>

                                    {% for s in pt.slots %}
                                    {% set is_ldr = (s.slot_number == 1) %}
                                    <div class="p-1 border-bottom border-secondary {% if is_ldr %}tw-matrix-row-leader{% endif %}">
                                        {% if session['role'] == 'admin' %}
                                        <div class="d-flex align-items-center gap-1">
                                            <select name="class_{{ pt.id }}_{{ s.slot_number }}"
                                                    id="mClass_{{ pt.id }}_{{ s.slot_number }}"
                                                    onchange="recalcTwMatrix()"
                                                    class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary py-0 px-1 matrix-cls-select"
                                                    data-side="DEFESA"
                                                    data-pt-id="{{ pt.id }}"
                                                    style="width: 66px; font-size:0.74rem;">
                                                <option value="">--</option>
                                                {% for cls in pw_classes %}
                                                <option value="{{ cls }}" {% if s.player_class == cls %}selected{% endif %}>{{ cls }}</option>
                                                {% endfor %}
                                            </select>
                                            <div class="flex-grow-1">
                                                <input type="text"
                                                       name="nick_{{ pt.id }}_{{ s.slot_number }}"
                                                       id="mNick_{{ pt.id }}_{{ s.slot_number }}"
                                                       value="{{ s.player_nick }}"
                                                       list="allTwPlayersDatalist"
                                                       oninput="onMatrixNickInput(this, 'mClass_{{ pt.id }}_{{ s.slot_number }}', 'mPilot_{{ pt.id }}_{{ s.slot_number }}')"
                                                       class="form-control form-control-sm bg-dark {% if is_ldr %}text-warning fw-bold border-warning{% else %}text-light border-secondary{% endif %} py-0 px-1 matrix-nick-input"
                                                       data-side="DEFESA"
                                                       data-pt-id="{{ pt.id }}"
                                                       style="font-size:0.78rem;"
                                                       placeholder="{% if is_ldr %}👑 Líder...{% else %}{{ s.slot_number }}. Nick...{% endif %}">
                                            </div>
                                        </div>
                                        <input type="text"
                                               name="pilot_{{ pt.id }}_{{ s.slot_number }}"
                                               id="mPilot_{{ pt.id }}_{{ s.slot_number }}"
                                               value="{{ s.pilot_nick or '' }}"
                                               class="form-control form-control-sm bg-black text-info border-0 py-0 px-1 mt-1"
                                               style="font-size:0.68rem;"
                                               placeholder="Opcional: Quem vai jogar...">
                                        {% else %}
                                        <div class="d-flex align-items-center justify-content-between px-1 py-1">
                                            <span class="badge cls-badge cls-{{ s.player_class }}" style="font-size:0.68rem;">{{ s.player_class or '—' }}</span>
                                            <div class="text-truncate ms-2 flex-grow-1 {% if is_ldr %}fw-bold text-warning{% else %}small{% endif %}">
                                                {% if is_ldr %}👑 {% endif %}
                                                {% if s.is_online_ts %}<span class="text-success">●</span> {% endif %}
                                                {{ s.player_nick or '—' }}
                                                {% if s.pilot_nick %}<small class="text-info d-block" style="font-size:0.65rem;">({{ s.pilot_nick }})</small>{% endif %}
                                            </div>
                                        </div>
                                        {% endif %}
                                    </div>
                                    {% endfor %}

                                    <div class="tw-matrix-footer text-info">
                                        TOTAL PT: <span id="ptCount_{{ pt.id }}" class="fs-6">{{ pt.filled_count }}</span> / 10
                                    </div>
                                </div>
                                {% endfor %}
                            </div>
                            {% else %}
                            <div class="text-center text-secondary py-4">
                                Nenhuma PT de <strong>DEFESA</strong> criada ainda. Crie acima escolhendo <strong>🛡️ DEFESA</strong> ou clique em <strong>Gerar 12 PTs do Modelo Excel</strong>.
                            </div>
                            {% endif %}
                        </div>

                        <!-- Tabela Lateral Direita: QUANTIDADE DEFESA (Igual às colunas O e P do Excel) -->
                        <div class="col-xl-3 col-lg-4">
                            <div class="tw-qty-sidebar p-2 h-100">
                                <div class="bg-primary text-white fw-bold text-center py-1 rounded mb-2" style="font-size:0.82rem;">
                                    QUANTIDADE DEFESA
                                </div>
                                <table class="table table-dark table-sm table-bordered border-secondary align-middle mb-0 text-center">
                                    <thead>
                                        <tr class="text-secondary" style="font-size:0.72rem;">
                                            <th>CLASSE</th>
                                            <th>QUANTIDADE</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {% for cls in pw_classes %}
                                        <tr>
                                            <td><span class="badge cls-badge cls-{{ cls }}">{{ cls }}</span></td>
                                            <td class="fw-bold fs-6" id="qtyDefense_{{ cls }}">{{ tw_defense_class_counts.get(cls, 0) }}</td>
                                        </tr>
                                        {% endfor %}
                                    </tbody>
                                    <tfoot>
                                        <tr class="table-info text-dark fw-black">
                                            <td>TOTAL</td>
                                            <td class="fs-5" id="qtyDefenseTotal">{{ total_defense_players }}</td>
                                        </tr>
                                    </tfoot>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            {% if session['role'] == 'admin' %}
            <div class="text-end">
                <button type="submit" class="btn btn-success fw-bold px-5 py-2 shadow">
                    <i class="fa-solid fa-floppy-disk me-1"></i> Salvar Todas as Alterações da Planilha (Ataque & Defesa)
                </button>
            </div>
            {% endif %}
        </form>
        {% else %}
        <div class="card p-5 text-center">
            <i class="fa-solid fa-table-columns fa-3x text-warning mb-3"></i>
            <h4>Sua Planilha de Controle de TW está pronta para começar!</h4>
            <p class="text-secondary small mb-3">
                Clique no botão abaixo para gerar automaticamente as 12 PTs do seu modelo Excel (<strong>CT, RECUPERAÇÃO, APOIO 1 a 4</strong> no Ataque + <strong>KILL CT 1 e 2, BACKDEF 1 a 4</strong> na Defesa) ou crie uma PT personalizada acima.
            </p>
            {% if session['role'] == 'admin' %}
            <form method="POST" action="{{ url_for('seed_default_tw_pts') }}">
                <button type="submit" class="btn btn-warning fw-bold text-dark px-4 py-2">
                    <i class="fa-solid fa-wand-magic-sparkles me-1"></i> Gerar 12 PTs Padrão do Modelo Excel Agora
                </button>
            </form>
            {% endif %}
        </div>
        {% endif %}

    <!-- ==========================================================
         MODO 2: VISUALIZAR E EDITAR PT INDIVIDUAL (COM SELETOR RÁPIDO DE PT)
         ========================================================== -->
    {% else %}
        <!-- Barra de Seleção Rápida de PT para Visualizar / Editar -->
        <div class="card p-3 mb-4 border-info">
            <div class="row g-2 align-items-center">
                <div class="col-md-5">
                    <label class="small text-info fw-bold mb-1">
                        <i class="fa-solid fa-magnifying-glass me-1"></i> Selecionar PT para Visualizar / Editar com Foco:
                    </label>
                    <select class="form-select form-select-sm bg-dark text-light border-secondary"
                            onchange="window.location.href=this.value">
                        <option value="{{ url_for('dashboard', tab='tw', tw_view='cards') }}" {% if not selected_pt_id %}selected{% endif %}>
                            -- Mostrar Todas as PTs ({{ tw_parties|length }}) --
                        </option>
                        <optgroup label="⚔️ PTs de ATAQUE">
                            {% for pt in attack_parties %}
                            <option value="{{ url_for('dashboard', tab='tw', tw_view='cards', pt_id=pt.id) }}" {% if selected_pt_id == pt.id %}selected{% endif %}>
                                [ATAQUE] {{ pt.pt_name }} - {{ pt.pt_function or 'Geral' }} ({{ pt.filled_count }}/10)
                            </option>
                            {% endfor %}
                        </optgroup>
                        <optgroup label="🛡️ PTs de DEFESA">
                            {% for pt in defense_parties %}
                            <option value="{{ url_for('dashboard', tab='tw', tw_view='cards', pt_id=pt.id) }}" {% if selected_pt_id == pt.id %}selected{% endif %}>
                                [DEFESA] {{ pt.pt_name }} - {{ pt.pt_function or 'Geral' }} ({{ pt.filled_count }}/10)
                            </option>
                            {% endfor %}
                        </optgroup>
                    </select>
                </div>
                <div class="col-md-7 text-md-end mt-2 mt-md-4">
                    {% if selected_pt_id %}
                    <a href="{{ url_for('dashboard', tab='tw', tw_view='cards') }}" class="btn btn-sm btn-outline-info">
                        Ver Todas as PTs
                    </a>
                    {% endif %}
                    <a href="{{ url_for('dashboard', tab='tw', tw_view='planilha') }}" class="btn btn-sm btn-warning text-dark fw-bold">
                        <i class="fa-solid fa-table me-1"></i> Voltar para Visão Planilha Lado a Lado
                    </a>
                </div>
            </div>
        </div>

        <div class="row g-4">
            {% for pt in tw_parties if (not selected_pt_id or pt.id == selected_pt_id) %}
            <div class="{% if selected_pt_id %}col-lg-8 mx-auto{% else %}col-lg-6{% endif %}">
                <div class="card p-3 h-100 {% if pt.pt_side == 'DEFESA' %}border-primary{% else %}border-danger{% endif %}">
                    {% if session['role'] == 'admin' %}
                    <form method="POST" action="{{ url_for('save_tw_pt', pt_id=pt.id) }}">
                        <div class="d-flex flex-wrap justify-content-between align-items-center border-bottom border-secondary pb-2 mb-2 gap-2">
                            <div class="d-flex gap-2 flex-grow-1">
                                <select name="pt_side" class="form-select form-select-sm bg-dark {% if pt.pt_side == 'DEFESA' %}text-info border-primary{% else %}text-danger border-danger{% endif %} fw-bold" style="max-width: 120px;">
                                    <option value="ATAQUE" {% if pt.pt_side == 'ATAQUE' %}selected{% endif %}>⚔️ ATAQUE</option>
                                    <option value="DEFESA" {% if pt.pt_side == 'DEFESA' %}selected{% endif %}>🛡️ DEFESA</option>
                                </select>
                                <input type="text" name="pt_name" value="{{ pt.pt_name }}" class="form-control form-control-sm bg-dark text-warning fw-bold border-secondary" style="max-width: 140px;" required>
                                <input type="text" name="pt_function" value="{{ pt.pt_function }}" class="form-control form-control-sm bg-dark text-info fw-semibold border-secondary" placeholder="Função da PT">
                            </div>
                            <span class="badge {% if pt.pt_side == 'DEFESA' %}bg-primary{% else %}bg-danger{% endif %}">{{ pt.filled_count }}/10</span>
                        </div>

                        <div class="row g-1 px-1 mb-1 text-secondary" style="font-size: 0.7rem; text-transform: uppercase;">
                            <div class="col-2">Vaga</div>
                            <div class="col-4">Nick (Guild / On / Novo)</div>
                            <div class="col-4">Quem vai Jogar (Opcional)</div>
                            <div class="col-2">Classe</div>
                        </div>

                        <div class="d-flex flex-column gap-2 mb-3">
                            {% for s in pt.slots %}
                            {% set is_ldr = (s.slot_number == 1) %}
                            <div class="row g-1 align-items-center {% if is_ldr %}leader-slot-row{% else %}member-slot-row{% endif %}">
                                <div class="col-2">
                                    {% if is_ldr %}
                                        <span class="badge bg-warning text-dark fw-bold w-100 py-1" style="font-size:0.7rem;">👑 LÍDER</span>
                                    {% else %}
                                        <span class="badge bg-dark border border-secondary text-secondary w-100 py-1" style="font-size:0.7rem;">
                                            #{{ s.slot_number }}
                                            {% if s.is_online_ts %}<span class="text-success ms-1">●</span>{% endif %}
                                        </span>
                                    {% endif %}
                                </div>
                                <div class="col-4">
                                    <input type="text"
                                           name="nick_{{ s.slot_number }}"
                                           value="{{ s.player_nick }}"
                                           list="allTwPlayersDatalist"
                                           oninput="autoFillTwSlot(this, 'pilotInput_{{ pt.id }}_{{ s.slot_number }}', 'classSelect_{{ pt.id }}_{{ s.slot_number }}')"
                                           class="form-control form-control-sm bg-dark {% if is_ldr %}text-warning fw-bold border-warning{% else %}text-light border-secondary{% endif %}"
                                           placeholder="{% if is_ldr %}Nick Líder...{% else %}Nick...{% endif %}">
                                </div>
                                <div class="col-4">
                                    <input type="text"
                                           name="pilot_{{ s.slot_number }}"
                                           id="pilotInput_{{ pt.id }}_{{ s.slot_number }}"
                                           value="{{ s.pilot_nick or '' }}"
                                           list="allTwPlayersDatalist"
                                           class="form-control form-control-sm bg-black text-info border-secondary"
                                           placeholder="Opcional: Piloto/Líder...">
                                </div>
                                <div class="col-2">
                                    <select name="class_{{ s.slot_number }}"
                                            id="classSelect_{{ pt.id }}_{{ s.slot_number }}"
                                            class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary px-1">
                                        <option value="">--</option>
                                        {% for cls in pw_classes %}
                                        <option value="{{ cls }}" {% if s.player_class == cls %}selected{% endif %}>{{ cls }}</option>
                                        {% endfor %}
                                    </select>
                                </div>
                            </div>
                            {% endfor %}
                        </div>

                        <div class="d-flex flex-wrap justify-content-between align-items-center pt-2 border-top border-secondary gap-2">
                            <div class="form-check small text-secondary mb-0">
                                <input class="form-check-input" type="checkbox" name="auto_register_new" value="1" id="autoReg{{ pt.id }}" checked>
                                <label class="form-check-label" for="autoReg{{ pt.id }}" style="font-size: 0.75rem;">
                                    Incluir na base se for nick novo
                                </label>
                            </div>
                            <div class="d-flex gap-2">
                                <button type="submit" class="btn btn-sm btn-success fw-bold px-3">
                                    <i class="fa-solid fa-floppy-disk me-1"></i> Salvar {{ pt.pt_name }}
                                </button>
                                <button type="submit" form="deletePtForm{{ pt.id }}" class="btn btn-sm btn-outline-danger py-0 px-2">
                                    <i class="fa-solid fa-trash"></i>
                                </button>
                            </div>
                        </div>
                    </form>
                    <form id="deletePtForm{{ pt.id }}" method="POST" action="{{ url_for('delete_tw_pt', pt_id=pt.id) }}" onsubmit="return confirm('Excluir a {{ pt.pt_name }}?');"></form>
                    {% else %}
                    <div class="d-flex justify-content-between align-items-center border-bottom border-secondary pb-2 mb-3">
                        <div>
                            <span class="badge {% if pt.pt_side == 'DEFESA' %}bg-primary{% else %}bg-danger{% endif %} me-1">{{ pt.pt_side }}</span>
                            <span class="fs-5 fw-bold text-warning">{{ pt.pt_name }}</span>
                            {% if pt.pt_function %}
                                <span class="badge bg-info text-dark ms-2">{{ pt.pt_function }}</span>
                            {% endif %}
                        </div>
                        <span class="badge bg-secondary">{{ pt.filled_count }}/10</span>
                    </div>
                    <div class="table-responsive">
                        <table class="table table-dark table-sm align-middle mb-0">
                            <thead>
                                <tr class="text-secondary small">
                                    <th>Vaga</th>
                                    <th>Nick / Char</th>
                                    <th>Quem vai Jogar</th>
                                    <th class="text-end">Classe</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for s in pt.slots %}
                                <tr>
                                    <td>{% if s.slot_number == 1 %}<span class="badge bg-warning text-dark fw-bold">👑 1. LÍDER</span>{% else %}<span class="small text-secondary">#{{ s.slot_number }}</span>{% endif %}</td>
                                    <td class="{% if s.slot_number == 1 %}fw-bold text-warning{% endif %}">{{ s.player_nick or '—' }}</td>
                                    <td class="small text-info">{{ s.pilot_nick or '—' }}</td>
                                    <td class="text-end">{% if s.player_class %}<span class="badge cls-badge cls-{{ s.player_class }}">{{ s.player_class }}</span>{% else %}—{% endif %}</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
    {% endif %}
    {% endif %}

    <!-- ==========================================================
         ABA 1: ESTOQUE (LISTA SUSPENSA + ENTRADAS POR DATA DE CHEGADA)
         ========================================================== -->
    {% if tab == 'estoque' %}
    <div class="row g-4">

        {% if session['role'] == 'admin' %}
        <div class="col-lg-4">

            <div class="card p-3 mb-4 border-primary">
                <h6 class="fw-bold text-info mb-1">
                    <i class="fa-solid fa-list-check me-1"></i> Incluir Mais no Estoque (Lista Suspensa)
                </h6>
                <p class="text-secondary small mb-3">
                    Selecione um item já cadastrado para acumular mais unidades registrando a data de chegada.
                </p>
                <form method="POST" action="{{ url_for('add_stock_from_dropdown') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Selecionar Item Cadastrado</label>
                        <select name="item_id" id="dropdownItemSelect" class="form-select form-select-sm bg-dark text-light border-secondary" required onchange="syncDropdownTkp(this)">
                            <option value="" data-tkp="">-- Escolha o item na lista --</option>
                            {% for it in catalog_items %}
                            <option value="{{ it.id }}" data-tkp="{{ it.tkp_value }}">
                                {{ it.item_name }} ({{ it.rarity }} | {{ it.tkp_value }} TKP | Saldo: {{ it.stock_qty }})
                            </option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <label class="form-label small text-secondary mb-1">Qtd que Chegou</label>
                            <input type="number" name="quantity" value="1" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                        </div>
                        <div class="col-6">
                            <label class="form-label small text-secondary mb-1">Valor Unit. (TKP)</label>
                            <input type="number" name="tkp_value" id="dropdownTkpInput" min="0" placeholder="Padrão do item" class="form-control form-control-sm bg-dark text-warning fw-bold border-secondary">
                        </div>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Data/Hora de Chegada (Opcional)</label>
                        <input type="datetime-local" name="arrival_date" class="form-control form-control-sm bg-dark text-light border-secondary">
                        <div class="form-text text-secondary" style="font-size: 0.7rem;">Deixe em branco para usar a data/hora de agora.</div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Boss / Origem da Entrada</label>
                        <input type="text" name="boss_source" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: GVG / TW / Boss">
                    </div>
                    <button type="submit" class="btn btn-info btn-sm w-100 fw-bold text-dark" {% if not catalog_items %}disabled{% endif %}>
                        <i class="fa-solid fa-layer-group me-1"></i> Acumular Quantidade no Estoque
                    </button>
                </form>
            </div>

            <div class="card p-3">
                <h6 class="fw-bold text-warning mb-1">
                    <i class="fa-solid fa-plus-circle me-1"></i> Cadastrar Novo Item + Qtd Inicial
                </h6>
                <p class="text-secondary small mb-3">
                    Cadastre um item inédito com seu valor em <strong>TKP</strong>. Ele ficará salvo na lista suspensa acima para futuras entradas.
                </p>
                <form method="POST" action="{{ url_for('create_new_inventory_item') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nome do Novo Item (Drop)</label>
                        <input type="text" name="item_name" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Joia / Equipamento" required>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-4">
                            <label class="form-label small text-secondary mb-1">Raridade</label>
                            <select name="rarity" class="form-select form-select-sm bg-dark text-light border-secondary">
                                <option value="Comum">Comum</option>
                                <option value="Raro" selected>Raro</option>
                                <option value="Épico">Épico</option>
                                <option value="Lendário">Lendário</option>
                            </select>
                        </div>
                        <div class="col-4">
                            <label class="form-label small text-secondary mb-1">Qtd Inicial</label>
                            <input type="number" name="quantity" value="1" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                        </div>
                        <div class="col-4">
                            <label class="form-label small text-secondary mb-1">Valor (TKP)</label>
                            <input type="number" name="tkp_value" value="1" min="0" class="form-control form-control-sm bg-dark text-warning fw-bold border-secondary" required>
                        </div>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Data/Hora de Chegada (Opcional)</label>
                        <input type="datetime-local" name="arrival_date" class="form-control form-control-sm bg-dark text-light border-secondary">
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Boss / Origem</label>
                        <input type="text" name="boss_source" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: TW / GVG">
                    </div>
                    <button type="submit" class="btn btn-warning btn-sm w-100 fw-bold text-dark">
                        <i class="fa-solid fa-box-archive me-1"></i> Cadastrar Item & Gerar 1ª Entrada
                    </button>
                </form>
            </div>

        </div>
        {% endif %}

        <div class="{% if session['role'] == 'admin' %}col-lg-8{% else %}col-lg-12{% endif %}">
            <div class="card p-3">
                <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
                    <div>
                        <h6 class="fw-bold mb-0">
                            <i class="fa-solid fa-calendar-days text-info me-1"></i> Registro do Estoque por Data de Chegada ({{ stock_entries|length }} entradas)
                        </h6>
                        <small class="text-secondary">
                            Histórico detalhado de cada lote que entrou no estoque conforme a data de chegada e quantidade.
                        </small>
                    </div>
                    <a href="{{ url_for('dashboard', tab='saque') }}" class="btn btn-sm btn-outline-warning">
                        <i class="fa-solid fa-hand-holding-dollar me-1"></i> Ver Consolidado para Saque
                    </a>
                </div>

                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Data de Chegada</th>
                                <th>Item / Raridade</th>
                                <th>Boss / Origem</th>
                                <th class="text-center">Qtd na Chegada</th>
                                <th class="text-center">Valor Unit. (TKP)</th>
                                <th class="text-center">Total Lote (TKP)</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Estornar</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for entry in stock_entries %}
                            <tr>
                                <td class="small text-info fw-semibold">
                                    <i class="fa-regular fa-clock me-1"></i>{{ entry.arrival_date }}
                                </td>
                                <td>
                                    <span class="badge badge-raridade-{{ entry.rarity }} me-1">{{ entry.rarity }}</span>
                                    <strong>{{ entry.item_name }}</strong>
                                </td>
                                <td class="small text-secondary">{{ entry.boss_source or '—' }}</td>
                                <td class="text-center">
                                    <span class="badge bg-success fs-6">+{{ entry.qty_added }} un.</span>
                                </td>
                                <td class="text-center">
                                    <span class="badge badge-tkp">{{ entry.tkp_value }} TKP</span>
                                </td>
                                <td class="text-center fw-bold text-warning">
                                    {{ entry.qty_added * entry.tkp_value }} TKP
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <form method="POST" action="{{ url_for('delete_stock_entry', entry_id=entry.id) }}" onsubmit="return confirm('Estornar esta entrada de {{ entry.qty_added }} un. do estoque?');">
                                        <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2" title="Excluir entrada e descontar do saldo">
                                            <i class="fa-solid fa-trash"></i>
                                        </button>
                                    </form>
                                </td>
                                {% endif %}
                            </tr>
                            {% else %}
                            <tr>
                                <td colspan="7" class="text-center text-secondary py-4">
                                    Nenhuma entrada de estoque registrada ainda. Cadastre o primeiro item ao lado!
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

    </div>
    {% endif %}

    <!-- ==========================================================
         ABA 2: DROPS DISPONÍVEIS PARA SAQUE (SOMA DE TODAS AS DATAS + TKP)
         ========================================================== -->
    {% if tab == 'saque' %}
    <div class="row g-4">

        <div class="col-lg-6">
            <div class="card p-3">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <h6 class="fw-bold text-warning mb-0">
                        <i class="fa-solid fa-vault me-1"></i> Drops Disponíveis para Saque (Soma Total)
                    </h6>
                    <span class="badge bg-dark border border-secondary">
                        {{ available_drops|length }} tipos disponíveis
                    </span>
                </div>
                <p class="text-secondary small mb-3">
                    Todos os drops disponíveis no momento <strong>somando todas as datas de entrada</strong>, com seu respectivo valor em <strong>TKP</strong>.
                </p>

                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Drop / Item</th>
                                <th class="text-center">Saldo Acumulado</th>
                                <th class="text-center">Valor em TKP</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Liberar Saque / Ações</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in available_drops %}
                            <tr>
                                <td>
                                    <span class="badge badge-raridade-{{ item.rarity }} me-1">{{ item.rarity }}</span>
                                    <strong>{{ item.item_name }}</strong>
                                    <div class="text-secondary" style="font-size: 0.72rem;">
                                        Soma de {{ item.batches_count }} entrada(s) | Última: {{ item.updated_at }}
                                    </div>
                                </td>
                                <td class="text-center">
                                    <span class="badge bg-success fs-6">{{ item.stock_qty }} un.</span>
                                </td>
                                <td class="text-center">
                                    <span class="badge badge-tkp fs-6">{{ item.tkp_value }} TKP</span>
                                    <div class="text-secondary" style="font-size: 0.7rem;">
                                        Total: {{ item.total_tkp_worth }} TKP
                                    </div>
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <div class="d-flex justify-content-end gap-1">
                                        <button class="btn btn-sm btn-warning fw-bold text-dark py-0 px-2" type="button" data-bs-toggle="collapse" data-bs-target="#saqueBox{{ item.id }}">
                                            <i class="fa-solid fa-gavel me-1"></i> Liberar Lance
                                        </button>
                                        <button class="btn btn-sm btn-outline-secondary py-0 px-2" type="button" data-bs-toggle="collapse" data-bs-target="#editTkpBox{{ item.id }}" title="Editar TKP">
                                            <i class="fa-solid fa-pen"></i>
                                        </button>
                                    </div>
                                </td>
                                {% endif %}
                            </tr>

                            {% if session['role'] == 'admin' %}
                            <tr class="collapse" id="saqueBox{{ item.id }}">
                                <td colspan="4" class="bg-black p-3 border-warning">
                                    <div class="small fw-bold text-warning mb-2">
                                        <i class="fa-solid fa-bolt me-1"></i> Configurar Liberação de Lance para "{{ item.item_name }}"
                                    </div>
                                    <form method="POST" action="{{ url_for('start_auction') }}" class="row g-2 align-items-end">
                                        <input type="hidden" name="item_id" value="{{ item.id }}">
                                        <div class="col-3">
                                            <label class="small text-secondary" style="font-size:0.72rem;">Qtd para Saque</label>
                                            <input type="number" name="qty" value="1" min="1" max="{{ item.stock_qty }}" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                        </div>
                                        <div class="col-4">
                                            <label class="small text-secondary" style="font-size:0.72rem;">Lance Mínimo (TKP)</label>
                                            <input type="number" name="min_bid" value="{{ item.tkp_value }}" min="0" class="form-control form-control-sm bg-dark text-warning fw-bold border-secondary" required>
                                        </div>
                                        <div class="col-3">
                                            <label class="small text-secondary" style="font-size:0.72rem;">Tempo (Min)</label>
                                            <input type="number" name="duration_minutes" value="15" min="1" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                        </div>
                                        <div class="col-2">
                                            <button type="submit" class="btn btn-sm btn-success w-100 fw-bold">Abrir</button>
                                        </div>
                                    </form>
                                </td>
                            </tr>

                            <tr class="collapse" id="editTkpBox{{ item.id }}">
                                <td colspan="4" class="bg-black p-2 border-secondary">
                                    <div class="d-flex justify-content-between align-items-center">
                                        <form method="POST" action="{{ url_for('update_item_tkp', item_id=item.id) }}" class="d-flex align-items-center gap-2">
                                            <span class="small text-secondary">Novo Valor em TKP:</span>
                                            <input type="number" name="tkp_value" value="{{ item.tkp_value }}" min="0" class="form-control form-control-sm bg-dark text-warning border-secondary" style="width: 110px;" required>
                                            <button type="submit" class="btn btn-sm btn-info">Salvar TKP</button>
                                        </form>
                                        <form method="POST" action="{{ url_for('delete_catalog_item', item_id=item.id) }}" onsubmit="return confirm('Excluir este item permanentemente do catálogo?');">
                                            <button type="submit" class="btn btn-sm btn-outline-danger">Excluir do Catálogo</button>
                                        </form>
                                    </div>
                                </td>
                            </tr>
                            {% endif %}
                            {% else %}
                            <tr>
                                <td colspan="4" class="text-center text-secondary py-4">
                                    Nenhum drop com saldo disponível para saque no momento. Adicione unidades na aba <strong>Estoque</strong>!
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="col-lg-6">

            <div class="card p-3 mb-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h6 class="fw-bold mb-0">
                        <i class="fa-solid fa-fire text-danger me-1"></i> Disputas de Saque Abertas (Seu saldo livre: <span class="text-warning">{{ my_wallet.available_to_bid }} TKP</span>)
                    </h6>
                    <a href="{{ url_for('dashboard', tab='saque') }}" class="btn btn-sm btn-outline-secondary">
                        <i class="fa-solid fa-rotate me-1"></i> Atualizar
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
                                <span class="badge bg-secondary ms-1">x{{ a.qty }}</span>
                                {% if a.status == 'Em Desempate' %}
                                    <span class="badge bg-warning text-dark ms-1">
                                        <i class="fa-solid fa-bolt me-1"></i> DESEMPATE 6 MIN (Rodada {{ a.round_number }})
                                    </span>
                                {% else %}
                                    <span class="badge bg-primary ms-1">Saque Aberto</span>
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
                            <strong>Empate em {{ a.min_bid }} TKP!</strong>
                            Novo lance de <strong>6 minutos</strong> aberto exclusivamente para os empatantes:
                            <strong>{{ a.tied_names | join(', ') }}</strong>.
                        </div>
                        {% endif %}

                        <div class="row g-2 align-items-center bg-black bg-opacity-50 rounded p-2 mb-2">
                            <div class="col-sm-4">
                                <small class="text-secondary d-block">Valor Base (TKP):</small>
                                <strong class="text-warning">{{ a.min_bid }} TKP</strong>
                            </div>
                            <div class="col-sm-4">
                                <small class="text-secondary d-block">Maior Lance Atual:</small>
                                <strong class="text-success fs-5">{{ a.current_highest_bid }} TKP</strong>
                            </div>
                            <div class="col-sm-4">
                                {% if a.can_user_bid %}
                                <form method="POST" action="{{ url_for('place_bid', auction_id=a.id) }}" class="d-flex gap-1">
                                    <input type="number" name="bid_amount"
                                           min="{% if a.status == 'Em Desempate' %}{{ a.min_bid + 1 }}{% else %}{{ a.min_bid }}{% endif %}"
                                           placeholder="TKP"
                                           class="form-control form-control-sm bg-dark text-light border-secondary" required>
                                    <button type="submit" class="btn btn-sm btn-success fw-bold text-nowrap">
                                        Dar Lance
                                    </button>
                                </form>
                                {% else %}
                                <span class="badge bg-secondary w-100 py-2">
                                    <i class="fa-solid fa-lock me-1"></i> Só Empatantes
                                </span>
                                {% endif %}
                            </div>
                        </div>

                        <div class="d-flex flex-wrap justify-content-between align-items-center mt-1">
                            <div class="small text-secondary">
                                <strong>Lances na rodada ({{ a.bids_list|length }}):</strong>
                                {% for b in a.bids_list %}
                                    <span class="badge bg-secondary bg-opacity-50 text-light me-1">
                                        {{ b.username }}: <strong class="text-warning">{{ b.bid_amount }} TKP</strong> ({{ b.bid_time }})
                                    </span>
                                {% else %}
                                    <span>Nenhum lance registrado ainda.</span>
                                {% endfor %}
                            </div>

                            {% if session['role'] == 'admin' %}
                            <div class="d-flex gap-1 mt-2 mt-sm-0">
                                <form method="POST" action="{{ url_for('force_end_auction', auction_id=a.id) }}">
                                    <button type="submit" class="btn btn-sm btn-outline-warning py-0 px-2" title="Encerrar tempo e apurar agora">
                                        <i class="fa-solid fa-flag-checkered me-1"></i> Encerrar Agora
                                    </button>
                                </form>
                                <form method="POST" action="{{ url_for('delete_auction', auction_id=a.id) }}" onsubmit="return confirm('Cancelar e devolver o item ao saldo disponível?');">
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
                        Nenhuma disputa de saque aberta agora. O Admin pode liberar qualquer item disponível ao lado!
                    </p>
                {% endif %}
            </div>

            <div class="card p-3">
                <h6 class="fw-bold mb-3">
                    <i class="fa-solid fa-handshake-simple text-success me-1"></i> Saques Vencidos & Entrega In-Game (OK)
                </h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Item</th>
                                <th>Vencedor</th>
                                <th>Lance Final</th>
                                <th>Status</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Confirmar</th>
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
                                <td>
                                    {% if a.winning_bid %}
                                        <span class="badge badge-tkp">{{ a.winning_bid }} TKP</span>
                                    {% else %}
                                        —
                                    {% endif %}
                                </td>
                                <td>
                                    {% if a.status == 'Aguardando Entrega' %}
                                        <span class="badge bg-warning text-dark">Aguardando Entrega</span>
                                    {% elif a.status == 'Entregue (OK)' %}
                                        <span class="badge bg-success">OK ({{ a.delivered_at }})</span>
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
                                                <i class="fa-solid fa-check me-1"></i> OK
                                            </button>
                                        </form>
                                        {% endif %}
                                        <form method="POST" action="{{ url_for('delete_auction', auction_id=a.id) }}" onsubmit="return confirm('Remover registro?');">
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
                                <td colspan="5" class="text-center text-secondary py-3">Nenhum saque finalizado ainda.</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

        </div>
    </div>
    {% endif %}

    <!-- ==========================================================
         ABA 3: EVENTOS & CHAMADA TS3 (COM FILTRO GVG / TW / TODAS)
         ========================================================== -->
    {% if tab == 'eventos' %}
    <div class="row g-4">
        <div class="col-lg-4">
            {% if session['role'] == 'admin' %}
            <div class="card p-3 mb-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-plus-circle text-primary me-1"></i> Iniciar Novo Evento</h6>
                <form method="POST" action="{{ url_for('create_event') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Nome do Evento / Guerra</label>
                        <input type="text" name="title" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: TW Sábado 20h / GVG" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label small text-secondary mb-1">Modalidade</label>
                        <select name="event_type" class="form-select form-select-sm bg-dark text-light border-secondary">
                            <option value="GVG + TW">GVG + TW</option>
                            <option value="TW (Ataque / Defesa)">TW (Ataque / Defesa)</option>
                            <option value="GVG">GVG</option>
                            <option value="Artes Marciais / Tigres">Artes Marciais / Tigres</option>
                            <option value="Geral TS3">Geral TS3</option>
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
                            Criado em {{ selected_event.created_at }} | Última leitura TS3: <strong>{{ selected_event.last_sync }}</strong> | Regra: <strong>1 Presença = +1 TKP</strong>
                        </small>
                    </div>

                    {% if session['role'] == 'admin' %}
                    <div class="d-flex flex-wrap align-items-center gap-2">
                        <form method="POST" action="{{ url_for('sync_event_ts3', event_id=selected_event.id) }}" class="d-flex gap-2 align-items-center">
                            <select name="filter_mode" class="form-select form-select-sm bg-dark text-warning fw-semibold border-secondary" style="width: auto;">
                                <option value="all">Todas as Salas (Exceto AFK)</option>
                                <option value="gvg_tw">Somente Salas de GVG + TW</option>
                                <option value="gvg">Somente Salas de GVG (01 a 10)</option>
                                <option value="tw">Somente Salas de TW (Ataque/Defesa)</option>
                                <option value="eventos">Somente Eventos (Artes Marciais/Tigres)</option>
                            </select>
                            <button type="submit" class="btn btn-success btn-sm fw-semibold text-nowrap py-2 px-3">
                                <i class="fa-solid fa-satellite-dish me-1"></i> Puxar Presença TS3
                            </button>
                        </form>
                        <form method="POST" action="{{ url_for('delete_event', event_id=selected_event.id) }}" onsubmit="return confirm('Excluir este evento e todas as presenças dele?');">
                            <button type="submit" class="btn btn-sm btn-outline-danger py-2">
                                <i class="fa-solid fa-trash"></i>
                            </button>
                        </form>
                    </div>
                    {% endif %}
                </div>

                {% if session['role'] == 'admin' %}
                <div class="mb-3">
                    <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#collapsePaste">
                        <i class="fa-solid fa-code me-1"></i> Contingência: Colar JSON da NeoTS ou Lista de Nicks
                    </button>
                    <div class="collapse mt-2" id="collapsePaste">
                        <form method="POST" action="{{ url_for('manual_paste_ts3', event_id=selected_event.id) }}" class="card card-body bg-dark border-secondary p-2">
                            <small class="text-secondary mb-1">
                                Você pode colar aqui tanto o código JSON da rota <code>viewer?_neots_action=view_channels</code> quanto uma lista de nicks:
                            </small>
                            <textarea name="raw_nicks" rows="3" class="form-control form-control-sm bg-black text-light border-secondary mb-2" placeholder='Cole aqui o JSON {"channels": ..., "clients": ...} ou os nicks um por linha'></textarea>
                            <div class="d-flex justify-content-between align-items-center">
                                <select name="filter_mode" class="form-select form-select-sm bg-dark text-light border-secondary" style="width: 240px;">
                                    <option value="all">Filtro: Todas as Salas (Exceto AFK)</option>
                                    <option value="gvg_tw">Filtro: Somente Salas GVG + TW</option>
                                    <option value="gvg">Filtro: Somente GVG</option>
                                    <option value="tw">Filtro: Somente TW</option>
                                </select>
                                <button type="submit" class="btn btn-sm btn-info fw-bold">Processar & Contabilizar (+1 TKP)</button>
                            </div>
                        </form>
                    </div>
                </div>
                {% endif %}

                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Login / Classe</th>
                                <th>Nick Cadastrado (TS3)</th>
                                <th>Status no Evento</th>
                                <th>Nick Lido / Sala no TS3</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">Ação Manual</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in users %}
                            {% set att = event_attendance.get(u.id) %}
                            <tr>
                                <td class="fw-semibold">
                                    {{ u.username }}
                                    <span class="badge cls-badge cls-{{ u.player_class }} ms-1">{{ u.player_class }}</span>
                                </td>
                                <td><span class="badge bg-secondary">{{ u.ts_nick }}</span></td>
                                <td>
                                    {% if att %}
                                        <span class="badge bg-success"><i class="fa-solid fa-check me-1"></i> PRESENTE (+1 TKP)</span>
                                    {% else %}
                                        <span class="badge bg-danger bg-opacity-50 text-light"><i class="fa-solid fa-xmark me-1"></i> AUSENTE</span>
                                    {% endif %}
                                </td>
                                <td class="small text-secondary">
                                    {% if att %}
                                        <strong class="text-light">{{ att.ts_nick_found }}</strong>
                                        <span class="text-info">({{ att.method }})</span>
                                        <span class="text-secondary" style="font-size:0.7rem;">às {{ att.checked_at }}</span>
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
                                            <button type="submit" class="btn btn-sm btn-outline-success py-0 px-2">+ Presença (+1 TKP)</button>
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

    <!-- ==========================================================
         ABA 4: PLAYERS, CLASSES, RESET DE SENHA & CARTEIRA TKP
         ========================================================== -->
    {% if tab == 'players' %}
    <div class="row g-4">
        {% if session['role'] == 'admin' %}
        <div class="col-lg-4">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-user-plus text-success me-1"></i> Cadastrar Player Direto (Já Ativo)</h6>
                <form method="POST" action="{{ url_for('create_user') }}">
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Login de Acesso ao Site</label>
                        <input type="text" name="username" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label small text-secondary mb-1">Senha Inicial</label>
                        <input type="text" name="password" class="form-control form-control-sm bg-dark text-light border-secondary" required>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-8">
                            <label class="form-label small text-secondary mb-1">Nick no TeamSpeak 3</label>
                            <input type="text" name="ts_nick" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="Ex: Sinister" required>
                        </div>
                        <div class="col-4">
                            <label class="form-label small text-warning fw-bold mb-1">Classe</label>
                            <select name="player_class" class="form-select form-select-sm bg-dark text-warning fw-bold border-secondary">
                                {% for cls in pw_classes %}
                                <option value="{{ cls }}">{{ cls }}</option>
                                {% endfor %}
                            </select>
                        </div>
                    </div>
                    <div class="row g-2 mb-3">
                        <div class="col-7">
                            <label class="form-label small text-secondary mb-1">Permissão</label>
                            <select name="role" class="form-select form-select-sm bg-dark text-light border-secondary">
                                <option value="player" selected>Player</option>
                                <option value="admin">Administrador</option>
                            </select>
                        </div>
                        <div class="col-5">
                            <label class="form-label small text-secondary mb-1">TKP Inicial</label>
                            <input type="number" name="bonus_tkp" value="0" class="form-control form-control-sm bg-dark text-warning fw-bold border-secondary">
                        </div>
                    </div>
                    <button type="submit" class="btn btn-success btn-sm w-100 fw-semibold">
                        <i class="fa-solid fa-check me-1"></i> Criar Conta Ativa
                    </button>
                </form>
            </div>
        </div>
        {% endif %}

        <div class="{% if session['role'] == 'admin' %}col-lg-8{% else %}col-lg-12{% endif %}">
            <div class="card p-3">
                <h6 class="fw-bold mb-3"><i class="fa-solid fa-trophy text-warning me-1"></i> Membros Ativos, Classes, Carteira TKP & Reset de Senha</h6>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle table-dark-custom mb-0">
                        <thead>
                            <tr>
                                <th>Login / Cargo</th>
                                <th>Nick TS3 / Classe</th>
                                <th>Carteira TKP</th>
                                <th>Presenças ({{ total_events }} Ev.)</th>
                                <th>Drops</th>
                                {% if session['role'] == 'admin' %}
                                <th class="text-end">TKP / Senha / Excluir</th>
                                {% endif %}
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in users %}
                            <tr>
                                <td>
                                    <div class="fw-bold">{{ u.username }}</div>
                                    {% if u.role == 'admin' %}
                                        <span class="badge bg-primary" style="font-size:0.65rem;">Admin</span>
                                    {% else %}
                                        <span class="badge bg-dark border border-secondary" style="font-size:0.65rem;">Player</span>
                                    {% endif %}
                                    {% if u.reset_status == 'solicitado' %}
                                        <span class="badge bg-warning text-dark ms-1" style="font-size:0.65rem;">Pediu Reset Senha</span>
                                    {% elif u.reset_status == 'aprovado' %}
                                        <span class="badge bg-success ms-1" style="font-size:0.65rem;">Nova Senha Liberada</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <span class="badge bg-secondary">{{ u.ts_nick }}</span>
                                    {% if session['role'] == 'admin' %}
                                    <form method="POST" action="{{ url_for('update_user_class', user_id=u.id) }}" class="d-inline ms-1">
                                        <select name="player_class" onchange="this.form.submit()" class="form-select form-select-sm d-inline-block py-0 px-1 bg-dark text-warning fw-bold border-secondary" style="width: 66px; font-size: 0.75rem;">
                                            {% for cls in pw_classes %}
                                            <option value="{{ cls }}" {% if u.player_class == cls %}selected{% endif %}>{{ cls }}</option>
                                            {% endfor %}
                                        </select>
                                    </form>
                                    {% else %}
                                    <span class="badge cls-badge cls-{{ u.player_class }} ms-1">{{ u.player_class }}</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <span class="badge badge-tkp fs-6"><i class="fa-solid fa-coins me-1"></i>{{ u.tkp_balance }} TKP</span>
                                    <div class="text-secondary" style="font-size:0.7rem;">
                                        Gasto: {{ u.spent_tkp }} | Livre: {{ u.available_to_bid }}
                                    </div>
                                </td>
                                <td>
                                    <div class="d-flex align-items-center gap-2">
                                        <div class="progress flex-grow-1 bg-dark" style="height: 8px; max-width: 80px;">
                                            <div class="progress-bar bg-success" style="width: {{ u.pct }}%;"></div>
                                        </div>
                                        <span class="small fw-semibold">{{ u.presences }} ({{ u.pct }}%)</span>
                                    </div>
                                </td>
                                <td>
                                    <span class="badge bg-warning text-dark fw-bold">{{ u.drops_won }}</span>
                                </td>
                                {% if session['role'] == 'admin' %}
                                <td class="text-end">
                                    <div class="d-flex justify-content-end align-items-center gap-1">
                                        <form method="POST" action="{{ url_for('adjust_user_tkp', user_id=u.id) }}" class="d-inline">
                                            <input type="hidden" name="delta" value="1">
                                            <button type="submit" class="btn btn-sm btn-outline-success py-0 px-2" title="Adicionar +1 TKP">+1</button>
                                        </form>
                                        <form method="POST" action="{{ url_for('adjust_user_tkp', user_id=u.id) }}" class="d-inline">
                                            <input type="hidden" name="delta" value="-1">
                                            <button type="submit" class="btn btn-sm btn-outline-warning py-0 px-2" title="Remover -1 TKP">-1</button>
                                        </form>

                                        {% if u.reset_status != 'aprovado' %}
                                        <form method="POST" action="{{ url_for('approve_password_reset', user_id=u.id) }}" class="d-inline">
                                            <button type="submit" class="btn btn-sm btn-outline-info py-0 px-2" title="Liberar para este player criar uma nova senha ao digitar o login">
                                                <i class="fa-solid fa-key"></i> Reset
                                            </button>
                                        </form>
                                        {% else %}
                                        <form method="POST" action="{{ url_for('cancel_password_reset', user_id=u.id) }}" class="d-inline">
                                            <button type="submit" class="btn btn-sm btn-success py-0 px-2" title="Reset já liberado! Clique se quiser cancelar">
                                                <i class="fa-solid fa-unlock"></i> Liberado
                                            </button>
                                        </form>
                                        {% endif %}

                                        {% if u.id != session['user_id'] %}
                                        <form method="POST" action="{{ url_for('delete_user', user_id=u.id) }}" class="d-inline" onsubmit="return confirm('Excluir o jogador {{ u.username }}?');">
                                            <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">
                                                <i class="fa-solid fa-user-xmark"></i>
                                            </button>
                                        </form>
                                        {% endif %}
                                    </div>
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

    <!-- ==========================================================
         ABA 5: CONFIGURAR INTEGRAÇÃO NEO TEAMSPEAK (API CANAIS)
         ========================================================== -->
    {% if tab == 'tsconfig' and session['role'] == 'admin' %}
    <div class="row justify-content-center">
        <div class="col-lg-8">
            <div class="card p-4">
                <h5 class="fw-bold mb-1">
                    <i class="fa-solid fa-satellite-dish text-primary me-2"></i> Integração API NEO TEAMSPEAK (Estrutura de Canais)
                </h5>
                <p class="text-secondary small mb-4">
                    Conecta direto na rota JSON da NEO TEAMSPEAK (serviço 383 - Arsenal PW) para ler todas as salas (GVG, TW, Eventos e Bate-Papo) sem depender da porta 10011.
                </p>

                <form method="POST" action="{{ url_for('save_tsconfig') }}">
                    <div class="mb-3">
                        <label class="form-label small text-secondary">URL da API de Canais da NEO TEAMSPEAK</label>
                        <input type="text" name="neots_api_url" value="{{ ts_cfg.neots_api_url or 'https://neots.com.br/wp-json/neots/v1/service/383/viewer?_neots_action=view_channels&force=1' }}" class="form-control bg-dark text-info border-secondary" required>
                    </div>

                    <div class="row g-3 mb-3">
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">E-mail / Login do Painel NeoTS (Opcional)</label>
                            <input type="text" name="query_user" value="{{ ts_cfg.query_user }}" class="form-control bg-dark text-light border-secondary" placeholder="Seu login no site neots.com.br">
                        </div>
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Senha do Painel NeoTS (Opcional)</label>
                            <input type="password" name="query_pass" value="{{ ts_cfg.query_pass }}" class="form-control bg-dark text-light border-secondary" placeholder="Sua senha no site neots.com.br">
                        </div>
                    </div>

                    <div class="mb-3">
                        <label class="form-label small text-secondary">Cookie da Sessão NeoTS</label>
                        <input type="text" name="neots_cookie" value="{{ ts_cfg.neots_cookie or '' }}" class="form-control form-control-sm bg-dark text-light border-secondary" placeholder="wordpress_logged_in_...">
                    </div>

                    <div class="row g-3 mb-4">
                        <div class="col-md-6">
                            <label class="form-label small text-secondary">Ignorar Salas com estes Nomes (separados por vírgula)</label>
                            <input type="text" name="ignore_channels" value="{{ ts_cfg.ignore_channels }}" class="form-control bg-dark text-light border-secondary" placeholder="AFK,Ausente">
                        </div>
                        <div class="col-md-3">
                            <label class="form-label small text-secondary">IP do TS3</label>
                            <input type="text" name="host" value="{{ ts_cfg.host }}" class="form-control bg-dark text-secondary border-secondary">
                        </div>
                        <div class="col-md-3">
                            <label class="form-label small text-secondary">Porta de Voz</label>
                            <input type="number" name="voice_port" value="{{ ts_cfg.voice_port }}" class="form-control bg-dark text-secondary border-secondary">
                        </div>
                    </div>

                    <div class="d-flex gap-2">
                        <button type="submit" name="action" value="save" class="btn btn-primary fw-semibold">
                            <i class="fa-solid fa-floppy-disk me-1"></i> Salvar Configurações
                        </button>
                        <button type="submit" name="action" value="test" class="btn btn-outline-info fw-semibold">
                            <i class="fa-solid fa-plug-circle-check me-1"></i> Salvar e Testar Leitura da API Agora
                        </button>
                    </div>
                </form>
            </div>
        </div>
    </div>
    {% endif %}

    <!-- ==========================================================
         ABA 6: MEU PERFIL (COM CLASSE)
         ========================================================== -->
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
                    <div class="row g-2 mb-3">
                        <div class="col-8">
                            <label class="form-label small text-secondary">Meu Nick no TeamSpeak 3</label>
                            <input type="text" name="ts_nick" value="{{ session['ts_nick'] }}" class="form-control bg-dark text-light border-secondary" required>
                        </div>
                        <div class="col-4">
                            <label class="form-label small text-warning fw-bold">Minha Classe</label>
                            <select name="player_class" class="form-select bg-dark text-warning fw-bold border-secondary">
                                {% for cls in pw_classes %}
                                <option value="{{ cls }}" {% if session.get('player_class', 'WR') == cls %}selected{% endif %}>{{ cls }}</option>
                                {% endfor %}
                            </select>
                        </div>
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
    const PW_CLASSES_LIST = ["WR", "MG", "WF", "WB", "EA", "EP", "MC", "PSY", "MS", "SK"];

    // Mapa equivalente ao PROCV (VLOOKUP) da aba BASE DE CONTAS do Excel
    const PLAYER_INFO_MAP = {
        {% for u in users %}
        "{{ u.ts_nick | lower | replace('\"', '') }}": {"cls": "{{ u.player_class }}", "pilot": ""},
        "{{ u.username | lower | replace('\"', '') }}": {"cls": "{{ u.player_class }}", "pilot": ""},
        {% endfor %}
        {% for ex in tw_extra_players %}
        "{{ ex.nick | lower | replace('\"', '') }}": {"cls": "{{ ex.player_class }}", "pilot": "{{ (ex.pilot_note or '') | replace('\"', '') }}"},
        {% endfor %}
    };

    function autoFillTwSlot(inputEl, pilotInputId, selectId) {
        const val = (inputEl.value || "").trim().toLowerCase();
        const selectEl = document.getElementById(selectId);
        const pilotEl = document.getElementById(pilotInputId);
        if (val && PLAYER_INFO_MAP[val]) {
            if (selectEl && PLAYER_INFO_MAP[val].cls) {
                selectEl.value = PLAYER_INFO_MAP[val].cls;
            }
            if (pilotEl && !pilotEl.value && PLAYER_INFO_MAP[val].pilot) {
                pilotEl.value = PLAYER_INFO_MAP[val].pilot;
            }
        } else if (!val && selectEl) {
            selectEl.value = "";
        }
    }

    // PROCV Automático + Recálculo de SUBTOTAL e CONT.SE (COUNTIF) na Visão Planilha Geral
    function onMatrixNickInput(nickInputEl, classSelectId, pilotInputId) {
        const val = (nickInputEl.value || "").trim().toLowerCase();
        const selectEl = document.getElementById(classSelectId);
        const pilotEl = document.getElementById(pilotInputId);

        if (val && PLAYER_INFO_MAP[val]) {
            if (selectEl && PLAYER_INFO_MAP[val].cls) {
                selectEl.value = PLAYER_INFO_MAP[val].cls;
            }
            if (pilotEl && !pilotEl.value && PLAYER_INFO_MAP[val].pilot) {
                pilotEl.value = PLAYER_INFO_MAP[val].pilot;
            }
        } else if (!val && selectEl) {
            selectEl.value = "";
        }
        recalcTwMatrix();
    }

    function recalcTwMatrix() {
        const attackCounts = {};
        const defenseCounts = {};
        const totalByClass = {};
        PW_CLASSES_LIST.forEach(c => {
            attackCounts[c] = 0;
            defenseCounts[c] = 0;
            totalByClass[c] = 0;
        });

        const ptCols = document.querySelectorAll('.tw-matrix-col');
        ptCols.forEach(col => {
            const ptId = col.getAttribute('data-pt-id');
            const side = col.getAttribute('data-pt-side');
            const nickInputs = col.querySelectorAll('.matrix-nick-input');
            let ptFilled = 0;

            nickInputs.forEach(inp => {
                const nickVal = (inp.value || "").trim();
                const selectId = inp.id.replace('mNick_', 'mClass_');
                const clsEl = document.getElementById(selectId);
                const clsVal = clsEl ? (clsEl.value || "").trim().toUpperCase() : "";

                if (nickVal) {
                    ptFilled += 1;
                    if (PW_CLASSES_LIST.includes(clsVal)) {
                        totalByClass[clsVal] += 1;
                        if (side === 'DEFESA') {
                            defenseCounts[clsVal] += 1;
                        } else {
                            attackCounts[clsVal] += 1;
                        }
                    }
                }
            });

            const ptCountBadge = document.getElementById('ptCount_' + ptId);
            if (ptCountBadge) {
                ptCountBadge.textContent = ptFilled;
            }
        });

        let sumAttack = 0;
        let sumDefense = 0;
        PW_CLASSES_LIST.forEach(c => {
            sumAttack += attackCounts[c];
            sumDefense += defenseCounts[c];

            const atkCell = document.getElementById('qtyAttack_' + c);
            if (atkCell) atkCell.textContent = attackCounts[c];

            const defCell = document.getElementById('qtyDefense_' + c);
            if (defCell) defCell.textContent = defenseCounts[c];

            document.querySelectorAll('.live-total-cls-' + c).forEach(el => {
                el.textContent = totalByClass[c];
            });
        });

        const atkTotEl = document.getElementById('qtyAttackTotal');
        if (atkTotEl) atkTotEl.textContent = sumAttack;

        const defTotEl = document.getElementById('qtyDefenseTotal');
        if (defTotEl) defTotEl.textContent = sumDefense;

        const bannerAtk = document.getElementById('bannerAttackTotal');
        if (bannerAtk) bannerAtk.textContent = sumAttack;

        const bannerDef = document.getElementById('bannerDefenseTotal');
        if (bannerDef) bannerDef.textContent = sumDefense;

        const topAtk = document.getElementById('topAttackTotal');
        if (topAtk) topAtk.textContent = sumAttack;

        const topDef = document.getElementById('topDefenseTotal');
        if (topDef) topDef.textContent = sumDefense;

        const grandTot = document.getElementById('grandTotalTwBadge');
        if (grandTot) grandTot.textContent = (sumAttack + sumDefense);
    }

    function syncDropdownTkp(selectEl) {
        const opt = selectEl.options[selectEl.selectedIndex];
        const tkp = opt ? opt.getAttribute('data-tkp') : '';
        const input = document.getElementById('dropdownTkpInput');
        if (input && tkp) {
            input.value = tkp;
        }
    }

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
