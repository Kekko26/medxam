"""
app.py - Applicazione web (Streamlit) per simulazioni d'esame a risposta multipla.

Avvio locale:   streamlit run app.py
"""

import hashlib
import io
import random

import streamlit as st

import db
import parse_pdf

LETTERE = "abcd"

st.set_page_config(page_title="Quiz d'esame", page_icon="🩺", layout="centered")


@st.cache_resource
def _prepara_database():
    db.init_db()
    return True


try:
    _prepara_database()
except Exception as e:  # database non raggiungibile / DATABASE_URL errato
    st.error(f"Impossibile collegarsi al database: {e}")
    st.stop()


# --------------------------------------------------------------------------- #
# Aspetto
# --------------------------------------------------------------------------- #
CSS = """
<style>
.hero { position: relative; overflow: hidden; color: #fff; border-radius: 18px;
        padding: 1.3rem 1.6rem; margin: 0 0 1.2rem 0;
        background: linear-gradient(135deg, #0F8B8D 0%, #2BB3A3 60%, #7FD6C2 100%);
        box-shadow: 0 6px 18px rgba(15,139,141,.25); }
.hero-title { font-size: 1.65rem; font-weight: 700; line-height: 1.25; }
.hero-sub { margin-top: .2rem; opacity: .92; font-size: .98rem; }
.hero svg { position: absolute; right: 8px; bottom: 6px; width: 46%; height: 52px; opacity: .38; }
[data-testid="stMetric"] { background: #fff; border: 1px solid #D3E8E8;
        border-radius: 14px; padding: .7rem 1rem; }
[data-testid="stSidebar"] { border-right: 1px solid #D3E8E8; }
[data-testid="stExpander"] { border-radius: 12px; background: #fff; }
.stButton > button, .stFormSubmitButton > button { border-radius: 10px; font-weight: 600; }
</style>
"""

ECG = ('<svg viewBox="0 0 200 40" preserveAspectRatio="none"><polyline fill="none" stroke="#fff" '
       'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" '
       'points="0,22 46,22 58,22 66,5 77,38 87,12 95,22 200,22"/></svg>')

st.markdown(CSS, unsafe_allow_html=True)


def intestazione(icona, titolo, sottotitolo=""):
    st.markdown(f'''<div class="hero"><div class="hero-title">{icona} {titolo}</div>
        <div class="hero-sub">{sottotitolo}</div>{ECG}</div>''', unsafe_allow_html=True)


def icona_argomento(nome):
    n = nome.lower()
    if "immun" in n:
        return "🛡️"
    if "micro" in n or "batter" in n or "virol" in n:
        return "🦠"
    return "📘"


# --------------------------------------------------------------------------- #
# Utilita'
# --------------------------------------------------------------------------- #
def flash(tipo, testo):
    st.session_state["flash"] = (tipo, testo)


def mostra_flash():
    f = st.session_state.pop("flash", None)
    if f:
        {"success": st.success, "error": st.error, "warning": st.warning}.get(f[0], st.info)(f[1])


def avviso_durante(d):
    if d["affidabilita"] == "incerta":
        return "⚠ Fonte incerta: la soluzione riportata nel PDF originale non è sicura."
    if d["affidabilita"] == "ignota":
        return "⚠ Risposta non disponibile per questa domanda: non conta nel punteggio."
    return None


def testo_opzioni(d):
    return "\n".join(f"{l}) {d['opz_' + l]}" for l in LETTERE)


