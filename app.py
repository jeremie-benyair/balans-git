"""Balans : prototype d'autorégulation numérique collaborative.

Lancement : streamlit run app.py
Les données sont stockées localement dans data/balans.db (non versionné).
Le style reprend celui du prototype HTML (palette indigo/lavande, polices Fraunces et
Outfit). Le thème de base est dans .streamlit/config.toml, le reste dans CSS ci-dessous.
"""
import io
import json
import math
import sqlite3
import struct
import wave
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
STATUS = {"active": "En cours", "completed": "Menée à terme", "stopped": "Arrêtée avant la fin",
          "scheduled": "Programmée"}
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
CREATE TABLE IF NOT EXISTS answers(
    profile TEXT, qid TEXT, value TEXT, PRIMARY KEY(profile, qid));
CREATE TABLE IF NOT EXISTS todos(
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, done INTEGER DEFAULT 0, xp INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS postpones(
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, at TEXT, minutes INTEGER, goal TEXT);
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


def scheduled_session():
    rows = query("SELECT * FROM sessions WHERE status='scheduled' ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def start_session(apps, goal, duration, delay_min=0):
    """Démarre une session maintenant, ou la programme dans delay_min minutes."""
    ensure_todolist()  # la todolist apparaît à la première activation
    execute(
        "INSERT INTO sessions(apps, goal, start, duration_min, status) VALUES(?,?,?,?,?)",
        (", ".join(apps), goal, (now() + timedelta(minutes=delay_min)).isoformat(), duration,
         "scheduled" if delay_min else "active"),
    )


def stop_session(session_id):
    execute(
        "UPDATE sessions SET status='stopped', ended_at=? WHERE id=?",
        (now().isoformat(), session_id),
    )


def close_expired():
    """Active les sessions programmées arrivées à l'heure, puis termine celles dont la durée est écoulée."""
    for s in query("SELECT * FROM sessions WHERE status='scheduled'"):
        if now() >= datetime.fromisoformat(s["start"]):
            execute("UPDATE sessions SET status='active' WHERE id=?", (s["id"],))
    for s in query("SELECT * FROM sessions WHERE status='active'"):
        if now() >= planned_end(s):
            xp = 10 + s["duration_min"] // 5
            execute(
                "UPDATE sessions SET status='completed', ended_at=?, xp=? WHERE id=?",
                (planned_end(s).isoformat(), xp, s["id"]),
            )


def held_minutes(s):
    end = datetime.fromisoformat(s["ended_at"]) if s["ended_at"] else now()
    return max(0, round((end - datetime.fromisoformat(s["start"])).total_seconds() / 60))


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


# ---------- Questionnaire de première connexion (cahier des charges du 12/10/2026) ----------
# Chaque réponse est enregistrée sous un code stable (identique côté ado et côté tuteur),
# ce qui permettra de croiser les réponses plus tard. Profils : teen, tutor1, tutor2.
def save_answer(profile, qid, value):
    execute(
        "INSERT INTO answers(profile, qid, value) VALUES(?, ?, ?) "
        "ON CONFLICT(profile, qid) DO UPDATE SET value=excluded.value",
        (profile, qid, json.dumps(value, ensure_ascii=False)),
    )


def load_answers(profile):
    rows = query("SELECT qid, value FROM answers WHERE profile=?", (profile,))
    return {r["qid"]: json.loads(r["value"]) for r in rows}


def Q(qid, text, options, multi=False, hint=""):
    return {"id": qid, "text": text, "options": options, "multi": multi, "hint": hint}


# Question de configuration, posée uniquement au premier tuteur qui se connecte.
SETUP_QUESTION = Q(
    "accounts",
    "Combien de comptes tuteur pour ce profil ?",
    [
        ("one", "Un seul compte tuteur, lié à celui de mon ado"),
        ("two", "Deux comptes tuteur, tous deux liés à celui de mon ado"),
    ],
    hint="Par exemple deux parents, ou deux foyers. Chaque tuteur remplit ensuite son propre "
    "questionnaire, sur son compte.",
)

_DUREES = [("lt15", "Moins de 15 min"), ("15_30", "15-30 min"), ("30_60", "30 min-1h"), ("gt60", "Plus d'1h")]
_FREQ = [("jamais", "Jamais"), ("parfois", "Parfois"), ("souvent", "Souvent")]
_ECRANS = [("jeux", "Jeux vidéo"), ("reseaux", "Réseaux sociaux"), ("streaming", "Vidéos, streaming"), ("autre", "Autre")]
_PLURI = "Plusieurs réponses possibles"

TEEN_QUESTIONS_14_17 = [
    Q("q1", "Tu fais quoi régulièrement, en dehors des écrans ?", [
        ("devoirs", "Mes devoirs, mes révisions"), ("sport", "Du sport"),
        ("creatif", "Musique, dessin, lego, bricolage"), ("amis", "Voir mes amis"),
        ("rien", "Franchement, pas grand-chose")], multi=True, hint=_PLURI),
    Q("q2", "Pendant les devoirs, combien de temps tu tiens sans regarder ton téléphone ou tes écrans ?", _DUREES),
    Q("q3", "Aujourd'hui, qui décide en général quand tu arrêtes les écrans ?", [
        ("parents", "Mes parents systématiquement"), ("depend", "Ça dépend des moments"),
        ("moi", "Moi principalement")]),
    Q("q4", "Qu'est-ce qui te donne le plus envie de continuer un effort ?", [
        ("immediat", "Un petit bonus tout de suite"), ("paliers", "Voir que je progresse petit à petit"),
        ("mixte", "Les deux autant")]),
    Q("q5", "Quand ton temps d'écran est bientôt fini, qu'est-ce qui t'aide le plus à t'arrêter sans que ça t'énerve ?", [
        ("visuel", "Un rappel visuel à l'avance"), ("son", "Un son"),
        ("rien", "Rien de particulier, ça passe bien")]),
    Q("q6", "Qu'est-ce que t'aimes faire sur tes écrans ?", _ECRANS, multi=True, hint=_PLURI),
    Q("q7", "Tu utilises les écrans le soir après l'heure du coucher ?", _FREQ),
    Q("q8", "Les écrans, ça crée des tensions à la maison ?", _FREQ),
]

# Jeu 11-13 ans : mêmes questions, mêmes codes de réponse, formulations plus simples.
_PLURI_JEUNE = "Tu peux en choisir plusieurs"
TEEN_QUESTIONS_11_13 = [
    Q("q1", "Qu'est-ce que tu fais souvent quand tu n'es pas sur un écran ?", [
        ("devoirs", "Mes devoirs et mes révisions"), ("sport", "Du sport"),
        ("creatif", "Musique, dessin, lego, bricolage"), ("amis", "Je vois mes amis"),
        ("rien", "Pas grand-chose, en vrai")], multi=True, hint=_PLURI_JEUNE),
    Q("q2", "Quand tu fais tes devoirs, combien de temps tu arrives à rester sans regarder ton téléphone ou ta tablette ?", [
        ("lt15", "Moins de 15 minutes"), ("15_30", "Entre 15 et 30 minutes"),
        ("30_60", "Entre 30 minutes et 1 heure"), ("gt60", "Plus d'1 heure")]),
    Q("q3", "Aujourd'hui, c'est qui qui décide quand tu arrêtes les écrans ?", [
        ("parents", "Mes parents, tout le temps"), ("depend", "Ça dépend des moments"),
        ("moi", "Moi, la plupart du temps")]),
    Q("q4", "Qu'est-ce qui te donne le plus envie de continuer quand tu fais un effort ?", [
        ("immediat", "Un petit bonus tout de suite"), ("paliers", "Voir que je progresse petit à petit"),
        ("mixte", "Les deux, pareil")]),
    Q("q5", "Quand ton temps d'écran va bientôt se terminer, qu'est-ce qui t'aide le plus à t'arrêter sans t'énerver ?", [
        ("visuel", "Un signal visuel avant la fin"), ("son", "Un son"),
        ("rien", "Rien de spécial, ça va")]),
    Q("q6", "Qu'est-ce que tu aimes faire sur tes écrans ?", [
        ("jeux", "Jeux vidéo"), ("reseaux", "Réseaux sociaux"), ("streaming", "Vidéos et streaming"),
        ("autre", "Autre")], multi=True, hint=_PLURI_JEUNE),
    Q("q7", "Est-ce que tu utilises des écrans le soir, après l'heure d'aller te coucher ?", _FREQ),
    Q("q8", "Est-ce que les écrans causent des disputes à la maison ?", _FREQ),
]

# Question d'âge : posée en premier à l'ado, elle choisit le jeu de formulations.
AGE_QUESTION = Q(
    "age",
    "Quel âge as-tu ?",
    [("11_13", "11-13 ans"), ("14_17", "14-17 ans")],
    hint="Cela nous aide à poser les questions de la façon la plus claire pour toi.",
)


def teen_questions():
    return TEEN_QUESTIONS_11_13 if load_answers("teen").get("age") == "11_13" else TEEN_QUESTIONS_14_17


# Volet tuteur : mêmes jeux de réponses (mêmes codes) reformulés à la 3e personne, Q8 spécifique.
TUTOR_QUESTIONS = [
    Q("q1", "Quelles activités votre ado fait-il régulièrement en dehors des écrans ?", [
        ("devoirs", "Ses devoirs, ses révisions"), ("sport", "Du sport"),
        ("creatif", "Musique, dessin, lego, bricolage"), ("amis", "Voir ses amis"),
        ("rien", "Pas grand-chose, selon moi")], multi=True, hint=_PLURI),
    Q("q2", "Pendant ses devoirs, combien de temps votre ado tient-il sans regarder ses écrans ?", _DUREES),
    Q("q3", "Aujourd'hui, qui décide en général quand les écrans s'arrêtent ?", [
        ("parents", "Les parents systématiquement"), ("depend", "Ça dépend des moments"),
        ("moi", "Mon ado principalement")]),
    Q("q4", "Qu'est-ce qui motive le plus votre ado : un bonus immédiat ou une progression ?", [
        ("immediat", "Un petit bonus tout de suite"), ("paliers", "Voir qu'il progresse petit à petit"),
        ("mixte", "Les deux autant")]),
    Q("q5", "Qu'est-ce qui aide votre ado à lâcher son téléphone sans que ça dégénère ?", [
        ("visuel", "Un rappel visuel à l'avance"), ("son", "Un son"),
        ("rien", "Rien de particulier, ça se passe bien")]),
    Q("q6", "Qu'est-ce que votre ado aime faire sur ses écrans ?", _ECRANS, multi=True, hint=_PLURI),
    Q("q7", "Votre ado utilise-t-il les écrans le soir après l'heure du coucher ?", _FREQ),
    Q("q8", "Votre principale préoccupation avec les écrans en ce moment :", [
        ("temps", "Le temps total passé"), ("horaires", "Le respect des horaires"),
        ("contenu", "Le contenu consulté"), ("scolaire", "L'impact sur le scolaire"),
        ("conflits", "Les conflits que ça génère")]),
]


def tutor_accounts():
    """1 ou 2 comptes tuteur, selon la réponse donnée par le premier tuteur."""
    return 2 if load_answers("tutor1").get("accounts") == "two" else 1


def tutor_profiles():
    return ["tutor1"] + (["tutor2"] if tutor_accounts() == 2 else [])


def steps_for(profile):
    if profile == "teen":
        return [AGE_QUESTION] + teen_questions()
    if profile == "tutor1":
        return [SETUP_QUESTION] + TUTOR_QUESTIONS
    return TUTOR_QUESTIONS


def has_answer(q, value):
    codes = [c for c, _ in q["options"]]
    if q["multi"]:
        return isinstance(value, list) and any(v in codes for v in value)
    return value in codes


def is_complete(profile):
    saved = load_answers(profile)
    return all(has_answer(q, saved.get(q["id"])) for q in steps_for(profile))


def role_label(r):
    # Libellés fixes : s'ils changeaient en cours de route, Streamlit réinitialiserait le sélecteur.
    return {"teen": "Profil ado", "tutor1": "Tuteur 1", "tutor2": "Tuteur 2"}[r]


# ---------- Paramétrage : effet des réponses sur l'application (cahier des charges) ----------
Q1_ORDER = ["devoirs", "sport", "creatif", "amis", "rien"]
OBJECTIVES = {  # Q1 -> objectifs proposés au moment de lancer une session
    "devoirs": ["Devoirs", "Révisions", "Travail perso"],
    "sport": ["Entraînement", "Match", "Préparation"],
    "creatif": ["Musique", "Dessin", "Créa", "Jeux"],
    "amis": ["Sortie", "Temps avec les amis"],
    "rien": ["Couper un moment"],
}
TODO_EXAMPLES = {  # Q1 -> tâches pré-remplies de la todolist
    "devoirs": ["Sortir mes affaires", "Faire l'exercice", "Relire le cours", "Ranger mon bureau"],
    "sport": ["Préparer mon sac", "Trajet", "Séance", "Ranger mes affaires"],
    "creatif": ["Installer le matériel", "20 min de pratique", "Ranger"],
    "amis": [],  # aucune tâche pré-remplie
    "rien": ["10 min : un jeu de société", "10 min : une partie de cartes", "10 min : construire un truc",
             "10 min : dessiner un perso", "10 min : cuisiner quelque chose"],
}
APP_FOR_Q6 = {"jeux": "🎮 Jeux vidéo", "reseaux": "💬 Réseaux sociaux", "streaming": "📺 Streaming"}
Q3_PALIER = {"parents": 1, "depend": 2, "moi": 3}
Q2_ORDER = ["lt15", "15_30", "30_60", "gt60"]
PAUSE_PLAN = {"lt15": (10, 3), "15_30": (20, 5), "30_60": (30, 5)}  # (toutes les X min, pause de Y min)
ALERT_ORDER = ["rien", "visuel", "son"]   # du moins au plus soutenant
NIGHT_ORDER = ["jamais", "parfois", "souvent"]
STATS_LEVEL = {"jamais": "detail", "parfois": "synthese", "souvent": "minimal"}
TODO_XP = 3
POSTPONE_MINUTES, POSTPONE_QUOTA = 5, 3
START_CHOICES = {"Maintenant": 0, "Dans 15 min": 15, "Dans 30 min": 30, "Dans 1 h": 60}
NIGHT_RULE_TITLE = "Pas d'écran la nuit"

PALIERS = {
    1: ("Palier 1 · Structure fixe",
        "Des créneaux de blocage fixés à l'avance, ensemble. L'ado ne les déplace pas seul, mais peut ajouter des "
        "blocages volontaires à tout moment. Les récompenses viennent du respect des créneaux."),
    2: ("Palier 2 · Structure souple",
        "Le volume total de blocage est fixé ensemble ; l'ado choisit quand le placer dans la journée. "
        "Les tuteurs voient les créneaux choisis, sans validation à donner."),
    3: ("Palier 3 · Autonomie",
        "L'ado fixe ses propres objectifs de blocage. Le cadre se réduit à quelques périodes non négociables "
        "(nuit, repas), définies une fois pour toutes. Les récompenses portent sur la régularité."),
}
MODULES = {
    "temps": ("Palier de temps et statistiques de suivi", "Le temps total passé est la préoccupation principale."),
    "horaires": ("Minuteur et réglage des rappels", "Le respect des horaires est la préoccupation principale."),
    "contenu": ("Éducation thérapeutique et outils de filtres", "Ce module arrivera dans une prochaine version."),
    "scolaire": ("Todolist et tâches liées aux devoirs", "L'impact sur le scolaire est la préoccupation principale."),
    "conflits": ("Tableau partagé et co-décision", "Les conflits sont la préoccupation principale."),
}
REWARD_TEXT = {
    "immediat": "Une récompense sur le moment : chaque tâche cochée et chaque session terminée.",
    "paliers": "Des points cumulés vers des étapes visibles, avec une récompense à chaque étape.",
    "mixte": "Les deux systèmes en parallèle : récompense immédiate et étapes visibles.",
}
ALERT_TEXT = {"visuel": "alerte visuelle", "son": "alerte sonore",
              "rien": "compte à rebours affiché, sans alerte renforcée"}
NIGHT_TEXT = {
    "jamais": "Pas de suggestion de blocage nocturne.",
    "parfois": "Blocage nocturne proposé en option, non activé par défaut.",
    "souvent": "Blocage nocturne activé par défaut, à valider par un tuteur.",
}
STATS_TEXT = {
    "detail": "Statistiques détaillées : durées, créneaux, pauses.",
    "synthese": "Statistiques synthétiques, sans détail jour par jour.",
    "minimal": "Peu de chiffres : les accords pris, ce qui a fonctionné, et des amorces de discussion à date fixe.",
}
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
             "octobre", "novembre", "décembre"]
DISCUSSION_STARTERS = [
    "Qu'est-ce qui a bien marché cette semaine ?",
    "Y a-t-il un accord qu'on voudrait ajuster ?",
    "Qu'est-ce qui t'a aidé à t'arrêter plus facilement ?",
]


def fr_date(d):
    return f"{DAYS_FR[d.weekday()]} {d.day} {MONTHS_FR[d.month - 1]}"


def compute_config():
    """Réglages déduits des réponses, selon les règles du cahier des charges.

    Quand les comptes ne répondent pas pareil : palier le plus bas (Q3), valeur la plus basse pour les
    pauses (Q2), option la plus soutenante pour l'alerte (Q5), réponse la plus élevée pour le blocage
    nocturne (Q7). L'ado prime pour la motivation (Q4), le tuteur pour le module mis en avant (Q8).
    """
    teen = load_answers("teen")
    tutors = [load_answers(p) for p in tutor_profiles()]
    everyone = [teen] + tutors
    q2 = min((a["q2"] for a in everyone), key=Q2_ORDER.index)
    codes_q1 = sorted(teen["q1"], key=Q1_ORDER.index)
    return {
        "palier": min(Q3_PALIER[a["q3"]] for a in everyone),
        "palier_gap": len({a["q3"] for a in everyone}) > 1,
        "pauses": PAUSE_PLAN.get(q2),
        "alert": max((a["q5"] for a in everyone), key=ALERT_ORDER.index),
        "night": max((a["q7"] for a in everyone), key=NIGHT_ORDER.index),
        "reward": teen["q4"],
        "objectives": [o for c in codes_q1 for o in OBJECTIVES[c]],
        "apps": [APP_FOR_Q6[c] for c in teen["q6"] if c in APP_FOR_Q6],
        "stats": STATS_LEVEL[teen["q8"]],
        "module": tutors[0]["q8"],
        "todo_codes": codes_q1,
    }


def default_todos(cfg):
    codes = list(cfg["todo_codes"])
    if cfg["module"] == "scolaire" and "devoirs" not in codes:
        codes = ["devoirs"] + codes   # module « scolaire » : tâches liées aux devoirs mises en avant
    return [task for c in codes for task in TODO_EXAMPLES[c]]


def ensure_todolist():
    """La todolist apparaît à la première activation d'une session, pré-remplie selon la Q1."""
    if get_setting("todolist_created", ""):
        return
    for title in default_todos(compute_config()):
        execute("INSERT INTO todos(title) VALUES(?)", (title,))
    set_setting("todolist_created", "1")


def total_xp():
    sessions_xp = query("SELECT COALESCE(SUM(xp), 0) AS n FROM sessions")[0]["n"]
    todos_xp = query("SELECT COALESCE(SUM(xp), 0) AS n FROM todos")[0]["n"]
    return sessions_xp + todos_xp


# --- Palier : proposition de départ, validation à deux, progression ---
def palier_current():
    value = get_setting("palier", "")
    return int(value) if value else None


def palier_target(cfg):
    return int(get_setting("palier_target", "") or cfg["palier"])


def palier_validated_by(who_key):
    return bool(get_setting("palier_ok_teen" if who_key == "teen" else "palier_ok_tutor", ""))


def validate_palier(who_key, cfg):
    set_setting("palier_ok_teen" if who_key == "teen" else "palier_ok_tutor", "1")
    if palier_validated_by("teen") and palier_validated_by("tutor"):
        set_setting("palier", str(palier_target(cfg)))
        set_setting("palier_since", now().date().isoformat())
        for key in ("palier_target", "palier_ok_teen", "palier_ok_tutor"):
            set_setting(key, "")


def gauge_percent():
    """Progression vers le palier suivant : jours (sur 21) avec au moins une session menée à terme, 15 jours = 100 %."""
    since = max((now() - timedelta(days=21)).date().isoformat(), get_setting("palier_since", ""))
    days = query(
        "SELECT DISTINCT substr(start, 1, 10) AS d FROM sessions WHERE status='completed' AND substr(start, 1, 10) >= ?",
        (since,),
    )
    return min(100, round(100 * len(days) / 15))


def maybe_propose_next_palier():
    current = palier_current()
    if current and current < 3 and not get_setting("palier_target", "") and gauge_percent() >= 100:
        set_setting("palier_target", str(current + 1))


# --- Report « je finis ma partie » ---
def week_monday():
    today = now().date()
    return today - timedelta(days=today.weekday())


def quota_left():
    used = query("SELECT COUNT(*) AS n FROM postpones WHERE substr(at, 1, 10) >= ?", (week_monday().isoformat(),))[0]["n"]
    return max(0, POSTPONE_QUOTA - used)


def already_postponed(session_id):
    return bool(query("SELECT id FROM postpones WHERE session_id=?", (session_id,)))


def postpone(s):
    new_start = max(datetime.fromisoformat(s["start"]), now()) + timedelta(minutes=POSTPONE_MINUTES)
    execute("UPDATE sessions SET start=?, status='scheduled' WHERE id=?", (new_start.isoformat(), s["id"]))
    execute("INSERT INTO postpones(session_id, at, minutes, goal) VALUES(?,?,?,?)",
            (s["id"], now().isoformat(), POSTPONE_MINUTES, s["goal"]))


def create_night_rule(proposed_by, teen_ok, parent_ok):
    execute(
        "INSERT INTO rules(title, apps, slot, proposed_by, teen_ok, parent_ok) VALUES(?,?,?,?,?,?)",
        (NIGHT_RULE_TITLE, "Tous les écrans", "Après l'heure du coucher", proposed_by, teen_ok, parent_ok),
    )


def apply_initial_config(cfg):
    """Appliqué une seule fois, à la première entrée dans l'application."""
    if get_setting("config_applied", ""):
        return
    if cfg["night"] == "souvent":   # activé par défaut, à valider par un tuteur
        create_night_rule("app", 1, 0)
    set_setting("config_applied", "1")


@st.cache_data
def beep_wav():
    rate, seconds = 22050, 0.5
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * seconds)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 880 * i / rate) * (1 - i / n))) for i in range(n)
        ))
    return buf.getvalue()


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

