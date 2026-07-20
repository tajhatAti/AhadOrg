#!/usr/bin/env python3
"""SQLite-leftover regression guard (complements validate_postgres_sql.py,
which checks PG GRAMMAR with pglast). This one checks SEMANTIC SQLite-only
tokens that parse fine but crash or misbehave on PostgreSQL at runtime —
e.g. `COLLATE NOCASE` (the production contacts crash of 2026-07-19).

Scans every runtime .py file. database.py has intentional, dialect-branched
SQLite usage (DDL translation, PRAGMA on the sqlite3 path, INSERT OR REPLACE
in the NON-postgres branch); those live behind explicit guards and are
exempted by line-level checks below.
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# token → human advice  (case-insensitive regex)
BANNED = [
    (r"COLLATE\s+NOCASE", "use ORDER BY LOWER(col) / WHERE LOWER(col) = LOWER(?)"),
    (r"GROUP_CONCAT\s*\(", "use STRING_AGG(expr, ',')"),
    (r"IFNULL\s*\(", "use COALESCE(a, b)"),
    (r"\bINSTR\s*\(", "use STRPOS(haystack, needle) — note PG arg order"),
    (r"datetime\s*\(\s*'now'\s*\)", "use NOW() — or pass a Python timestamp"),
    (r"date\s*\(\s*'now'\s*\)", "use CURRENT_DATE"),
    (r"julianday\s*\(", "use EXTRACT(EPOCH FROM ts)"),
    (r"LIMIT\s+-1", "omit LIMIT entirely (or LIMIT ALL)"),
    (r"RANDOM\s*\(\s*\)", "PG random() returns float — not SQLite's big int"),
    (r"\bRAND\s*\(\s*\)", "MySQL syntax — use random()"),
    (r"\bGLOB\s*['\"]", "PG has no GLOB — use LIKE / SIMILAR TO"),
    (r"PRAGMA\s+", "SQLite-only — never valid on PostgreSQL"),
    (r"INSERT\s+OR\s+(?:REPLACE|IGNORE)", "use INSERT … ON CONFLICT (branched by DIALECT)"),
    (r"\bREPLACE\s+INTO\b", "MySQL syntax — use ON CONFLICT"),
]

# Runtime files to scan (skip: tests, validator itself, migration tooling)
FILES = ["app.py", "database.py", "snippet_page.py", "runner/app.py", "bot/app.py"]

# database.py exemptions: guard-branched dialect handling.
#   - DDL translation path replaces these for postgres BEFORE sending
#   - PRAGMA only runs on the sqlite3 path (never on a psycopg2 connection)
#   - INSERT OR REPLACE lives in the else-branch of `if DIALECT == "postgres"`
def exempt(fname, line_no, lines, pattern):
    line = lines[line_no - 1].strip()
    if line.startswith("#"):              # comments never execute
        return True
    if fname == "database.py":
        if pattern.startswith("PRAGMA"):
            return True                   # sqlite3 path only
        if pattern.startswith("COLLATE"):
            return True                   # DDL string + CITEXT translator (guarded)
        if pattern.startswith(r"INSERT\s+OR"):
            return True                   # dialect-branched
        return False
    if pattern.startswith(r"INSERT\s+OR") or pattern == r"\bREPLACE\s+INTO\b":
        # allowed ONLY inside the SQLite half of an explicit dialect branch —
        # look back ~10 lines for `else:` (of `if DIALECT == "postgres":`)
        lookback = "\n".join(lines[max(0, line_no - 14):line_no])
        return "if DIALECT" in lookback and "else:" in lookback
    return False


findings = []
scanned = 0
for fname in FILES:
    path = os.path.join(ROOT, fname)
    if not os.path.exists(path):
        continue
    scanned += 1
    lines = open(path, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines, 1):
        for pat, advice in BANNED:
            if re.search(pat, line, re.IGNORECASE):
                if exempt(fname, i, lines, pat):
                    continue
                findings.append((fname, i, line.strip()[:90], pat, advice))

print(f"Scanned {scanned} runtime files for {len(BANNED)} SQLite-only patterns.\n")
if findings:
    for fname, i, line, pat, advice in findings:
        print(f"✗ {fname}:{i}: {line}\n    pattern: {pat}  →  {advice}\n")
    print(f"FAIL — {len(findings)} leftover(s) found.")
    sys.exit(1)
print("PASS — zero SQLite-only leftovers in runtime code.")
