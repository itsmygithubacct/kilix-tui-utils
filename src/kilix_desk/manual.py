"""The stack's help book, and the pager run that reads it.

One text source for every help surface this desktop offers: the System ▸
Manual place lists these topics, and running this file as a program pages
one — through `$PAGER` (defaulting to `less`), or held for Enter when no
pager exists, because help that flashes and repaints in the same instant is
indistinguishable from a crash.

The topics are the book Kilix 95's Help renders, reworded where that
desktop's pixels differ from this one's text rows. Keeping the wording
shared is deliberate: a user who read the guide on one desktop has read it
on both.

The Pleb Recovery Guide is the one topic with a document behind it,
resolved through the same candidate ladder Kilix 95 walks — the installer's
`$PLEB_RECOVERY_DOC_DST` override, the installed copy under
/usr/local/share/doc/pleb, then the source checkout. When no candidate
exists the topic degrades to self-help text naming the exact repair
commands, because a recovery entry that only says "not installed" fails at
precisely the moment it was needed.

Man pages are the rest of the book: the same discovery Kilix 95's System
Manual browser performs, listed by the desk so `/` can search them and
`man` can render them.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap

if __package__ in (None, ""):        # run as a program, not imported
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

from kilix_desk import sources       # noqa: E402

PATH = os.path.abspath(__file__)
WIDTH = 76
PLEB_RECOVERY_DOC = "/usr/local/share/doc/pleb/RECOVERY.md"

# The book: (key, title, blocks); a block is (kind, text) — "h" heading,
# "p" paragraph, "b" bullet, "c" verbatim command — or ("l", label, url).
TOPICS: tuple[tuple[str, str, tuple], ...] = (
    ("welcome", "Welcome to Kilix TUI", (
        ("h", "Welcome to Kilix TUI"),
        ("p", "Kilix TUI is the text-native desktop for the Plebian-OS "
              "stack: one list, one cursor, and a trail on the first row "
              "saying where you are."),
        ("b", "Enter or Right walks into a place; Esc or Left walks back "
              "out."),
        ("b", "/ filters the list you are looking at as you type."),
        ("b", "! opens a prompt that runs a command of your own."),
        ("b", "? shows every key, on any screen."),
        ("b", "1-6 jump straight to a section; Tab cycles them."),
        ("p", "Programs launches the stack's tools, Machine watches the "
              "hardware, System maintains the stack, Session manages panes "
              "and transcripts, and Power asks before anything "
              "irreversible."),
        ("p", "Inside Kilix an entry may open in its own page; everywhere "
              "else the same entry runs in place and hands the terminal "
              "back when it exits."),
    )),
    ("kilix", "Using Kilix", (
        ("h", "Using Kilix"),
        ("p", "Kilix is the host terminal and app runner behind this "
              "desktop. It opens terminal pages, runs graphical programs "
              "inside panes, and provides the desktop's services."),
        ("b", "Session > New terminal opens a normal shell page."),
        ("b", "Session > Mux terminal opens a persistent mux session."),
        ("b", "kilix run COMMAND runs an X11 program in a contained pane."),
        ("b", "kilix open-url URL prefers a desktop browser and falls back "
              "to the text browser in a pane."),
        ("b", "kilix install owns the one software catalog; Programs > "
              "Install software is the same list."),
        ("l", "Kilix repository",
         "https://github.com/itsmygithubacct/kilix"),
    )),
    ("pleb", "Using Pleb", (
        ("h", "Using Pleb"),
        ("p", "Pleb owns the installed user session around Kilix: display "
              "manager wiring, session startup, autologin and kiosk "
              "behaviour, and updates of the user-facing stack."),
        ("b", "pleb install installs or refreshes the session pieces."),
        ("b", "pleb update updates the session stack when available."),
        ("b", "System > OS control exposes the session and update actions "
              "when the helpers are installed."),
        ("b", "Use Pleb when the login or session wiring is wrong; use "
              "Kilix when the terminal runtime itself needs attention."),
        ("l", "Pleb repository",
         "https://github.com/itsmygithubacct/pleb"),
    )),
    ("plebianos", "Using Plebian-OS", (
        ("h", "Using Plebian-OS"),
        ("p", "Plebian-OS is the Debian-based system image and installer "
              "layer that provisions Kilix, Pleb, and this desktop."),
        ("b", "System > Update the stack updates everything; Update and "
              "restart desktop re-execs this desktop on the new code."),
        ("b", "Use Reinstall dependencies only when the installed stack is "
              "missing required system packages."),
        ("b", "Shut down and reboot from Power, so the session can restore "
              "the terminal cleanly."),
        ("b", "If the desktop opens but external apps fail, check the "
              "system dependencies: Xvfb, ffmpeg, browsers, audio, Python "
              "modules."),
        ("l", "Plebian-OS repository",
         "https://github.com/itsmygithubacct/plebian-os"),
    )),
    ("terminal", "Terminal basics", (
        ("h", "Terminal basics"),
        ("p", "A terminal runs command-line programs in a shell. Commands "
              "usually read standard input, write standard output, and "
              "report errors on standard error."),
        ("b", "pwd prints the current directory; cd DIR changes it."),
        ("b", "ls lists files; ls -la includes hidden files and details."),
        ("b", "Tab completes file and command names in most shells."),
        ("b", "Ctrl+C interrupts the foreground program; Ctrl+D sends end "
              "of input."),
        ("b", "command --help often prints a quick usage summary."),
        ("b", "man COMMAND opens the manual page; System > Manual > Man "
              "pages reaches the same references from this desktop."),
        ("b", "Pipes like producer | consumer send output from one command "
              "into another."),
    )),
    ("tmux", "Using tmux", (
        ("h", "Using tmux"),
        ("p", "tmux keeps terminal sessions alive and lets one terminal "
              "hold multiple windows and panes. Session > Tmux manager "
              "drives it from a list."),
        ("b", "tmux new -s NAME starts a named session."),
        ("b", "tmux attach -t NAME reconnects to a session."),
        ("b", "tmux ls lists sessions; tmux kill-session -t NAME stops "
              "one."),
        ("b", "The default prefix is Ctrl+B: then C creates a window, N "
              "moves to the next one, % and \" split, D detaches."),
        ("l", "tmux manual", "https://man.openbsd.org/tmux.1"),
    )),
    ("bash", "Using bash", (
        ("h", "Using bash"),
        ("p", "bash is the default command shell on many Linux systems. It "
              "runs commands, expands variables, keeps history, and "
              "combines commands with pipes and redirection."),
        ("b", "echo $NAME prints a variable; export NAME=value hands it to "
              "child processes."),
        ("b", "history shows recent commands; Ctrl+R searches them."),
        ("b", "alias ll='ls -la' shortens a command for this shell; "
              "lasting aliases live in ~/.bashrc."),
        ("b", "> writes output to a file; >> appends; 2> redirects "
              "errors."),
        ("b", "Quote paths with spaces, for example cd \"My Folder\"."),
        ("l", "GNU Bash manual",
         "https://www.gnu.org/software/bash/manual/bash.html"),
    )),
    # Kilix 95's Voice Help, reworded for a desktop that *is* terminal text:
    # the boundary worth naming is that the speech widgets are host chrome,
    # so the entries here can only ever be settings and diagnostics.
    ("voice", "Where speak and dictate live", (
        ("h", "Where speak and dictate live"),
        ("p", "Read aloud and Dictation are the speaking-head and "
              "microphone buttons in the page strip at the top of Kilix — "
              "host chrome, not entries in this desktop."),
        ("p", "They work on terminal panes: click the speaking head to "
              "read the pane you are looking at, or the microphone to "
              "dictate into it, and click the same button again to stop. "
              "This desktop is terminal text, so they work right here."),
        ("p", "The Read aloud and Dictation entries in the Voice place "
              "open each tool's settings and diagnostics — device, model, "
              "level meter. Neither opens a microphone: capture is "
              "click-to-talk and only ever starts from an explicit "
              "press."),
        ("p", "On an installed release, if the voice runtime is missing, "
              "those entries install Kilix's exact pinned runtime before "
              "opening. Voice status reports the runtime's state; Voice "
              "doctor diagnoses it."),
    )),
    ("recovery", "Pleb Recovery Guide", (
        ("h", "Pleb Recovery Guide"),
        ("p", "The installed recovery guide was not found. pleb install "
              "publishes it at /usr/local/share/doc/pleb/RECOVERY.md; this "
              "topic shows the document itself whenever any copy is "
              "readable."),
        ("p", "If pleb update reports that libxxhash is missing, run:"),
        ("c", "sudo /usr/local/sbin/plebian-os-install-deps"),
        ("c", "pleb update"),
        ("p", "If that helper is unavailable, install the immediate "
              "dependency directly:"),
        ("c", "sudo apt-get update"),
        ("c", "sudo apt-get install libxxhash-dev"),
    )),
)


def topics() -> list[tuple[str, str]]:
    """(key, title) for every topic, in book order."""
    return [(key, title) for key, title, _blocks in TOPICS]


def render(key: str) -> str:
    """One topic as wrapped plain text, ready for a pager."""
    for topic_key, _title, blocks in TOPICS:
        if topic_key == key:
            break
    else:
        raise KeyError(key)
    out: list[str] = []
    for index, block in enumerate(blocks):
        kind, text = block[0], block[1]
        if kind == "h":
            out.append(text)
            out.append("─" * min(len(text), WIDTH))
        elif kind == "b":
            out.append(textwrap.fill(text, WIDTH, initial_indent="  · ",
                                     subsequent_indent="    "))
        elif kind == "c":
            out.append("    " + text)
        elif kind == "l":
            out.append(f"  {text}: {block[2]}")
        else:
            out.append(textwrap.fill(text, WIDTH))
        # Runs of bullets or commands stay tight; everything else breathes.
        next_kind = blocks[index + 1][0] if index + 1 < len(blocks) else ""
        if not (kind == next_kind and kind in ("b", "c")):
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# ── the recovery guide's document ladder ─────────────────────────────────────


def recovery_candidates() -> list[str]:
    """Recovery-guide locations, in installed-then-source priority order.

    `$PLEB_RECOVERY_DOC_DST` is primarily Pleb's installer destination
    override; honouring it here makes a deliberately relocated guide
    discoverable from the same environment. The source leg goes through the
    shared checkout finder, so a development machine reads the guide it
    would install.
    """
    candidates = (os.environ.get("PLEB_RECOVERY_DOC_DST"),
                  PLEB_RECOVERY_DOC,
                  os.path.join(sources.component_dir("pleb"),
                               "docs", "RECOVERY.md"))
    out: list[str] = []
    for path in candidates:
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if path not in out:
            out.append(path)
    return out


def recovery_path() -> str | None:
    """The first readable recovery document, or None when none exists."""
    for path in recovery_candidates():
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


# ── man pages, discovered the way `man` finds them ───────────────────────────

MAN_COMPRESSED = ("gz", "bz2", "xz", "lzma", "zst")
DEFAULT_MANPATH = ("/usr/local/share/man", "/usr/share/man", "/usr/local/man")
_MAN_PAGE = re.compile(r"^(.+)\.([0-9][A-Za-z0-9]*)(?:\.([^.]+))?$")


def man_roots() -> list[str]:
    """The manpath, read the way `man` itself reads it.

    An empty field in `$MANPATH` means "insert the system list here", so it
    expands to the defaults rather than vanishing; `manpath -q` answers for
    the common case where the variable is unset.
    """
    env = os.environ.get("MANPATH")
    if env is not None:
        roots: list[str] = []
        for part in env.split(":"):
            if part:
                roots.append(os.path.expanduser(part))
            else:
                roots.extend(DEFAULT_MANPATH)
        return roots
    try:
        result = subprocess.run(["manpath", "-q"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result and result.returncode == 0 and result.stdout.strip():
        return [os.path.expanduser(part)
                for part in result.stdout.strip().split(":") if part]
    return list(DEFAULT_MANPATH)


def man_pages(roots: list[str] | None = None) -> list[dict]:
    """Every installed manual page, sorted; first manpath occurrence wins.

    The same discovery Kilix 95's System Manual browser performs: `man*/`
    directories under each root, `name.section[.compression]` filenames.
    A page is {"name", "section", "label"}; `man SECTION NAME` renders it.
    """
    seen: set[tuple[str, str]] = set()
    pages: list[dict] = []
    for root in (man_roots() if roots is None else roots):
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if not os.path.basename(base).startswith("man"):
                continue
            for filename in files:
                match = _MAN_PAGE.match(filename)
                if not match:
                    continue
                name, section, compression = match.groups()
                if compression and compression not in MAN_COMPRESSED:
                    continue
                key = (name.lower(), section.lower())
                if key in seen:
                    continue
                seen.add(key)
                pages.append({"name": name, "section": section,
                              "label": f"{name} ({section})"})
    pages.sort(key=lambda page: (page["name"].lower(),
                                 page["section"].lower()))
    return pages


# ── the pager run ────────────────────────────────────────────────────────────


def _pager_argv() -> list[str] | None:
    """`$PAGER` resolved to an argv, or None when nothing can page."""
    try:
        argv = shlex.split(os.environ.get("PAGER") or "less")
    except ValueError:
        argv = ["less"]
    if argv and shutil.which(argv[0]):
        return argv
    return None


def _page(text: str) -> int:
    """Show `text` through the pager, held for Enter when there is none."""
    pager = _pager_argv()
    if pager:
        try:
            return subprocess.run(pager, input=text.encode()).returncode
        except OSError:
            pass
    sys.stdout.write(text)
    sys.stdout.flush()
    try:
        input("\n— press Enter to return —")
    except EOFError:
        pass
    return 0


def _page_file(path: str) -> int:
    """Page a document by path, so the pager can name it and search it."""
    pager = _pager_argv()
    if pager:
        try:
            return subprocess.run([*pager, path]).returncode
        except OSError:
            pass
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return _page(handle.read())
    except OSError:
        return _page(render("recovery"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "--list":
        for key, title in topics():
            print(f"{key}\t{title}")
        return 0
    key = argv[0]
    if key not in {topic_key for topic_key, _title in topics()}:
        known = ", ".join(topic_key for topic_key, _title in topics())
        print(f"manual: unknown topic {key!r} (topics: {known})",
              file=sys.stderr)
        return 2
    if key == "recovery":
        if found := recovery_path():
            return _page_file(found)
    return _page(render(key))


if __name__ == "__main__":
    raise SystemExit(main())
