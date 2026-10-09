#!/usr/bin/env python3
"""
parse_pdf.py - Estrae domande a risposta multipla da un PDF d'esame.

Uso:
    python parse_pdf.py M-I_Febbraio_2024.pdf
    python parse_pdf.py M-I_Febbraio_2024.pdf --esame "Microbiologia-Immunologia" --out domande.json

Struttura attesa del PDF:
    RIGA DI INTESTAZIONE (es. "MICROBIOLOGIA-IMMUNOLOGIA, 05/02/2024")  -> nome esame (la data viene ignorata)
    IMMUNO                      -> intestazione di sezione (= argomento)
    1. Testo domanda
    a) ...  b) ...  c) ...  d) ...
    ...
    SOLUZIONI                   -> soluzioni della sezione appena conclusa
    1. A
    4. B?                       -> risposta incerta
    6. D/E?                     -> due candidate, incerta
    18. ?                       -> risposta ignota
    MICRO                       -> nuova sezione ...

Per ogni domanda calcola:
  - affidabilita: "certa" | "incerta" | "ignota"   (NON manda in revisione)
  - problemi: elenco di problemi SINTATTICI          (se presenti -> da_rivedere = True)
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict

import pdfplumber

LETTERE = "abcd"

Q_RE = re.compile(r"^(\d+)\s*\.\s+(\S.*)$")            # "12. Testo..."
OPT_RE = re.compile(r"^([a-dA-D])\)\s*(.*)$")           # "a) Testo..."
OPT_ROTTA_RE = re.compile(r"^(\S{1,2})\)\s+(\S.*)$")    # "6) Testo..." (etichetta rotta)
SEC_RE = re.compile(r"^[A-ZÀ-Ü][A-ZÀ-Ü0-9 ]{1,40}$")    # "IMMUNO", "MICRO"
SOL_RE = re.compile(r"^(\d+)\s*[.)]\s*(.*)$")           # "10.B", "4. B?"
RESIDUO_RE = re.compile(r"[a-zà-ù]\d{1,3}\.?$")         # "timo-dipendente7."


# --------------------------------------------------------------------------- #
# Estrazione testo
# --------------------------------------------------------------------------- #
def estrai_righe(pdf_path):
    righe = []
    with pdfplumber.open(pdf_path) as pdf:
        for pagina in pdf.pages:
            testo = pagina.extract_text() or ""
            for r in testo.splitlines():
                r = r.strip()
                if r:
                    righe.append(r)
    return righe


# --------------------------------------------------------------------------- #
# Utilita'
# --------------------------------------------------------------------------- #
def normalizza(testo):
    """Minuscolo, senza accenti/punteggiatura: serve per riconoscere i duplicati."""
    t = unicodedata.normalize("NFKD", testo)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return t.strip()


def hash_domanda(testo, opzioni):
    base = normalizza(testo) + "|" + "|".join(normalizza(opzioni.get(l, "")) for l in LETTERE)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def parse_soluzione(grezza):
    """Ritorna (lettere_candidate_maiuscole, affidabilita)."""
    grezza = (grezza or "").strip()
    incerta = "?" in grezza
    lettere = []
    for l in re.findall(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])", grezza):
        l = l.upper()
        if l not in lettere:
            lettere.append(l)
    if not lettere:
        return [], "ignota"
    if len(lettere) > 1 or incerta:
        return lettere, "incerta"
    return lettere, "certa"


# --------------------------------------------------------------------------- #
# Parsing a stati
# --------------------------------------------------------------------------- #
def nuova_domanda(numero, testo):
    return {
        "numero": numero,
        "testo": testo,
        "opzioni_lista": [],   # [{"etichetta": "a", "originale": "a", "testo": "..."}]
        "problemi": [],
    }


def parse_righe(righe, esame_override=None):
    avvisi = []
    sezioni = {}      # nome -> {"domande": [...], "soluzioni": {n: grezza}}
    ordine = []
    esame = esame_override

    def sezione(nome):
        if nome not in sezioni:
            sezioni[nome] = {"domande": [], "soluzioni": {}}
            ordine.append(nome)
        return sezioni[nome]

    # Intestazione: prima riga, se non e' una domanda o un semplice nome di sezione
    if righe and not Q_RE.match(righe[0]) and not (SEC_RE.match(righe[0])):
        intestazione = righe.pop(0)
        if not esame:
            esame = intestazione.split(",")[0].strip().title()

    corrente = None   # nome sezione
    modo = "domande"
    q = None

    for riga in righe:
        # --- cambio modo / sezione ---
        if riga.upper() == "SOLUZIONI":
            if corrente is None:
                corrente = "GENERALE"
                sezione(corrente)
            modo, q = "soluzioni", None
            continue
        if SEC_RE.match(riga) and not OPT_RE.match(riga):
            corrente, modo, q = riga, "domande", None
            sezione(corrente)
            continue

        # --- soluzioni ---
        if modo == "soluzioni":
            m = SOL_RE.match(riga)
            if m:
                n = int(m.group(1))
                if n in sezioni[corrente]["soluzioni"]:
                    avvisi.append(f"[{corrente}] soluzione {n} presente piu' volte: tengo l'ultima")
                sezioni[corrente]["soluzioni"][n] = m.group(2).strip()
            else:
                avvisi.append(f"[{corrente}] riga nelle soluzioni non riconosciuta: {riga!r}")
            continue

        # --- domande ---
        if corrente is None:
            corrente = "GENERALE"
            sezione(corrente)
        sez = sezioni[corrente]

        m = Q_RE.match(riga)
        if m:
            n = int(m.group(1))
            ultimo = sez["domande"][-1]["numero"] if sez["domande"] else 0
            ha_opzioni = bool(q and q["opzioni_lista"])
            if n == ultimo + 1 or (n > ultimo and (ha_opzioni or q is None)):
                q = nuova_domanda(n, m.group(2).strip())
                if n != ultimo + 1:
                    q["problemi"].append({
                        "codice": "NUM_NON_CONSECUTIVO",
                        "messaggio": f"numerazione non consecutiva (dopo la {ultimo} arriva la {n})",
                    })
                sez["domande"].append(q)
                continue

        if q is None:
            avvisi.append(f"[{corrente}] riga fuori da ogni domanda ignorata: {riga!r}")
            continue

        m = OPT_RE.match(riga)
        if m:
            q["opzioni_lista"].append({"etichetta": m.group(1).lower(),
                                       "originale": m.group(1),
                                       "testo": m.group(2).strip()})
            continue

        m = OPT_ROTTA_RE.match(riga)
        if m and q["opzioni_lista"]:
            q["opzioni_lista"].append({"etichetta": None,       # da assegnare per posizione
                                       "originale": m.group(1),
                                       "testo": m.group(2).strip()})
            continue

        # riga di continuazione (a capo dentro la domanda o dentro un'opzione)
        if q["opzioni_lista"]:
            q["opzioni_lista"][-1]["testo"] += " " + riga
        else:
            q["testo"] += " " + riga

    return esame, ordine, sezioni, avvisi


# --------------------------------------------------------------------------- #
# Validazione e costruzione del risultato
# --------------------------------------------------------------------------- #
def costruisci(esame, ordine, sezioni):
    risultato = []
    avvisi = []

    for nome in ordine:
        sez = sezioni[nome]
        soluzioni = sez["soluzioni"]
        numeri_domande = {q["numero"] for q in sez["domande"]}

        for n in sorted(set(soluzioni) - numeri_domande):
            avvisi.append(f"[{nome}] soluzione {n} ('{soluzioni[n]}') senza domanda corrispondente")

        for q in sez["domande"]:
            problemi = list(q["problemi"])
            opz = {}

            # assegna lettere alle opzioni con etichetta rotta, per posizione
            for pos, o in enumerate(q["opzioni_lista"]):
                atteso = LETTERE[pos] if pos < len(LETTERE) else None
                if o["etichetta"] is None:
                    problemi.append({
                        "codice": "ETICHETTA_ROTTA",
                        "messaggio": f"etichetta opzione '{o['originale']})' non valida: "
                                     f"interpretata come '{atteso})' in base alla posizione",
                    })
                    o["etichetta"] = atteso
                elif o["etichetta"] != atteso:
                    problemi.append({
                        "codice": "ORDINE_OPZIONI",
                        "messaggio": f"opzione '{o['etichetta']})' in posizione {pos + 1} "
                                     f"(atteso '{atteso})')",
                    })

            for o in q["opzioni_lista"]:
                et = o["etichetta"]
                if et is None or et in opz:
                    problemi.append({
                        "codice": "OPZIONE_DUPLICATA_O_IN_ECCESSO",
                        "messaggio": f"opzione in eccesso o con etichetta ripetuta: {o['testo'][:50]!r}",
                    })
                    continue
                opz[et] = o["testo"]

            if not q["testo"].strip():
                problemi.append({"codice": "TESTO_VUOTO", "messaggio": "testo della domanda vuoto"})
            if len(q["opzioni_lista"]) != 4:
                problemi.append({
                    "codice": "NUMERO_OPZIONI",
                    "messaggio": f"trovate {len(q['opzioni_lista'])} opzioni invece di 4",
                })
            for l, t in opz.items():
                if not t.strip():
                    problemi.append({"codice": "OPZIONE_VUOTA", "messaggio": f"opzione '{l})' vuota"})

            # residui di impaginazione (es. numero della domanda successiva attaccato)
            for etichetta, t in [("domanda", q["testo"])] + [(f"opzione {l})", t) for l, t in opz.items()]:
                if RESIDUO_RE.search(t.strip()):
                    problemi.append({
                        "codice": "RESIDUO_IMPAGINAZIONE",
                        "messaggio": f"{etichetta} termina con cifre attaccate: ...{t.strip()[-25:]!r}",
                    })

            # soluzione
            if q["numero"] in soluzioni:
                grezza = soluzioni[q["numero"]]
                candidate, affidabilita = parse_soluzione(grezza)
                valide = [c.lower() for c in candidate if c.lower() in opz]
                for c in candidate:
                    if c.lower() not in LETTERE or c.lower() not in opz:
                        problemi.append({
                            "codice": "SOLUZIONE_LETTERA_INESISTENTE",
                            "messaggio": f"la soluzione '{grezza}' cita '{c}', che non e' tra le opzioni",
                        })
                if candidate and not valide:
                    affidabilita = "ignota"
            else:
                grezza, affidabilita, valide = None, "ignota", []
                problemi.append({"codice": "SOLUZIONE_MANCANTE",
                                 "messaggio": "nessuna soluzione abbinata a questa domanda"})

            risultato.append({
                "esame": esame,
                "argomento": nome,
                "numero": q["numero"],
                "testo": re.sub(r"\s+", " ", q["testo"]).strip(),
                "opzioni": {l: re.sub(r"\s+", " ", t).strip() for l, t in opz.items()},
                "risposte": valide,                  # la prima e' quella proposta
                "soluzione_grezza": grezza,
                "affidabilita": affidabilita,
                "problemi": problemi,
                "da_rivedere": bool(problemi),
                "hash": hash_domanda(q["testo"], opz),
            })
    return risultato, avvisi


def trova_duplicati(domande):
    gruppi = defaultdict(list)
    for d in domande:
        gruppi[d["hash"]].append(d)
    duplicati = []
    for h, gruppo in gruppi.items():
        if len(gruppo) > 1:
            primarie = {g["risposte"][0] if g["risposte"] else None for g in gruppo}
            duplicati.append({
                "hash": h,
                "testo": gruppo[0]["testo"],
                "occorrenze": [f"{g['argomento']} #{g['numero']}" for g in gruppo],
                "risposte": [g["risposte"][0].upper() if g["risposte"] else None for g in gruppo],
                "conflitto_risposte": len(primarie) > 1,
            })
    return duplicati


# --------------------------------------------------------------------------- #
# Report a schermo
# --------------------------------------------------------------------------- #
def stampa_report(esame, domande, avvisi, duplicati):
    tot = len(domande)
    da_rivedere = [d for d in domande if d["da_rivedere"]]
    puliti = [d for d in domande if not d["da_rivedere"]]
    per_aff = defaultdict(int)
    for d in domande:
        per_aff[d["affidabilita"]] += 1

    print(f"\nESAME: {esame}")
    print(f"Domande estratte: {tot}")
    por_arg = defaultdict(int)
    for d in domande:
        por_arg[d["argomento"]] += 1
    for a, n in por_arg.items():
        print(f"   - {a}: {n}")

    print(f"\nImportabili direttamente: {len(puliti)}")
    print(f"Da rivedere (problemi sintattici): {len(da_rivedere)}")
    for d in da_rivedere:
        print(f"   [{d['argomento']} #{d['numero']}]")
        for p in d["problemi"]:
            print(f"        {p['codice']}: {p['messaggio']}")

    print(f"\nAffidabilita' delle risposte:  certa={per_aff['certa']}  "
          f"incerta={per_aff['incerta']}  ignota={per_aff['ignota']}")
    for d in domande:
        if d["affidabilita"] != "certa":
            print(f"   [{d['argomento']} #{d['numero']}] {d['affidabilita']:7s} "
                  f"soluzione nel PDF: {d['soluzione_grezza']!r:10} -> proposta: "
                  f"{[r.upper() for r in d['risposte']] or 'nessuna'}")

    if duplicati:
        print(f"\nDuplicati dentro lo stesso file: {len(duplicati)}")
        for dup in duplicati:
            flag = "  <-- RISPOSTE IN CONFLITTO" if dup["conflitto_risposte"] else ""
            print(f"   {' = '.join(dup['occorrenze'])}  risposte {dup['risposte']}{flag}")
            print(f"        {dup['testo'][:80]}")

    if avvisi:
        print("\nAvvisi:")
        for a in avvisi:
            print(f"   {a}")
    print()


# --------------------------------------------------------------------------- #
def analizza(sorgente, esame_override=None):
    """Punto d'ingresso per l'app. `sorgente` e' un percorso o un file-like (es. BytesIO)."""
    righe = estrai_righe(sorgente)
    esame, ordine, sezioni, avvisi1 = parse_righe(righe, esame_override)
    domande, avvisi2 = costruisci(esame, ordine, sezioni)
    return {
        "esame": esame,
        "domande": domande,
        "duplicati": trova_duplicati(domande),
        "avvisi": avvisi1 + avvisi2,
    }


def main():
    ap = argparse.ArgumentParser(description="Estrae domande a risposta multipla da un PDF.")
    ap.add_argument("pdf")
    ap.add_argument("--esame", help="Nome dell'esame (default: ricavato dall'intestazione del PDF)")
    ap.add_argument("--out", help="File JSON di output (default: <nome pdf>.json)")
    args = ap.parse_args()

    righe = estrai_righe(args.pdf)
    esame, ordine, sezioni, avvisi1 = parse_righe(righe, args.esame)
    domande, avvisi2 = costruisci(esame, ordine, sezioni)
    duplicati = trova_duplicati(domande)

    out = args.out or re.sub(r"\.pdf$", "", args.pdf, flags=re.I) + ".json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"file": args.pdf, "esame": esame, "domande": domande,
                   "duplicati": duplicati, "avvisi": avvisi1 + avvisi2},
                  f, ensure_ascii=False, indent=2)

    stampa_report(esame, domande, avvisi1 + avvisi2, duplicati)
    print(f"JSON scritto in: {out}")


if __name__ == "__main__":
    sys.exit(main())
