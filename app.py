"""Balans : prototype d'autorégulation numérique collaborative.

Lancement : streamlit run app.py
Les données sont stockées localement dans data/balans.db (non versionné).
Le style reprend celui du prototype HTML (palette indigo/lavande, polices Fraunces et
Outfit). Le thème de base est dans .streamlit/config.toml, le reste dans CSS ci-dessous.
"""
import sqlite3
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import streamlit as st

DB_PATH = Path("data/balans.db")
APPS = ["🎮 Jeux vidéo", "💬 Réseaux sociaux", "📺 Streaming", "🌐 Navigateur", "✉️ Messagerie"]
GOAL_OTHER = "✨ Autre"
GOALS = ["📚 Devoirs", "🍽️ Repas", "⚽ Sport", "😴 Sommeil", "👨‍👩‍👧 Famille", GOAL_OTHER]
AVATARS = ["🦊", "🐺", "🦉", "🐙", "🦄", "🐢"]
LEVELS = ["Explorateur", "Régulier", "Ancré", "Stratège", "Maître de son temps"]
STATUS = {"active": "En cours", "completed": "Menée à terme", "stopped": "Arrêtée avant la fin"}
DAY_LETTERS = ["L", "M", "M", "J", "V", "S", "D"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apps TEXT, goal TEXT, start TEXT, duration_min INTEGER,
    status TEXT, ended_at TEXT, xp INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, apps TEXT, slot TEXT, proposed_by TEXT,
    teen_ok INTEGER DEFAULT 0, parent_ok INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


# ---------- Base de données ----------
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def query(sql, params=()):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def execute(sql, params=()):
    con = connect()
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def get_setting(key, default):
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------- Logique métier ----------
def now():
    return datetime.now().replace(microsecond=0)


def planned_end(s):
    return datetime.fromisoformat(s["start"]) + timedelta(minutes=s["duration_min"])


def active_session():
    rows = query("SELECT * FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def start_session(apps, goal, duration):
    execute(
        "INSERT INTO sessions(apps, goal, start, duration_min, status) VALUES(?,?,?,?, 'active')",
        (", ".join(apps), goal, now().isoformat(), duration),
    )


def stop_session(session_id):
    execute(
        "UPDATE sessions SET status='stopped', ended_at=? WHERE id=?",
        (now().isoformat(), session_id),
    )


def close_expired():
    """Termine les sessions dont la durée est écoulée et attribue l'XP."""
    for s in query("SELECT * FROM sessions WHERE status='active'"):
        if now() >= planned_end(s):
            xp = 10 + s["duration_min"] // 5
            execute(
                "UPDATE sessions SET status='completed', ended_at=?, xp=? WHERE id=?",
                (planned_end(s).isoformat(), xp, s["id"]),
            )


def held_minutes(s):
    end = datetime.fromisoformat(s["ended_at"]) if s["ended_at"] else now()
    return round((end - datetime.fromisoformat(s["start"])).total_seconds() / 60)


def rule_is_active(r):
    # Une règle n'est valable que si l'adolescent ET le parent sont d'accord.
    return bool(r["teen_ok"] and r["parent_ok"])


def level_info(xp):
    lvl = xp // 100
    return lvl + 1, LEVELS[min(lvl, len(LEVELS) - 1)], xp % 100


def compute_badges(done, active_rules):
    """Retourne tous les badges : (nom, description, obtenu ?)."""
    return [
        ("Premier engagement", "Une première session menée à terme", len(done) >= 1),
        ("Régularité", "5 sessions menées à terme", len(done) >= 5),
        ("Concentration", "Une session d'au moins 1 h", any(s["duration_min"] >= 60 for s in done)),
        ("Constance", "Des sessions sur 3 jours différents", len({s["start"][:10] for s in done}) >= 3),
        ("Co-constructeur", "Une règle définie à deux", bool(active_rules)),
    ]


# ---------- Style (repris du prototype HTML) ----------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Outfit:wght@300;400;500;600;700&display=swap');
:root{
  --cream:#EDEFFA; --cream-2:#DFE3F4; --ink:#262444; --ink-soft:#5E5C82;
  --teal:#4F4CC0; --teal-deep:#37339B; --sage:#7FA6E6; --sage-soft:#D8E1F8;
  --coral:#8E7CE0; --coral-soft:#E0D7F7; --amber:#F0B84E; --sky:#A9C8F0;
  --line:rgba(38,36,68,.10); --shadow:0 18px 40px -18px rgba(55,51,155,.45);
}
.stApp{
  color:var(--ink);
  background:
    radial-gradient(1200px 600px at 15% -5%, #f3f1fc 0%, transparent 60%),
    radial-gradient(900px 700px at 110% 10%, #e3e9fa 0%, transparent 55%),
    linear-gradient(160deg,#e7e6f7 0%, #dde0f3 100%);
}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"],[data-testid="stDecoration"],footer,#MainMenu{display:none !important}
.block-container{max-width:780px;padding:2rem 1rem 4rem}
.stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp li{
  font-family:'Outfit',sans-serif;
}

/* ---- En-tête ---- */
.brand{display:flex;align-items:center;gap:14px}
.logo{width:48px;height:48px;flex:0 0 48px;border-radius:16px;position:relative;overflow:hidden;
  background:linear-gradient(150deg,var(--teal) 0%, var(--sage) 130%);
  display:grid;place-items:center;box-shadow:var(--shadow)}
.logo:before{content:"";position:absolute;width:22px;height:22px;border-radius:50%;
  background:var(--amber);top:8px;right:7px;opacity:.9}
.logo svg{position:relative;z-index:2}
.stApp .brand h1{font-family:'Fraunces',serif;font-weight:600;font-size:30px;letter-spacing:-.5px;
  line-height:1;padding:0;margin:0;color:var(--ink)}
.brand p{font-size:13px;color:var(--ink-soft);margin:3px 0 0;line-height:1.3}

/* ---- Sélecteur de profil (toggle) ---- */
.st-key-role_toggle{display:flex;justify-content:flex-end}
.st-key-role_toggle [role="radiogroup"]{display:inline-flex;gap:0;background:#fff;border-radius:40px;
  padding:5px;border:1px solid var(--line);box-shadow:0 6px 20px -10px rgba(55,51,155,.4)}
.st-key-role_toggle label[data-testid="stRadioOption"]{margin:0;padding:9px 18px;border-radius:30px;
  cursor:pointer;transition:.25s}
.st-key-role_toggle label[data-testid="stRadioOption"] > div > div:first-child{display:none}
.st-key-role_toggle label[data-testid="stRadioOption"] p{margin:0;font-weight:600;font-size:14px;color:var(--ink-soft)}
.st-key-role_toggle label[data-selected="true"]{background:var(--teal);box-shadow:0 8px 18px -8px var(--teal)}
.st-key-role_toggle label[data-selected="true"] p{color:#fff}

/* ---- Onglets = contrôle segmenté ---- */
.stApp [role="tablist"]{background:var(--cream-2);border-radius:30px;padding:4px;gap:0;border:none !important;
  box-shadow:none}
.stApp [data-testid="stTabs"] > div, .stApp [data-testid="stTabs"] > div::after{border-bottom:none !important}
.stApp [data-testid="stTab"]{flex:1;justify-content:center;height:auto;padding:9px 6px;border-radius:24px;
  background:none;border:none}
.stApp [data-testid="stTab"] p{font-size:13px;font-weight:600;color:var(--ink-soft)}
.stApp [data-testid="stTab"][aria-selected="true"]{background:#fff;box-shadow:0 4px 12px -6px var(--teal)}
.stApp [data-testid="stTab"][aria-selected="true"] p{color:var(--teal)}
.stApp [role="tablist"] .react-aria-SelectionIndicator, .stApp [data-baseweb="tab-highlight"],
.stApp [data-baseweb="tab-border"]{display:none}

/* ---- Typographie ---- */
.eyebrow{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--teal);
  font-weight:700;margin:22px 0 6px}
.stApp .h-title{font-family:'Fraunces',serif;font-weight:600;font-size:26px;letter-spacing:-.5px;line-height:1.1}
.stApp .sub{font-size:13.5px;color:var(--ink-soft);line-height:1.45;margin:6px 0 0}
.hint{font-size:12px;color:var(--ink-soft);text-align:center;margin-top:28px}
.hint b{color:var(--teal)}

/* ---- Cartes ---- */
.card, div[class*="st-key-card_"]{background:#fff;border-radius:22px;padding:18px;
  box-shadow:0 12px 28px -20px rgba(55,51,155,.5);border:1px solid var(--line);margin-top:14px}
div[class*="st-key-card_"]{gap:.9rem}
div[class*="st-key-card_"] .h-title{margin-bottom:4px}
.card.teal{background:linear-gradient(150deg,var(--teal) 0%, var(--teal-deep) 120%);color:#fff;border:none}
.card.teal .eyebrow{color:#d9d8ff;margin-top:0}
.card.coral{background:linear-gradient(150deg,#9b8ae6,#8E7CE0);color:#fff;border:none}
.big{font-family:'Fraunces',serif;font-size:46px;font-weight:600;letter-spacing:-1px;line-height:1.05;margin:6px 0 12px}
.bar{height:8px;background:rgba(255,255,255,.22);border-radius:10px;overflow:hidden;margin-bottom:14px}
.bar i{display:block;height:100%;background:#fff;border-radius:10px}

.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;
  background:var(--sage-soft);color:var(--teal-deep);padding:5px 11px;border-radius:30px;margin:0 6px 6px 0}
.pill.coral{background:var(--coral-soft);color:#5b4b9e}
.pill.muted{background:var(--cream-2);color:var(--ink-soft)}
.card.teal .pill{background:rgba(255,255,255,.18);color:#fff}
.tag{font-size:9px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;padding:3px 8px;border-radius:20px}
.tag.ado{background:var(--sage-soft);color:var(--teal-deep)}
.tag.par{background:var(--coral-soft);color:#5b4b9e}

.row{display:flex;gap:10px;margin-top:14px}
.stat{flex:1;background:#fff;border-radius:18px;padding:14px;border:1px solid var(--line);text-align:center}
.stat b{font-family:'Fraunces',serif;font-size:22px;display:block;line-height:1}
.stat span{font-size:11px;color:var(--ink-soft)}

.ring{position:relative;width:130px;height:130px;margin:6px auto 0}
.ring svg{transform:rotate(-90deg)}
.ring .center{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.ring .center b{font-family:'Fraunces',serif;font-size:30px;font-weight:600;line-height:1}
.ring .center small{font-size:11px;color:var(--ink-soft)}

.bars{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;height:110px;margin-top:12px}
.bars .col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:6px;height:100%}
.bars .col i{display:block;width:100%;max-width:34px;border-radius:10px 10px 6px 6px;
  background:linear-gradient(180deg,var(--sage),var(--teal))}
.bars .col i.empty{height:6px;background:var(--cream-2)}
.bars .col span{font-size:11px;color:var(--ink-soft);font-weight:600}
.bars .col.today span{color:var(--teal)}

.timeline{display:flex;flex-direction:column;margin-top:6px}
.tl-item{display:flex;gap:12px;padding:11px 0}
.tl-dot{width:14px;height:14px;border-radius:50%;background:var(--sage);margin-top:4px;flex:0 0 14px;position:relative}
.tl-item:not(:last-child) .tl-dot:after{content:"";position:absolute;left:50%;top:16px;
  width:2px;height:40px;background:var(--line);transform:translateX(-50%)}
.tl-dot.stopped{background:var(--coral-soft)} .tl-dot.active{background:var(--teal)}
.tl-item b{font-size:14px;display:block}
.tl-item small{font-size:12px;color:var(--ink-soft);display:block;margin-top:2px}

.profile{display:flex;align-items:center;gap:14px}
.avatar{width:64px;height:64px;border-radius:22px;display:grid;place-items:center;font-size:34px;
  background:linear-gradient(150deg,var(--sage-soft),var(--coral-soft))}
.xpbar{height:8px;background:var(--cream-2);border-radius:10px;overflow:hidden;margin:14px 0 4px}
.xpbar i{display:block;height:100%;background:var(--teal);border-radius:10px}
.opt{display:flex;align-items:center;gap:12px;background:#fff;border:1.5px solid var(--line);
  border-radius:16px;padding:12px 14px;margin-top:10px}
.opt .emoji{font-size:22px;width:40px;height:40px;display:grid;place-items:center;
  background:var(--cream);border-radius:12px;flex:0 0 40px}
.opt b{font-size:14px;display:block}
.opt small{font-size:11.5px;color:var(--ink-soft)}
.opt.locked{opacity:.5}

/* ---- Widgets Streamlit ---- */
.stApp [data-testid="stWidgetLabel"] p{font-size:13px;font-weight:600;color:var(--ink)}
.stApp [data-testid="stElementContainer"]:has(.stButton){width:100% !important}
.stApp .stButton{width:100%;margin-top:8px}
.stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-primaryFormSubmit"]{
  width:100%;border:none;border-radius:18px;padding:.85rem 1rem;font-weight:600;font-size:15px;
  background:var(--teal);color:#fff;box-shadow:0 14px 26px -14px var(--teal)}
.stApp [data-testid="stBaseButton-secondary"], .stApp [data-testid="stBaseButton-secondaryFormSubmit"]{
  width:100%;border-radius:18px;padding:.8rem 1rem;font-weight:600;font-size:15px;
  background:transparent;color:var(--teal);border:1.5px solid var(--teal)}
.stApp [data-testid="stBaseButton-secondary"]:hover{background:var(--sage-soft);color:var(--teal-deep)}
.stApp [data-testid="stTextInputRootElement"]{border-radius:14px;background:#fff;border:1.5px solid var(--line);
  box-shadow:none}
.stApp [data-testid="stTextInputRootElement"] input{border:none;background:transparent}
[data-testid="stForm"]{border:none;padding:0}

/* choix en pastilles (st.pills) = cartes "pick" du prototype */
.stApp button[data-variant="pills"], .stApp button[data-variant="pillsActive"]{
  border-radius:16px;padding:.55rem .9rem;background:#fff;border:1.5px solid var(--line);
  color:var(--ink);font-weight:500;transition:.18s}
.stApp button[data-variant="pills"]:hover{border-color:var(--sage);transform:translateY(-1px)}
.stApp button[aria-pressed="true"]{border-color:var(--teal);background:#f0eefb;
  box-shadow:0 0 0 3px var(--sage-soft);color:var(--teal-deep)}

/* curseur de durée : piste dégradée comme dans le prototype */
.stApp [data-testid="stSlider"] div[style*="touch-action"] > div:first-child{
  height:8px;border-radius:8px;background:linear-gradient(90deg,var(--sky),var(--sage),var(--coral)) !important}
</style>
"""

BRAND = """<div class="brand"><div class="logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M12 3v18M5 8c3 0 4 2 7 2s4-2 7-2" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="5" cy="8" r="2.2" fill="#fff"/><circle cx="19" cy="8" r="2.2" fill="#fff"/></svg></div><div><h1>Balans</h1><p>Co-décider le temps d'écran, ensemble</p></div></div>"""


def esc(value):
    """Échappe le texte saisi par l'utilisateur avant de l'insérer dans du HTML."""
    return escape(str(value)).replace("$", "&#36;")


def html(content):
    st.markdown(content, unsafe_allow_html=True)


def heading(eyebrow, title, sub=""):
    sub_html = f'<p class="sub">{esc(sub)}</p>' if sub else ""
    html(f'<div class="eyebrow">{esc(eyebrow)}</div><div class="h-title">{esc(title)}</div>{sub_html}')


def pills_html(items, css=""):
    return "".join(f'<span class="pill {css}">{esc(i)}</span>' for i in items)


def clock(remaining):
    total = int(remaining.total_seconds())
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------- Interface ----------
st.set_page_config(page_title="Balans", page_icon="⚖️", initial_sidebar_state="collapsed")
html(CSS)
close_expired()

col_brand, col_toggle = st.columns([3, 2], vertical_alignment="center")
with col_brand:
    html(BRAND)
with col_toggle:
    with st.container(key="role_toggle"):
        role = st.radio("Profil", ["Profil ado", "Profil parent"], horizontal=True, label_visibility="collapsed")
is_teen = role == "Profil ado"
who = "teen" if is_teen else "parent"
nickname = get_setting("nickname", "Adolescent")

tab_session, tab_board, tab_rules, tab_progress = st.tabs(["Session", "Tableau", "Règles", "Progression"])


@st.fragment(run_every=1)
def live_session():
    close_expired()
    s = active_session()
    if not s:
        st.rerun()
        return
    remaining = max(planned_end(s) - now(), timedelta(0))
    pct = min(100, max(0, 100 * (1 - remaining.total_seconds() / (s["duration_min"] * 60))))
    html(
        '<div class="card teal"><div class="eyebrow">Session en cours</div>'
        f'<div class="h-title">{esc(s["goal"])}</div>'
        f'<div class="big">{clock(remaining)}</div>'
        f'<div class="bar"><i style="width:{pct:.0f}%"></i></div>'
        f'{pills_html(s["apps"].split(", "))}</div>'
    )


# --- Onglet Session ---
with tab_session:
    s = active_session()
    if s:
        heading(
            f"Salut {nickname} 👋" if is_teen else "Bonjour 👋",
            "Tu tiens ton plan" if is_teen else "Une session est en cours",
        )
        live_session()
        if is_teen:
            if st.button("Arrêter maintenant"):
                stop_session(s["id"])
                st.rerun()
        else:
            html('<p class="sub">Seul l\'adolescent peut activer ou arrêter une session.</p>')
    elif is_teen:
        heading(f"Salut {nickname} 👋", "Commencer une session", "Tu choisis, tu décides de la durée.")
        with st.container(key="card_start"):
            apps = st.pills("Applications à mettre en pause", APPS, selection_mode="multi")
            other = st.text_input("Autre application (facultatif)")
            goal = st.pills("Objectif de la session", GOALS)
            if goal == GOAL_OTHER:
                goal = st.text_input("Précisez l'objectif") or GOAL_OTHER
            duration = st.slider("Durée", 15, 180, 45, step=5, format="%d min")
            if st.button("Activer la pause", type="primary"):
                chosen = list(apps or []) + ([other.strip()] if other.strip() else [])
                if not chosen:
                    st.warning("Sélectionnez au moins une application.")
                elif not goal:
                    st.warning("Choisissez un objectif.")
                else:
                    start_session(chosen, goal, duration)
                    st.rerun()
    else:
        heading("Bonjour 👋", "Comment se passe la semaine ?")
        html(
            '<div class="card"><div class="h-title" style="font-size:20px">🔒 Aucune session en cours</div>'
            '<p class="sub">Seul l\'adolescent peut activer une pause : c\'est un choix volontaire, '
            "jamais une surprise. Retrouvez l'historique dans l'onglet Tableau.</p></div>"
        )

# --- Onglet Tableau partagé ---
with tab_board:
    heading("Tableau partagé", "Temps activés volontairement",
            "Identique pour l'adolescent et le parent : un support de discussion, ni surveillance ni sanction.")
    sessions = query("SELECT * FROM sessions ORDER BY start DESC")
    if not sessions:
        html('<div class="card"><p class="sub" style="margin:0">Aucune session pour l\'instant.</p></div>')
    else:
        n_done = sum(x["status"] == "completed" for x in sessions)
        n_stop = sum(x["status"] == "stopped" for x in sessions)
        rate = round(100 * n_done / (n_done + n_stop)) if (n_done + n_stop) else 0
        circ = 2 * 3.14159 * 52
        html(
            '<div class="card"><div class="eyebrow" style="margin-top:0">Engagements tenus</div>'
            '<div class="ring"><svg width="130" height="130" viewBox="0 0 130 130">'
            '<circle cx="65" cy="65" r="52" fill="none" stroke="#DFE3F4" stroke-width="12"/>'
            f'<circle cx="65" cy="65" r="52" fill="none" stroke="#4F4CC0" stroke-width="12" stroke-linecap="round" '
            f'stroke-dasharray="{circ * rate / 100:.1f} {circ:.1f}"/></svg>'
            f'<div class="center"><b>{rate}%</b><small>des sessions</small></div></div></div>'
            '<div class="row">'
            f'<div class="stat"><b>{len(sessions)}</b><span>Sessions</span></div>'
            f'<div class="stat"><b>{n_done}</b><span>Menées à terme</span></div>'
            f'<div class="stat"><b>{n_stop}</b><span>Arrêtées avant la fin</span></div></div>'
        )

        days = [(now() - timedelta(days=i)).date() for i in range(6, -1, -1)]
        minutes = {d: 0 for d in days}
        for x in sessions:
            d = datetime.fromisoformat(x["start"]).date()
            if d in minutes:
                minutes[d] += held_minutes(x)
        peak = max(minutes.values()) or 1
        cols = "".join(
            f'<div class="col{" today" if d == days[-1] else ""}">'
            + (f'<i style="height:{max(14, 78 * m / peak):.0f}px"></i>' if m else '<i class="empty"></i>')
            + f"<span>{DAY_LETTERS[d.weekday()]}</span></div>"
            for d, m in minutes.items()
        )
        html(
            '<div class="card"><div class="eyebrow" style="margin-top:0">7 derniers jours</div>'
            f'<div class="bars">{cols}</div>'
            '<p class="sub">Minutes de pause volontaire. Les jours sans barre sont des périodes libres.</p></div>'
        )

        items = ""
        for x in sessions[:8]:
            dot = {"active": "active", "stopped": "stopped"}.get(x["status"], "")
            when = x["start"].replace("T", " ")[5:16]
            items += (
                f'<div class="tl-item"><div class="tl-dot {dot}"></div><div>'
                f'<b>{esc(x["goal"])}</b>'
                f'<small>{when} · {held_minutes(x)} / {x["duration_min"]} min · {STATUS[x["status"]]}</small>'
                f'<small>{esc(x["apps"])}</small></div></div>'
            )
        html(f'<div class="card"><div class="eyebrow" style="margin-top:0">Historique</div><div class="timeline">{items}</div></div>')

# --- Onglet Règles communes ---
with tab_rules:
    heading("Co-décision", "Règles co-construites" if not is_teen else "Nos règles, à deux",
            "Une règle n'est valable que lorsque l'adolescent et le parent l'ont tous deux approuvée.")
    my_col = "teen_ok" if is_teen else "parent_ok"
    for r in query("SELECT * FROM rules ORDER BY id DESC"):
        active = rule_is_active(r)
        with st.container(key=f"card_rule_{r['id']}"):
            tag = '<span class="tag ado">Proposée par l\'ado</span>' if r["proposed_by"] == "teen" \
                else '<span class="tag par">Proposée par le parent</span>'
            state = '<span class="pill">Active</span>' if active else '<span class="pill coral">En discussion</span>'
            ado_pill = f'<span class="pill {"" if r["teen_ok"] else "muted"}">Ado {"✓" if r["teen_ok"] else "en attente"}</span>'
            par_pill = f'<span class="pill {"" if r["parent_ok"] else "muted"}">Parent {"✓" if r["parent_ok"] else "en attente"}</span>'
            html(
                f'<div><div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">'
                f'<div class="h-title" style="font-size:19px">{esc(r["title"])}</div>{state}</div>'
                f'<p class="sub">{esc(r["apps"])} · {esc(r["slot"])}</p>'
                f'<div style="margin-top:10px">{tag}</div>'
                f'<div style="margin-top:10px">{ado_pill}{par_pill}</div></div>'
            )
            c1, c2 = st.columns(2)
            if r[my_col]:
                if c1.button("Retirer mon accord", key=f"withdraw{r['id']}"):
                    execute(f"UPDATE rules SET {my_col}=0 WHERE id=?", (r["id"],))
                    st.rerun()
            else:
                if c1.button("Approuver", key=f"approve{r['id']}", type="primary"):
                    execute(f"UPDATE rules SET {my_col}=1 WHERE id=?", (r["id"],))
                    st.rerun()
            if not active:
                if c2.button("Supprimer", key=f"delete{r['id']}"):
                    execute("DELETE FROM rules WHERE id=?", (r["id"],))
                    st.rerun()

    with st.container(key="card_newrule"):
        html('<div class="h-title" style="font-size:19px">Proposer une règle</div>')
        with st.form("new_rule", clear_on_submit=True):
            title = st.text_input("Intitulé", placeholder="Pas d'écran pendant les repas")
            rule_apps = st.pills("Applications concernées", APPS, selection_mode="multi")
            slot = st.text_input("Créneau", placeholder="Tous les jours 19h-20h")
            if st.form_submit_button("Proposer", type="primary"):
                if not title.strip() or not rule_apps:
                    st.warning("Indiquez un intitulé et au moins une application.")
                else:
                    execute(
                        "INSERT INTO rules(title, apps, slot, proposed_by, teen_ok, parent_ok) "
                        "VALUES(?,?,?,?,?,?)",
                        (title.strip(), ", ".join(rule_apps), slot.strip() or "Non précisé",
                         who, int(is_teen), int(not is_teen)),
                    )
                    st.rerun()

# --- Onglet Progression ---
with tab_progress:
    finished = query("SELECT * FROM sessions WHERE status!='active' ORDER BY start DESC")
    done = [x for x in finished if x["status"] == "completed"]
    xp = sum(x["xp"] for x in finished)
    level, level_name, xp_in_level = level_info(xp)
    avatar = get_setting("avatar", AVATARS[0])

    heading("Progression", "Ton parcours" if is_teen else "Son parcours")
    html(
        f'<div class="card"><div class="profile"><div class="avatar">{esc(avatar)}</div><div>'
        f'<div class="h-title" style="font-size:22px">{esc(nickname)}</div>'
        f'<span class="pill" style="margin-top:6px">Niveau {level} · {esc(level_name)}</span></div></div>'
        f'<div class="xpbar"><i style="width:{xp_in_level}%"></i></div>'
        f'<p class="sub" style="margin:0">{xp} XP · encore {100 - xp_in_level} XP avant le niveau suivant</p>'
        '<p class="sub">L\'XP récompense les engagements menés à terme. Arrêter plus tôt ne retire '
        "aucun point : c'est une information, pas une sanction.</p></div>"
    )

    if is_teen:
        with st.container(key="card_profile"):
            html('<div class="h-title" style="font-size:19px">Personnaliser</div>')
            choice = st.pills("Avatar", AVATARS, default=avatar)
            new_name = st.text_input("Pseudo", value=nickname)
            if choice and choice != avatar:
                set_setting("avatar", choice)
                st.rerun()
            if new_name.strip() and new_name.strip() != nickname:
                set_setting("nickname", new_name.strip())
                st.rerun()

    html('<div class="eyebrow">Badges</div>')
    active_rules = [r for r in query("SELECT * FROM rules") if rule_is_active(r)]
    for name, desc, earned in compute_badges(done, active_rules):
        html(
            f'<div class="opt{"" if earned else " locked"}"><div class="emoji">{"🏅" if earned else "🔒"}</div>'
            f"<div><b>{esc(name)}</b><small>{esc(desc)}</small></div></div>"
        )

html(
    '<div class="hint"><b>Prototype</b> · le blocage réel des applications n\'est pas encore branché : '
    "les sessions sont enregistrées et partagées, rien n'est bloqué sur l'appareil.</div>"
)
