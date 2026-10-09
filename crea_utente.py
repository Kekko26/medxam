#!/usr/bin/env python3
"""
Crea un utente nel database.

Uso:
    python crea_utente.py mario uploader     # crea direttamente nel database (serve DATABASE_URL)
    python crea_utente.py luca               # ruolo 'utente' (default)
    python crea_utente.py luca --sql         # stampa solo l'INSERT da incollare nell'SQL Editor di Supabase

Ruoli: 'uploader' (carica PDF, revisiona, conferma risposte) oppure 'utente' (fa simulazioni).
"""
import argparse
import getpass
import sys

import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("username")
    ap.add_argument("ruolo", nargs="?", default="utente", choices=["uploader", "utente"])
    ap.add_argument("--sql", action="store_true", help="stampa l'INSERT invece di scrivere nel database")
    a = ap.parse_args()

    pw = getpass.getpass("Password: ")
    if len(pw) < 6:
        sys.exit("La password deve avere almeno 6 caratteri.")
    if pw != getpass.getpass("Ripeti password: "):
        sys.exit("Le password non coincidono.")

    if a.sql:
        u = a.username.replace("'", "''")
        print(f"\nINSERT INTO utenti (username, password_hash, ruolo) "
              f"VALUES ('{u}', '{db.crea_hash(pw)}', '{a.ruolo}');\n")
    else:
        db.init_db()
        db.crea_utente(a.username, pw, a.ruolo)
        print(f"Utente '{a.username}' ({a.ruolo}) creato.")


if __name__ == "__main__":
    main()
