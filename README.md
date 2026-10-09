# Quiz d'esame — guida rapida

App web per simulazioni d'esame a risposta multipla. Un uploader carica i PDF; l'app li legge,
salva le domande e le rimescola nelle simulazioni (per esame, per argomento, oppure miste).

## File
| File | A cosa serve |
|---|---|
| `app.py` | L'applicazione web (Streamlit) |
| `db.py` | Database: funziona con SQLite (locale) e PostgreSQL/Supabase (online) |
| `parse_pdf.py` | Lettura dei PDF (usato dall'app; si può lanciare anche da solo per provare un PDF) |
| `crea_utente.py` | Crea gli utenti (non c'è registrazione: li crei tu) |
| `requirements.txt` | Librerie necessarie |

## 1. Prova in locale (5 minuti)
```
pip install -r requirements.txt
python crea_utente.py tuonome uploader
streamlit run app.py
```
Si apre il browser. Accedi, vai su **Carica PDF** e carica il tuo file. I dati finiscono nel file
`quiz.db` accanto all'app (solo per prova: online si usa Supabase).

## 2. Mettila online (gratis)
1. **Supabase**: crea un progetto su supabase.com. In *Project Settings → Database → Connection string*
   copia la stringa **Session pooler** (funziona anche da Streamlit Cloud) e sostituisci `[YOUR-PASSWORD]`.
2. **GitHub**: crea un repository **privato** e carica questi file (`.gitignore` evita di caricare PDF e segreti).
3. **Streamlit Community Cloud** (share.streamlit.io): *New app* → scegli il repository e `app.py`.
   In *Advanced settings → Secrets* incolla:
   ```
   DATABASE_URL = "postgresql://...la tua stringa..."
   ```
4. Alla prima apertura l'app crea da sola le tabelle.
5. **Primo utente**: da PC con la stringa nelle variabili d'ambiente (`DATABASE_URL=... python crea_utente.py tuonome uploader`),
   oppure `python crea_utente.py tuonome uploader --sql` e incolli l'INSERT stampato nello *SQL Editor* di Supabase.
6. Per ogni amico: `python crea_utente.py nome` (ruolo `utente`) e gli mandi link + credenziali.

## Come si comporta
- **Domande pulite** → attive subito. **Domande con problemi di formattazione** → pagina *Revisione*.
- **Risposte incerte** (`B?`, `D/C?`, `?`) → entrano comunque, con avviso ⚠ in simulazione e correzione;
  le confermi dalla pagina *Risposte incerte*. Una risposta confermata non viene più cambiata da nuovi PDF.
- **Duplicati** tra PDF → una sola domanda; se le soluzioni sono in conflitto diventa incerta con entrambe le candidate.
- Gli amici possono **segnalare** un errore a fine simulazione; le vedi in *Segnalazioni*.
