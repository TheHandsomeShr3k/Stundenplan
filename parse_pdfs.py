#!/usr/bin/env python3
"""
parse_pdfs.py
Liest Vorlesungsplan + Klausurplan der DHBW Heidenheim (BWL-Versicherung, WVS2024/4)
aus und schreibt eine saubere data.json fuer die Web-App.

Wird taeglich von GitHub Actions ausgefuehrt. Laedt die PDFs vom DHBW-Server
(mit Browser-User-Agent, weil der Server nackte Requests blockiert) und parst sie.
"""

import json
import re
import sys
import datetime
import urllib.request

import pdfplumber

# --- Konfiguration -------------------------------------------------------

VORLESUNG_URL = ("https://www.heidenheim.dhbw.de/fileadmin/Heidenheim/Studienangebot/"
                 "Bachelor_Wirtschaft/BWL_-_Versicherung_Versicherungsvertrieb_und_"
                 "Finanzberatung/Informationen_fuer_Studierende/vorlesungsplan-vers24-4.pdf")
KLAUSUR_URL = ("https://www.heidenheim.dhbw.de/fileadmin/Heidenheim/Studienangebot/"
               "Bachelor_Wirtschaft/BWL_-_Versicherung_Versicherungsvertrieb_und_"
               "Finanzberatung/Informationen_fuer_Studierende/WVS2024_4-Klausurplan.pdf")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

TIME_BLOCKS = ["8:30 - 11:45", "12:30 - 15:45", "16:00 - 17:30"]
BLOCK_TIMES = [("08:30", "11:45"), ("12:30", "15:45"), ("16:00", "17:30")]

# --- Hilfsfunktionen -----------------------------------------------------

def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(path, "wb") as f:
        f.write(data)
    return path


def parse_fach_dozent(cell):
    """ 'RisikoMgt (Mattejat)' -> [{'fach': 'RisikoMgt', 'dozent': 'Mattejat'}]
    Gibt eine LISTE zurueck, weil manchmal zwei Faecher in einer Zelle kleben,
    z.B. 'RisikoMgt (Mattejat)FDL-Marketing'. """
    cell = (cell or "").strip()
    if not cell:
        return []
    results = []
    for m in re.finditer(r"([^()]+?)\s*\(([^)]*)\)", cell):
        results.append({"fach": m.group(1).strip(" -"), "dozent": m.group(2).strip()})
    rest = re.sub(r"[^()]+?\s*\([^)]*\)", "", cell).strip(" -")
    if rest:
        results.append({"fach": rest, "dozent": ""})
    if not results:
        results.append({"fach": cell, "dozent": ""})
    return results


def parse_vorlesung(path):
    eintraege = []
    raum = "M603"
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        # Raum aus Kopf-/Fusstext ziehen, falls vorhanden
        txt = page.extract_text() or ""
        mraum = re.search(r"Vorlesungsraum:\s*([A-Za-z0-9/]+)", txt)
        if mraum:
            raum = mraum.group(1)

        tables = page.extract_tables()
        if not tables:
            return {"raum": raum, "eintraege": []}
        rows = tables[0]

        current_kw = ""
        for row in rows:
            # erwartet: [KW, Datum, Wochentag, Block1, Block2, Block3, Bemerkung]
            if len(row) < 7:
                continue
            kw, datum, wtag, b1, b2, b3, bem = [(c or "").strip() for c in row[:7]]
            if kw:
                current_kw = kw
            # nur echte Datumszeilen
            if not re.match(r"\d{2}\.\d{2}\.\d{4}", datum):
                continue

            blocks = [b1, b2, b3]
            for i, bcell in enumerate(blocks):
                bcell = bcell.strip()
                if not bcell:
                    continue
                low = bcell.lower()
                is_special = any(w in low for w in
                                 ["klausuren", "feiertag", "karfreitag", "ostermontag",
                                  "pfingst", "himmelfahrt", "fronleichnam", "tag der arbeit"])
                if is_special:
                    eintraege.append({
                        "kw": current_kw, "datum": datum, "wochentag": wtag,
                        "block": i, "start": BLOCK_TIMES[i][0], "ende": BLOCK_TIMES[i][1],
                        "fach": bcell, "dozent": "", "raw": bcell, "raum": raum,
                        "special": True, "bemerkung": bem,
                    })
                    continue
                for fd in parse_fach_dozent(bcell):
                    eintraege.append({
                        "kw": current_kw, "datum": datum, "wochentag": wtag,
                        "block": i, "start": BLOCK_TIMES[i][0], "ende": BLOCK_TIMES[i][1],
                        "fach": fd["fach"], "dozent": fd["dozent"], "raw": bcell,
                        "raum": raum, "special": False, "bemerkung": bem,
                    })
    return {"raum": raum, "eintraege": eintraege}


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip()


def parse_klausur(path):
    meta = {}
    klausuren = []
    with pdfplumber.open(path) as pdf:
        # Meta aus Seite 1
        txt = pdf.pages[0].extract_text() or ""
        for key, pat in [
            ("kurs", r"Kurs/Semester:\s*(\S+)"),
            ("ort", r"Prüfungsort:\s*(.+)"),
            ("zeitraum", r"Prüfungszeitraum:\s*(.+)"),
            ("stand", r"\(Stand:\s*([\d.]+)\)"),
        ]:
            m = re.search(pat, txt)
            if m:
                meta[key] = m.group(1).strip()

        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) < 8:
                        continue
                    datum_cell = clean(row[0])
                    # echte Klausur mit Datum?
                    mdat = re.match(r"(\d{2}\.\d{2}\.\d{4})", datum_cell)
                    # Uhrzeit steht je nach Tabellenform in row[3]
                    zeit = clean(row[3]) if len(row) > 3 else ""
                    modul = clean(row[6]) if len(row) > 6 else ""
                    dozent = clean(row[7]) if len(row) > 7 else ""

                    if mdat and re.search(r"\d{2}:\d{2}", zeit):
                        raum = clean(row[10]) if len(row) > 10 else ""
                        klausuren.append({
                            "typ": "klausur",
                            "datum": mdat.group(1),
                            "zeit": zeit,
                            "modul": modul,
                            "dozent": dozent,
                            "raum": raum,
                            "termin_fest": True,
                        })
                    elif modul and ("bekanntgegeben" in datum_cell.lower()
                                    or "präsentation" in datum_cell.lower()
                                    or "vorlesungsplan" in datum_cell.lower()):
                        klausuren.append({
                            "typ": "sonstige",
                            "datum": "",
                            "zeit": "",
                            "modul": modul,
                            "dozent": dozent,
                            "raum": "",
                            "termin_fest": False,
                            "hinweis": clean(datum_cell),
                        })
    return {"meta": meta, "klausuren": klausuren}


def main():
    try:
        download(VORLESUNG_URL, "vorlesung.pdf")
        download(KLAUSUR_URL, "klausur.pdf")
    except Exception as e:
        print("WARNUNG: Download fehlgeschlagen:", e, file=sys.stderr)
        print("Nutze lokale PDFs, falls vorhanden.", file=sys.stderr)

    vorlesung = parse_vorlesung("vorlesung.pdf")
    klausur = parse_klausur("klausur.pdf")

    data = {
        "stand": datetime.datetime.now().isoformat(timespec="seconds"),
        "vorlesung": vorlesung,
        "klausur": klausur,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"OK: {len(vorlesung['eintraege'])} Vorlesungs-Eintraege, "
          f"{len(klausur['klausuren'])} Klausur-Eintraege geschrieben.")


if __name__ == "__main__":
    main()
