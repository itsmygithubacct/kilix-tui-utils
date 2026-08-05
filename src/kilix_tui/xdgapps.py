"""Freedesktop application discovery, shared by every desktop and tool.

The scanner Kilix 95's Start menu has always used, promoted to the SDK so a
launcher catalog is one implementation instead of one per desktop: it reads
installed application ``.desktop`` files from the standard XDG data locations
(the same way an XFCE/garcon menu does) and exposes them, grouped by
freedesktop category.

No UI and no launching here — pure stdlib, driven entirely by
``$XDG_DATA_HOME`` / ``$XDG_DATA_DIRS`` (spec defaults when unset); nothing
about the host machine is hardcoded.  ``scan()`` returns parsed entry dicts,
``grouped()`` buckets them, ``entries_in()`` reads one folder of launchers,
``parse_desktop_file()`` reads one file so user launchers (a desktop folder
of ``.desktop`` files) share the same parser.  How an entry is *opened* stays
with each consumer, because that is where the paradigms genuinely differ.

The scan is cached on the mtimes of every applications directory and
``.desktop`` file, so calling ``scan()`` or ``grouped()`` per frame costs a
handful of ``stat`` calls until something actually changes.

This module is deliberately location-independent — pure stdlib, no
intra-package imports — because it exists twice by design: authored here in
``kilix_sdk`` and mirrored byte-for-byte into the shared TUI core
(kilix-tui-utils ``src/kilix_tui/xdgapps.py``, kept in step by that repo's
``tools/sync_xdgapps.py`` and pinned by its parity test) so the TUI stack
stays standalone-installable.

Added in SDK 1.8; ``entries_in()`` and ``grouped(force=)`` added in SDK 1.9.
"""
import os
import shutil

# freedesktop main category → kilix bucket, in match priority order (a more
# specific category wins over the generic Utility/System catch-alls)
_CATEGORY_BUCKETS = [
    ("Game", "Games"),
    ("Graphics", "Graphics"),
    ("Development", "Development"),
    ("Education", "Education"),
    ("Office", "Office"),
    ("AudioVideo", "Multimedia"),
    ("Audio", "Multimedia"),
    ("Video", "Multimedia"),
    ("Network", "Internet"),
    ("Settings", "System"),
    ("System", "System"),
    ("Utility", "Accessories"),
]

# stable display order for grouped()
BUCKET_ORDER = ["Accessories", "Development", "Education", "Games", "Graphics",
                "Internet", "Multimedia", "Office", "System", "Other"]

# Exec field codes to strip (%% is a literal percent)
_FIELD_CODES = "fFuUichkdDnNvm"

_cache = None
_cache_sig = None


# ── XDG locations ────────────────────────────────────────────────────────────

def app_dirs():
    """Existing "applications" dirs in XDG precedence order (user first)."""
    home = (os.environ.get("XDG_DATA_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "share"))
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    out, seen = [], set()
    for base in [home] + [p for p in dirs.split(":") if p]:
        app = os.path.join(os.path.expanduser(base), "applications")
        real = os.path.abspath(app)
        if real in seen:
            continue
        seen.add(real)
        if os.path.isdir(app):
            out.append(app)
    return out


# ── parsing ──────────────────────────────────────────────────────────────────

def unescape(v):
    """Desktop Entry string escapes: \\s \\n \\t \\r \\\\."""
    out, i, n = [], 0, len(v)
    while i < n:
        c = v[i]
        if c == "\\" and i + 1 < n:
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r",
                        "\\": "\\"}.get(v[i + 1], v[i + 1]))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_field_codes(exec_str):
    # collapse only the whitespace a removed field code left dangling; keep
    # runs of spaces that belong to a (quoted) argument intact
    out, i, n = [], 0, len(exec_str)
    while i < n:
        c = exec_str[i]
        if c == "%" and i + 1 < n:
            nxt = exec_str[i + 1]
            if nxt == "%":
                out.append("%")
                i += 2
                continue
            if nxt in _FIELD_CODES:
                i += 2
                while i < n and exec_str[i] in " \t":
                    i += 1
                while out and out[-1] in " \t":
                    out.pop()
                if out and i < n:
                    out.append(" ")
                continue
        out.append(c)
        i += 1
    return "".join(out).strip()


def parse_desktop_file(path):
    """Return the [Desktop Entry] key→value dict, or None if unreadable.
    First value wins for a repeated key; only the Desktop Entry group."""
    entry, in_group = {}, False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("[") and s.endswith("]"):
                    in_group = (s == "[Desktop Entry]")
                    continue
                if not in_group or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k:
                    entry.setdefault(k, v.strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return entry or None


def _locale_variants():
    """Locale keys to try, most specific first, from the environment."""
    loc = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
           or os.environ.get("LANG") or "").split(".")[0]
    mod = ""
    if "@" in loc:
        loc, mod = loc.split("@", 1)
    lang, country = loc, ""
    if "_" in loc:
        lang, country = loc.split("_", 1)
    out = []
    if country and mod:
        out.append("%s_%s@%s" % (lang, country, mod))
    if country:
        out.append("%s_%s" % (lang, country))
    if lang and mod:
        out.append("%s@%s" % (lang, mod))
    if lang:
        out.append(lang)
    return out