def form_domanda(d, prefisso):
    """Modifica testo, opzioni e risposta di una domanda (uploader)."""
    with st.form(f"{prefisso}_{d['id']}"):
        testo = st.text_area("Testo della domanda", d["testo"])
        opz = {l: st.text_input(f"Opzione {l})", d["opz_" + l]) for l in LETTERE}
        scelte = ["—"] + list(LETTERE)
        princ = d["risposte"][0] if d["risposte"] else "—"
        alt = d["risposte"][1] if len(d["risposte"]) > 1 else "—"
        c1, c2 = st.columns(2)
        p = c1.selectbox("Risposta corretta", scelte, index=scelte.index(princ))
        a = c2.selectbox("Alternativa possibile (se incerta)", scelte, index=scelte.index(alt))
        conf = st.checkbox("Risposta verificata (toglie l'avviso di fonte incerta)",
                           value=bool(d["confermata"]))
        salva = st.form_submit_button("Salva", type="primary")
    if salva:
        ok, msg = db.salva_domanda(d["id"], testo, opz,
                                   None if p == "—" else p,
                                   None if a == "—" else a, conf)
        if ok:
            flash("success", msg)
            st.rerun()
        else:
            st.error(msg)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def pagina_login():
    intestazione("🩺", "Quiz d'esame", "Microbiologia · Immunologia — accesso riservato al gruppo")
    with st.form("login"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Accedi", type="primary")
    if ok:
        utente = db.login(u.strip(), p)
        if utente:
            st.session_state["utente"] = utente
            st.rerun()
        else:
            st.error("Credenziali non valide.")


# --------------------------------------------------------------------------- #
# Simulazione
# --------------------------------------------------------------------------- #
def pagina_simulazione(utente):
    intestazione("🩺", "Simulazione d'esame", "Allenati con le domande degli appelli passati")
    sim = st.session_state.get("sim")
    if sim is None:
        _sim_setup()
    elif not sim["consegnata"]:
        _sim_quiz(utente, sim)
    else:
        _sim_correzione(utente, sim)


def _sim_setup():
    esami = [e for e in db.lista_esami() if e["n"] > 0]
    if not esami:
        st.info("Non ci sono ancora domande attive. Un uploader deve caricare almeno un PDF.")
        return

    modo = st.radio("Modalità", ["Per esame", "Per argomento", "Totalmente mista"], horizontal=True)
    esame_id = argomento = None
    descr = "Totalmente mista"

    if modo == "Per esame":
        e = st.selectbox("Esame", esami, format_func=lambda e: f"{e['nome']} ({e['n']} domande)")
        esame_id, descr = e["id"], f"Esame: {e['nome']}"
    elif modo == "Per argomento":
        e = st.selectbox("Esame", [None] + esami,
                         format_func=lambda e: "Tutti gli esami" if e is None else e["nome"])
        esame_id = e["id"] if e else None
        argomenti = db.lista_argomenti(esame_id)
        if not argomenti:
            st.warning("Nessun argomento disponibile.")
            return
        argomento = st.selectbox("Argomento", argomenti, format_func=lambda a: f"{icona_argomento(a)} {a}")
        descr = f"Argomento: {argomento}" + (f" ({e['nome']})" if e else "")

    escludi = st.checkbox("Escludi le domande con risposta incerta o non disponibile")
    disponibili = db.conta_disponibili(esame_id, argomento, escludi)
    if disponibili == 0:
        st.warning("Nessuna domanda disponibile con questi filtri.")
        return
    n = st.number_input(f"Numero di domande (disponibili: {disponibili})", min_value=1,
                        max_value=disponibili, value=min(20, disponibili), step=1,
                        key=f"n_{disponibili}")

    if st.button("Inizia simulazione", type="primary"):
        domande = db.estrai_domande(esame_id, argomento, int(n), escludi)
        st.session_state["sim"] = {"id": random.randint(1, 10**9), "domande": domande,
                                   "descrizione": descr, "consegnata": False}
        st.rerun()


def _sim_quiz(utente, sim):
    st.caption(f"{sim['descrizione']} · {len(sim['domande'])} domande")
    with st.form("quiz"):
        for i, d in enumerate(sim["domande"]):
            with st.container(border=True):
                st.caption(f"{icona_argomento(d['argomento'])} {d['argomento']} · domanda {i + 1} di {len(sim['domande'])}")
                st.markdown(f"**{d['testo']}**")
                avviso = avviso_durante(d)
                if avviso:
                    st.caption(avviso)
                st.radio(f"Domanda {i + 1}", list(LETTERE), index=None,
                         key=f"r_{sim['id']}_{i}",
                         format_func=lambda l, d=d: f"{l}) {d['opz_' + l]}",
                         label_visibility="collapsed")
        consegna = st.form_submit_button("Consegna", type="primary")

    if consegna:
        risultati = []
        for i, d in enumerate(sim["domande"]):
            scelta = st.session_state.get(f"r_{sim['id']}_{i}")
            princ = d["risposte"][0] if d["risposte"] else None
            risultati.append({"domanda_id": d["id"], "scelta": scelta,
                              "corretta": None if princ is None else int(scelta == princ)})
        db.salva_tentativo(utente["id"], sim["descrizione"], risultati)
        sim["risultati"], sim["consegnata"] = risultati, True
        st.rerun()

    if st.button("Annulla simulazione"):
        st.session_state.pop("sim", None)
        st.rerun()


def _sim_correzione(utente, sim):
    risultati = sim["risultati"]
    valutabili = [r for r in risultati if r["corretta"] is not None]
    corrette = sum(r["corretta"] for r in valutabili)
    incerte = sum(1 for d in sim["domande"] if d["affidabilita"] != "certa")

    perc = round(100 * corrette / len(valutabili)) if valutabili else 0
    esito = "🎉 Ottimo lavoro!" if perc >= 80 else "👍 Buon risultato" if perc >= 60 else "📚 Da ripassare"
    st.subheader(esito)
    c1, c2, c3 = st.columns(3)
    c1.metric("Corrette", f"{corrette} / {len(valutabili)}")
    c2.metric("Percentuale", f"{perc}%")
    c3.metric("Domande con fonte incerta", incerte)
    if incerte:
        st.warning("Il punteggio considera corretta la risposta proposta. Nelle domande con "
                   "⚠ la soluzione di partenza non è sicura: controlla i dettagli sotto.")

    for i, (d, r) in enumerate(zip(sim["domande"], risultati)):
        with st.container(border=True):
            st.caption(f"{icona_argomento(d['argomento'])} {d['argomento']} · domanda {i + 1}")
            st.markdown(f"**{d['testo']}**")
            st.text(testo_opzioni(d))
            scelta, princ = r["scelta"], (d["risposte"][0] if d["risposte"] else None)
            tua = f"{scelta})" if scelta else "nessuna risposta"
            if r["corretta"] is None:
                st.warning(f"Risposta non disponibile per questa domanda. La tua: {tua}")
            elif r["corretta"]:
                st.success(f"Corretto: {princ})")
            else:
                st.error(f"La tua risposta: {tua} · Risposta proposta: {princ})")
            if d["affidabilita"] == "incerta":
                alt = [x for x in d["risposte"][1:]]
                nota = f"⚠ Fonte incerta. Risposta proposta: {princ}) "
                if alt:
                    nota += f"· altra candidata: {', '.join(x + ')' for x in alt)}"
                if scelta in alt:
                    nota += " · la tua risposta coincide con l'altra candidata."
                st.warning(nota)
            with st.expander("Segnala un errore in questa domanda"):
                with st.form(f"segn_{sim['id']}_{i}"):
                    msg = st.text_area("Cosa non torna?", key=f"msg_{sim['id']}_{i}")
                    invia = st.form_submit_button("Invia segnalazione")
                if invia:
                    if msg.strip():
                        db.segnala(d["id"], utente["id"], msg)
                        st.success("Segnalazione inviata, grazie.")
                    else:
                        st.error("Scrivi un messaggio.")

    st.divider()
    if st.button("Nuova simulazione", type="primary"):
        st.session_state.pop("sim", None)
        st.rerun()


def pagina_storico(utente):
    intestazione("📈", "Le mie simulazioni", "Il tuo storico")
    righe = db.storico(utente["id"])
    if not righe:
        st.info("Non hai ancora fatto simulazioni.")
        return
    st.dataframe(
        [{"Data": r["creato"].replace("T", " "), "Simulazione": r["descrizione"],
          "Domande": r["totale"], "Punteggio": f"{r['corrette']}/{r['valutabili']}"} for r in righe],
        hide_index=True)


# --------------------------------------------------------------------------- #
# Pagine per uploader
# --------------------------------------------------------------------------- #
def pagina_carica(utente):
    intestazione("📄", "Carica un PDF", "L'app legge le domande e le salva da sola")
    st.write("Carica un PDF di domande. L'app lo legge da sola: le domande pulite diventano "
             "subito disponibili, solo quelle con problemi di formattazione finiscono in revisione.")
    n_up = st.session_state.get("up_n", 0)
    f = st.file_uploader("PDF con domande e soluzioni", type="pdf", key=f"upl_{n_up}")
    if not f:
        return

    dati = f.getvalue()
    chiave = hashlib.sha1(dati).hexdigest()
    if st.session_state.get("anteprima_chiave") != chiave:
        try:
            st.session_state["anteprima"] = parse_pdf.analizza(io.BytesIO(dati))
            st.session_state["anteprima_chiave"] = chiave
        except Exception as e:
            st.error(f"Non riesco a leggere questo PDF: {e}")
            return
    p = st.session_state["anteprima"]
    domande = p["domande"]
    if not domande:
        st.error("Non ho trovato domande. Se il PDF è una scansione (immagine) serve prima "
                 "un riconoscimento del testo (OCR).")
        return

    da_rivedere = [d for d in domande if d["da_rivedere"]]
    c1, c2, c3 = st.columns(3)
    c1.metric("Domande trovate", len(domande))
    c2.metric("Pulite", len(domande) - len(da_rivedere))
    c3.metric("Da rivedere", len(da_rivedere))
    inc = sum(1 for d in domande if d["affidabilita"] == "incerta")
    ign = sum(1 for d in domande if d["affidabilita"] == "ignota")
    st.caption(f"Risposte incerte: {inc} · senza risposta: {ign} (non bloccano l'importazione)")
    if p["duplicati"]:
        st.info(f"{len(p['duplicati'])} domande compaiono due volte nel file e verranno unite.")
    if p["avvisi"]:
        with st.expander(f"{len(p['avvisi'])} avvisi di lettura"):
            for a in p["avvisi"]:
                st.write("•", a)

    esame = st.text_input("Nome dell'esame", value=p["esame"] or "",
                          help="Le domande vengono raggruppate sotto questo nome.")
    if st.button("Importa nel database", type="primary"):
        if not esame.strip():
            st.error("Indica il nome dell'esame.")
            return
        stats = db.importa(domande, esame, f.name)
        righe = [f"Importate {stats['attive']} domande attive"
                 + (f" e {stats['in_revisione']} in revisione" if stats["in_revisione"] else "")
                 + (f"; {stats['duplicate']} già presenti (non duplicate)" if stats["duplicate"] else "")
                 + "."]
        righe += [f"⚠ {c}" for c in stats["conflitti"]]
        flash("success", "\n\n".join(righe))
        for k in ("anteprima", "anteprima_chiave"):
            st.session_state.pop(k, None)
        st.session_state["up_n"] = n_up + 1
        st.rerun()


def pagina_revisione(utente):
    intestazione("🛠️", "Revisione", "Domande con problemi di formattazione")
    st.write("Domande con problemi di formattazione. Correggi e salva: diventano subito attive.")
    elenco = db.domande_per_stato("in_revisione")
    if not elenco:
        st.success("Niente da rivedere. 🎉")
        return
    for i, d in enumerate(elenco):
        with st.expander(f"{d['esame']} · {d['argomento']} #{d['numero_origine']}", expanded=(i == 0)):
            for pr in d["problemi"]:
                st.warning(pr["messaggio"])
            form_domanda(d, "rev")


def pagina_incerte(utente):
    intestazione("⚠️", "Risposte incerte", "Conferma la risposta giusta per tutto il gruppo")
    ignote = st.radio("Mostra", ["Con risposta incerta", "Senza risposta"], horizontal=True) == "Senza risposta"
    elenco = db.domande_incerte(solo_ignote=ignote)
    if not elenco:
        st.success("Nessuna domanda in questa categoria.")
        return
    st.caption(f"{len(elenco)} domande. Conferma la risposta giusta: l'avviso sparisce per tutti.")
    for d in elenco:
        with st.expander(f"{d['esame']} · {d['argomento']} #{d['numero_origine']} — {d['testo'][:70]}"):
            st.markdown(f"**{d['testo']}**")
            st.text(testo_opzioni(d))
            if d["risposte"]:
                st.caption("Candidate nel PDF: " + ", ".join(r.upper() for r in d["risposte"]))
            with st.form(f"inc_{d['id']}"):
                idx = LETTERE.index(d["risposte"][0]) if d["risposte"] else 0
                lettera = st.selectbox("Risposta corretta", list(LETTERE), index=idx)
                ok = st.form_submit_button("Conferma risposta", type="primary")
            if ok:
                db.conferma_risposta(d["id"], lettera)
                flash("success", "Risposta confermata.")
                st.rerun()


def pagina_segnalazioni(utente):
    intestazione("🚩", "Segnalazioni", "Errori segnalati dagli utenti")
    elenco = db.segnalazioni_aperte()
    if not elenco:
        st.success("Nessuna segnalazione aperta.")
        return
    for s in elenco:
        with st.expander(f"{s['esame']} · {s['argomento']} #{s['numero_origine']} — da {s['username']}"):
            st.markdown(f"**{s['testo']}**")
            st.text(testo_opzioni(s))
            st.info(f"Segnalazione: {s['messaggio']}")
            if s["risposte"]:
                st.caption("Risposta attuale: " + ", ".join(r.upper() for r in s["risposte"])
                           + f" ({s['affidabilita']})")
            with st.form(f"sg_{s['segnalazione_id']}"):
                idx = LETTERE.index(s["risposte"][0]) if s["risposte"] else 0
                lettera = st.selectbox("Risposta corretta", list(LETTERE), index=idx)
                conferma = st.form_submit_button("Conferma risposta e chiudi", type="primary")
                chiudi = st.form_submit_button("Chiudi senza modificare")
            if conferma:
                db.conferma_risposta(s["id"], lettera)
                db.chiudi_segnalazione(s["segnalazione_id"])
                flash("success", "Risposta confermata e segnalazione chiusa.")
                st.rerun()
            if chiudi:
                db.chiudi_segnalazione(s["segnalazione_id"])
                flash("success", "Segnalazione chiusa.")
                st.rerun()
    st.caption("Per correggere il testo di una domanda usa la pagina «Tutte le domande».")


def pagina_tutte(utente):
    intestazione("🗂️", "Tutte le domande", "Cerca e correggi")
    q = st.text_input("Cerca nel testo")
    elenco = db.cerca_domande(q, limite=50)
    st.caption(f"{len(elenco)} risultati (massimo 50)")
    for d in elenco:
        etichette = {"incerta": " ⚠", "ignota": " ⚠"}.get(d["affidabilita"], "")
        rev = " [in revisione]" if d["stato"] == "in_revisione" else ""
        with st.expander(f"{d['argomento']} #{d['numero_origine']}{etichette}{rev} — {d['testo'][:70]}"):
            form_domanda(d, "mod")


# --------------------------------------------------------------------------- #
# Navigazione
# --------------------------------------------------------------------------- #
def main():
    utente = st.session_state.get("utente")
    if not utente:
        pagina_login()
        return

    pagine = {"sim": ("🩺 Simulazione", pagina_simulazione), "storico": ("📈 Le mie simulazioni", pagina_storico)}
    etichette = {k: v[0] for k, v in pagine.items()}

    if utente["ruolo"] == "uploader":
        cont = db.conteggi()
        pagine.update({
            "carica": ("📄 Carica PDF", pagina_carica),
            "revisione": ("🛠️ Revisione", pagina_revisione),
            "incerte": ("⚠️ Risposte incerte", pagina_incerte),
            "segnalazioni": ("🚩 Segnalazioni", pagina_segnalazioni),
            "tutte": ("🗂️ Tutte le domande", pagina_tutte),
        })
        etichette = {k: v[0] for k, v in pagine.items()}
        etichette["revisione"] += f" ({cont['revisione']})"
        etichette["incerte"] += f" ({cont['incerte']})"
        etichette["segnalazioni"] += f" ({cont['segnalazioni']})"

    with st.sidebar:
        st.markdown("### 🩺 Quiz d'esame")
        st.write(f"👤 **{utente['username']}**" + (" · uploader" if utente["ruolo"] == "uploader" else ""))
        scelta = st.radio("Menu", list(pagine), format_func=lambda k: etichette[k],
                          label_visibility="collapsed")
        if st.button("Esci"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    mostra_flash()
    if scelta not in pagine:      # difesa in profondita': solo le pagine del proprio ruolo
        scelta = "sim"
    pagine[scelta][1](utente)


main()
