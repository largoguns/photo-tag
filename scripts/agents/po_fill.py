"""Apply translations from a JSON file to the empty or fuzzy entries of a .po catalog.

Usage:
    python scripts/agents/po_fill.py locale/en/LC_MESSAGES/django.po todo.json

Existing translations are never touched. Fuzzy flags and "#|" hints are removed from
the entries that get filled. Exits with status 1 and lists what is still missing.
Then compile with msgfmt (see README, section "Traducciones").
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from po_untranslated import entries  # noqa: E402


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def main(po_path, json_path):
    translations = json.loads(Path(json_path).read_text(encoding="utf-8"))
    header = Path(po_path).read_text(encoding="utf-8").strip().split("\n\n")[0]
    out, missing = [header], []
    for msgid, plural, msgstrs, fuzzy, lines in entries(po_path):
        if not fuzzy and all(msgstrs):
            out.append("\n".join(lines))
            continue
        key = f"{msgid}||{plural}" if plural else msgid
        value = translations.get(key)
        if not value or (plural and not all(value)):
            missing.append(key)
            out.append("\n".join(lines))
            continue
        comments = []
        for line in lines:
            if line.startswith("#|"):
                continue
            if line.startswith("#,"):
                flags = [f for f in line[3:].split(", ") if f != "fuzzy"]
                if flags:
                    comments.append("#, " + ", ".join(flags))
            elif line.startswith("#"):
                comments.append(line)
        body = [line for line in lines if not line.startswith("#")]
        first_msgstr = next(i for i, line in enumerate(body) if line.startswith("msgstr"))
        new = comments + body[:first_msgstr]
        if plural:
            new += [f"msgstr[0] {quote(value[0])}", f"msgstr[1] {quote(value[1])}"]
        else:
            new.append(f"msgstr {quote(value)}")
        out.append("\n".join(new))
    Path(po_path).write_text("\n\n".join(out) + "\n", encoding="utf-8")
    if missing:
        print("Still untranslated:", *missing, sep="\n  ")
        sys.exit(1)
    print("All entries translated.")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
