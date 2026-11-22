#!/usr/bin/env python3
"""Build ION KYC's extra sanctions lists (US OFAC SDN + HK CEDB) for SharePoint.

Runs weekly in GitHub Actions. Downloads the official public files, normalises
names the same way the ION KYC app does, and writes:

  other/entries.json  - every current entry (full snapshot)
  other/delta.json    - what changed since last week's snapshot (adds/updates/removes)
  other/report.txt    - counts and sources, for the audit trail

If a download fails or looks wrong, the script exits non-zero and nothing is
committed, so last week's files stay in place.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

OUT = "other"
UA = "Mozilla/5.0 (compatible; ION-Group-HK-KYC-weekly-check)"
OFAC_BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
CEDB_BASE = "https://www.cedb.gov.hk/datagovhk/TFSlists/"
CEDB_FILES = ["afghanistan", "central_african_republic", "dprk", "drcongo", "haiti",
              "isil_and_al_qaida", "libya", "somalia", "south_sudan", "sudan", "yemen"]
UN_FILE = "consolidated.xml"  # this repo's weekly UN copy


def fetch(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(20 * (i + 1))
    raise RuntimeError(f"download failed: {url}: {last}")


# --- name normalisation: must match the ION KYC app and the UN import flow ---
PUNCT = re.compile(r"[^0-9A-ZÀ-ÖØ-Þ ]")


def search_key(names):
    text = " ".join(n for n in names if n).upper()
    text = PUNCT.sub(" ", text)
    text = re.sub(r" +", " ", text).strip()
    return " " + text + " "


def skeleton(key):
    out = []
    for i, c in enumerate(key):
        p = key[i - 1] if i else " "
        if c == " " or (c != p and (p == " " or c not in "AEIOUY")):
            out.append(c)
    return "".join(out)


BUCKETS = 20  # the app loads the list in 20 groups (each well under its 2,000-row limit)


def entry(source, ref, name, aliases, etype, programs):
    aliases = [a for a in dict.fromkeys(a.strip() for a in aliases) if a and a != name]
    key = search_key([name] + aliases)
    digits = re.sub(r"\D", "", ref)
    bucket = int(digits) % BUCKETS if digits else sum(map(ord, ref)) % BUCKETS
    return {
        "ListKey": f"{source}|{ref}",
        "Bucket": bucket,
        "Source": source,
        "RefNumber": ref,
        "Title": name[:250],
        "Aliases": "; ".join(aliases)[:20000],
        "EntryType": etype,
        "Programs": programs[:250],
        "SearchKey": key[:60000],
        "SkelKey": skeleton(key)[:60000],
    }


# --- US OFAC SDN (SDN.CSV + ALT.CSV, no header rows, "-0-" = empty) ---
def ofac():
    sdn = fetch(OFAC_BASE + "SDN.CSV").decode("utf-8", "replace")
    alt = fetch(OFAC_BASE + "ALT.CSV").decode("utf-8", "replace")
    aliases = {}
    for row in csv.reader(io.StringIO(alt)):
        if len(row) >= 4 and row[0].strip().isdigit():
            v = row[3].strip()
            if v and v != "-0-":
                aliases.setdefault(row[0].strip(), []).append(v)
    out = {}
    for row in csv.reader(io.StringIO(sdn)):
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        ent, name, typ, prog = (c.strip() for c in row[:4])
        typ = "" if typ == "-0-" else typ
        etype = {"individual": "Individual", "vessel": "Vessel", "aircraft": "Aircraft"}.get(typ.lower(), "Entity")
        e = entry("OFAC SDN", f"OFAC {ent}", name, aliases.get(ent, []), etype,
                  "" if prog == "-0-" else prog)
        out[e["ListKey"]] = e
    if len(out) < 5000:
        raise RuntimeError(f"OFAC SDN looks incomplete: only {len(out)} entries")
    return out


# --- HK CEDB lists (same XML layout as the UN list) ---
def names_from_un_xml(node, is_ind):
    t = lambda tag: (node.findtext(tag) or "").strip()  # noqa: E731
    if is_ind:
        name = " ".join(x for x in (t("FIRST_NAME"), t("SECOND_NAME"), t("THIRD_NAME"), t("FOURTH_NAME")) if x)
        al = [ (a.findtext("ALIAS_NAME") or "").strip() for a in node.findall("INDIVIDUAL_ALIAS")]
    else:
        name = t("FIRST_NAME")
        al = [(a.findtext("ALIAS_NAME") or "").strip() for a in node.findall("ENTITY_ALIAS")]
    return t("REFERENCE_NUMBER"), name, al, t("UN_LIST_TYPE")


def un_refs():
    try:
        root = ET.parse(UN_FILE).getroot()
    except Exception:  # noqa: BLE001
        return set()
    return {(n.findtext("REFERENCE_NUMBER") or "").strip()
            for n in root.iter() if n.tag in ("INDIVIDUAL", "ENTITY")}


def cedb(prev):
    """CEDB lists mirror the UN list. Only names NOT already on the UN list are kept
    (normally none); the report records that the two were compared."""
    un = un_refs()
    out, seen, failed, per = {}, 0, [], {}
    for f in CEDB_FILES:
        try:
            root = ET.fromstring(fetch(CEDB_BASE + f + ".xml"))
        except Exception as ex:  # noqa: BLE001
            failed.append(f"{f}: {ex}")
            continue
        n = 0
        for node in root.iter():
            if node.tag not in ("INDIVIDUAL", "ENTITY"):
                continue
            ref, name, al, lt = names_from_un_xml(node, node.tag == "INDIVIDUAL")
            if not ref or not name:
                continue
            n += 1
            seen += 1
            if ref not in un:
                e = entry("HK CEDB", f"HK {ref}", name, al,
                          "Individual" if node.tag == "INDIVIDUAL" else "Entity", lt or f)
                out[e["ListKey"]] = e
        per[f] = n
    if failed:  # keep last week's CEDB entries rather than drop them
        out = {k: v for k, v in prev.items() if v["Source"] == "HK CEDB"}
    return out, seen, failed, per, len(un)


def main():
    os.makedirs(OUT, exist_ok=True)
    prev, prev_version = {}, None
    p = os.path.join(OUT, "entries.json")
    if os.path.exists(p):
        snap = json.load(open(p, encoding="utf-8"))
        prev_version = snap.get("version")
        prev = {e["ListKey"]: e for e in snap["entries"]}

    cur = ofac()
    c_entries, c_seen, c_failed, c_per, n_un = cedb(prev)
    cur.update(c_entries)

    prev_ofac = sum(1 for e in prev.values() if e["Source"] == "OFAC SDN")
    new_ofac = sum(1 for e in cur.values() if e["Source"] == "OFAC SDN")
    if prev_ofac and new_ofac < 0.8 * prev_ofac:
        raise RuntimeError(f"OFAC count fell from {prev_ofac} to {new_ofac}; not updating")

    fields = ("Title", "Aliases", "EntryType", "Programs", "SkelKey")
    adds = [cur[k] for k in cur if k not in prev]
    updates = [cur[k] for k in cur if k in prev and any(cur[k][f] != prev[k].get(f) for f in fields)]
    removes = [k for k in prev if k not in cur]

    now = dt.datetime.now(dt.timezone.utc)
    version = now.strftime("%Y-%m-%dT%H:%MZ")
    entries = sorted(cur.values(), key=lambda e: e["ListKey"])
    json.dump({"version": version, "count": len(entries), "entries": entries},
              open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # base_version lets the SharePoint flow check this delta follows the version it
    # last applied; if not, it skips and asks for a full reload instead.
    json.dump({"version": version, "base_version": prev_version,
               "count": len(entries), "upserts": adds + updates, "removes": removes,
               "added": len(adds), "updated": len(updates), "removed": len(removes)},
              open(os.path.join(OUT, "delta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    lines = [
        f"Version: {version}",
        f"US OFAC SDN entries: {new_ofac} (source {OFAC_BASE}SDN.CSV + ALT.CSV)",
        f"HK CEDB names checked: {c_seen} across {len(c_per)} regime files (source {CEDB_BASE})",
        "  per file: " + ", ".join(f"{k} {v}" for k, v in c_per.items()),
        f"HK CEDB names not on the UN list ({n_un} UN refs): {len(c_entries)}",
        f"CEDB download problems: {'; '.join(c_failed) if c_failed else 'none'}",
        f"Changes this week: {len(adds)} added, {len(updates)} updated, {len(removes)} removed",
    ]
    open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)