def localized(entry, key):
    for loc in _locale_variants():
        v = entry.get("%s[%s]" % (key, loc))
        if v is not None:
            return v
    return entry.get(key)


def truthy(v):
    return str(v).strip().lower() == "true"


def _tryexec_ok(prog):
    if not prog:
        return True
    prog = unescape(prog)
    if os.path.isabs(prog):
        return os.path.isfile(prog) and os.access(prog, os.X_OK)
    return shutil.which(prog) is not None


def build_entry(p, path, fid):
    """A parsed dict → an entry dict, or None if the spec says to skip it."""
    if p.get("Type") != "Application":
        return None
    if truthy(p.get("NoDisplay")) or truthy(p.get("Hidden")):
        return None
    if not _tryexec_ok(p.get("TryExec")):
        return None
    exec_raw = p.get("Exec")
    if not exec_raw:
        return None
    name = (localized(p, "Name")
            or os.path.splitext(os.path.basename(path))[0])
    cats = [c for c in unescape(p.get("Categories", "")).split(";") if c]
    return {
        "id": fid,
        "name": unescape(name),
        "exec": strip_field_codes(unescape(exec_raw)),
        "icon": p.get("Icon", ""),
        "categories": cats,
        "terminal": truthy(p.get("Terminal")),
        "path": path,
        "workdir": unescape(p.get("Path", "")),
    }


# ── scanning (cached on app-dir mtimes) ──────────────────────────────────────

def _walk(root):
    """(full_path, desktop-file id) for every *.desktop under root."""
    out = []
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".desktop"):
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, root)
                out.append((full, rel.replace(os.sep, "-")))
    return sorted(out, key=lambda t: t[1])


def _mtime(d):
    try:
        return os.stat(d).st_mtime
    except OSError:
        return 0.0


def _sig(dirs):
    """Mtimes of every dir and .desktop file under the app dirs, so an
    in-place edit or a change inside a subdir invalidates the cache."""
    out = []
    for d in dirs:
        for base, _subdirs, files in os.walk(d):
            out.append((base, _mtime(base)))
            for fn in files:
                if fn.endswith(".desktop"):
                    p = os.path.join(base, fn)
                    out.append((p, _mtime(p)))
    return tuple(out)


def scan(force=False):
    """Parsed application entries, deduped by id (user dir wins), name-sorted.
    Cached; only rescans when an app dir/file mtime changed or force=True."""
    global _cache, _cache_sig
    dirs = app_dirs()
    sig = _sig(dirs)
    if not force and _cache is not None and sig == _cache_sig:
        return _cache
    seen, entries = set(), []
    for d in dirs:
        for path, fid in _walk(d):
            if fid in seen:                 # a higher-precedence dir won
                continue
            parsed = parse_desktop_file(path)
            if parsed is None:              # unreadable: let a lower dir try
                continue
            seen.add(fid)
            e = build_entry(parsed, path, fid)
            if e is not None:
                entries.append(e)
    entries.sort(key=lambda e: e["name"].lower())
    _cache, _cache_sig = entries, sig
    return entries


def entries_in(directory):
    """Parsed entries from one folder of ``.desktop`` files, name-sorted.

    No recursion and no cache: this is for a user's desktop-launcher folder
    (the files a Create Launcher wizard writes), which is small and read at
    the moment it is shown, not for the XDG application dirs.
    """
    out = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".desktop"):
            continue
        path = os.path.join(directory, fn)
        parsed = parse_desktop_file(path)
        if parsed is None:
            continue
        entry = build_entry(parsed, path, fn)
        if entry is not None:
            out.append(entry)
    out.sort(key=lambda e: e["name"].lower())
    return out


# ── categorization ───────────────────────────────────────────────────────────

def bucket(entry):
    cats = set(entry.get("categories") or [])
    for cat, b in _CATEGORY_BUCKETS:
        if cat in cats:
            return b
    return "Other"


def grouped(force=False):
    """{bucket: [entry, …]} for non-empty buckets, in display order."""
    out = {}
    for e in scan(force):
        out.setdefault(bucket(e), []).append(e)
    return {b: out[b] for b in BUCKET_ORDER if b in out}


__all__ = [
    "BUCKET_ORDER",
    "app_dirs",
    "bucket",
    "build_entry",
    "entries_in",
    "grouped",
    "localized",
    "parse_desktop_file",
    "scan",
    "strip_field_codes",
    "truthy",
    "unescape",
]
