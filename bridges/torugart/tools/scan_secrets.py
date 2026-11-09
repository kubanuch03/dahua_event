#!/usr/bin/env python3
"""Поиск секретов в дереве или истории: значения не печатаются, только путь, коммит и число совпадений.
  scan_secrets.py tree <каталог>
  scan_secrets.py history <ref> [--from <коммит>]   (известные значения берутся из файлов указанного коммита)"""
import hashlib, os, re, subprocess, sys

PATTERNS = [re.compile(p) for p in (
    r"""api_key\s*=\s*["']([^"']{6,})["']""",
    r"""["']?password["']?\s*[:=]\s*["']([^"']{4,})["']""",
    r"""\.get\(\s*["']password["']\s*,\s*["']([^"']{4,})["']\s*\)""",
    r"""DOLAPIKEY["']?\s*[:=]\s*["']([^"']{6,})["']""",
)]
SKIP = {"", "<hidden>", "password", "changeme", "admin", "x", "pw", "k", "задан"}


TOP = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, cwd=TOP, **kw).stdout


def values_in(text):
    out = set()
    for rx in PATTERNS:
        for m in rx.finditer(text):
            v = m.group(1)
            if v not in SKIP and not v.startswith(("$", "{", "<", "os.environ")):
                out.add(v)
    return out


def known_from_commit(commit):
    vals = set()
    for path in sh("git", "ls-tree", "-r", "--name-only", commit).decode().split("\n"):
        if path.endswith((".py", ".yml", ".yaml", ".env", ".template", ".md", ".txt")):
            vals |= values_in(sh("git", "show", f"{commit}:{path}").decode("utf-8", "replace"))
    return vals


def scan_tree(root, known):
    hits = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "__pycache__", "dist", "node_modules", "data")]
        for f in fn:
            p = os.path.join(dp, f)
            if f.endswith((".whl", ".jpg", ".png", ".pyc", ".db")):
                continue
            try:
                t = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            found = values_in(t) | {v for v in known if v in t}
            if found:
                hits.append((os.path.relpath(p, root), len(found)))
    return hits


def scan_history(ref, known):
    hits = []
    hashes = {hashlib.sha256(v.encode()).hexdigest() for v in known}
    for c in sh("git", "rev-list", ref).decode().split():
        for path in sh("git", "ls-tree", "-r", "--name-only", c).decode().split("\n"):
            if not path.endswith((".py", ".yml", ".yaml", ".env", ".template", ".md", ".txt", ".sh")):
                continue
            t = sh("git", "show", f"{c}:{path}").decode("utf-8", "replace")
            n = sum(1 for v in known if v in t)
            if n:
                hits.append((c[:8], path, n))
    return hits


if __name__ == "__main__":
    mode = sys.argv[1]
    frm = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else "20c36b4"
    known = known_from_commit(frm)
    print(f"известных литералов в {frm}: {len(known)} (значения не печатаются)")
    if mode == "tree":
        h = scan_tree(sys.argv[2], known)
        print(f"дерево {sys.argv[2]}: файлов с совпадениями {len(h)}")
        for p, n in h: print("  ", p, n)
        sys.exit(1 if h else 0)
    h = scan_history(sys.argv[2], known)
    commits = sorted({c for c, _, _ in h})
    print(f"история {sys.argv[2]}: коммитов с совпадениями {len(commits)}, файло-вхождений {len(h)}")
    for c, p, n in h[:15]: print("  ", c, p, n)
    sys.exit(1 if h else 0)
