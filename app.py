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
APPS = ["Jeux vidéo", "Réseaux sociaux", "Streaming", "Navigateur", "Messagerie"]
GOAL_OTHER = "Autre"
GOALS = ["Devoirs", "Repas", "Sport", "Sommeil", "Famille", GOAL_OTHER]
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
    if "signed_at" not in [r["name"] for r in con.execute("PRAGMA table_info(rules)")]:
        con.execute("ALTER TABLE rules ADD COLUMN signed_at TEXT")   # date à laquelle les deux ont signé
        con.commit()
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
    """Retourne tous les badges : (nom, description, obtenu ?, progression)."""
    days = len({s["start"][:10] for s in done})
    long_session = any(s["duration_min"] >= 60 for s in done)
    return [
        ("Premier engagement", "Une première session menée à terme", len(done) >= 1, f"{min(len(done), 1)}/1"),
        ("Régularité", "5 sessions menées à terme", len(done) >= 5, f"{min(len(done), 5)}/5"),
        ("Concentration", "Une session d'au moins 1 h", long_session, f"{int(long_session)}/1"),
        ("Constance", "Des sessions sur 3 jours différents", days >= 3, f"{min(days, 3)}/3"),
        ("Co-constructeur", "Une règle définie à deux", bool(active_rules), f"{int(bool(active_rules))}/1"),
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
APP_FOR_Q6 = {"jeux": "Jeux vidéo", "reseaux": "Réseaux sociaux", "streaming": "Streaming"}
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
PALIER_NAMES = {1: "Structure fixe", 2: "Structure souple", 3: "Autonomie"}
# Ce que chaque palier veut dire (version ado, version tuteur) : on décrit, on ne promet pas encore de fonction.
PALIER_MEANING = {
    1: ("Des créneaux fixés ensemble, qui ne bougent pas tout seuls. Tu peux toujours ajouter des blocages volontaires.",
        "Des créneaux fixés ensemble, qui ne bougent pas tout seuls. Votre ado peut toujours ajouter des blocages volontaires."),
    2: ("Le volume de blocage est fixé ensemble ; toi, tu choisis quand placer tes pauses dans la journée.",
        "Le volume de blocage est fixé ensemble ; votre ado choisit quand placer ses pauses dans la journée."),
    3: ("Tu fixes toi-même tes objectifs. Seules quelques périodes (nuit, repas) restent communes.",
        "Votre ado fixe lui-même ses objectifs. Seules quelques périodes (nuit, repas) restent communes."),
}
PALIER_NOW = {
    1: ("Vous avez fixé vos créneaux ensemble.", "Vous avez fixé les créneaux ensemble."),
    2: ("Le volume est fixé ensemble, tu choisis quand.", "Le volume est fixé ensemble, votre ado choisit quand."),
    3: ("Tu fixes toi-même tes objectifs.", "Votre ado fixe lui-même ses objectifs."),
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
    "paliers": "Des points cumulés vers des niveaux visibles, avec une récompense à chaque niveau.",
    "mixte": "Les deux systèmes en parallèle : récompense immédiate et niveaux visibles.",
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
        set_setting(f"palier_date_{palier_target(cfg)}", now().date().isoformat())
        set_setting("palier", str(palier_target(cfg)))
        set_setting("palier_since", now().date().isoformat())
        for key in ("palier_target", "palier_ok_teen", "palier_ok_tutor"):
            set_setting(key, "")


def gauge_word(percent):
    """La jauge se dit avec des mots, jamais avec un décompte de jours."""
    return "Début" if percent < 25 else "En route" if percent < 60 else "Presque" if percent < 100 else "Prêt"


MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def short_date(iso):
    """'2026-11-12' -> '12 nov.'"""
    if not iso:
        return ""
    d = datetime.fromisoformat(iso[:10])
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"


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


def set_rule_ok(rule_id, col, value):
    """Approuve ou retire son accord ; la date de signature n'existe que lorsque les deux ont signé."""
    execute(f"UPDATE rules SET {col}=? WHERE id=?", (value, rule_id))
    r = query("SELECT teen_ok, parent_ok, signed_at FROM rules WHERE id=?", (rule_id,))[0]
    if r["teen_ok"] and r["parent_ok"]:
        if not r["signed_at"]:
            execute("UPDATE rules SET signed_at=? WHERE id=?", (now().date().isoformat(), rule_id))
    else:
        execute("UPDATE rules SET signed_at=NULL WHERE id=?", (rule_id,))


def decisions_for(me_key):
    """Décisions communes ouvertes : (accords à ma réponse, accords en attente de l'autre, palier à confirmer
    par moi, palier en attente de l'autre)."""
    col = "teen_ok" if me_key == "teen" else "parent_ok"
    mine, theirs = [], []
    for r in query("SELECT * FROM rules ORDER BY id DESC"):
        if r["teen_ok"] and r["parent_ok"]:
            continue
        (theirs if r[col] else mine).append(r)
    palier_open = palier_current() is None or bool(get_setting("palier_target", ""))
    return mine, theirs, palier_open and not palier_validated_by(me_key), palier_open and palier_validated_by(me_key)


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
  /* Même identité (indigo, bleu, ambre, Fraunces + Outfit), mais le violet est réservé à l'action principale */
  --cream:#F4F6FA; --cream-2:#E6EAF2; --ink:#222640; --ink-soft:#5A6079;
  --teal:#4F4CC0; --teal-deep:#37339B; --sage:#7FA6E6; --sage-soft:#E1EAFA;
  --coral:#E9A23B; --coral-soft:#FFF0D2; --amber:#F0B84E; --amber-soft:#FFF6E2; --amber-ink:#7A4B00;
  --sky:#A9C8F0; --line:rgba(34,38,64,.10); --shadow:0 14px 30px -22px rgba(34,38,64,.45);
}
.stApp{color:var(--ink);background:linear-gradient(180deg,#F8F9FC 0%,#EEF1F7 100%)}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stToolbar"],[data-testid="stDecoration"],footer,#MainMenu{display:none !important}
.block-container{max-width:1200px;padding:2rem 2.2rem 4rem}
@media (max-width:640px){.block-container{padding:1.2rem 1rem 3rem}}
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

/* ---- Sélecteur de profil : petit contrôle rectangulaire, plus de pilule ---- */
.st-key-role_toggle{display:flex;justify-content:flex-end}
.st-key-role_toggle > *{margin-left:auto}
.st-key-role_toggle [role="radiogroup"]{display:inline-flex;gap:2px;background:#fff;border-radius:12px;
  padding:4px;border:1px solid var(--line);box-shadow:none}
.st-key-role_toggle label[data-testid="stRadioOption"]{margin:0;padding:8px 16px;border-radius:9px;
  cursor:pointer;transition:.2s}
.st-key-role_toggle label[data-testid="stRadioOption"] > div > div:first-child{display:none}
.st-key-role_toggle label[data-testid="stRadioOption"] p{margin:0;font-weight:600;font-size:14px;color:var(--ink-soft)}
.st-key-role_toggle label[data-selected="true"]{background:var(--teal)}
.st-key-role_toggle label[data-selected="true"] p{color:#fff}

/* ---- Onglets : simple soulignement, plus de segments en pilule ---- */
.stApp [role="tablist"]{background:none;border-radius:0;padding:0;gap:4px;margin-top:10px;
  border:none !important;border-bottom:1px solid var(--line) !important;box-shadow:none}
.stApp [data-testid="stTab"]{flex:0 0 auto;justify-content:center;height:auto;padding:12px 18px;border-radius:0;
  background:none;border:none;border-bottom:3px solid transparent;margin-bottom:-1px}
.stApp [data-testid="stTab"] p{font-size:15px;font-weight:600;color:var(--ink-soft)}
.stApp [data-testid="stTab"]:hover p{color:var(--ink)}
.stApp [data-testid="stTab"][aria-selected="true"]{background:none;box-shadow:none;border-bottom-color:var(--teal)}
.stApp [data-testid="stTab"][aria-selected="true"] p{color:var(--ink)}
@media (max-width:640px){.stApp [data-testid="stTab"]{padding:10px 9px}.stApp [data-testid="stTab"] p{font-size:14px}}
.stApp [role="tablist"] .react-aria-SelectionIndicator, .stApp [data-baseweb="tab-highlight"],
.stApp [data-baseweb="tab-border"]{display:none}

/* ---- Typographie : une vraie échelle (titre de page > titre de section > texte) ---- */
.eyebrow{font-size:11.5px;letter-spacing:1.4px;text-transform:uppercase;color:var(--ink-soft);
  font-weight:700;margin:26px 0 6px}
.stApp .page-title{font-family:'Fraunces',serif;font-weight:600;font-size:40px;letter-spacing:-1px;
  line-height:1.05;color:var(--ink)}
@media (max-width:640px){.stApp .page-title{font-size:30px}}
.stApp .h-title{font-family:'Fraunces',serif;font-weight:600;font-size:22px;letter-spacing:-.4px;line-height:1.15}
.stApp .sub{font-size:14.5px;color:var(--ink-soft);line-height:1.5;margin:8px 0 0}
.hint{font-size:12.5px;color:var(--ink-soft);text-align:center;margin-top:32px}
.hint b{color:var(--ink)}

/* ---- Trois niveaux de contenant : panneau (surface de travail), bandeau (état), section (sans boîte) ---- */
.card, div[class*="st-key-card_"]{background:#fff;border-radius:16px;padding:22px;
  box-shadow:var(--shadow);border:1px solid var(--line);margin-top:16px}
div[class*="st-key-card_"]{gap:.9rem}
div[class*="st-key-card_"] .h-title{margin-bottom:2px}
.card.teal{background:linear-gradient(150deg,var(--teal) 0%, var(--teal-deep) 120%);color:#fff;border:none;padding:26px}
.card.teal .eyebrow{color:#d9d8ff;margin-top:0}
.card.teal .sub{color:rgba(255,255,255,.88)}
.big{font-family:'Fraunces',serif;font-size:64px;font-weight:600;letter-spacing:-2px;line-height:1;margin:8px 0 14px}
@media (max-width:640px){.big{font-size:48px}}
.bar{height:8px;background:rgba(255,255,255,.22);border-radius:10px;overflow:hidden;margin-bottom:14px}
.bar i{display:block;height:100%;background:#fff;border-radius:10px}

.sec{margin-top:30px}
.sec-h{font-family:'Fraunces',serif;font-weight:600;font-size:20px;letter-spacing:-.3px;
  margin:0 0 10px;padding-top:16px;border-top:1px solid var(--line)}
.note{border-left:3px solid var(--sage);padding:2px 0 2px 16px;margin-top:20px}
.note .eyebrow{margin:0 0 4px}
.note.warn{border-left-color:var(--amber);background:var(--amber-soft);padding:14px 16px;border-radius:0 12px 12px 0}
.note.warn .eyebrow{color:var(--amber-ink)}
.card.coral{background:var(--amber-soft);border:none;border-left:3px solid var(--amber);border-radius:0 12px 12px 0;
  box-shadow:none;padding:14px 16px}
.card.coral .eyebrow{color:var(--amber-ink);margin-top:0}
.topics{font-size:16px;font-weight:600;line-height:1.5}

/* ---- Statuts et étiquettes : un point + du texte, plus de pilules ---- */
.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;color:var(--ink);
  background:none;padding:0;margin:0 16px 4px 0}
.pill:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--sage);flex:0 0 8px}
.pill.coral:before{background:var(--amber)}
.pill.muted{color:var(--ink-soft);font-weight:500}
.pill.muted:before{background:#C6CCDB}
.card.teal .pill{color:#fff;font-weight:500}
.card.teal .pill:before{background:rgba(255,255,255,.75)}
.tag{font-size:11px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--ink-soft)}
.tag.ado{color:var(--teal)} .tag.par{color:var(--amber-ink)} .tag.app{color:var(--ink-soft)}

/* ---- Chiffres : grands, sans boîtes ---- */
.kpi{display:flex;align-items:center;gap:28px;flex-wrap:wrap;margin-top:8px}
.stats{display:flex;flex-direction:column;gap:12px}
.stat{display:flex;align-items:baseline;gap:10px}
.stat b{font-family:'Fraunces',serif;font-size:34px;line-height:1;font-weight:600}
.stat span{font-size:13.5px;color:var(--ink-soft)}
.ring{position:relative;width:130px;height:130px}
.ring svg{transform:rotate(-90deg)}
.ring .center{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.ring .center b{font-family:'Fraunces',serif;font-size:30px;font-weight:600;line-height:1}
.ring .center small{font-size:12px;color:var(--ink-soft)}

.bars{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;height:150px;margin-top:14px}
.bars .col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:6px;height:100%}
.bars .col i{display:block;width:100%;max-width:44px;border-radius:8px 8px 4px 4px;
  background:linear-gradient(180deg,var(--sage),var(--teal))}
.bars .col i.empty{height:6px;background:var(--cream-2)}
.bars .col span{font-size:12px;color:var(--ink-soft);font-weight:600}
.bars .col.today span{color:var(--teal)}

.timeline{display:flex;flex-direction:column;margin-top:2px}
.tl-item{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}
.tl-item:last-child{border-bottom:none}
.tl-dot{width:10px;height:10px;border-radius:50%;background:var(--sage);margin-top:6px;flex:0 0 10px}
.tl-dot.stopped{background:#C6CCDB} .tl-dot.active{background:var(--teal)}
.tl-item b{font-size:14.5px;display:block}
.tl-item small{font-size:12.5px;color:var(--ink-soft);display:block;margin-top:2px}

.profile{display:flex;align-items:center;gap:16px}
.avatar{width:72px;height:72px;border-radius:20px;display:grid;place-items:center;font-size:38px;
  background:linear-gradient(150deg,var(--sage-soft),var(--amber-soft))}
.xpbar{height:8px;background:var(--cream-2);border-radius:10px;overflow:hidden;margin:16px 0 6px}
.xpbar i{display:block;height:100%;background:var(--teal);border-radius:10px}

.steps{display:flex;margin:6px 0 4px}
.step{flex:1;position:relative;padding-top:22px;min-width:0}
.step:before{content:"";position:absolute;top:5px;left:0;right:0;height:2px;background:var(--cream-2)}
.step i{position:absolute;top:0;left:0;width:12px;height:12px;border-radius:50%;background:var(--cream-2)}
.step.on:before{background:var(--teal)} .step.on i{background:var(--teal)}
.step b{font-size:14px;display:block}
.step small{font-size:12px;color:var(--ink-soft)}
.step.off b, .step.off small{color:var(--ink-soft)}

.badge-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:0 28px}
.opt{display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--line);padding:12px 0}
.opt .emoji{font-size:20px;width:38px;height:38px;display:grid;place-items:center;
  background:var(--cream-2);border-radius:11px;flex:0 0 38px}
.opt b{font-size:14.5px;display:block}
.opt small{font-size:12.5px;color:var(--ink-soft)}
.opt.locked{opacity:.5}

/* ---- Liste de réglages et tableau de croisement : un seul panneau, pas une carte par ligne ---- */
.dl{background:#fff;border-radius:16px;box-shadow:var(--shadow);border:1px solid var(--line);padding:4px 24px;margin-top:16px}
.dl-row{display:grid;grid-template-columns:190px 1fr;gap:22px;padding:18px 0;border-bottom:1px solid var(--line)}
.dl-row:last-child{border-bottom:none}
.dl-k{font-family:'Fraunces',serif;font-weight:600;font-size:17px;line-height:1.25}
.dl-v{font-size:14.5px;line-height:1.55}
.dl-v small{display:block;color:var(--ink-soft);font-size:12.5px;margin-top:5px}
.dl-v .chips{margin-top:8px}
@media (max-width:700px){.dl-row{grid-template-columns:1fr;gap:6px}}

.xt{background:#fff;border-radius:16px;box-shadow:var(--shadow);border:1px solid var(--line);overflow:hidden;margin-top:18px}
.xt-row{display:grid;gap:20px;padding:16px 24px;border-bottom:1px solid var(--line);align-items:start;
  grid-template-columns:1.3fr 1fr 1fr}
.xt-row.n3{grid-template-columns:1.3fr 1fr 1fr 1fr}
.xt-row:last-child{border-bottom:none}
.xt-row.head{font-size:11.5px;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink-soft);
  font-weight:700;background:var(--cream);padding-top:12px;padding-bottom:12px}
.xt-row.gap{box-shadow:inset 4px 0 0 var(--amber);background:#FFFBF1}
.xt-row.tens > div:nth-child(3){grid-column:3 / -1}
.xt-q{font-weight:600;font-size:15px}
.xt-q small{display:block;font-weight:600;color:var(--amber-ink);font-size:12.5px;margin-top:3px}
.xt .who{display:none;font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;
  color:var(--ink-soft);margin-bottom:2px}
.xt .who.show{display:block}
@media (max-width:760px){
  .xt-row, .xt-row.n3{grid-template-columns:1fr;gap:8px}
  .xt-row.head{display:none} .xt .who{display:block}
  .xt-row.tens > div:nth-child(3){grid-column:auto}
}

.onb-prog{height:6px;background:var(--cream-2);border-radius:10px;overflow:hidden;margin:18px 0 4px}
.onb-prog i{display:block;height:100%;background:var(--teal);border-radius:10px;transition:width .4s ease}

/* ---- Session : étapes numérotées, durée héros, résumé qui porte le bouton ---- */
.step-h{display:flex;align-items:center;gap:10px;font-family:'Fraunces',serif;font-weight:600;font-size:18px;margin:24px 0 10px}
.step-h:first-child{margin-top:2px}
.step-h .n{width:26px;height:26px;border-radius:50%;background:var(--sage-soft);color:var(--teal-deep);
  font-family:'Outfit',sans-serif;font-size:13px;font-weight:700;display:grid;place-items:center;flex:0 0 26px}
.dur-hero{display:flex;align-items:baseline;gap:10px;margin:0 0 2px}
.dur-hero b{font-family:'Fraunces',serif;font-size:76px;line-height:1;letter-spacing:-2px;font-weight:600}
.dur-hero span{font-size:22px;color:var(--ink-soft);font-weight:500}
.dur-sub{font-size:14px;color:var(--ink-soft);margin:0 0 12px}
.st-key-summary{position:sticky;top:1rem;background:linear-gradient(150deg,var(--teal) 0%,var(--teal-deep) 120%);
  color:#fff;border-radius:16px;padding:26px;margin-top:16px;box-shadow:var(--shadow);gap:.7rem}
.summary .eyebrow{color:#d9d8ff;margin:0 0 8px}
.summary .big-dur{font-family:'Fraunces',serif;font-size:64px;line-height:1;font-weight:600;letter-spacing:-2px}
.summary .big-dur small{font-size:20px;font-weight:500;letter-spacing:0;margin-left:6px;opacity:.85}
.summary .line{font-size:16px;margin-top:12px;line-height:1.4;font-weight:500}
.summary .line small{display:block;opacity:.8;font-size:13px;font-weight:400;margin-top:2px}
.summary .line.missing{color:#FFE2A8}
.stApp .st-key-summary [data-testid="stBaseButton-primary"]{width:100%;height:64px;background:#fff;color:var(--teal-deep);
  font-size:18px;font-weight:700;border-radius:14px;box-shadow:0 16px 28px -16px rgba(0,0,0,.65)}
.stApp .st-key-summary [data-testid="stBaseButton-primary"]:hover{background:#F1F3FF}
.stApp .st-key-summary [data-testid="stBaseButton-primary"] p{color:var(--teal-deep);font-weight:700}
.st-key-summary [data-testid="stElementContainer"]:has(.stButton){width:100% !important}
.st-key-summary .stButton{width:100%;margin-top:22px}
.st-key-summary [data-testid="stCaptionContainer"]{color:rgba(255,255,255,.85)}
.st-key-summary button:disabled{opacity:.55}

/* ---- Co-décision : « Nous deux », à faire ensemble, signature à deux ---- */
.duo-line{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:16px 0 0;font-size:14.5px;color:var(--ink-soft)}
.duo-line b{color:var(--ink);font-weight:600}
.duo-line .sep{color:#B5BBCB}
.duo{display:inline-flex}
.duo .av{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:var(--sage-soft);
  border:2px solid #fff;font-size:18px}
.duo .av.t{margin-left:-10px;background:var(--amber-soft);font-weight:700;font-size:14px;color:var(--amber-ink)}
.together{background:var(--amber-soft);border-left:3px solid var(--amber);border-radius:0 12px 12px 0;
  padding:12px 16px;margin:18px 0 0;font-size:14.5px;line-height:1.5}
.together .go{color:var(--ink-soft)}
.sigs{display:flex;gap:28px;margin:14px 0 16px;flex-wrap:wrap}
.sig{min-width:150px;border-bottom:2px dashed #C6CCDB;padding-bottom:5px}
.sig b{display:block;font-size:13.5px;color:var(--ink-soft);font-weight:600}
.sig small{font-size:12px;color:var(--ink-soft)}
.sig.on{border-bottom:2px solid var(--teal)}
.sig.on b{font-family:'Fraunces',serif;font-size:17px;color:var(--ink)}
.sig.on small{color:var(--teal);font-weight:600}
.turn{font-size:12.5px;font-weight:700;color:var(--amber-ink)}
.signed{font-size:13px;color:var(--teal);font-weight:600}
.accords-h{font-family:'Fraunces',serif;font-weight:600;font-size:22px;margin:30px 0 4px}

/* ---- Progression : où j'en suis, le chemin d'autonomie, le prochain palier ---- */
.where{display:flex;align-items:center;gap:22px;margin-top:22px;flex-wrap:wrap}
.where .avatar{width:88px;height:88px;font-size:46px;border-radius:26px}
.where .txt{flex:1;min-width:240px}
.where .txt .h-title{font-size:32px}
.where .xp{text-align:right}
.where .xp b{font-family:'Fraunces',serif;font-size:36px;font-weight:600;line-height:1;display:block}
.where .xp span{font-size:13px;color:var(--ink-soft)}
.path{display:flex;align-items:flex-start;margin:26px 0 6px;padding:4px 6px 0}
.path .stop{flex:0 0 170px;text-align:center}
.path .slot{height:46px;display:grid;place-items:end center;font-size:34px;line-height:1}
.path .stop i{display:block;width:22px;height:22px;border-radius:50%;margin:8px auto 12px;background:var(--cream-2);border:3px solid var(--cream-2)}
.path .stop.done i{background:var(--teal);border-color:var(--teal)}
.path .stop.here i{width:32px;height:32px;margin:2px auto 8px;background:#fff;border:7px solid var(--teal);box-shadow:0 0 0 6px var(--sage-soft)}
.path .stop.next i{background:#fff;border-color:var(--teal)}
.path .stop b{display:block;font-family:'Fraunces',serif;font-size:17px}
.path .stop span{display:block;font-size:14px}
.path .stop small{display:block;font-size:12.5px;color:var(--ink-soft);margin-top:2px}
.path .stop.later b, .path .stop.later span{color:var(--ink-soft)}
.path .link{flex:1;height:5px;background:var(--cream-2);border-radius:5px;margin-top:66px;position:relative;overflow:hidden}
.path .link em{position:absolute;left:0;top:0;bottom:0;background:var(--teal);border-radius:5px}
.path .link em.v{display:none}
@media (max-width:700px){
  .path{flex-direction:column;align-items:flex-start;padding-left:8px}
  .path .stop{flex:none;width:100%;display:grid;grid-template-columns:44px 1fr;column-gap:14px;text-align:left;align-items:center}
  .path .slot{display:none}
  .path .stop i{grid-row:1 / span 3;margin:0 auto}
  .path .stop.here i{margin:0 auto}
  .path .link{flex:none;width:5px;height:30px;margin:0 0 0 19px}
  .path .link em.h{display:none} .path .link em.v{display:block;left:0;right:0;top:0;bottom:auto;width:100%}
}
.nextp .eyebrow{margin-top:0}
.nextp .h-title{font-size:32px;margin:2px 0 4px}
.nextp .lbl{font-size:12px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--ink-soft);margin:16px 0 4px}
.gauge-words{display:flex;justify-content:space-between;font-size:13px;color:var(--ink-soft);margin-top:6px;font-weight:500}
.gauge-words .on{color:var(--teal);font-weight:700}
.opt.todo .emoji{background:none;border:1.5px dashed #B5BBCB;color:#B5BBCB}
.opt .prog{font-size:12.5px;color:var(--teal);font-weight:600}

/* ---- Boutons : une action principale pleine, le reste en retrait ---- */
.stApp button:disabled{opacity:.4;box-shadow:none;cursor:not-allowed}
.stApp .stButton{margin-top:6px}
.stApp [data-testid="stWidgetLabel"] p{font-size:13.5px;font-weight:600;color:var(--ink)}
.stApp [data-testid="stBaseButton-primary"], .stApp [data-testid="stBaseButton-primaryFormSubmit"]{
  border:none;border-radius:12px;padding:.75rem 1.6rem;font-weight:600;font-size:15.5px;
  background:var(--teal);color:#fff;box-shadow:0 10px 20px -14px var(--teal)}
.stApp [data-testid="stBaseButton-secondary"], .stApp [data-testid="stBaseButton-secondaryFormSubmit"]{
  border-radius:12px;padding:.65rem 1.2rem;font-weight:500;font-size:14.5px;
  background:transparent;color:var(--ink);border:1px solid rgba(34,38,64,.22)}
.stApp [data-testid="stBaseButton-secondary"]:hover{background:#fff;border-color:var(--teal);color:var(--teal)}
.st-key-card_start .stButton, .st-key-card_newrule .stButton, .st-key-card_newrule [data-testid="stFormSubmitButton"],
.st-key-card_start .stButton > button, .st-key-card_newrule [data-testid="stFormSubmitButton"] > button{width:100%}
.st-key-card_start [data-testid="stElementContainer"]:has(.stButton),
.st-key-card_onb [data-testid="stHorizontalBlock"] > div:last-child [data-testid="stElementContainer"]:has(.stButton),
.st-key-card_newrule [data-testid="stElementContainer"]:has([data-testid="stFormSubmitButton"]){width:100% !important}
.st-key-card_onb [data-testid="stHorizontalBlock"] > div:last-child .stButton,
.st-key-card_onb [data-testid="stHorizontalBlock"] > div:last-child .stButton > button{width:100%}
.st-key-card_todo .stButton{margin-top:0}
.stApp .st-key-card_todo [data-testid="stBaseButton-secondary"]{padding:.2rem .6rem;min-height:2.3rem;height:2.3rem;
  border-radius:10px;font-size:14px;border-color:transparent;color:var(--ink-soft)}
.stApp .st-key-card_todo [data-testid="stBaseButton-secondary"]:hover{background:var(--cream-2);color:var(--ink)}
.st-key-card_todo [data-testid="stHorizontalBlock"]{gap:.6rem}
div[class*="st-key-row_rule_"]{border-bottom:1px solid var(--line);padding:20px 0;gap:.6rem}
.stApp [data-testid="stTextInputRootElement"]{border-radius:10px;background:#fff;border:1px solid rgba(34,38,64,.18);
  box-shadow:none}
.stApp [data-testid="stTextInputRootElement"] input{border:none;background:transparent}
[data-testid="stForm"]{border:none;padding:0}

/* choix de réponse : tuiles rectangulaires, plus de pilules */
.stApp button[data-variant="pills"], .stApp button[data-variant="pillsActive"]{
  border-radius:10px;padding:.6rem 1rem;background:#fff;border:1px solid rgba(34,38,64,.18);
  color:var(--ink);font-weight:500;transition:.15s}
.stApp button[data-variant="pills"]:hover{border-color:var(--teal)}
.stApp button[aria-pressed="true"]{border:1.5px solid var(--teal);background:var(--sage-soft);
  box-shadow:none;color:var(--teal-deep);font-weight:600}

/* curseur de durée : de bleu clair à l'indigo de la marque */
.stApp [data-testid="stSlider"] div[style*="touch-action"] > div:first-child{
  height:8px;border-radius:8px;background:linear-gradient(90deg,var(--sky),var(--sage),var(--teal)) !important}
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
    eyebrow_html = f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    html(f'{eyebrow_html}<div class="page-title">{esc(title)}</div>{sub_html}')


def narrow():
    """Écrans de parcours (questionnaire, attente) : colonne de lecture plus resserrée que l'application."""
    html("<style>.block-container{max-width:880px}</style>")


def sec(title, body):
    """Section sans boîte : un filet, un titre, du contenu."""
    return f'<div class="sec"><div class="sec-h">{esc(title)}</div>{body}</div>'


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

    narrow()
    html(f'<div class="onb-prog"><i style="width:{100 * (i + 1) / len(steps):.0f}%"></i></div>')
    if i == 0:
        if profile == "teen":
            intro = ("8 questions, environ 5 minutes. Il n'y a pas de bonne ou de mauvaise réponse : "
                     "tes réponses servent à adapter l'application et à en discuter avec tes parents.")
        else:
            intro = ("8 questions, environ 5 minutes. Il n'y a pas de bonne ou de mauvaise réponse : "
                     "vos réponses servent de point de départ pour en discuter avec votre ado, jamais de verdict.")
        html(f'<p class="sub" style="margin:14px 0 0">{esc(intro)}</p>')
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
        c1, c2 = st.columns([1, 2])
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
    narrow()
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


def render_crossing(profile, review=False):
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
        html('<div class="note warn"><div class="eyebrow">À aborder ensemble</div>'
             f'<div class="topics">{" · ".join(esc(x) for x in topics)}</div></div>')
    else:
        html('<div class="note"><div class="eyebrow">À aborder ensemble</div>'
             '<p class="sub" style="margin:0">Vos réponses se rejoignent sur toutes les questions.</p></div>')

    n = len(profiles)
    head = f'<div class="xt-row head n{n}"><div></div>' + "".join(f"<div>{esc(side_name(p))}</div>" for p in profiles) + "</div>"
    rows = ""
    for qid in order:
        cells = "".join(
            f'<div><span class="who">{esc(side_name(p))}</span>{esc(" · ".join(labels_for(p, qid, answers[p])))}</div>'
            for p in profiles
        )
        flag = "<small>À aborder ensemble</small>" if gaps[qid] else ""
        rows += (f'<div class="xt-row n{n}{" gap" if gaps[qid] else ""}">'
                 f'<div class="xt-q">{esc(CROSS_TITLES[qid])}{flag}</div>{cells}</div>')

    # Tensions (ado) et préoccupation (tuteurs) : deux questions différentes, sans attribution nominative.
    tutor_concerns = []
    for p in tutor_profiles():
        for label in labels_for(p, "q8", answers[p]):
            if label not in tutor_concerns:
                tutor_concerns.append(label)
    rows += (
        f'<div class="xt-row n{n} tens"><div class="xt-q">Tensions et préoccupation</div>'
        f'<div><span class="who show">Ado · tensions à la maison</span>'
        f'{esc(" · ".join(labels_for("teen", "q8", answers["teen"])))}</div>'
        f'<div><span class="who show">Tuteurs · principale préoccupation</span>{esc(" · ".join(tutor_concerns))}</div></div>'
    )
    html(f'<div class="xt">{head}{rows}</div>')
    if review:
        if st.button("Fermer", type="primary", key=f"cross_close_{profile}"):
            st.session_state.pop("review_crossing", None)
            st.rerun()
    elif st.button("Continuer vers la co-décision", type="primary", key=f"cross_continue_{profile}"):
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
if st.session_state.get("review_crossing"):      # « Revoir nos réponses », depuis l'onglet Règles
    render_crossing(role, review=True)
    st.stop()

cfg = compute_config()
apply_initial_config(cfg)
maybe_propose_next_palier()
if "_toast" in st.session_state:
    st.toast(st.session_state.pop("_toast"))
instant_reward = cfg["reward"] in ("immediat", "mixte")   # Q4 : récompense sur le moment
level_reward = cfg["reward"] in ("paliers", "mixte")      # Q4 : étapes visibles

def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def render_together():
    """Le slogan devient un état permanent : qui décide avec qui, et où en sont les accords."""
    rules = query("SELECT * FROM rules")
    active = sum(rule_is_active(r) for r in rules)
    open_n = len(rules) - active
    palier = palier_current()
    avatar = get_setting("avatar", AVATARS[0])
    others = "ses tuteurs" if tutor_accounts() == 2 else "son tuteur"
    open_txt = plural(open_n, "à décider", "à décider") if open_n else "rien en attente"
    html(
        f'<div class="duo-line"><span class="duo"><span class="av">{esc(avatar)}</span><span class="av t">T</span></span>'
        f'<b>{esc(nickname)} et {others}</b><span class="sep">·</span>'
        f'{plural(active, "accord actif", "accords actifs")}<span class="sep">·</span>{open_txt}<span class="sep">·</span>'
        f'{"Palier " + str(palier) if palier else "palier à choisir ensemble"}</div>'
    )


def sigs_html(r):
    """La double signature : deux lignes, vides tant que l'autre n'a pas répondu."""
    def sig(name, ok):
        return (f'<div class="sig{" on" if ok else ""}"><b>{esc(name)}</b>'
                f'<small>{"✓ signé" if ok else "en attente"}</small></div>')
    return f'<div class="sigs">{sig(nickname, r["teen_ok"])}{sig("Tuteur", r["parent_ok"])}</div>'


render_together()
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
        '<div class="note"><div class="eyebrow">À la une</div>'
        f'<div class="h-title" style="font-size:19px">{esc(title)}</div><p class="sub">{esc(desc)}</p>'
        f'<p class="sub" style="color:var(--ink);font-weight:600">{esc(extra)}</p></div>'
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
                '<div class="note warn"><div class="eyebrow">Préavis</div>'
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
            '<div class="note warn"><div class="eyebrow">Pause</div>'
            f'<div class="h-title" style="font-size:20px">{clock(pause_end - now())}</div>'
            '<p class="sub">Le téléphone reste bloqué. Tu peux te lever, boire un verre d\'eau…</p></div>'
        )
        return
    idx = int(elapsed // (interval * 60))
    seen_key = f"pause_seen_{s['id']}_{idx}"
    if idx >= 1 and elapsed - idx * interval * 60 < 120 and remaining.total_seconds() > brk * 60 \
            and not st.session_state.get(seen_key):
        html('<div class="note warn"><div class="h-title" style="font-size:19px">Pause courte proposée</div>'
             f'<p class="sub">{brk} minutes, si tu en as envie. « Je continue » écarte seulement cette suggestion.</p></div>')
        c1, c2 = st.columns(2)
        if c1.button(f"Prendre {brk} min", key=f"pause_take_{s['id']}_{idx}", type="primary"):
            st.session_state[seen_key] = True
            st.session_state["pause_end"] = now() + timedelta(minutes=brk)
            st.rerun()
        if c2.button("Je continue", key=f"pause_skip_{s['id']}_{idx}"):
            st.session_state[seen_key] = True
            st.rerun()


# --- Onglet Session : un seul objectif, lancer la session ---
QUICK_DURATIONS = {"15 min": 15, "30 min": 30, "45 min": 45, "60 min": 60, "90 min": 90}


def on_quick_duration():
    chosen = st.session_state.get("dur_quick")
    if chosen:
        st.session_state["session_duration"] = QUICK_DURATIONS[chosen]


def on_slider_duration():
    value = st.session_state["session_duration"]
    st.session_state["dur_quick"] = next((k for k, v in QUICK_DURATIONS.items() if v == value), None)


def step_head(n, text):
    html(f'<div class="step-h"><span class="n">{n}</span>{esc(text)}</div>')


def join_fr(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " et " + items[-1]


with tab_session:
    s = active_session() or scheduled_session()
    me_key = "teen" if is_teen else "tutor"
    teen_todos = is_teen and bool(get_setting("todolist_created", ""))
    todos_first = teen_todos and cfg["module"] == "scolaire"

    # Décisions communes qui attendent une réponse : une seule ligne, en tête de page
    mine, _theirs, palier_mine, _palier_theirs = decisions_for(me_key)
    waiting = [r["title"] for r in mine]
    if palier_mine:
        waiting.append(f"Palier {palier_target(cfg)} · {PALIER_NAMES[palier_target(cfg)]}")
    if waiting:
        verb = ("t'attend" if is_teen else "vous attend") if len(waiting) == 1 else ("t'attendent" if is_teen else "vous attendent")
        html(f'<div class="together"><b>À faire ensemble</b> · {plural(len(waiting), "décision", "décisions")} {verb} : '
             f'{esc(" · ".join(waiting))} <span class="go">→ onglets Règles et Réglages</span></div>')

    main, side = st.columns([3, 2], gap="large")

    with main:
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
            if todos_first:
                render_todolist(instant_reward)
        elif is_teen:
            heading(f"Salut {nickname} 👋", "Commencer une session", "Tu choisis, tu décides de la durée.")
            last = query("SELECT * FROM sessions WHERE status='completed' ORDER BY ended_at DESC LIMIT 1")
            if instant_reward and last and now() - datetime.fromisoformat(last[0]["ended_at"]) < timedelta(minutes=30):
                html(f'<div class="note warn"><div class="eyebrow">Bravo</div><div class="h-title" style="font-size:20px">'
                     f'Session terminée : +{last[0]["xp"]} XP 🎉</div><p class="sub">{esc(last[0]["goal"])}</p></div>')
            if todos_first:
                render_todolist(instant_reward)
            with st.container(key="card_start"):
                step_head(1, "Quoi mettre en pause")
                apps = st.pills("Applications à mettre en pause", APPS, selection_mode="multi",
                                default=cfg["apps"], key="session_apps", label_visibility="collapsed")
                with st.expander("+ Autre application"):
                    other = st.text_input("Autre application (facultatif)", key="session_other")
                step_head(2, "Pour quoi faire")
                goal = st.pills("Objectif de la session", cfg["objectives"] + [GOAL_OTHER], key="session_goal",
                                label_visibility="collapsed")
                if goal == GOAL_OTHER:
                    goal = st.text_input("Précisez l'objectif") or GOAL_OTHER
                step_head(3, "Combien de temps, et quand")
                hero = st.empty()
                if "session_duration" not in st.session_state:
                    st.session_state["session_duration"] = 45
                    st.session_state["dur_quick"] = "45 min"
                st.pills("Durées rapides", list(QUICK_DURATIONS), selection_mode="single", key="dur_quick",
                         on_change=on_quick_duration, label_visibility="collapsed")
                st.slider("Durée", 15, 180, step=5, key="session_duration", on_change=on_slider_duration,
                          format="%d min", label_visibility="collapsed")
                duration = st.session_state["session_duration"]
                when = st.pills("Démarrage", ["Maintenant", "Plus tard"], default="Maintenant", key="session_when",
                                label_visibility="collapsed")
                delay = 0
                if when == "Plus tard":
                    later = st.pills("Dans combien de temps ?", [k for k in START_CHOICES if k != "Maintenant"],
                                     default="Dans 15 min", key="session_later", label_visibility="collapsed")
                    delay = START_CHOICES[later or "Dans 15 min"]
                start_dt = now() + timedelta(minutes=delay)
                end_dt = start_dt + timedelta(minutes=duration)
                pause_txt = (f"Pause de {cfg['pauses'][1]} min toutes les {cfg['pauses'][0]} min"
                             if cfg["pauses"] else "Pas de pause automatique")
                hero.markdown(
                    f'<div class="dur-hero"><b>{duration}</b><span>min</span></div>'
                    f'<p class="dur-sub">Jusqu\'à {end_dt:%H:%M} · {pause_txt}</p>', unsafe_allow_html=True)
            if teen_todos and not todos_first:
                render_todolist(instant_reward)
            render_highlight(cfg)
        else:
            heading("Bonjour 👋", "Comment se passe la semaine ?")
            html(
                '<div class="note"><div class="h-title" style="font-size:20px">🔒 Aucune session en cours</div>'
                '<p class="sub">Seul l\'adolescent peut activer une pause : c\'est un choix volontaire, '
                "jamais une surprise. Retrouvez l'historique dans l'onglet Tableau.</p></div>"
            )

    with side:
        if s:
            if teen_todos and not todos_first:
                render_todolist(instant_reward)
        elif is_teen:
            # Le résumé reste sous les yeux et porte le seul bouton qui compte
            apps_all = list(apps or []) + ([other.strip()] if other.strip() else [])
            ready = bool(apps_all) and bool(goal)
            apps_line = (f'<div class="line">Je mets en pause {esc(join_fr(apps_all))}</div>' if apps_all
                         else '<div class="line missing">Choisis au moins une application</div>')
            goal_line = (f'<div class="line">Objectif : {esc(goal)}</div>' if goal
                         else '<div class="line missing">Choisis un objectif</div>')
            when_line = "Démarre maintenant" if delay == 0 else f"Démarre à {start_dt:%H:%M}"
            with st.container(key="summary"):
                html(
                    f'<div class="summary"><div class="eyebrow">Ma session</div>'
                    f'<div class="big-dur">{duration}<small>min</small></div>{apps_line}{goal_line}'
                    f'<div class="line">{when_line} · fin à {end_dt:%H:%M}<small>{esc(pause_txt)}</small></div></div>'
                )
                label = f"Lancer ma session de {duration} min" if delay == 0 else f"Programmer pour {start_dt:%H:%M}"
                if st.button(label, type="primary", disabled=not ready, key="launch"):
                    start_session(apps_all, goal, duration, delay)
                    st.rerun()
                st.caption("Tu peux l'arrêter à tout moment." if ready
                           else "Choisis une application et un objectif pour continuer.")
        else:
            render_highlight(cfg)

# --- Onglet Tableau partagé (niveau de détail selon la Q8 de l'ado) ---
with tab_board:
    heading("Tableau partagé", "Temps activés volontairement",
            "Identique pour l'adolescent et les tuteurs : un support de discussion, ni surveillance ni sanction.")
    sessions = query("SELECT * FROM sessions WHERE status!='scheduled' ORDER BY start DESC")
    level = cfg["stats"]
    left_blocks, right_blocks = [], []
    if not sessions:
        left_blocks.append('<div class="note"><p class="sub" style="margin:0">Aucune session pour l\'instant.</p></div>')
    else:
        n_done = sum(x["status"] == "completed" for x in sessions)
        n_stop = sum(x["status"] == "stopped" for x in sessions)
        if level in ("detail", "synthese"):
            rate = round(100 * n_done / (n_done + n_stop)) if (n_done + n_stop) else 0
            circ = 2 * 3.14159 * 52
            left_blocks.append(
                '<div class="card"><div class="eyebrow" style="margin-top:0">Engagements tenus</div><div class="kpi">'
                '<div class="ring"><svg width="130" height="130" viewBox="0 0 130 130">'
                '<circle cx="65" cy="65" r="52" fill="none" stroke="#E6EAF2" stroke-width="12"/>'
                f'<circle cx="65" cy="65" r="52" fill="none" stroke="#4F4CC0" stroke-width="12" stroke-linecap="round" '
                f'stroke-dasharray="{circ * rate / 100:.1f} {circ:.1f}"/></svg>'
                f'<div class="center"><b>{rate}%</b><small>des sessions</small></div></div>'
                '<div class="stats">'
                f'<div class="stat"><b>{len(sessions)}</b><span>Sessions</span></div>'
                f'<div class="stat"><b>{n_done}</b><span>Menées à terme</span></div>'
                f'<div class="stat"><b>{n_stop}</b><span>Arrêtées avant la fin</span></div></div></div></div>'
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
                + (f'<i style="height:{max(14, 100 * m / peak):.0f}px"></i>' if m else '<i class="empty"></i>')
                + f"<span>{DAY_LETTERS[d.weekday()]}</span></div>"
                for d, m in minutes.items()
            )
            left_blocks.append(sec("7 derniers jours",
                f'<div class="bars">{cols}</div>'
                '<p class="sub">Minutes de pause volontaire. Les jours sans barre sont des périodes libres.</p>'))
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
            right_blocks.append(sec("Historique", f'<div class="timeline">{items}</div>'))
        if level == "minimal":
            # Tension forte : peu de chiffres, les accords pris et ce qui a fonctionné, une amorce à date fixe.
            goals = [x["goal"] for x in sessions if x["status"] == "completed"]
            worked = (f"{len(goals)} session(s) menée(s) à terme, surtout pour : {esc(max(set(goals), key=goals.count))}."
                      if goals else "Les premières réussites apparaîtront ici.")
            left_blocks.append(sec("Ce qui a fonctionné", f'<p class="sub" style="margin:0">{worked}</p>'))
            sunday = now().date() + timedelta(days=(6 - now().weekday()) % 7)
            starter = DISCUSSION_STARTERS[(sunday.toordinal() // 7) % len(DISCUSSION_STARTERS)]
            right_blocks.append(
                '<div class="note"><div class="eyebrow">Rendez-vous de discussion</div>'
                f'<div class="h-title" style="font-size:24px">{esc(fr_date(sunday).capitalize())}</div>'
                f'<p class="sub">{esc(starter)}</p></div>')
    # Reports « je finis ma partie » : visibles par tous, quel que soit le niveau de détail
    reports = query("SELECT * FROM postpones ORDER BY id DESC LIMIT 5")
    if reports:
        rows = "".join(
            f'<div class="tl-item"><div class="tl-dot stopped"></div><div><b>{esc(r["goal"])}</b>'
            f'<small>{r["at"].replace("T", " ")[5:16]} · report de {r["minutes"]} min</small></div></div>'
            for r in reports
        )
        block = sec("Reports « je finis ma partie »",
                    f'<div class="timeline">{rows}</div><p class="sub">{quota_left()} report(s) restant(s) cette semaine.</p>')
        right_blocks.append(block)
    decided = ""
    pal = palier_current()
    if pal:
        decided += (f'<div class="tl-item"><div class="tl-dot"></div><div><b>Palier {pal} · {esc(PALIER_NAMES[pal])}</b>'
                    f'<small>Signé à deux{" · " + short_date(get_setting(f"palier_date_{pal}", "")) if get_setting(f"palier_date_{pal}", "") else ""}</small></div></div>')
    for r in query("SELECT * FROM rules ORDER BY id DESC"):
        if rule_is_active(r):
            decided += (f'<div class="tl-item"><div class="tl-dot"></div><div><b>{esc(r["title"])}</b>'
                        f'<small>Signé à deux{" · " + short_date(r["signed_at"]) if r["signed_at"] else ""}</small></div></div>')
    right_blocks.insert(0, sec("Décidé ensemble", f'<div class="timeline">{decided}</div>' if decided else
                               '<p class="sub" style="margin:0">Pas encore d\'accord : c\'est un bon sujet pour la prochaine discussion.</p>'))
    col_l, col_r = st.columns([2, 3], gap="large")
    with col_l:
        for block in left_blocks:
            html(block)
    with col_r:
        for block in right_blocks:
            html(block)

# --- Onglet Règles : ce qu'on décide ensemble, et ce qui attend une réponse ---
def render_rule_row(r, my_col, kind):
    """kind : 'mine' (à ma réponse), 'theirs' (en attente de l'autre) ou 'active' (signé à deux)."""
    with st.container(key=f"row_rule_{r['id']}"):
        if r["proposed_by"] == "teen":
            tag = '<span class="tag ado">Proposée par l\'ado</span>'
        elif r["proposed_by"] == "app":
            tag = '<span class="tag app">Suggérée par l\'application</span>'
        else:
            tag = '<span class="tag par">Proposée par le tuteur</span>'
        status = ""          # les titres de zone (« À toi de répondre », « En attente… ») disent déjà l'état
        if kind == "active":
            when_signed = f" · {short_date(r['signed_at'])}" if r["signed_at"] else ""
            status = f'<span class="signed">Signé à deux{when_signed}</span>'
        html(
            f'<div><div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">'
            f'<div class="h-title" style="font-size:20px">{esc(r["title"])}</div><div>{status}</div></div>'
            f'<p class="sub">{esc(r["apps"])} · {esc(r["slot"])}</p>'
            f'<div style="margin-top:8px">{tag}</div>{sigs_html(r)}</div>'
        )
        c1, c2, _ = st.columns([1, 1, 1.4])
        if kind == "mine":
            if c1.button("Approuver", key=f"approve{r['id']}", type="primary"):
                set_rule_ok(r["id"], my_col, 1)
                st.rerun()
        elif c1.button("Retirer mon accord", key=f"withdraw{r['id']}"):
            set_rule_ok(r["id"], my_col, 0)
            st.rerun()
        if kind != "active" and c2.button("Supprimer", key=f"delete{r['id']}"):
            execute("DELETE FROM rules WHERE id=?", (r["id"],))
            st.rerun()


with tab_rules:
    heading("Co-décision", "Nos règles, à deux" if is_teen else "Nos accords, à deux",
            "Une règle n'est valable que lorsque l'adolescent et un tuteur l'ont tous deux approuvée.")
    my_col = "teen_ok" if is_teen else "parent_ok"
    col_l, col_r = st.columns([3, 2], gap="large")
    with col_l:
        all_rules = query("SELECT * FROM rules ORDER BY id DESC")
        to_answer = [r for r in all_rules if not rule_is_active(r) and not r[my_col]]
        waiting_other = [r for r in all_rules if not rule_is_active(r) and r[my_col]]
        signed = [r for r in all_rules if rule_is_active(r)]
        if to_answer:
            html(f'<div class="accords-h">{"À toi" if is_teen else "À vous"} de répondre</div>')
            for r in to_answer:
                render_rule_row(r, my_col, "mine")
        if waiting_other:
            html(f'<div class="accords-h">En attente de {esc("votre tuteur" if is_teen else nickname)}</div>')
            for r in waiting_other:
                render_rule_row(r, my_col, "theirs")
        pal = palier_current()
        html('<div class="accords-h">Nos accords</div>')
        if not signed and not pal:
            html('<div class="note"><p class="sub" style="margin:0">Rien de signé pour l\'instant : '
                 "proposez une règle, elle sera valable dès que vous l'aurez approuvée tous les deux.</p></div>")
        else:
            with st.container(key="card_accords"):
                if pal:
                    pal_date = get_setting(f"palier_date_{pal}", "")
                    html(
                        f'<div style="padding:4px 0 14px;border-bottom:1px solid var(--line)"><div style="display:flex;'
                        f'justify-content:space-between;gap:10px"><div class="h-title" style="font-size:20px">'
                        f'Palier {pal} · {esc(PALIER_NAMES[pal])}</div><span class="signed">Signé à deux'
                        f'{" · " + short_date(pal_date) if pal_date else ""}</span></div>'
                        f'<p class="sub">{esc(PALIER_MEANING[pal][0 if is_teen else 1])}</p>'
                        f'{sigs_html({"teen_ok": 1, "parent_ok": 1})}</div>'
                    )
                for r in signed:
                    render_rule_row(r, my_col, "active")
    with col_r:
        if st.button("Revoir nos réponses", key="review_answers"):
            st.session_state["review_crossing"] = True
            st.rerun()
        with st.container(key="card_newrule"):
            html('<div class="h-title" style="font-size:20px">Proposer une règle</div>')
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

# --- Onglet Progression : une histoire (où j'en suis, ce qui m'attend), pas un inventaire ---
def path_html(current, target, avatar, gauge, next_proposed):
    """Le chemin d'autonomie : trois paliers reliés, l'avatar sur le palier actuel."""
    out = ""
    for n in (1, 2, 3):
        date = short_date(get_setting(f"palier_date_{n}", ""))
        signed_txt = "validé à deux" + (f" · {date}" if date else "")
        slot = ""
        if current and n < current:
            cls, sub = "done", signed_txt
        elif current == n:
            cls, sub, slot = "here", signed_txt, esc(avatar)
        elif current is None and n == target:
            cls, sub, slot = "next", "proposé pour commencer", esc(avatar)
        elif current and n == current + 1:
            cls, sub = "next", "prêt à valider" if next_proposed else "prochain palier"
        else:
            cls, sub = "later", ""
        out += (f'<div class="stop {cls}"><div class="slot">{slot}</div><i></i><b>Palier {n}</b>'
                f'<span>{esc(PALIER_NAMES[n])}</span><small>{sub}</small></div>')
        if n < 3:
            if current and n < current:
                fill = 100
            elif current == n:
                fill = 100 if next_proposed else gauge
            else:
                fill = 0
            out += f'<div class="link"><em class="h" style="width:{fill}%"></em><em class="v" style="height:{fill}%"></em></div>'
    return f'<div class="path">{out}</div>'


with tab_progress:
    finished = query("SELECT * FROM sessions WHERE status IN ('completed', 'stopped') ORDER BY start DESC")
    done = [x for x in finished if x["status"] == "completed"]
    xp = total_xp()
    level_n, level_name, xp_in_level = level_info(xp)
    avatar = get_setting("avatar", AVATARS[0])
    me = "teen" if is_teen else "tutor"
    voice = 0 if is_teen else 1
    current = palier_current()
    target = palier_target(cfg)
    next_proposed = bool(get_setting("palier_target", "")) and current is not None
    gauge = gauge_percent()

    # 1. Où j'en suis
    heading("Progression", "Ton parcours" if is_teen else "Son parcours")
    shown = current or target
    now_line = PALIER_NOW[current][voice] if current else f"Point de départ proposé : palier {target}."
    level_line = f"Niveau {level_n} · {esc(level_name)}" if level_reward else "XP gagnés"
    html(
        f'<div class="where"><div class="avatar">{esc(avatar)}</div><div class="txt">'
        f'<div class="eyebrow" style="margin:0 0 4px">Où j\'en suis</div>'
        f'<div class="h-title">Palier {shown} · {esc(PALIER_NAMES[shown])}</div>'
        f'<p class="sub">{esc(now_line)}</p></div>'
        f'<div class="xp"><b>{xp} XP</b><span>{level_line}</span></div></div>'
    )

    # 2. Le chemin d'autonomie
    html(path_html(current, target, avatar, gauge, next_proposed))

    # 3. Ce qui m'attend : le prochain palier, au centre
    pending = current is None or next_proposed
    upcoming = target if pending else (current + 1 if current and current < 3 else None)
    with st.container(key="card_next"):
        if upcoming:
            eyebrow = ("Votre point de départ" if current is None
                       else "Prêt pour le palier suivant" if next_proposed else "Prochain palier")
            html(
                f'<div class="nextp"><div class="eyebrow">{eyebrow}</div>'
                f'<div class="h-title">Palier {upcoming} · {esc(PALIER_NAMES[upcoming])}</div>'
                f'<div class="lbl">Ce que ça veut dire</div><p class="sub" style="margin:0;color:var(--ink)">'
                f'{esc(PALIER_MEANING[upcoming][voice])}</p></div>'
            )
            if pending:
                html(sigs_html({"teen_ok": int(palier_validated_by("teen")), "parent_ok": int(palier_validated_by("tutor"))}))
                if not palier_validated_by(me):
                    if st.button("Valider ce palier", type="primary", key="palier_validate_prog"):
                        validate_palier(me, cfg)
                        st.rerun()
                else:
                    html('<p class="sub">Ta signature est posée : on attend l\'autre.</p>' if is_teen
                         else '<p class="sub">Votre signature est posée : on attend ' + esc(nickname) + '.</p>')
            else:
                word = gauge_word(gauge)
                words = ["Début", "En route", "Presque", "Prêt"]
                marks = "".join(f'<span class="{"on" if words.index(w) <= words.index(word) else ""}">{w}</span>' for w in words)
                html(
                    '<div class="nextp"><div class="lbl">Comment avancer</div>'
                    f'<p class="sub" style="margin:0;color:var(--ink)">Chaque jour où {"tu tiens" if is_teen else "votre ado tient"} '
                    "un engagement fait avancer le chemin.</p>"
                    f'<div class="xpbar" style="margin-top:14px"><i style="width:{gauge}%"></i></div>'
                    f'<div class="gauge-words">{marks}</div></div>'
                )
        else:
            html(
                '<div class="nextp"><div class="eyebrow">Palier 3 atteint</div>'
                f'<div class="h-title">{esc(PALIER_NOW[3][voice])}</div>'
                '<p class="sub">Ce qui compte maintenant : la régularité.</p></div>'
            )

    # 4. L'effort raconté par l'XP, et les souvenirs de route
    col_l, col_r = st.columns([1, 1], gap="large")
    with col_l:
        week_sessions = [x for x in done if x["start"][:10] >= week_monday().isoformat()]
        week_xp = sum(x["xp"] for x in week_sessions)
        n_tasks = query("SELECT COUNT(*) AS n FROM todos WHERE done=1")[0]["n"]
        if week_sessions:
            bilan = (f"{plural(len(week_sessions), 'session tenue', 'sessions tenues')}, +{week_xp} XP")
        else:
            bilan = "Pas encore de session menée à terme cette semaine"
        tasks_line = f'<p class="sub">{plural(n_tasks, "tâche cochée", "tâches cochées")} au total · +{TODO_XP} XP chacune.</p>'
        html(sec("Cette semaine", f'<p class="sub" style="margin:0;color:var(--ink);font-weight:600;font-size:17px">{esc(bilan)}</p>{tasks_line}'))
        if level_reward:
            steps = "".join(
                f'<div class="step {"on" if level_n - 1 >= n else "off"}"><i></i><b>{esc(name)}</b>'
                f"<small>{n * 100} XP</small></div>"
                for n, name in enumerate(LEVELS)
            )
            html(sec("Niveaux", f'<div class="steps">{steps}</div>'
                     f'<p class="sub">{xp_in_level} XP sur 100 · encore {100 - xp_in_level} XP avant le niveau suivant.</p>'))
        if instant_reward:
            rows = "".join(
                f'<div class="tl-item"><div class="tl-dot"></div><div><b>{esc(x["goal"])}</b>'
                f'<small>+{x["xp"]} XP · session menée à terme</small></div></div>' for x in done[:5]
            ) or '<p class="sub" style="margin:0">Tes prochaines récompenses apparaîtront ici.</p>'
            html(sec("Récompenses récentes", f'<div class="timeline">{rows}</div>'))
        html('<p class="sub">Arrêter plus tôt ne retire aucun point : c\'est une information, pas une sanction.</p>')
        if is_teen:
            with st.expander("Modifier mon avatar"):
                choice = st.pills("Avatar", AVATARS, default=avatar)
                new_name = st.text_input("Pseudo", value=nickname)
                if choice and choice != avatar:
                    set_setting("avatar", choice)
                    st.rerun()
                if new_name.strip() and new_name.strip() != nickname:
                    set_setting("nickname", new_name.strip())
                    st.rerun()
    with col_r:
        active_rules = [r for r in query("SELECT * FROM rules") if rule_is_active(r)]
        badges_html = "".join(
            f'<div class="opt{"" if earned else " todo"}"><div class="emoji">{"🏅" if earned else "○"}</div>'
            f'<div><b>{esc(name)}</b><small>{esc(desc)}</small>'
            f'{"" if earned else f"<span class=prog>{prog}</span>"}</div></div>'
            for name, desc, earned, prog in compute_badges(done, active_rules)
        )
        html(sec("Souvenirs de route", f'<div class="badge-grid" style="grid-template-columns:1fr">{badges_html}</div>'))


# --- Onglet Réglages : ce que les réponses règlent ---
def dl_row(title, text, caption="", chips=None):
    chips_html = f'<div class="chips">{pills_html(chips)}</div>' if chips else ""
    caption_html = f"<small>{esc(caption)}</small>" if caption else ""
    return (f'<div class="dl-row"><div class="dl-k">{esc(title)}</div>'
            f'<div class="dl-v">{esc(text)}{chips_html}{caption_html}</div></div>')


def render_dl(rows):
    html('<div class="dl">' + "".join(rows) + "</div>")


with tab_settings:
    me = "teen" if is_teen else "tutor"
    heading("Paramétrage", "Ce que vos réponses règlent",
            "Chaque réglage vient du questionnaire. Rien n'est caché, et tout peut être rediscuté ensemble.")
    col_l, col_r = st.columns([2, 3], gap="large")

    with col_l:
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
            note = ('<p class="sub" style="color:var(--amber-ink);font-weight:600">Les réponses ne sont pas identiques : '
                    "le palier le plus bas est proposé, et c'est le premier sujet à discuter.</p>")
        status = ""
        if pending:
            status = (f'<div style="margin-top:12px"><span class="pill {"" if palier_validated_by("teen") else "muted"}">Ado '
                      f'{"✓" if palier_validated_by("teen") else "en attente"}</span>'
                      f'<span class="pill {"" if palier_validated_by("tutor") else "muted"}">Tuteur '
                      f'{"✓" if palier_validated_by("tutor") else "en attente"}</span></div>')
        html(f'<div class="card"><div class="eyebrow" style="margin-top:0">{esc(eyebrow)}</div>'
             f'<div class="h-title" style="font-size:26px">{esc(p_title)}</div><p class="sub">{esc(p_text)}</p>{note}{status}</div>')
        if pending and not palier_validated_by(me):
            if st.button("Valider ce palier", type="primary", key="palier_validate"):
                validate_palier(me, cfg)
                st.rerun()

    with col_r:
        rows_a = [
            dl_row("Objectifs proposés", "Ils apparaissent quand on lance une session.",
                   "D'après : ce que l'ado fait en dehors des écrans.", cfg["objectives"]),
            dl_row("Todolist", "Elle apparaît à la première activation d'une session, pré-remplie. "
                   "L'ado peut tout renommer, supprimer ou compléter.",
                   "D'après : ce que l'ado fait en dehors des écrans.",
                   default_todos(cfg) or ["Aucune tâche pré-remplie"]),
            dl_row("Rappels",
                   f"Préavis à 10 et 5 minutes avant un créneau : {ALERT_TEXT[cfg['alert']]}. "
                   f"« Je finis ma partie » reporte le début de {POSTPONE_MINUTES} minutes, "
                   f"{POSTPONE_QUOTA} fois par semaine, une fois par créneau.",
                   "D'après : ce qui aide à s'arrêter (l'option la plus soutenante parmi les réponses)."),
        ]
        if cfg["pauses"]:
            interval, brk = cfg["pauses"]
            rows_a.append(dl_row("Pauses", f"Une pause courte de {brk} minutes est proposée toutes les {interval} minutes pendant une session.",
                                 "D'après : le temps de concentration (la valeur la plus basse parmi les réponses)."))
        else:
            rows_a.append(dl_row("Pauses", "Pas de pause automatique.",
                                 "D'après : le temps de concentration (la valeur la plus basse parmi les réponses)."))
        rows_a.append(dl_row("Récompenses", REWARD_TEXT[cfg["reward"]], "D'après : la réponse de l'ado."))
        rows_a.append(dl_row("Blocage nocturne", NIGHT_TEXT[cfg["night"]],
                             "D'après : la réponse la plus élevée parmi les réponses."))
        render_dl(rows_a)
        has_night_rule = any(r["title"] == NIGHT_RULE_TITLE for r in query("SELECT title FROM rules"))
        if cfg["night"] == "parfois" and not has_night_rule:
            if st.button("Proposer le blocage nocturne", key="night_propose"):
                create_night_rule(who, int(is_teen), int(not is_teen))
                st.rerun()
        render_dl([
            dl_row("Tableau partagé", STATS_TEXT[cfg["stats"]], "D'après : les tensions à la maison déclarées par l'ado."),
            dl_row("Applications présélectionnées",
                   "Elles sont cochées au moment de lancer une session, à valider une par une.",
                   "D'après : ce que l'ado aime faire sur ses écrans.",
                   cfg["apps"] or ["Aucune : l'ado choisit librement"]),
            dl_row("Module mis en avant", MODULES[cfg["module"]][0], "D'après : la préoccupation principale du tuteur."),
        ])

html(
    '<div class="hint"><b>Prototype</b> · le blocage réel des applications n\'est pas encore branché : '
    "les sessions sont enregistrées et partagées, rien n'est bloqué sur l'appareil.</div>"
)
