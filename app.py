"""Tracker de pool LNH — usage personnel.

Sources : API publique LNH (stats officielles, TOI PP/PK, Corsi/Fenwick)
          + MoneyPuck (xG, xGF%, GSAx) en option.
Lancer : double-clic sur LANCER.bat (Windows) ou lancer.command (Mac).
"""
import html as html_mod
import io
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

BASE = Path(__file__).parent
LIGUES_DIR = BASE / "ligues"
IDS_FILE = BASE / "ids_joueurs.json"
UA = {"User-Agent": "Mozilla/5.0 (tracker de pool personnel)"}
STATS = "https://api.nhle.com/stats/rest/en"
WEB = "https://api-web.nhle.com/v1"
SEARCH = "https://search.d3.nhle.com/api/v1/search/player"
HSC = "https://hockeystatcards.com"
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{an}/regular/{type}.csv"

st.set_page_config(page_title="Tracker pool LNH", page_icon="🏒", layout="wide")


# ---------------------------------------------------------------- utilitaires
def norm(nom: str) -> str:
    """Nom comparable : sans accents, sans (R), minuscules, sans ponctuation."""
    nom = re.sub(r"\([rca]\)", "", nom, flags=re.I)
    nom = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    nom = re.sub(r"[^a-z ]", " ", nom.lower())
    return " ".join(nom.split())