.onb-prog{height:6px;background:var(--cream-2);border-radius:10px;overflow:hidden;margin:18px 0 4px}
.onb-prog i{display:block;height:100%;background:var(--teal);border-radius:10px;transition:width .4s ease}
.card.teal .sub{color:rgba(255,255,255,.88)}
.card.coral .sub{color:rgba(255,255,255,.92)}
.st-key-card_todo .stButton{margin-top:0}
.stApp .st-key-card_todo [data-testid="stBaseButton-secondary"]{padding:.25rem .5rem;min-height:2.4rem;height:2.4rem;border-radius:12px;font-size:14px}
.st-key-card_todo [data-testid="stHorizontalBlock"]{gap:.6rem}
.card.coral .eyebrow{color:#fff;margin-top:0;opacity:.85}
.tag.app{background:var(--cream-2);color:var(--ink-soft)}
.cmp{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}
.cmp .side{flex:1 1 180px;background:var(--cream);border-radius:16px;padding:12px 14px;font-size:14px;line-height:1.4}
.cmp .side .tag{display:inline-block;margin-bottom:6px}
.stApp button:disabled{opacity:.4;box-shadow:none;cursor:not-allowed}
.stApp .stButton{margin-top:10px}

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


def render_onboarding(profile):
    """Une question à la fois, réponses fermées, barre de progression. Reprend là où l'on s'est arrêté."""
    steps = steps_for(profile)
    saved = load_answers(profile)
    step_key = f"onb_step_{profile}"
    if step_key not in st.session_state:
        st.session_state[step_key] = next(
            (i for i, s in enumerate(steps) if not has_answer(s, saved.get(s["id"]))), 0
        )
    i = min(st.session_state[step_key], len(steps) - 1)
    q = steps[i]
    preliminaries = ("accounts", "age")          # questions de configuration, hors des 8 questions
    n_questions = sum(1 for s in steps if s["id"] not in preliminaries)
    offset = len(steps) - n_questions
    label = "Avant de commencer" if q["id"] in preliminaries else f"Question {i + 1 - offset} sur {n_questions}"

    html(f'<div class="onb-prog"><i style="width:{100 * (i + 1) / len(steps):.0f}%"></i></div>')
    if i == 0:
        if profile == "teen":
            intro = ("8 questions, environ 5 minutes. Il n'y a pas de bonne ou de mauvaise réponse : "
                     "tes réponses servent à adapter l'application et à en discuter avec tes parents.")
        else:
            intro = ("8 questions, environ 5 minutes. Il n'y a pas de bonne ou de mauvaise réponse : "
                     "vos réponses servent de point de départ pour en discuter avec votre ado, jamais de verdict.")
        html(f'<div class="card"><p class="sub" style="margin:0">{esc(intro)}</p></div>')
    heading(label, q["text"], q["hint"])

    codes = [c for c, _ in q["options"]]
    labels = dict(q["options"])
    previous = saved.get(q["id"])
    with st.container(key="card_onb"):
        common = dict(format_func=labels.get, key=f"ans_{profile}_{q['id']}", label_visibility="collapsed")
        if q["multi"]:
            default = [c for c in (previous or []) if c in codes]
            answer = st.pills(q["text"], codes, selection_mode="multi", default=default, **common)
        else:
            answer = st.pills(q["text"], codes, selection_mode="single",
                              default=previous if previous in codes else None, **common)
        last = i == len(steps) - 1
        c1, c2 = st.columns(2)
        if c1.button("Retour", disabled=i == 0, key=f"back_{profile}"):
            st.session_state[step_key] = i - 1
            st.rerun()
        if c2.button("Terminer" if last else "Suivant", type="primary",
                     disabled=not has_answer(q, answer), key=f"next_{profile}"):
            save_answer(profile, q["id"], answer)
            if last:
                st.session_state.pop(step_key, None)
            else:
                st.session_state[step_key] = i + 1
            st.rerun()


def side_name(profile):
    if profile == "teen":
        return "Ado"
    if profile == "tutor2":
        return "Tuteur 2"
    return "Tuteur 1" if tutor_accounts() == 2 else "Tuteur"


def all_done():
    """Vrai quand l'ado et tous les comptes tuteur ont terminé leur questionnaire."""
    return is_complete("teen") and all(is_complete(p) for p in tutor_profiles())


def as_list(value):
    return value if isinstance(value, list) else [value]


def labels_for(profile, qid, answers):
    names = dict(next(q for q in steps_for(profile) if q["id"] == qid)["options"])
    return [names[c] for c in as_list(answers.get(qid)) if c in names]


@st.fragment(run_every=3)
def watch_others():
    """Vérifie régulièrement si tous les comptes ont terminé (utile quand chacun est sur son appareil)."""
    if all_done():
        st.rerun()


def render_waiting(profile):
    """Page d'attente : ce compte a terminé, mais pas encore tous les autres."""
    pending = [p for p in ["teen"] + tutor_profiles() if not is_complete(p)]
    if profile == "teen":
        title, text = "C'est enregistré", "On attend la réponse de tes parents."
    else:
        title = "Merci, c'est enregistré"
        text = "On attend les réponses de : " + ", ".join(side_name(p) for p in pending) + "."
    status = "".join(
        f'<span class="pill {"" if is_complete(p) else "muted"}">{esc(side_name(p))} '
        f'{"✓" if is_complete(p) else "en attente"}</span>'
        for p in ["teen"] + tutor_profiles()
    )
    html(
        f'<div class="card teal"><div class="eyebrow">Questionnaire</div>'
        f'<div class="h-title">{esc(title)}</div><p class="sub">{esc(text)}</p>'
        f'<div style="margin-top:14px">{status}</div></div>'
    )
    html('<p class="sub">Vos réponses seront mises côte à côte dès que tout le monde aura terminé.</p>')
    watch_others()


CROSS_TITLES = {
    "q1": "Activités en dehors des écrans",
    "q2": "Concentration pendant les devoirs",
    "q3": "Qui décide d'arrêter les écrans",
    "q4": "Ce qui motive à continuer un effort",
    "q5": "Ce qui aide à s'arrêter sans tension",
    "q6": "Ce qui plaît sur les écrans",
    "q7": "Écrans le soir après le coucher",
}


def render_crossing(profile):
    """Page post-questionnaires : réponses côte à côte, écarts signalés comme sujets de discussion."""
    profiles = ["teen"] + tutor_profiles()
    answers = {p: load_answers(p) for p in profiles}
    gaps = {}
    for qid in CROSS_TITLES:
        sets = [set(as_list(answers[p].get(qid))) for p in profiles]
        gaps[qid] = any(s != sets[0] for s in sets)
    # Un écart sur « qui décide » est le premier sujet de co-décision.
    order = sorted(CROSS_TITLES, key=lambda qid: (not gaps[qid], qid != "q3"))
    topics = [CROSS_TITLES[qid] for qid in order if gaps[qid]]

    heading("Vos réponses", "Côte à côte",
            "Un point de départ pour en discuter ensemble, jamais un verdict.")
    if topics:
        items = "".join(f'<span class="pill coral">{esc(x)}</span>' for x in topics)
        html(f'<div class="card"><div class="eyebrow" style="margin-top:0">À aborder ensemble</div>{items}</div>')
    else:
        html('<div class="card"><div class="eyebrow" style="margin-top:0">À aborder ensemble</div>'
             '<p class="sub" style="margin:0">Vos réponses se rejoignent sur toutes les questions.</p></div>')

    for qid in order:
        sides = "".join(
            f'<div class="side"><span class="tag {"ado" if p == "teen" else "par"}">{esc(side_name(p))}</span>'
            f'<div>{esc(" · ".join(labels_for(p, qid, answers[p])))}</div></div>'
            for p in profiles
        )
        state = '<span class="pill coral">À aborder ensemble</span>' if gaps[qid] \
            else '<span class="pill">D\'accord</span>'
        html(
            f'<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">'
            f'<div class="h-title" style="font-size:18px">{esc(CROSS_TITLES[qid])}</div>{state}</div>'
            f'<div class="cmp">{sides}</div></div>'
        )

    # Tensions (ado) et préoccupation (tuteurs) : deux questions différentes, sans attribution nominative.
    tutor_concerns = []
    for p in tutor_profiles():
        for label in labels_for(p, "q8", answers[p]):
            if label not in tutor_concerns:
                tutor_concerns.append(label)
    html(
        '<div class="card"><div class="h-title" style="font-size:18px">Tensions et préoccupation</div><div class="cmp">'
        f'<div class="side"><span class="tag ado">Ado · tensions à la maison</span>'
        f'<div>{esc(" · ".join(labels_for("teen", "q8", answers["teen"])))}</div></div>'
        f'<div class="side"><span class="tag par">Tuteurs · principale préoccupation</span>'
        f'<div>{esc(" · ".join(tutor_concerns))}</div></div></div></div>'
    )
    if st.button("Continuer vers la co-décision", type="primary", key=f"cross_continue_{profile}"):
        set_setting(f"crossing_seen_{profile}", "1")
        st.rerun()


# ---------- Interface ----------
st.set_page_config(page_title="Balans", page_icon="⚖️", initial_sidebar_state="collapsed")
html(CSS)
close_expired()

col_brand, col_toggle = st.columns([3, 2], vertical_alignment="center")
with col_brand:
    html(BRAND)
with col_toggle:
    with st.container(key="role_toggle"):
        # La liste d'options ne change jamais (sinon Streamlit perd la sélection en cours de route) :
        # le profil « Tuteur 2 » est simplement masqué tant qu'il n'existe pas.
        if st.session_state.get("profile_choice") == "tutor2" and tutor_accounts() == 1:
            st.session_state["profile_choice"] = "tutor1"
        role = st.radio("Profil", ["teen", "tutor1", "tutor2"], format_func=role_label, horizontal=True,
                        label_visibility="collapsed", key="profile_choice")
if tutor_accounts() == 1:
    html('<style>.st-key-role_toggle [role="radiogroup"] > div:nth-child(3){display:none}</style>')
is_teen = role == "teen"
who = "teen" if is_teen else "tutor"
nickname = get_setting("nickname", "Adolescent")

# Première connexion : questionnaire propre à chaque compte (ado, tuteur 1, tuteur 2).
# 1) questionnaire de ce compte ; 2) attente tant que tous les comptes n'ont pas fini ;
# 3) page post-questionnaires (croisement) ; 4) ensuite seulement, l'application.
if not is_complete(role):
    render_onboarding(role)
    st.stop()
if not all_done():
    render_waiting(role)
    st.stop()
if not get_setting(f"crossing_seen_{role}", ""):
    render_crossing(role)
    st.stop()

cfg = compute_config()
apply_initial_config(cfg)
maybe_propose_next_palier()
if "_toast" in st.session_state:
    st.toast(st.session_state.pop("_toast"))
instant_reward = cfg["reward"] in ("immediat", "mixte")   # Q4 : récompense sur le moment
level_reward = cfg["reward"] in ("paliers", "mixte")      # Q4 : étapes visibles

tab_session, tab_board, tab_rules, tab_progress, tab_settings = st.tabs(
    ["Session", "Tableau", "Règles", "Progression", "Réglages"]
)


# ---------- Todolist (pré-remplie selon la Q1, modifiable par l'ado) ----------
def on_todo_toggle(todo_id, instant):
    done = bool(st.session_state[f"todo_done_{todo_id}"])
    execute("UPDATE todos SET done=?, xp=? WHERE id=?", (int(done), TODO_XP if done else 0, todo_id))
    if done and instant:
        st.session_state["_toast"] = f"+{TODO_XP} XP 🎉"


def on_todo_rename(todo_id):
    title = st.session_state[f"todo_title_{todo_id}"].strip()
    if title:
        execute("UPDATE todos SET title=? WHERE id=?", (title, todo_id))


def on_todo_delete(todo_id):
    execute("DELETE FROM todos WHERE id=?", (todo_id,))


def on_todo_add():
    title = st.session_state.get("todo_new", "").strip()
    if title:
        execute("INSERT INTO todos(title) VALUES(?)", (title,))
        st.session_state["todo_new"] = ""


def render_todolist(instant):
    with st.container(key="card_todo"):
        html('<div class="h-title" style="font-size:19px">Ma todolist</div>'
             '<p class="sub">Coche, renomme, supprime ou ajoute des tâches : c\'est ta liste.</p>')
        todos = query("SELECT * FROM todos ORDER BY id")
        if not todos:
            html('<p class="sub">Aucune tâche pour le moment. Ajoute la première ci-dessous.</p>')
        for todo in todos:
            c1, c2, c3 = st.columns([1, 10, 1.6], vertical_alignment="center")
            c1.checkbox("Fait", value=bool(todo["done"]), key=f"todo_done_{todo['id']}",
                        on_change=on_todo_toggle, args=(todo["id"], instant), label_visibility="collapsed")
            c2.text_input("Tâche", value=todo["title"], key=f"todo_title_{todo['id']}",
                          on_change=on_todo_rename, args=(todo["id"],), label_visibility="collapsed")
            c3.button("✕", key=f"todo_del_{todo['id']}", on_click=on_todo_delete, args=(todo["id"],))
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        c1.text_input("Nouvelle tâche", key="todo_new", placeholder="Ajouter une tâche…", on_change=on_todo_add)
        c2.button("Ajouter", key="todo_add_btn", on_click=on_todo_add)


# ---------- Module mis en avant (Q8 tuteur) ----------
def render_highlight(cfg):
    module = cfg["module"]
    title, desc = MODULES[module]
    if module == "temps":
        n = query("SELECT COUNT(*) AS n FROM sessions WHERE status='completed' AND substr(start, 1, 10) >= ?",
                  (week_monday().isoformat(),))[0]["n"]
        extra = f"Palier {palier_current() or palier_target(cfg)} · {n} session(s) menée(s) à terme cette semaine"
    elif module == "horaires":
        extra = f"Préavis à 10 et 5 minutes · {ALERT_TEXT[cfg['alert']]}"
    elif module == "contenu":
        extra = "Bientôt disponible."
    elif module == "scolaire":
        extra = "La todolist est mise en avant, avec des tâches liées aux devoirs."
    else:
        pending = sum(not rule_is_active(r) for r in query("SELECT * FROM rules"))
        extra = f"{pending} règle(s) en discussion" if pending else "Aucune règle en attente."
    html(
        '<div class="card"><div class="eyebrow" style="margin-top:0">À la une</div>'
        f'<div class="h-title" style="font-size:19px">{esc(title)}</div><p class="sub">{esc(desc)}</p>'
        f'<p class="sub" style="color:var(--teal);font-weight:600">{esc(extra)}</p></div>'
    )


# ---------- Session en direct : préavis (Q5), report, pauses (Q2) ----------
def postpone_button(s):
    if already_postponed(s["id"]):
        st.button("Je finis ma partie", key=f"pp_{s['id']}", disabled=True)
        st.caption("Un seul report par créneau.")
    elif quota_left() <= 0:
        st.button("Je finis ma partie", key=f"pp_{s['id']}", disabled=True)
        st.caption(f"Reports épuisés cette semaine, retour le {fr_date(week_monday() + timedelta(days=7))}.")
    elif st.button("Je finis ma partie", key=f"pp_{s['id']}"):
        postpone(s)
        st.rerun()


@st.fragment(run_every=1)
def live_session(shown_status, alert, pauses, teen_view):
    close_expired()
    s = active_session() or scheduled_session()
    if not s or s["status"] != shown_status:
        st.rerun()
        return
    start = datetime.fromisoformat(s["start"])
    if s["status"] == "scheduled":
        secs = max(0.0, (start - now()).total_seconds())
        html(
            '<div class="card teal"><div class="eyebrow">Prochain créneau</div>'
            f'<div class="h-title">{esc(s["goal"])}</div>'
            f'<div class="big">{clock(timedelta(seconds=secs))}</div>'
            f'<p class="sub">Démarre à {start.strftime("%H:%M")} · {s["duration_min"]} min</p>'
            f'{pills_html(s["apps"].split(", "))}</div>'
        )
        threshold = 300 if secs <= 300 else (600 if secs <= 600 else None)
        if threshold and alert != "rien":   # Q5 : préavis à 10 et 5 minutes, visuel ou sonore
            html(
                '<div class="card coral"><div class="eyebrow">Préavis</div>'
                f'<div class="h-title" style="font-size:20px">Le créneau démarre dans {clock(timedelta(seconds=secs))}</div>'
                f'<p class="sub">Objectif : {esc(s["goal"])}</p></div>'
            )
            if alert == "son" and teen_view:
                beep_key = f"beep_{s['id']}_{s['start']}_{threshold}"
                if not st.session_state.get(beep_key):
                    st.session_state[beep_key] = True
                    st.audio(beep_wav(), format="audio/wav", autoplay=True)
        if teen_view and secs <= 600:
            postpone_button(s)
        return

    remaining = max(planned_end(s) - now(), timedelta(0))
    elapsed = (now() - start).total_seconds()
    pct = min(100, max(0, 100 * (1 - remaining.total_seconds() / (s["duration_min"] * 60))))
    html(
        '<div class="card teal"><div class="eyebrow">Session en cours</div>'
        f'<div class="h-title">{esc(s["goal"])}</div>'
        f'<div class="big">{clock(remaining)}</div>'
        f'<div class="bar"><i style="width:{pct:.0f}%"></i></div>'
        f'{pills_html(s["apps"].split(", "))}</div>'
    )
    if teen_view and elapsed <= 60:   # « Je finis ma partie » reste possible au moment de la coupure
        postpone_button(s)
    if not (teen_view and pauses):
        return
    interval, brk = pauses
    pause_end = st.session_state.get("pause_end")
    if pause_end and now() < pause_end:
        html(
            '<div class="card coral"><div class="eyebrow">Pause</div>'
            f'<div class="h-title" style="font-size:20px">{clock(pause_end - now())}</div>'
            '<p class="sub">Le téléphone reste bloqué. Tu peux te lever, boire un verre d\'eau…</p></div>'
        )
        return
    idx = int(elapsed // (interval * 60))
    seen_key = f"pause_seen_{s['id']}_{idx}"
    if idx >= 1 and elapsed - idx * interval * 60 < 120 and remaining.total_seconds() > brk * 60 \
            and not st.session_state.get(seen_key):
        html('<div class="card"><div class="h-title" style="font-size:19px">Pause courte proposée</div>'
             f'<p class="sub">{brk} minutes, si tu en as envie. « Je continue » écarte seulement cette suggestion.</p></div>')
        c1, c2 = st.columns(2)
        if c1.button(f"Prendre {brk} min", key=f"pause_take_{s['id']}_{idx}", type="primary"):
            st.session_state[seen_key] = True
            st.session_state["pause_end"] = now() + timedelta(minutes=brk)
            st.rerun()
        if c2.button("Je continue", key=f"pause_skip_{s['id']}_{idx}"):
            st.session_state[seen_key] = True
            st.rerun()


# --- Onglet Session ---
with tab_session:
    s = active_session() or scheduled_session()
    palier_pending = (not get_setting("palier", "")) or bool(get_setting("palier_target", ""))
    if palier_pending and not palier_validated_by("teen" if is_teen else "tutor"):
        html('<div class="card coral"><div class="eyebrow">À valider ensemble</div>'
             '<div class="h-title" style="font-size:19px">Un palier est proposé</div>'
             '<p class="sub">Il est dans l\'onglet Réglages, à valider ensemble.</p></div>')
    teen_todos = is_teen and bool(get_setting("todolist_created", ""))
    todos_first = teen_todos and cfg["module"] == "scolaire"

    if s:
        heading(
            f"Salut {nickname} 👋" if is_teen else "Bonjour 👋",
            ("Tu tiens ton plan" if s["status"] == "active" else "Ton prochain créneau") if is_teen
            else ("Une session est en cours" if s["status"] == "active" else "Un créneau est programmé"),
        )
        live_session(s["status"], cfg["alert"], cfg["pauses"], is_teen)
        if is_teen:
            if s["status"] == "active":
                if st.button("Arrêter maintenant"):
                    stop_session(s["id"])
                    st.rerun()
            elif st.button("Annuler la programmation"):
                execute("DELETE FROM sessions WHERE id=?", (s["id"],))
                st.rerun()
        else:
            html('<p class="sub">Seul l\'adolescent peut activer ou arrêter une session.</p>')
        if teen_todos:
            render_todolist(instant_reward)
    elif is_teen:
        heading(f"Salut {nickname} 👋", "Commencer une session", "Tu choisis, tu décides de la durée.")
        last = query("SELECT * FROM sessions WHERE status='completed' ORDER BY ended_at DESC LIMIT 1")
        if instant_reward and last and now() - datetime.fromisoformat(last[0]["ended_at"]) < timedelta(minutes=30):
            html(f'<div class="card coral"><div class="eyebrow">Bravo</div><div class="h-title" style="font-size:20px">'
                 f'Session terminée : +{last[0]["xp"]} XP 🎉</div><p class="sub">{esc(last[0]["goal"])}</p></div>')
        render_highlight(cfg)
        if todos_first:
            render_todolist(instant_reward)
        with st.container(key="card_start"):
            apps = st.pills("Applications à mettre en pause", APPS, selection_mode="multi",
                            default=cfg["apps"], key="session_apps")
            other = st.text_input("Autre application (facultatif)")
            goal = st.pills("Objectif de la session", cfg["objectives"] + [GOAL_OTHER], key="session_goal")
            if goal == GOAL_OTHER:
                goal = st.text_input("Précisez l'objectif") or GOAL_OTHER
            when = st.pills("Démarrage", list(START_CHOICES), default="Maintenant", key="session_when")
            duration = st.slider("Durée", 15, 180, 45, step=5, format="%d min")
            if cfg["pauses"]:
                interval, brk = cfg["pauses"]
                st.caption(f"Pause courte proposée toutes les {interval} min ({brk} min).")
            else:
                st.caption("Pas de pause automatique.")
            if st.button("Activer la pause", type="primary"):
                chosen = list(apps or []) + ([other.strip()] if other.strip() else [])
                if not chosen:
                    st.warning("Sélectionnez au moins une application.")
                elif not goal:
                    st.warning("Choisissez un objectif.")
                else:
                    start_session(chosen, goal, duration, START_CHOICES[when or "Maintenant"])
                    st.rerun()
        if teen_todos and not todos_first:
            render_todolist(instant_reward)
    else:
        heading("Bonjour 👋", "Comment se passe la semaine ?")
        render_highlight(cfg)
        html(
            '<div class="card"><div class="h-title" style="font-size:20px">🔒 Aucune session en cours</div>'
            '<p class="sub">Seul l\'adolescent peut activer une pause : c\'est un choix volontaire, '
            "jamais une surprise. Retrouvez l'historique dans l'onglet Tableau.</p></div>"
        )

# --- Onglet Tableau partagé (niveau de détail selon la Q8 de l'ado) ---
with tab_board:
    heading("Tableau partagé", "Temps activés volontairement",
            "Identique pour l'adolescent et les tuteurs : un support de discussion, ni surveillance ni sanction.")
    sessions = query("SELECT * FROM sessions WHERE status!='scheduled' ORDER BY start DESC")
    level = cfg["stats"]
    if not sessions:
        html('<div class="card"><p class="sub" style="margin:0">Aucune session pour l\'instant.</p></div>')
    else:
        n_done = sum(x["status"] == "completed" for x in sessions)
        n_stop = sum(x["status"] == "stopped" for x in sessions)
        if level in ("detail", "synthese"):
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
        if level == "detail":
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
                when_txt = x["start"].replace("T", " ")[5:16]
                items += (
                    f'<div class="tl-item"><div class="tl-dot {dot}"></div><div>'
                    f'<b>{esc(x["goal"])}</b>'
                    f'<small>{when_txt} · {held_minutes(x)} / {x["duration_min"]} min · {STATUS[x["status"]]}</small>'
                    f'<small>{esc(x["apps"])}</small></div></div>'
                )
            html(f'<div class="card"><div class="eyebrow" style="margin-top:0">Historique</div><div class="timeline">{items}</div></div>')
        if level == "minimal":
            # Tension forte : peu de chiffres, les accords pris et ce qui a fonctionné, une amorce à date fixe.
            agreements = [r for r in query("SELECT * FROM rules") if rule_is_active(r)]
            accords = "".join(f'<span class="pill">{esc(r["title"])}</span>' for r in agreements) or \
                '<p class="sub" style="margin:0">Pas encore d\'accord pris : c\'est un bon sujet pour la prochaine discussion.</p>'
            html(f'<div class="card"><div class="eyebrow" style="margin-top:0">Accords pris</div>{accords}</div>')
            goals = [x["goal"] for x in sessions if x["status"] == "completed"]
            worked = (f"{len(goals)} session(s) menée(s) à terme, surtout pour : {esc(max(set(goals), key=goals.count))}."
                      if goals else "Les premières réussites apparaîtront ici.")
            html(f'<div class="card"><div class="eyebrow" style="margin-top:0">Ce qui a fonctionné</div><p class="sub" style="margin:0">{worked}</p></div>')
            sunday = now().date() + timedelta(days=(6 - now().weekday()) % 7)
            starter = DISCUSSION_STARTERS[(sunday.toordinal() // 7) % len(DISCUSSION_STARTERS)]
            html(f'<div class="card"><div class="eyebrow" style="margin-top:0">Rendez-vous de discussion</div>'
                 f'<div class="h-title" style="font-size:19px">{esc(fr_date(sunday).capitalize())}</div>'
                 f'<p class="sub">{esc(starter)}</p></div>')
    # Reports « je finis ma partie » : visibles par tous, quel que soit le niveau de détail
    reports = query("SELECT * FROM postpones ORDER BY id DESC LIMIT 5")
    if reports:
        rows = "".join(
            f'<div class="tl-item"><div class="tl-dot stopped"></div><div><b>{esc(r["goal"])}</b>'
            f'<small>{r["at"].replace("T", " ")[5:16]} · report de {r["minutes"]} min</small></div></div>'
            for r in reports
        )
        html('<div class="card"><div class="eyebrow" style="margin-top:0">Reports « je finis ma partie »</div>'
             f'<div class="timeline">{rows}</div>'
             f'<p class="sub">{quota_left()} report(s) restant(s) cette semaine.</p></div>')

# --- Onglet Règles communes ---
with tab_rules:
    heading("Co-décision", "Règles co-construites" if not is_teen else "Nos règles, à deux",
            "Une règle n'est valable que lorsque l'adolescent et un tuteur l'ont tous deux approuvée.")
    my_col = "teen_ok" if is_teen else "parent_ok"
    for r in query("SELECT * FROM rules ORDER BY id DESC"):
        active = rule_is_active(r)
        with st.container(key=f"card_rule_{r['id']}"):
            if r["proposed_by"] == "teen":
                tag = '<span class="tag ado">Proposée par l\'ado</span>'
            elif r["proposed_by"] == "app":
                tag = '<span class="tag app">Suggérée par l\'application</span>'
            else:
                tag = '<span class="tag par">Proposée par le tuteur</span>'
            state = '<span class="pill">Active</span>' if active else '<span class="pill coral">En discussion</span>'
            ado_pill = f'<span class="pill {"" if r["teen_ok"] else "muted"}">Ado {"✓" if r["teen_ok"] else "en attente"}</span>'
            par_pill = f'<span class="pill {"" if r["parent_ok"] else "muted"}">Tuteur {"✓" if r["parent_ok"] else "en attente"}</span>'
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

# --- Onglet Progression (récompenses selon la Q4 de l'ado) ---
with tab_progress:
    finished = query("SELECT * FROM sessions WHERE status IN ('completed', 'stopped') ORDER BY start DESC")
    done = [x for x in finished if x["status"] == "completed"]
    xp = total_xp()
    level_n, level_name, xp_in_level = level_info(xp)
    avatar = get_setting("avatar", AVATARS[0])

    heading("Progression", "Ton parcours" if is_teen else "Son parcours")
    level_html = ""
    if level_reward:
        level_html = (
            f'<span class="pill" style="margin-top:6px">Niveau {level_n} · {esc(level_name)}</span>'
        )
    bar_html = ""
    if level_reward:
        bar_html = (f'<div class="xpbar"><i style="width:{xp_in_level}%"></i></div>'
                    f'<p class="sub" style="margin:0">{xp} XP · encore {100 - xp_in_level} XP avant l\'étape suivante</p>')
    else:
        bar_html = f'<p class="sub" style="margin:10px 0 0">{xp} XP gagnés</p>'
    html(
        f'<div class="card"><div class="profile"><div class="avatar">{esc(avatar)}</div><div>'
        f'<div class="h-title" style="font-size:22px">{esc(nickname)}</div>{level_html}</div></div>'
        f'{bar_html}'
        '<p class="sub">L\'XP récompense les engagements menés à terme. Arrêter plus tôt ne retire '
        "aucun point : c'est une information, pas une sanction.</p></div>"
    )

    if instant_reward:
        recent = done[:5]
        rows = "".join(
            f'<div class="tl-item"><div class="tl-dot"></div><div><b>{esc(x["goal"])}</b>'
            f'<small>+{x["xp"]} XP · session menée à terme</small></div></div>' for x in recent
        ) or '<p class="sub" style="margin:0">Tes prochaines récompenses apparaîtront ici.</p>'
        n_tasks = query("SELECT COUNT(*) AS n FROM todos WHERE done=1")[0]["n"]
        html('<div class="card"><div class="eyebrow" style="margin-top:0">Récompenses récentes</div>'
             f'<div class="timeline">{rows}</div>'
             f'<p class="sub">{n_tasks} tâche(s) cochée(s) · +{TODO_XP} XP chacune.</p></div>')
    if level_reward:
        html('<div class="eyebrow">Étapes</div>')
        for n, name in enumerate(LEVELS):
            reached = level_n - 1 >= n
            html(f'<div class="opt{"" if reached else " locked"}"><div class="emoji">{"🏁" if reached else "🔒"}</div>'
                 f"<div><b>{esc(name)}</b><small>À partir de {n * 100} XP</small></div></div>")

    current = palier_current()
    if current and current < 3 and not get_setting("palier_target", ""):
        html('<div class="card"><div class="eyebrow" style="margin-top:0">Vers le palier suivant</div>'
             f'<div class="xpbar"><i style="width:{gauge_percent()}%"></i></div>'
             '<p class="sub" style="margin:0">Elle avance chaque jour où un engagement est tenu.</p></div>')

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


# --- Onglet Réglages : ce que les réponses règlent ---
def setting_card(title, text, caption="", chips=None):
    chips_html = f'<div style="margin-top:8px">{pills_html(chips)}</div>' if chips else ""
    caption_html = f'<p class="sub" style="font-size:12px">{esc(caption)}</p>' if caption else ""
    html(f'<div class="card"><div class="h-title" style="font-size:18px">{esc(title)}</div>'
         f'<p class="sub">{esc(text)}</p>{chips_html}{caption_html}</div>')


with tab_settings:
    me = "teen" if is_teen else "tutor"
    heading("Paramétrage", "Ce que vos réponses règlent",
            "Chaque réglage vient du questionnaire. Rien n'est caché, et tout peut être rediscuté ensemble.")

    # Palier : proposition de départ, puis passages validés à deux
    current = palier_current()
    target = palier_target(cfg)
    pending = current is None or bool(get_setting("palier_target", ""))
    shown = target if pending else current
    p_title, p_text = PALIERS[shown]
    if current is None:
        eyebrow = "Palier de départ proposé"
    elif pending:
        eyebrow = "Conditions réunies pour passer au palier suivant"
    else:
        eyebrow = "Palier actuel"
    note = ""
    if current is None and cfg["palier_gap"]:
        note = ('<p class="sub" style="color:var(--teal);font-weight:600">Les réponses ne sont pas identiques : '
                "le palier le plus bas est proposé, et c'est le premier sujet à discuter.</p>")
    status = ""
    if pending:
        status = (f'<div style="margin-top:10px"><span class="pill {"" if palier_validated_by("teen") else "muted"}">Ado '
                  f'{"✓" if palier_validated_by("teen") else "en attente"}</span>'
                  f'<span class="pill {"" if palier_validated_by("tutor") else "muted"}">Tuteur '
                  f'{"✓" if palier_validated_by("tutor") else "en attente"}</span></div>')
    html(f'<div class="card"><div class="eyebrow" style="margin-top:0">{esc(eyebrow)}</div>'
         f'<div class="h-title" style="font-size:19px">{esc(p_title)}</div><p class="sub">{esc(p_text)}</p>{note}{status}</div>')
    if pending and not palier_validated_by(me):
        if st.button("Valider ce palier", type="primary", key="palier_validate"):
            validate_palier(me, cfg)
            st.rerun()

    setting_card("Objectifs proposés", "Ils apparaissent quand on lance une session.",
                 "D'après : ce que l'ado fait en dehors des écrans.", cfg["objectives"])
    todo_preview = default_todos(cfg)
    setting_card("Todolist", "Elle apparaît à la première activation d'une session, pré-remplie. "
                 "L'ado peut tout renommer, supprimer ou compléter.",
                 "D'après : ce que l'ado fait en dehors des écrans.",
                 todo_preview or ["Aucune tâche pré-remplie"])
    setting_card("Rappels",
                 f"Préavis à 10 et 5 minutes avant un créneau : {ALERT_TEXT[cfg['alert']]}. "
                 f"« Je finis ma partie » reporte le début de {POSTPONE_MINUTES} minutes, "
                 f"{POSTPONE_QUOTA} fois par semaine, une fois par créneau.",
                 "D'après : ce qui aide à s'arrêter (l'option la plus soutenante parmi les réponses).")
    if cfg["pauses"]:
        interval, brk = cfg["pauses"]
        setting_card("Pauses", f"Une pause courte de {brk} minutes est proposée toutes les {interval} minutes pendant une session.",
                     "D'après : le temps de concentration (la valeur la plus basse parmi les réponses).")
    else:
        setting_card("Pauses", "Pas de pause automatique.",
                     "D'après : le temps de concentration (la valeur la plus basse parmi les réponses).")
    setting_card("Récompenses", REWARD_TEXT[cfg["reward"]], "D'après : la réponse de l'ado.")
    setting_card("Blocage nocturne", NIGHT_TEXT[cfg["night"]],
                 "D'après : la réponse la plus élevée parmi les réponses.")
    has_night_rule = any(r["title"] == NIGHT_RULE_TITLE for r in query("SELECT title FROM rules"))
    if cfg["night"] == "parfois" and not has_night_rule:
        if st.button("Proposer le blocage nocturne", key="night_propose"):
            create_night_rule(who, int(is_teen), int(not is_teen))
            st.rerun()
    setting_card("Tableau partagé", STATS_TEXT[cfg["stats"]], "D'après : les tensions à la maison déclarées par l'ado.")
    setting_card("Applications présélectionnées",
                 "Elles sont cochées au moment de lancer une session, à valider une par une.",
                 "D'après : ce que l'ado aime faire sur ses écrans.",
                 cfg["apps"] or ["Aucune : l'ado choisit librement"])
    setting_card("Module mis en avant", MODULES[cfg["module"]][0],
                 "D'après : la préoccupation principale du tuteur.")

html(
    '<div class="hint"><b>Prototype</b> · le blocage réel des applications n\'est pas encore branché : '
    "les sessions sont enregistrées et partagées, rien n'est bloqué sur l'appareil.</div>"
)
