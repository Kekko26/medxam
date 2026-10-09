"""
db.py - Accesso al database.

- In locale usa SQLite (file quiz.db), senza configurare nulla.
- Online usa PostgreSQL (Supabase): basta impostare DATABASE_URL
  (variabile d'ambiente oppure st.secrets["DATABASE_URL"]).

Tutto il resto dell'app parla solo con le funzioni di questo file.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import parse_pdf

LETTERE = "abcd"


# --------------------------------------------------------------------------- #
# Connessione
# --------------------------------------------------------------------------- #
def _database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            import streamlit as st
            url = st.secrets.get("DATABASE_URL")
        except Exception:
            url = None
    return url or "sqlite:///quiz.db"


def _is_pg(url):
    return url.startswith(("postgres://", "postgresql://"))


class _Conn:
    """Piccolo adattatore: stesse query per SQLite e PostgreSQL (segnaposto '?')."""

    def __init__(self, raw, pg):
        self.raw, self.pg = raw, pg

    def execute(self, sql, params=()):
        if self.pg:
            cur = self.raw.cursor()
            cur.execute(sql.replace("?", "%s"), tuple(params))
            return cur
        return self.raw.execute(sql, tuple(params))

    def query(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        r = self.execute(sql, params).fetchone()
        return dict(r) if r else None

    def insert(self, sql, params=()):
        if self.pg:
            return self.execute(sql + " RETURNING id", params).fetchone()["id"]
        return self.execute(sql, params).lastrowid


@contextmanager
def connessione():
    url = _database_url()
    if _is_pg(url):
        import psycopg2
        import psycopg2.extras
        raw = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        c = _Conn(raw, True)
    else:
        raw = sqlite3.connect(url.replace("sqlite:///", "", 1))
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys=ON")
        c = _Conn(raw, False)
    try:
        yield c
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _adesso():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def _schema(pg):
    pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
        f"""CREATE TABLE IF NOT EXISTS utenti (
            id {pk},
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            ruolo TEXT NOT NULL DEFAULT 'utente',      -- 'uploader' | 'utente'
            attivo INTEGER NOT NULL DEFAULT 1)""",
        f"""CREATE TABLE IF NOT EXISTS esami (
            id {pk},
            nome TEXT UNIQUE NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS domande (
            id {pk},
            esame_id INTEGER NOT NULL REFERENCES esami(id),
            argomento TEXT NOT NULL,
            numero_origine INTEGER,
            testo TEXT NOT NULL,
            opz_a TEXT NOT NULL DEFAULT '',
            opz_b TEXT NOT NULL DEFAULT '',
            opz_c TEXT NOT NULL DEFAULT '',
            opz_d TEXT NOT NULL DEFAULT '',
            risposte TEXT NOT NULL DEFAULT '',          -- es. 'c' oppure 'c,b' (la prima e' la proposta)
            affidabilita TEXT NOT NULL DEFAULT 'ignota',-- 'certa' | 'incerta' | 'ignota'
            confermata INTEGER NOT NULL DEFAULT 0,      -- 1 = confermata da un uploader
            stato TEXT NOT NULL DEFAULT 'attiva',       -- 'attiva' | 'in_revisione'
            problemi TEXT NOT NULL DEFAULT '[]',        -- JSON: problemi sintattici da correggere
            hash TEXT NOT NULL,
            fonte TEXT,
            creato TEXT)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_domande_hash ON domande(hash)",
        f"""CREATE TABLE IF NOT EXISTS segnalazioni (
            id {pk},
            domanda_id INTEGER NOT NULL REFERENCES domande(id),
            utente_id INTEGER NOT NULL REFERENCES utenti(id),
            messaggio TEXT NOT NULL,
            stato TEXT NOT NULL DEFAULT 'aperta',       -- 'aperta' | 'chiusa'
            creato TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS tentativi (
            id {pk},
            utente_id INTEGER NOT NULL REFERENCES utenti(id),
            creato TEXT,
            descrizione TEXT,
            totale INTEGER,
            valutabili INTEGER,
            corrette INTEGER)""",
        f"""CREATE TABLE IF NOT EXISTS risposte_date (
            id {pk},
            tentativo_id INTEGER NOT NULL REFERENCES tentativi(id),
            domanda_id INTEGER NOT NULL REFERENCES domande(id),
            scelta TEXT,
            corretta INTEGER)""",                       # NULL = non valutabile
    ]


TABELLE = ["utenti", "esami", "domande", "segnalazioni", "tentativi", "risposte_date"]


def init_db():
    with connessione() as c:
        for stmt in _schema(c.pg):
            c.execute(stmt)
        pg = c.pg
    if pg:
        # Su Supabase le tabelle sarebbero leggibili dall'API pubblica del progetto.
        # Con RLS attivo e nessuna policy l'API non vede nulla; l'app, che si collega
        # come proprietario del database, continua a funzionare normalmente.
        for t in TABELLE:
            try:
                with connessione() as c:
                    c.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Utenti e password (solo libreria standard)
# --------------------------------------------------------------------------- #
def crea_hash(password, iterazioni=200_000):
    sale = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(sale), iterazioni)
    return f"pbkdf2${iterazioni}${sale}${h.hex()}"


