"""List the entries of a .po catalog that are empty or fuzzy, as a JSON skeleton.

Usage:
    python scripts/agents/po_untranslated.py locale/en/LC_MESSAGES/django.po > todo.json

Fill the JSON values (English text) and apply them with po_fill.py. Plural entries
use the key "singular||plural" and a two-item list as value.
"""

import json
import re
import sys


def unquote(lines):
    return "".join(re.findall(r'"(.*)"', "\n".join(lines))).replace('\\"', '"')


def entries(path):
    """Yield (msgid, msgid_plural, msgstrs, fuzzy, block_lines) for every entry."""
    blocks = open(path, encoding="utf-8").read().strip().split("\n\n")
    for block in blocks[1:]:
        lines = block.split("\n")
        sections, current = {}, None
        for line in lines:
            match = re.match(r"^(msgid_plural|msgid|msgstr(?:\[\d\])?) (.*)$", line)
            if match:
                current = match.group(1)
                sections[current] = [match.group(2)]
            elif current and line.startswith('"'):
                sections[current].append(line)
        msgstrs = [unquote(v) for k, v in sorted(sections.items()) if k.startswith("msgstr")]
        fuzzy = any(line.startswith("#,") and "fuzzy" in line for line in lines)
        plural = unquote(sections["msgid_plural"]) if "msgid_plural" in sections else None
        yield unquote(sections["msgid"]), plural, msgstrs, fuzzy, lines


def main(path):
    todo = {}
    for msgid, plural, msgstrs, fuzzy, _lines in entries(path):
        if fuzzy or not all(msgstrs):
            if plural:
                todo[f"{msgid}||{plural}"] = ["", ""]
            else:
                todo[msgid] = ""
    json.dump(todo, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1])