def mmss(sec):
    if sec is None or pd.isna(sec):
        return ""
    sec = int(round(float(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def toi_sec(txt):
    """'18:34' -> 1114 secondes."""
    try:
        m, s = str(txt).split(":")
        return int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return None


def pct(x):
    """Accepte 0.523 ou 52.3, retourne 52.3."""
    if x is None or pd.isna(x):
        return None
    x = float(x)
    return round(x * 100, 1) if abs(x) <= 1 else round(x, 1)


def pct_maj(nb, pct_fo, pct_sommaire):
    """% mises au jeu; vide si le joueur n'en prend pas."""
    if nb is not None and not pd.isna(nb) and float(nb) == 0:
        return None
    v = pct_fo if pct_fo is not None and not pd.isna(pct_fo) else pct_sommaire
    return pct(v)


def get_json(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def saison_libelle(s: int) -> str:
    s = str(s)
    return f"{s[:4]}-{s[6:]}"


def saison_courante() -> int:
    d = datetime.now()
    debut = d.year if d.month >= 9 else d.year - 1
    return debut * 10000 + debut + 1


# ---------------------------------------------------------------- alignements
def lire_ligue(path: Path):
    groupe, joueurs = "Alignement", []
    for ligne in path.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", ligne)
        if m:
            groupe = m.group(1).strip()
            continue
        nom = re.sub(r"\([RCA]\)", "", ligne, flags=re.I).strip()
        joueurs.append({"Groupe": groupe, "Joueur": nom, "cle": norm(nom)})
    return joueurs


def charger_ids():
    if IDS_FILE.exists():
        try:
            return {norm(k): int(v) for k, v in json.loads(IDS_FILE.read_text(encoding="utf-8")).items()}
        except (ValueError, json.JSONDecodeError):
            st.warning("ids_joueurs.json est mal formé — il est ignoré.")
    return {}


# ---------------------------------------------------------------- données LNH
@st.cache_data(ttl=3600, show_spinner=False)
def rapport(categorie: str, nom_rapport: str, saison: int, filtre_extra: str = "", par_match=False):
    """Rapport du site stats LNH, paginé. categorie = skater | goalie."""
    exp = f"gameTypeId=2 and seasonId={saison}" + (f" and {filtre_extra}" if filtre_extra else "")
    lignes, debut = [], 0
    while True:
        js = get_json(
            f"{STATS}/{categorie}/{nom_rapport}",
            {
                "isAggregate": "false",
                "isGame": "true" if par_match else "false",
                "start": debut,
                "limit": 100,
                "sort": json.dumps([{"property": "playerId", "direction": "ASC"}]),
                "cayenneExp": exp,
            },
        )
        data = js.get("data", [])
        lignes += data
        debut += len(data)
        if not data or debut >= js.get("total", 0):
            break
    return pd.DataFrame(lignes)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def moneypuck(type_: str, saison: int):
    an = str(saison)[:4]
    r = requests.get(MONEYPUCK.format(an=an, type=type_), headers=UA, timeout=40)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def chercher_id(nom: str):
    js = get_json(SEARCH, {"culture": "en-us", "limit": 20, "q": nom})
    cands = [p for p in js if norm(p.get("name", "")) == norm(nom)]
    if not cands:
        return None
    cands.sort(key=lambda p: not p.get("active", False))
    return int(cands[0]["playerId"])


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fiche(pid: int):
    return get_json(f"{WEB}/player/{pid}/landing")


@st.cache_data(ttl=1800, show_spinner=False)
def journal(pid: int, saison: int):
    return pd.DataFrame(get_json(f"{WEB}/player/{pid}/game-log/{saison}/2").get("gameLog", []))


# ---------------------------------------------------------------- HockeyStatCards
def get_hsc(chemin, params=None):
    """Requête HSC polie : reprises avec délai si le site limite le débit (429) ou flanche (5xx)."""
    for attente in (0, 2, 5, 10):
        if attente:
            time.sleep(attente)
        r = requests.get(f"{HSC}{chemin}", params=params, headers=UA, timeout=25)
        if r.status_code == 429 or r.status_code >= 500:
            continue
        r.raise_for_status()
        return r.text
    r.raise_for_status()
    return r.text


def texte_page(html):
    """HTML -> texte brut sans accents, un espace entre chaque élément."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    txt = html_mod.unescape(re.sub(r"<[^>]+>", " ", html))
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return " ".join(txt.split())


NB = r"([+-]?\d+(?:\.\d+)?)"


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def hsc_joueur(pid: int, saison: int):
    """Game Score moyen et cotes offensive/défensive avec leur centile."""
    t = texte_page(get_hsc(f"/players/{pid}", {"year": saison, "type": 2}))
    out = {}
    for cle, mot in (("gs", "Game score"), ("off", "Offense"), ("def", "Defense")):
        m = (re.search(rf"{mot}\s+{NB}\s+(\d+)\s*%", t, re.I)
             or re.search(rf"{NB}\s+{mot}(?: Rating)?\s+Percentile\s+(\d+)", t, re.I))
        if m:
            out[cle], out[cle + "_c"] = float(m.group(1)), int(m.group(2))
    return out


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def hsc_match(game_id: int):
    return texte_page(get_hsc(f"/games/{game_id}"))


def gs_du_match(game_id, nom):
    texte = securiser(hsc_match, int(game_id))
    if not texte:
        return None
    nom = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    m = re.search(re.escape(nom) + r"\s+(-?\d+\.\d+)", texte, re.I)
    return float(m.group(1)) if m else None


POS_GROUPE = {"C": "A", "L": "A", "R": "A", "F": "A", "LW": "A", "RW": "A", "D": "D"}


def lignes_tableau(html):
    """Lit le tableau HTML des joueurs HSC : [(playerId, {colonne: texte})]."""
    entetes, out = [], []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html):
        cellules = [texte_page(c) for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", tr)]
        if not entetes and "Pos" in cellules and "GS" in cellules:
            entetes = cellules
            continue
        m = re.search(r"/players/(\d+)", tr)
        if entetes and m and len(cellules) == len(entetes):
            out.append((int(m.group(1)), dict(zip(entetes, cellules))))
    return out


def page_hsc(saison, extra=None):
    params = {"year": saison, "type": 2, **(extra or {})}
    return get_hsc("/players", params)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def classement_hsc(saison):
    """Classement complet HSC (patineurs, 5 MJ et +) : DataFrame playerId, pos, gs."""
    html1 = page_hsc(saison)
    lignes = lignes_tableau(html1)
    if not lignes:
        raise ValueError("tableau HSC illisible")
    m = re.search(r"Page\s+1\s+of\s+(\d+)", texte_page(html1))
    nb_pages = int(m.group(1)) if m else 1
    ids_p1 = {pid for pid, _ in lignes}
    taille = len(lignes)

    # Trouver le paramètre de pagination qui fonctionne
    regle = None
    if nb_pages > 1:
        for essai in (lambda n: {"page": n}, lambda n: {"p": n}, lambda n: {"pg": n},
                      lambda n: {"offset": (n - 1) * taille}, lambda n: {"start": (n - 1) * taille}):
            l2 = lignes_tableau(page_hsc(saison, essai(2)))
            if l2 and not ({pid for pid, _ in l2} & ids_p1):
                regle = essai
                break
        if regle is None:
            raise ValueError("pagination HSC introuvable")
        with ThreadPoolExecutor(max_workers=4) as ex:
            for l in ex.map(lambda n: lignes_tableau(page_hsc(saison, regle(n))), range(2, nb_pages + 1)):
                lignes += l

    df = pd.DataFrame([{"playerId": pid, "pos": c.get("Pos", ""),
                        "gs": pd.to_numeric(c.get("GS"), errors="coerce")} for pid, c in lignes])
    return df.drop_duplicates("playerId").dropna(subset=["gs"])


def classement_moneypuck(d):
    """Plan B : Game Score recalculé (formule de Luszczyszyn) avec MoneyPuck."""
    tous = d.get("mp_sk_tout", pd.DataFrame())
    if tous.empty:
        return pd.DataFrame()
    a = tous[tous["situation"] == "all"].set_index("playerId")
    e = tous[tous["situation"] == "5on5"].set_index("playerId")
    def g(df, c):
        return df[c].reindex(a.index).fillna(0) if c in df else pd.Series(0.0, index=a.index)
    gs = (0.75 * g(a, "I_F_goals") + 0.7 * g(a, "I_F_primaryAssists") + 0.55 * g(a, "I_F_secondaryAssists")
          + 0.075 * g(a, "I_F_shotsOnGoal") + 0.05 * g(a, "shotsBlockedByPlayer")
          + 0.15 * g(a, "penaltiesDrawn") - 0.15 * g(a, "penalties")
          + 0.01 * g(a, "faceoffsWon") - 0.01 * g(a, "faceoffsLost")
          + (0.05 * g(e, "OnIce_F_shotAttempts") - 0.05 * g(e, "OnIce_A_shotAttempts")
             + 0.15 * g(e, "OnIce_F_goals") - 0.15 * g(e, "OnIce_A_goals")))
    mj = g(a, "games_played")
    pos = a["position"] if "position" in a else pd.Series("", index=a.index)
    df = pd.DataFrame({"playerId": a.index, "pos": pos, "gs": gs / mj.where(mj > 0), "mj": mj})
    return df[df["mj"] >= 5].dropna(subset=["gs"])


def rangs_par_position(classement):
    if classement is None or classement.empty:
        return {}
    c = classement.copy()
    c["grp"] = c["pos"].astype(str).str.upper().map(POS_GROUPE)
    c = c.dropna(subset=["grp"])
    c["rang"] = c.groupby("grp")["gs"].rank(ascending=False, method="min").astype(int)
    totaux = c["grp"].value_counts().to_dict()
    nom = {"A": "att.", "D": "déf."}
    return {int(pid): f"{r}e / {totaux[gp]} {nom[gp]}" for pid, r, gp in zip(c["playerId"], c["rang"], c["grp"])}


def source_rangs(d, saison):
    """Retourne (rangs, libellé source). HSC exact d'abord, sinon calcul MoneyPuck."""
    cl = securiser(classement_hsc, saison)
    if cl is not None and not cl.empty:
        return rangs_par_position(cl), "HockeyStatCards", dict(zip(cl["playerId"].astype(int), cl["gs"]))
    return rangs_par_position(classement_moneypuck(d)), "calculé avec MoneyPuck (HSC indisponible)", {}


def rangs_gardiens(d, min_mj=3):
    mp = d.get("mp_g", pd.DataFrame())
    if mp.empty or not {"xGoals", "goals"} <= set(mp.columns):
        return {}
    mp = mp.drop_duplicates("playerId").copy()
    if "games_played" in mp:
        mp = mp[mp["games_played"] >= min_mj]
    mp["gsax"] = mp["xGoals"] - mp["goals"]
    mp["rang"] = mp["gsax"].rank(ascending=False, method="min").astype(int)
    return {int(p): f"{r}e / {len(mp)}" for p, r in zip(mp["playerId"], mp["rang"])}


def ajouter_hsc(pat, saison, rangs=None, gs_classement=None):
    if pat.empty:
        return pat
    with ThreadPoolExecutor(max_workers=2) as ex:
        res = list(ex.map(lambda pid: securiser(hsc_joueur, int(pid), saison, defaut={}) or {}, pat["_pid"]))
    pos = pat.columns.get_loc("PTS/MJ") + 1
    for i, (col, cle) in enumerate((("GS/MJ", "gs"), ("Cent. OFF", "off_c"), ("Cent. DÉF", "def_c"))):
        pat.insert(pos + i, col, pd.to_numeric(pd.Series([r.get(cle) for r in res], index=pat.index), errors="coerce"))
    # GS/MJ : le classement complet (déjà chargé) est la source principale; la fiche du joueur sert de secours
    gs_cl = pd.Series([(gs_classement or {}).get(int(p)) for p in pat["_pid"]], index=pat.index, dtype="float")
    pat["GS/MJ"] = gs_cl.fillna(pat["GS/MJ"]).round(2)
    pat.insert(pos + 1, "Rang GS", [(rangs or {}).get(int(p), "") for p in pat["_pid"]])
    return pat


def securiser(fn, *args, defaut=None, erreurs=None, libelle=""):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — on veut que l'app reste debout
        if erreurs is not None:
            erreurs.append(f"{libelle} : {e}")
        return defaut


# ---------------------------------------------------------------- chargement
def donnees_saison(saison, erreurs):
    vide = pd.DataFrame()
    d = {
        "sk_sum": securiser(rapport, "skater", "summary", saison, defaut=vide, erreurs=erreurs, libelle="Stats patineurs"),
        "sk_toi": securiser(rapport, "skater", "timeonice", saison, defaut=vide, erreurs=erreurs, libelle="Temps de glace"),
        "sk_pct": securiser(rapport, "skater", "percentages", saison, defaut=vide, erreurs=erreurs, libelle="Corsi/Fenwick"),
        "sk_rt": securiser(rapport, "skater", "realtime", saison, defaut=vide, erreurs=erreurs, libelle="Mises en échec/tirs bloqués"),
        "sk_fo": securiser(rapport, "skater", "faceoffwins", saison, defaut=vide, erreurs=erreurs, libelle="Mises au jeu"),
        "g_sum": securiser(rapport, "goalie", "summary", saison, defaut=vide, erreurs=erreurs, libelle="Stats gardiens"),
        "mp_sk_tout": securiser(moneypuck, "skaters", saison, defaut=vide, erreurs=erreurs, libelle="MoneyPuck patineurs (xG)"),
        "mp_g_tout": securiser(moneypuck, "goalies", saison, defaut=vide, erreurs=erreurs, libelle="MoneyPuck gardiens (GSAx)"),
    }
    for cle in ("sk", "g"):
        t = d[f"mp_{cle}_tout"]
        d[f"mp_{cle}"] = t[t["situation"] == "all"].copy() if not t.empty and "situation" in t else vide
    return d


def index_noms(*dfs):
    idx = {}
    for df in dfs:
        if df is None or df.empty:
            continue
        col = "skaterFullName" if "skaterFullName" in df else "goalieFullName"
        for nom, pid in zip(df[col], df["playerId"]):
            idx.setdefault(norm(str(nom)), int(pid))
    return idx


def resoudre(joueurs, idx_noms, overrides):
    a_chercher = [j for j in joueurs if j["cle"] not in overrides and j["cle"] not in idx_noms]
    trouves = {}
    if a_chercher:
        with ThreadPoolExecutor(max_workers=8) as ex:
            res = ex.map(lambda j: securiser(chercher_id, j["Joueur"]), a_chercher)
            for j, pid in zip(a_chercher, res):
                trouves[j["cle"]] = pid
    for j in joueurs:
        j["playerId"] = overrides.get(j["cle"]) or idx_noms.get(j["cle"]) or trouves.get(j["cle"])
    return joueurs


def colonne(df, nom):
    return df[nom] if nom in df else pd.Series([None] * len(df), index=df.index)


def table_patineurs(roster, d):
    s = d["sk_sum"]
    if s.empty:
        return pd.DataFrame()
    s = s.drop_duplicates("playerId")
    df = roster.merge(s, on="playerId", how="inner")
    for cle, cols in (("sk_toi", ["ppTimeOnIcePerGame", "shTimeOnIcePerGame", "evTimeOnIcePerGame"]),
                      ("sk_pct", ["satPercentage", "usatPercentage"]),
                      ("sk_rt", ["hits", "blockedShots", "takeaways", "giveaways"])):
        src = d[cle]
        if not src.empty:
            garder = ["playerId"] + [c for c in cols if c in src]
            df = df.merge(src.drop_duplicates("playerId")[garder], on="playerId", how="left")
    fo = d.get("sk_fo", pd.DataFrame())
    if not fo.empty:
        fo = fo.drop_duplicates("playerId").rename(columns={"faceoffWinPct": "fo_pct"})
        garder = ["playerId"] + [c for c in ("totalFaceoffs", "totalFaceoffWins", "fo_pct") if c in fo]
        df = df.merge(fo[garder], on="playerId", how="left")
    mp = d["mp_sk"]
    if not mp.empty:
        mp = mp[["playerId", "I_F_xGoals", "onIce_xGoalsPercentage"]].drop_duplicates("playerId")
        df = df.merge(mp, on="playerId", how="left")

    out = pd.DataFrame({
        "Joueur": df["Joueur"],
        "Groupe": df["Groupe"],
        "Éq.": colonne(df, "teamAbbrevs"),
        "Pos": colonne(df, "positionCode"),
        "MJ": colonne(df, "gamesPlayed"),
        "B": colonne(df, "goals"),
        "P": colonne(df, "assists"),
        "PTS": colonne(df, "points"),
        "PTS/MJ": colonne(df, "pointsPerGame").astype(float).round(2),
        "+/-": colonne(df, "plusMinus"),
        "PPP": colonne(df, "ppPoints"),
        "TOI/MJ": colonne(df, "timeOnIcePerGame").map(mmss),
        "TOI PP/MJ": colonne(df, "ppTimeOnIcePerGame").map(mmss),
        "TOI PK/MJ": colonne(df, "shTimeOnIcePerGame").map(mmss),
        "Tirs": colonne(df, "shots"),
        "%Tir": colonne(df, "shootingPct").map(pct),
        "MAJ": colonne(df, "totalFaceoffs"),
        "MAJ G": colonne(df, "totalFaceoffWins"),
        "MAJ%": pd.Series([pct_maj(n, a, b) for n, a, b in zip(colonne(df, "totalFaceoffs"), colonne(df, "fo_pct"), colonne(df, "faceoffWinPct"))], index=df.index),
        "MÉ": colonne(df, "hits"),
        "TB": colonne(df, "blockedShots"),
        "CF%": colonne(df, "satPercentage").map(pct),
        "FF%": colonne(df, "usatPercentage").map(pct),
        "xG": colonne(df, "I_F_xGoals").astype(float).round(1),
        "xGF%": colonne(df, "onIce_xGoalsPercentage").map(pct),
        "_pid": df["playerId"],
    })
    return out.sort_values(["PTS", "B"], ascending=False).reset_index(drop=True)


def table_gardiens(roster, d):
    g = d["g_sum"]
    if g.empty:
        return pd.DataFrame()
    df = roster.merge(g.drop_duplicates("playerId"), on="playerId", how="inner")
    mp = d["mp_g"]
    if not mp.empty and {"xGoals", "goals"} <= set(mp.columns):
        mp = mp[["playerId", "xGoals", "goals"]].drop_duplicates("playerId")
        mp["GSAx"] = (mp["xGoals"] - mp["goals"]).round(1)
        df = df.merge(mp[["playerId", "GSAx"]], on="playerId", how="left")
    out = pd.DataFrame({
        "Gardien": df["Joueur"],
        "Groupe": df["Groupe"],
        "Éq.": colonne(df, "teamAbbrevs"),
        "MJ": colonne(df, "gamesPlayed"),
        "Dép.": colonne(df, "gamesStarted"),
        "V": colonne(df, "wins"),
        "D": colonne(df, "losses"),
        "DP": colonne(df, "otLosses"),
        "Moy.": colonne(df, "goalsAgainstAverage").astype(float).round(2),
        "%Arr.": colonne(df, "savePct").astype(float).round(3),
        "BL": colonne(df, "shutouts"),
        "GSAx": colonne(df, "GSAx"),
        "Rang GSAx": [rangs_gardiens(d).get(int(p), "") for p in df["playerId"]],
        "_pid": df["playerId"],
    })
    return out.sort_values("MJ", ascending=False).reset_index(drop=True)


def ligne_hors_lnh(j, saison):
    """Dernière saison connue hors LNH (AHL, junior, Europe, NCAA…)."""
    f = fiche(j["playerId"])
    totaux = [t for t in f.get("seasonTotals", []) if t.get("gameTypeId", 2) == 2]
    if not totaux:
        return None
    derniere = max(t["season"] for t in totaux)
    cette = [t for t in totaux if t["season"] == derniere]
    t = max(cette, key=lambda x: x.get("gamesPlayed", 0))
    gardien = f.get("position") == "G"
    equipe = t.get("teamName", {})
    equipe = equipe.get("default", "") if isinstance(equipe, dict) else str(equipe)
    return {
        "Joueur": j["Joueur"],
        "Groupe": j["Groupe"],
        "Pos": f.get("position", ""),
        "Saison": saison_libelle(derniere) + ("" if derniere == saison else " ⚠"),
        "Ligue": t.get("leagueAbbrev", ""),
        "Équipe": equipe,
        "MJ": t.get("gamesPlayed"),
        "B / V": t.get("wins") if gardien else t.get("goals"),
        "P / Moy.": round(t["goalsAgainstAvg"], 2) if gardien and t.get("goalsAgainstAvg") is not None else t.get("assists"),
        "PTS / %Arr.": round(t["savePctg"], 3) if gardien and t.get("savePctg") is not None else t.get("points"),
    }


def detail_joueur(pid, nom, saison, gardien, cle_ui=""):
    jl = securiser(journal, pid, saison, defaut=pd.DataFrame())
    if jl is None or jl.empty:
        st.info(f"Aucun match LNH pour {nom} en {saison_libelle(saison)}.")
        return
    if not gardien:
        toi = securiser(rapport, "skater", "timeonice", saison, f"playerId={pid}", True, defaut=pd.DataFrame())
        if toi is not None and not toi.empty and "gameId" in toi:
            jl = jl.merge(toi[["gameId", "ppTimeOnIce", "shTimeOnIce"]], on="gameId", how="left")
        fo = securiser(rapport, "skater", "faceoffwins", saison, f"playerId={pid}", True, defaut=pd.DataFrame())
        if fo is not None and not fo.empty and "gameId" in fo:
            garder = ["gameId"] + [c for c in ("totalFaceoffs", "totalFaceoffWins") if c in fo]
            jl = jl.merge(fo[garder], on="gameId", how="left")
    jl = jl.sort_values("gameDate", ascending=False)
    n = st.slider("Derniers matchs", 5, max(5, len(jl)), min(10, len(jl)), key=f"n_{pid}_{cle_ui}") if len(jl) > 5 else len(jl)
    jl = jl.head(n)
    aujourd_hui = datetime.now().strftime("%Y-%m-%d")
    with ThreadPoolExecutor(max_workers=4) as ex:
        jl["gs"] = list(ex.map(lambda g: gs_du_match(g[0], nom) if g[1] < aujourd_hui else None,
                               zip(jl["gameId"], jl["gameDate"])))

    if gardien:
        vue = pd.DataFrame({
            "Date": jl["gameDate"], "Adv.": colonne(jl, "opponentAbbrev"),
            "Déc.": colonne(jl, "decision"), "Tirs": colonne(jl, "shotsAgainst"),
            "BA": colonne(jl, "goalsAgainst"), "%Arr.": colonne(jl, "savePctg").astype(float).round(3),
            "TOI": colonne(jl, "toi"), "GS": jl["gs"],
        })
        st.dataframe(vue, hide_index=True, width="stretch")
        return

    vue = pd.DataFrame({
        "Date": jl["gameDate"], "Adv.": colonne(jl, "opponentAbbrev"), "GS": jl["gs"],
        "B": colonne(jl, "goals"), "P": colonne(jl, "assists"), "PTS": colonne(jl, "points"),
        "+/-": colonne(jl, "plusMinus"), "PPP": colonne(jl, "powerPlayPoints"),
        "Tirs": colonne(jl, "shots"), "TOI": colonne(jl, "toi"),
        "TOI PP": colonne(jl, "ppTimeOnIce").map(mmss), "TOI PK": colonne(jl, "shTimeOnIce").map(mmss),
        "MAJ G-P": [f"{int(g)}-{int(n) - int(g)}" if pd.notna(n) and n else "" for g, n in
                    zip(colonne(jl, "totalFaceoffWins"), colonne(jl, "totalFaceoffs"))],
    })
    c1, c2, c3, c4 = st.columns(4)
    mj = len(vue)
    c1.metric("Points", f"{int(vue['PTS'].sum())} en {mj} MJ")
    c2.metric("PTS/MJ", f"{vue['PTS'].sum() / mj:.2f}")
    tois = jl["toi"].map(toi_sec).dropna()
    c3.metric("TOI moyen", mmss(tois.mean()) if len(tois) else "—")
    pp = colonne(jl, "ppTimeOnIce").dropna()
    c4.metric("TOI PP moyen", mmss(pp.mean()) if len(pp) else "—")
    st.dataframe(vue, hide_index=True, width="stretch")

    graph = pd.DataFrame({
        "Date": jl["gameDate"],
        "TOI total (min)": jl["toi"].map(toi_sec) / 60,
        "TOI PP (min)": colonne(jl, "ppTimeOnIce").astype(float) / 60,
        "TOI PK (min)": colonne(jl, "shTimeOnIce").astype(float) / 60,
    }).sort_values("Date").set_index("Date")
    st.caption("Utilisation par match (minutes)")
    st.line_chart(graph)
    if jl["gs"].notna().any():
        st.caption("Game Score par match (HockeyStatCards)")
        st.bar_chart(jl[["gameDate", "gs"]].rename(columns={"gameDate": "Date", "gs": "Game Score"})
                     .sort_values("Date").set_index("Date"))


# ---------------------------------------------------------------- interface
def gestion_ligues():
    with st.sidebar.expander("Gérer les alignements"):
        fichiers = sorted(LIGUES_DIR.glob("*.txt"))
        noms = [f.stem for f in fichiers]
        choix = st.selectbox("Ligue", noms + ["+ Nouvelle ligue"])
        if choix == "+ Nouvelle ligue":
            nouveau = st.text_input("Nom de la ligue")
            if st.button("Créer la ligue") and nouveau.strip():
                (LIGUES_DIR / f"{nouveau.strip()}.txt").write_text("[Alignement]\n", encoding="utf-8")
                st.rerun()
            return
        chemin = LIGUES_DIR / f"{choix}.txt"
        texte = st.text_area("Un joueur par ligne · [Groupe] pour séparer", chemin.read_text(encoding="utf-8"), height=320)
        if st.button("Enregistrer l'alignement"):
            chemin.write_text(texte, encoding="utf-8")
            st.success("Alignement enregistré.")
            st.rerun()


GROUPES_ESPOIRS = {"espoirs", "espoir", "prospects", "prospect"}


def onglet_ligue(path, saison, d, idx_noms, overrides):
    joueurs = lire_ligue(path)
    if not joueurs:
        st.info("Aucun joueur dans cette ligue. Ajoute-les dans la barre de gauche, section Gérer les alignements.")
        return
    joueurs = resoudre(joueurs, idx_noms, overrides)
    roster = pd.DataFrame(joueurs)
    est_espoir = roster["Groupe"].str.lower().isin(GROUPES_ESPOIRS)

    # Un espoir qui a joué dans la LNH cette saison passe dans la section LNH.
    ids_lnh_saison = set()
    for cle in ("sk_sum", "g_sum"):
        if not d[cle].empty and "playerId" in d[cle]:
            ids_lnh_saison |= set(d[cle]["playerId"].astype(int))
    espoir_lnh = est_espoir & roster["playerId"].isin(ids_lnh_saison)
    espoirs = roster[est_espoir & ~espoir_lnh]
    principal = roster[~est_espoir | espoir_lnh]

    t_align, t_esp = st.tabs(["Alignement", f"Espoirs ({len(espoirs)})"])
    with t_align:
        if principal.empty:
            st.info("Aucun joueur hors espoirs dans cette ligue.")
        else:
            groupes = list(dict.fromkeys(principal["Groupe"]))
            choix = st.multiselect("Groupes affichés", groupes, default=groupes, key=f"g_{path.stem}")
            section(principal[principal["Groupe"].isin(choix)], saison, d, f"{path.stem}_a")
    with t_esp:
        if espoirs.empty:
            st.info("Aucun espoir hors LNH dans cette ligue. Les espoirs qui jouent dans la LNH sont dans "
                    "l'onglet Alignement; pour en ajouter, crée une section [Espoirs] dans l'alignement.")
        else:
            section(espoirs, saison, d, f"{path.stem}_e", espoirs_mode=True)
            if espoir_lnh.any():
                st.caption("Dans la LNH cette saison (voir Alignement) : "
                           + ", ".join(roster[espoir_lnh]["Joueur"]))


def afficher_hors_lnh(connus, roster, saison, ids_lnh, espoirs_mode=False):
    hors = connus[~connus["playerId"].isin(ids_lnh)].to_dict("records")
    if hors:
        st.subheader("Espoirs — AHL, junior, Europe, NCAA" if espoirs_mode else "Hors LNH — AHL, junior, Europe, NCAA")
        with ThreadPoolExecutor(max_workers=8) as ex:
            lignes = list(ex.map(lambda j: securiser(ligne_hors_lnh, j, saison), hors))
        lignes = [l for l in lignes if l]
        if lignes:
            hl = pd.DataFrame(lignes)
            if espoirs_mode:
                hl = hl.drop(columns="Groupe").sort_values(["Ligue", "Joueur"])
            for c in ("B / V", "P / Moy.", "PTS / %Arr."):
                hl[c] = hl[c].map(lambda v: "" if v is None or pd.isna(v) else (f"{v:g}" if isinstance(v, float) else str(v)))
            pos = hl["Pos"].astype(str).str.upper()
            noms_pat = {"B / V": "B", "P / Moy.": "P", "PTS / %Arr.": "PTS"}
            noms_g = {"B / V": "V", "P / Moy.": "Moy.", "PTS / %Arr.": "%Arr."}
            for etiquette, masque, noms in (("Attaquants", ~pos.isin(["D", "G"]), noms_pat),
                                            ("Défenseurs", pos.eq("D"), noms_pat),
                                            ("Gardiens", pos.eq("G"), noms_g)):
                if masque.any():
                    st.markdown(f"**{etiquette}**")
                    st.dataframe(hl[masque].rename(columns=noms), hide_index=True, width="stretch")
            st.caption("⚠ = la saison en cours n'est pas encore disponible pour ce joueur; dernière saison connue affichée.")

    introuvables = roster[roster["playerId"].isna()]["Joueur"].tolist()
    if introuvables:
        st.warning(
            "Joueurs non trouvés : " + ", ".join(introuvables) +
            ". Vérifie l'orthographe, ou ajoute leur ID LNH dans ids_joueurs.json "
            "(l'ID est le numéro à la fin de l'URL du joueur sur nhl.com)."
        )



def section(roster, saison, d, cle_ui, espoirs_mode=False):
    connus = roster.dropna(subset=["playerId"]).astype({"playerId": int})

    rangs, src_rang, gs_classement = source_rangs(d, saison)
    pat = ajouter_hsc(table_patineurs(connus, d), saison, rangs, gs_classement)
    gar = table_gardiens(connus, d)
    ids_lnh = set(pat.get("_pid", [])) | set(gar.get("_pid", []))

    if espoirs_mode:
        afficher_hors_lnh(connus, roster, saison, set(), espoirs_mode=True)
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Joueurs avec MJ dans la LNH", len(ids_lnh))
    c2.metric("Points LNH (patineurs)", int(pat["PTS"].sum()) if not pat.empty else 0)
    c3.metric("Victoires LNH (gardiens)", int(gar["V"].sum()) if not gar.empty else 0)

    est_d = pat["Pos"].astype(str).str.upper().eq("D") if not pat.empty else pd.Series(dtype=bool)
    for titre, sous in (("Attaquants — LNH", pat[~est_d] if not pat.empty else pat),
                        ("Défenseurs — LNH", pat[est_d] if not pat.empty else pat)):
        st.subheader(titre)
        if sous.empty:
            st.caption("Aucun joueur de ces groupes à cette position n'a joué dans la LNH cette saison.")
            continue
        sous = sous.drop(columns="_pid")
        if titre.startswith("Défenseurs"):
            sous = sous.drop(columns=[c for c in ("MAJ", "MAJ G", "MAJ%") if c in sous])
        st.dataframe(sous, hide_index=True, width="stretch", height=min(38 * (len(sous) + 1), 760))
    if not pat.empty:
        st.caption(f"Rang GS : source {src_rang}.")

    st.subheader("Gardiens — LNH")
    if gar.empty:
        st.caption("Aucun gardien de ces groupes n'a joué dans la LNH cette saison.")
    else:
        st.dataframe(gar.drop(columns="_pid"), hide_index=True, width="stretch")

    afficher_hors_lnh(connus, roster, saison, ids_lnh)

    st.subheader("Détail d'un joueur")
    options = {}
    for _, r in pat.iterrows():
        options[f"{r['Joueur']} ({r['Éq.']})"] = (int(r["_pid"]), r["Joueur"], False)
    for _, r in gar.iterrows():
        options[f"{r['Gardien']} ({r['Éq.']}) — G"] = (int(r["_pid"]), r["Gardien"], True)
    if options:
        cle = st.selectbox("Joueur", list(options), key=f"d_{cle_ui}")
        detail_joueur(options[cle][0], options[cle][1], saison, options[cle][2], cle_ui)


def main():
    LIGUES_DIR.mkdir(exist_ok=True)
    st.title("🏒 Tracker pool LNH")

    courante = saison_courante()
    saisons = [courante, courante - 10001]
    saison = st.sidebar.selectbox("Saison", saisons, format_func=saison_libelle)
    if st.sidebar.button("Rafraîchir les données", type="primary"):
        st.cache_data.clear()
        st.rerun()
    gestion_ligues()

    fichiers = sorted(LIGUES_DIR.glob("*.txt"))
    if not fichiers:
        st.info("Aucune ligue. Crée-en une dans la barre de gauche, section Gérer les alignements.")
        return

    erreurs = []
    with st.spinner("Chargement des stats LNH…"):
        d = donnees_saison(saison, erreurs)
        prec = donnees_saison(saison - 10001, [])  # pour reconnaître les noms des joueurs sans MJ cette saison
    idx_noms = index_noms(d["sk_sum"], d["g_sum"], prec["sk_sum"], prec["g_sum"])
    overrides = charger_ids()

    st.sidebar.caption(f"Données en cache 1 h · chargées à {datetime.now():%H:%M}")
    if erreurs:
        with st.sidebar.expander(f"⚠ {len(erreurs)} source(s) indisponible(s)"):
            for e in erreurs:
                st.write(e)

    for onglet, path in zip(st.tabs([f.stem for f in fichiers]), fichiers):
        with onglet:
            onglet_ligue(path, saison, d, idx_noms, overrides)

    st.caption("Sources : API LNH (stats, TOI, CF%/FF%) · MoneyPuck (xG, xGF%, GSAx) · HockeyStatCards (GS, centiles). "
               "CF% et FF% = 5 c. 5. MAJ = mises au jeu (G = gagnées). GS = Game Score, Cent. = centile (HockeyStatCards). Rang GS = rang du GS/MJ par position (attaquants ou défenseurs, 5 MJ et +). Rang GSAx = rang parmi les gardiens (3 MJ et +). MÉ = mises en échec, TB = tirs bloqués.")


main()