def verifica_password(password, memorizzato):
    try:
        _, it, sale, h = memorizzato.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(sale), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def crea_utente(username, password, ruolo="utente"):
    if ruolo not in ("uploader", "utente"):
        raise ValueError("ruolo deve essere 'uploader' o 'utente'")
    with connessione() as c:
        return c.insert(
            "INSERT INTO utenti (username, password_hash, ruolo) VALUES (?, ?, ?)",
            (username, crea_hash(password), ruolo))


def login(username, password):
    with connessione() as c:
        u = c.one("SELECT * FROM utenti WHERE username = ? AND attivo = 1", (username,))
    if u and verifica_password(password, u["password_hash"]):
        return {"id": u["id"], "username": u["username"], "ruolo": u["ruolo"]}
    return None


# --------------------------------------------------------------------------- #
# Logica delle risposte
# --------------------------------------------------------------------------- #
def unisci_risposte(esistente, nuova):
    """
    Combina la risposta gia' nel database con quella di una domanda duplicata.
    `esistente` e `nuova`: dict con risposte (lista), affidabilita, confermata.
    Ritorna (risposte, affidabilita, conflitto: bool).
    Rendere idempotente il reimport dello stesso PDF e' un requisito.
    """
    ex, nu = list(esistente["risposte"]), list(nuova["risposte"])
    if esistente.get("confermata"):
        return ex, esistente["affidabilita"], False
    if not nu:
        return ex, esistente["affidabilita"], False
    if not ex:
        return nu, nuova["affidabilita"], False
    unione = ex + [r for r in nu if r not in ex]
    if unione == ex:
        # la nuova non aggiunge nulla; se l'esistente era una sola lettera incerta
        # e la nuova la conferma come certa, si risolve
        if len(ex) == 1 and nu == ex and nuova["affidabilita"] == "certa":
            return ex, "certa", False
        return ex, esistente["affidabilita"], False
    return unione, "incerta", True


def _riga_domanda(r):
    if r is None:
        return None
    r = dict(r)
    r["risposte"] = r["risposte"].split(",") if r["risposte"] else []
    r["problemi"] = json.loads(r["problemi"] or "[]")
    return r


def valida_domanda(testo, opzioni):
    errori = []
    if not testo.strip():
        errori.append("Il testo della domanda e' vuoto.")
    for l in LETTERE:
        if not opzioni.get(l, "").strip():
            errori.append(f"L'opzione {l}) e' vuota.")
    return errori


# --------------------------------------------------------------------------- #
# Importazione
# --------------------------------------------------------------------------- #
def _get_or_create_esame(c, nome):
    e = c.one("SELECT id FROM esami WHERE lower(nome) = lower(?)", (nome,))
    return e["id"] if e else c.insert("INSERT INTO esami (nome) VALUES (?)", (nome,))


def importa(domande, esame_nome, fonte=""):
    """
    Salva le domande estratte dal parser.
    - pulite -> stato 'attiva'
    - con problemi sintattici -> stato 'in_revisione'
    - gia' presenti (stesso hash) -> non duplicate: si uniscono solo le risposte
    """
    esame_nome = esame_nome.strip()
    if not esame_nome:
        raise ValueError("Nome esame mancante")
    stats = {"attive": 0, "in_revisione": 0, "duplicate": 0, "conflitti": [],
             "incerte": 0, "ignote": 0}
    with connessione() as c:
        esame_id = _get_or_create_esame(c, esame_nome)
        for d in domande:
            esistente = _riga_domanda(c.one("SELECT * FROM domande WHERE hash = ?", (d["hash"],)))
            if esistente:
                stats["duplicate"] += 1
                ris, aff, conflitto = unisci_risposte(
                    esistente, {"risposte": d["risposte"], "affidabilita": d["affidabilita"]})
                if conflitto:
                    stats["conflitti"].append(
                        f"{d['argomento']} #{d['numero']}: risposte in conflitto, "
                        f"ora candidate {', '.join(r.upper() for r in ris)}")
                if ris != esistente["risposte"] or aff != esistente["affidabilita"]:
                    c.execute("UPDATE domande SET risposte = ?, affidabilita = ? WHERE id = ?",
                              (",".join(ris), aff, esistente["id"]))
                continue

            stato = "in_revisione" if d["da_rivedere"] else "attiva"
            o = d["opzioni"]
            c.insert(
                """INSERT INTO domande (esame_id, argomento, numero_origine, testo,
                       opz_a, opz_b, opz_c, opz_d, risposte, affidabilita, confermata,
                       stato, problemi, hash, fonte, creato)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                (esame_id, d["argomento"], d["numero"], d["testo"],
                 o.get("a", ""), o.get("b", ""), o.get("c", ""), o.get("d", ""),
                 ",".join(d["risposte"]), d["affidabilita"], stato,
                 json.dumps(d["problemi"], ensure_ascii=False), d["hash"], fonte, _adesso()))
            stats["in_revisione" if d["da_rivedere"] else "attive"] += 1
            if d["affidabilita"] == "incerta":
                stats["incerte"] += 1
            elif d["affidabilita"] == "ignota":
                stats["ignote"] += 1
    return stats


# --------------------------------------------------------------------------- #
# Modifica / revisione / conferma
# --------------------------------------------------------------------------- #
def salva_domanda(domanda_id, testo, opzioni, principale, alternativa=None, confermata=False):
    """
    Salva una domanda corretta a mano (revisione o modifica singola).
    Ritorna (ok, messaggio). La domanda diventa 'attiva'.
    """
    errori = valida_domanda(testo, opzioni)
    if errori:
        return False, " ".join(errori)
    if principale and alternativa == principale:
        return False, "La risposta alternativa coincide con quella principale."

    with connessione() as c:
        attuale = _riga_domanda(c.one("SELECT * FROM domande WHERE id = ?", (domanda_id,)))
        if not attuale:
            return False, "Domanda non trovata."

        risposte = [r for r in (principale, alternativa) if r]
        if confermata and principale:
            risposte, aff, conf = [principale], "certa", 1
        elif not risposte:
            aff, conf = "ignota", 0
        elif risposte == attuale["risposte"]:
            aff, conf = attuale["affidabilita"], attuale["confermata"]
        else:
            aff, conf = "incerta", 0

        nuovo_hash = parse_pdf.hash_domanda(testo, opzioni)
        altra = _riga_domanda(c.one("SELECT * FROM domande WHERE hash = ? AND id <> ?",
                                    (nuovo_hash, domanda_id)))
        if altra:
            # dopo la correzione coincide con una domanda gia' presente: si unisce
            ris, aff2, _ = unisci_risposte(
                altra, {"risposte": risposte, "affidabilita": aff})
            c.execute("UPDATE domande SET risposte = ?, affidabilita = ? WHERE id = ?",
                      (",".join(ris), aff2, altra["id"]))
            c.execute("UPDATE segnalazioni SET domanda_id = ? WHERE domanda_id = ?",
                      (altra["id"], domanda_id))
            c.execute("UPDATE risposte_date SET domanda_id = ? WHERE domanda_id = ?",
                      (altra["id"], domanda_id))
            c.execute("DELETE FROM domande WHERE id = ?", (domanda_id,))
            return True, "Salvata: coincideva con una domanda gia' presente, le due sono state unite."

        c.execute(
            """UPDATE domande SET testo = ?, opz_a = ?, opz_b = ?, opz_c = ?, opz_d = ?,
                   risposte = ?, affidabilita = ?, confermata = ?, stato = 'attiva',
                   problemi = '[]', hash = ? WHERE id = ?""",
            (testo.strip(), opzioni["a"].strip(), opzioni["b"].strip(), opzioni["c"].strip(),
             opzioni["d"].strip(), ",".join(risposte), aff, conf, nuovo_hash, domanda_id))
    return True, "Domanda salvata."


def conferma_risposta(domanda_id, lettera):
    if lettera not in LETTERE:
        return False
    with connessione() as c:
        c.execute("UPDATE domande SET risposte = ?, affidabilita = 'certa', confermata = 1 "
                  "WHERE id = ?", (lettera, domanda_id))
    return True


# --------------------------------------------------------------------------- #
# Elenchi
# --------------------------------------------------------------------------- #
def lista_esami():
    with connessione() as c:
        return c.query(
            """SELECT e.id, e.nome, COUNT(d.id) AS n FROM esami e
               LEFT JOIN domande d ON d.esame_id = e.id AND d.stato = 'attiva'
               GROUP BY e.id, e.nome ORDER BY e.nome""")


def lista_argomenti(esame_id=None):
    sql = "SELECT DISTINCT argomento FROM domande WHERE stato = 'attiva'"
    params = []
    if esame_id:
        sql += " AND esame_id = ?"
        params.append(esame_id)
    with connessione() as c:
        return [r["argomento"] for r in c.query(sql + " ORDER BY argomento", params)]


def _filtro_sim(esame_id, argomento, escludi_incerte):
    sql, params = " WHERE d.stato = 'attiva'", []
    if esame_id:
        sql += " AND d.esame_id = ?"
        params.append(esame_id)
    if argomento:
        sql += " AND d.argomento = ?"
        params.append(argomento)
    if escludi_incerte:
        sql += " AND d.affidabilita = 'certa'"
    return sql, params


def conta_disponibili(esame_id=None, argomento=None, escludi_incerte=False):
    filtro, params = _filtro_sim(esame_id, argomento, escludi_incerte)
    with connessione() as c:
        return c.one("SELECT COUNT(*) AS n FROM domande d" + filtro, params)["n"]


def estrai_domande(esame_id=None, argomento=None, n=20, escludi_incerte=False):
    filtro, params = _filtro_sim(esame_id, argomento, escludi_incerte)
    with connessione() as c:
        righe = c.query(
            "SELECT d.*, e.nome AS esame FROM domande d JOIN esami e ON e.id = d.esame_id"
            + filtro + " ORDER BY RANDOM() LIMIT ?", params + [n])
    return [_riga_domanda(r) for r in righe]


def domande_per_stato(stato):
    with connessione() as c:
        righe = c.query(
            "SELECT d.*, e.nome AS esame FROM domande d JOIN esami e ON e.id = d.esame_id "
            "WHERE d.stato = ? ORDER BY e.nome, d.argomento, d.numero_origine", (stato,))
    return [_riga_domanda(r) for r in righe]


def domande_incerte(solo_ignote=False):
    cond = "d.affidabilita = 'ignota'" if solo_ignote else "d.affidabilita = 'incerta'"
    with connessione() as c:
        righe = c.query(
            "SELECT d.*, e.nome AS esame FROM domande d JOIN esami e ON e.id = d.esame_id "
            f"WHERE d.stato = 'attiva' AND {cond} "
            "ORDER BY e.nome, d.argomento, d.numero_origine")
    return [_riga_domanda(r) for r in righe]


def cerca_domande(testo="", limite=50):
    with connessione() as c:
        righe = c.query(
            "SELECT d.*, e.nome AS esame FROM domande d JOIN esami e ON e.id = d.esame_id "
            "WHERE lower(d.testo) LIKE lower(?) ORDER BY d.id DESC LIMIT ?",
            (f"%{testo.strip()}%", limite))
    return [_riga_domanda(r) for r in righe]


def conteggi():
    with connessione() as c:
        return {
            "revisione": c.one("SELECT COUNT(*) AS n FROM domande WHERE stato = 'in_revisione'")["n"],
            "segnalazioni": c.one("SELECT COUNT(*) AS n FROM segnalazioni WHERE stato = 'aperta'")["n"],
            "incerte": c.one("SELECT COUNT(*) AS n FROM domande WHERE stato = 'attiva' "
                             "AND affidabilita <> 'certa'")["n"],
        }


# --------------------------------------------------------------------------- #
# Segnalazioni e tentativi
# --------------------------------------------------------------------------- #
def segnala(domanda_id, utente_id, messaggio):
    with connessione() as c:
        return c.insert(
            "INSERT INTO segnalazioni (domanda_id, utente_id, messaggio, creato) VALUES (?, ?, ?, ?)",
            (domanda_id, utente_id, messaggio.strip(), _adesso()))


def segnalazioni_aperte():
    with connessione() as c:
        righe = c.query(
            """SELECT s.id AS segnalazione_id, s.messaggio, s.creato AS segnalata_il,
                      u.username, d.*, e.nome AS esame
               FROM segnalazioni s
               JOIN domande d ON d.id = s.domanda_id
               JOIN esami e ON e.id = d.esame_id
               JOIN utenti u ON u.id = s.utente_id
               WHERE s.stato = 'aperta' ORDER BY s.id""")
    out = []
    for r in righe:
        r = _riga_domanda(r)
        r["id"] = r["id"]            # id della domanda
        out.append(r)
    return out


def chiudi_segnalazione(segnalazione_id):
    with connessione() as c:
        c.execute("UPDATE segnalazioni SET stato = 'chiusa' WHERE id = ?", (segnalazione_id,))


def salva_tentativo(utente_id, descrizione, risultati):
    """risultati: lista di dict {domanda_id, scelta, corretta (1 | 0 | None se non valutabile)}"""
    valutabili = [r for r in risultati if r["corretta"] is not None]
    with connessione() as c:
        tid = c.insert(
            "INSERT INTO tentativi (utente_id, creato, descrizione, totale, valutabili, corrette) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utente_id, _adesso(), descrizione, len(risultati), len(valutabili),
             sum(1 for r in valutabili if r["corretta"])))
        for r in risultati:
            c.execute("INSERT INTO risposte_date (tentativo_id, domanda_id, scelta, corretta) "
                      "VALUES (?, ?, ?, ?)",
                      (tid, r["domanda_id"], r["scelta"], r["corretta"]))
    return tid


def storico(utente_id):
    with connessione() as c:
        return c.query("SELECT creato, descrizione, totale, valutabili, corrette FROM tentativi "
                       "WHERE utente_id = ? ORDER BY id DESC LIMIT 100", (utente_id,))
