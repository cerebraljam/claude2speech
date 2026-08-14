#!/usr/bin/env python3
"""claude2speech — speak Claude Code's chat output aloud.

Installed as a Claude Code `Stop` hook. On each turn end it reads the session
transcript, pulls out the assistant's *text* blocks only (never tool calls,
tool output, or thinking), strips the markdown, and speaks it with a voice
chosen deterministically from the project name.

Modes:
    speak.py                  hook mode (reads hook JSON on stdin)
    speak.py --narrate        speak new text mid-turn (PreToolUse hook)
    speak.py --interrupt      stop playback for this project (UserPromptSubmit hook)
    speak.py --voices         list the voice pool
    speak.py --voice          show which voice this project gets
    speak.py --demo           speak every voice in the pool, to compare them
    speak.py --test TEXT      speak TEXT with this project's voice
    speak.py --dry-run        print what would be spoken, don't speak
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
# Read from this script's directory, not the project being spoken about — and
# named for the project rather than a generic "config.json", which would be a
# collision waiting to happen if it ever becomes a per-project override.
CONFIG_NAME = "claude2speech.json"
# Deliberately not $TMPDIR: on macOS that is a per-session launchd path, and
# --interrupt must find the pidfile written by a different process.
STATE_DIR = os.path.expanduser("~/.claude/claude2speech")

DEFAULTS = {
    "volume": 0.5,          # 0.0 - 1.0, applied per-invocation via afplay
    "rate": 190,            # words per minute
    "max_chars": 700,       # truncate long answers at a sentence boundary
    "locales": ["en_US", "en_GB"],
    "voice_overrides": {},  # {"project-name": "Daniel"}
    "live_narration": True,  # speak text as it appears, via the PreToolUse hook
    "prefer_high_quality": True,  # use Premium/Enhanced voices if any exist
}

VOICE_CACHE_TTL = 3600  # seconds

# Voices excluded from the random pool.
#
# Two families, both verifiable via AVSpeechSynthesisVoice identifiers:
#
#   com.apple.speech.synthesis.voice.*  classic MacinTalk novelty voices —
#                                       they sing, beep, or gargle.
#   com.apple.eloquence.*               Eloquence, a 1990s formant synthesizer
#                                       Apple ships for accessibility users who
#                                       prefer its speed. Intelligible if you're
#                                       used to it, rough going if you aren't.
#
# The real voices are com.apple.voice.* — those are the ones we keep.
BLOCKED_VOICES = {
    # MacinTalk novelty
    "agnes", "albert", "bad news", "bahh", "bells", "boing", "bruce",
    "bubbles", "cellos", "deranged", "fred", "good news", "hysterical",
    "jester", "junior", "kathy", "organ", "pipe organ", "princess",
    "ralph", "superstar", "trinoids", "vicki", "victoria", "whisper",
    "wobble", "zarvox",
    # Eloquence
    "eddy", "flo", "grandma", "grandpa", "reed", "rocko", "sandy", "shelley",
}

# macOS appends the quality tier to the name, e.g. "Ava (Premium)". These are
# the large downloadable voices and are dramatically clearer than the compact
# ones installed by default.
_HQ = re.compile(r"\((premium|enhanced)\)", re.I)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, CONFIG_NAME)
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:
            print(f"claude2speech: bad {CONFIG_NAME} ({e}), using defaults",
                  file=sys.stderr)

    for key, cast in (("volume", float), ("rate", int), ("max_chars", int)):
        env = os.environ.get("CLAUDE2SPEECH_" + key.upper())
        if env:
            try:
                cfg[key] = cast(env)
            except ValueError:
                pass

    cfg["volume"] = max(0.0, min(1.0, float(cfg["volume"])))
    return cfg


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------

def all_voices(refresh=False):
    """[(name, locale), ...] as reported by `say -v '?'`.

    Cached: enumerating ~200 voices costs ~400ms, and the narration hook runs
    before every tool call. Voices change only when you install one, so a stale
    read for an hour is harmless — and `--voices` always refreshes.
    """
    cache = os.path.join(STATE_DIR, "voices.tsv")
    if not refresh:
        try:
            if time.time() - os.path.getmtime(cache) < VOICE_CACHE_TTL:
                with open(cache) as f:
                    rows = [l.rstrip("\n").split("\t") for l in f]
                return [(r[0], r[1]) for r in rows if len(r) == 2]
        except OSError:
            pass

    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    voices = []
    for line in out.splitlines():
        head = line.split("#")[0].rstrip()
        m = re.search(r"\s+([a-z]{2}(?:_[A-Z]{2})?)$", head)
        if not m:
            continue
        name = head[:m.start()].strip()
        if name:
            voices.append((name, m.group(1)))

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(cache, "w") as f:
            for name, locale in voices:
                f.write(f"{name}\t{locale}\n")
    except OSError:
        pass
    return voices


def voice_pool(cfg):
    """Natural-sounding voices in the configured locales, sorted for stability.

    If any Premium/Enhanced voices are installed, the pool narrows to those —
    they are strictly better, so there is no reason to roll a compact voice
    once a good one exists.
    """
    locales = set(cfg["locales"])
    pool = []
    for name, locale in all_voices():
        if locale not in locales:
            continue
        # "Flo (English (UK))" -> "flo"; "Ava (Premium)" -> "ava"
        base = name.split("(")[0].strip().lower()
        if base in BLOCKED_VOICES:
            continue
        pool.append(name)

    pool = sorted(set(pool))
    if cfg.get("prefer_high_quality", True):
        good = [v for v in pool if _HQ.search(v)]
        if good:
            return good
    return pool


def project_key(cwd):
    return os.path.basename(os.path.abspath(cwd)) or "default"


def resolve_voice(requested, cfg):
    """Map a requested voice name onto an actually-installed one.

    `say -v <unknown>` does not fail — it silently substitutes a default, which
    would give every misconfigured project the same voice with no warning. So
    overrides are validated here instead.

    Matching is lenient: "Ava" resolves to "Ava (Premium)" if that is what is
    installed, because nobody wants to type the quality suffix.

    Returns (voice_or_None, note_or_None).
    """
    installed = all_voices()
    names = [name for name, _ in installed]
    if requested in names:
        return requested, None

    low = requested.strip().lower()
    for name in names:
        if name.lower() == low:
            return name, None

    # A bare "Sandy" matches several installed voices across languages. Rank so
    # an in-locale, non-blocked, high-quality variant wins — otherwise a bare
    # name can quietly select a Chinese novelty voice.
    locales = set(cfg["locales"])
    variants = [(n, loc) for n, loc in installed
                if n.split("(")[0].strip().lower() == low]
    if variants:
        def rank(item):
            name, locale = item
            base = name.split("(")[0].strip().lower()
            m = _HQ.search(name)
            quality = 2 if not m else (0 if m.group(1).lower() == "premium" else 1)
            return (base in BLOCKED_VOICES, locale not in locales, quality, name)
        best = sorted(variants, key=rank)[0][0]
        return best, f"voice {requested!r} resolved to {best!r}"

    return None, (f"voice {requested!r} is not installed — "
                  f"falling back to automatic selection")


def pick_voice(cfg, key):
    override = os.environ.get("CLAUDE2SPEECH_VOICE") or \
        cfg.get("voice_overrides", {}).get(key)
    if override:
        voice, note = resolve_voice(override, cfg)
        if note:
            print(f"claude2speech: {note}", file=sys.stderr)
        if voice:
            return voice

    pool = voice_pool(cfg)
    if not pool:
        return None
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return pool[int(digest, 16) % len(pool)]


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------

def _text_blocks(entry):
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and b.get("text", "").strip()]


def _is_real_user_turn(entry):
    """A user message that isn't just a tool_result being fed back in."""
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return False


def _load_rows(transcript_path):
    try:
        with open(transcript_path, errors="replace") as f:
            rows = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            return rows
    except OSError:
        return []


def _ends_with_text(rows):
    """True if the newest assistant entry is prose rather than a tool call.

    Stop can fire fractionally before Claude's closing message is flushed to
    the transcript. When that happens the newest assistant entry is still the
    last tool_use (or a thinking block), which is the signal to wait.
    """
    for entry in reversed(rows):
        if entry.get("isSidechain") or entry.get("type") != "assistant":
            continue
        return bool(_text_blocks(entry))
    return False


def wait_for_closing_text(transcript_path, timeout=3.0, interval=0.1):
    """Poll until the closing message lands, then return the turn's entries."""
    deadline = time.time() + timeout
    while True:
        rows = _load_rows(transcript_path)
        if _ends_with_text(rows) or time.time() >= deadline:
            return turn_entries(rows)
        time.sleep(interval)


def turn_entries(transcript_path):
    """[(id, text), ...] — assistant text blocks since the last real user turn.

    Chronological. The id is the transcript entry uuid, which is what lets the
    narration hook and the Stop hook agree on what has already been spoken.
    Accepts a path or an already-parsed list of rows.
    """
    entries = (transcript_path if isinstance(transcript_path, list)
               else _load_rows(transcript_path))

    collected = []
    for entry in reversed(entries):
        # Subagent messages live in the same file; SubagentStop covers those.
        if entry.get("isSidechain"):
            continue
        if _is_real_user_turn(entry):
            break
        if entry.get("type") != "assistant":
            continue
        blocks = _text_blocks(entry)
        if not blocks:
            continue
        text = "\n\n".join(blocks)
        ident = entry.get("uuid") or hashlib.md5(text.encode()).hexdigest()
        collected.insert(0, (ident, text))

    return collected


def load_spoken(key):
    try:
        with open(_state_path(key, "spoken")) as f:
            return set(f.read().split())
    except OSError:
        return set()


def mark_spoken(key, idents):
    seen = load_spoken(key)
    seen.update(idents)
    try:
        with open(_state_path(key, "spoken"), "w") as f:
            # Bounded: only the current turn is ever consulted.
            f.write("\n".join(list(seen)[-200:]))
    except OSError:
        pass


# --------------------------------------------------------------------------
# markdown -> speech
# --------------------------------------------------------------------------

# The lookbehind keeps the absolute pattern from biting into the middle of a
# relative path ("src/utils/helper.ts" must not become "srchelper.ts").
_PATH_ABS = re.compile(
    r"(?<![A-Za-z0-9._~\-])(?:~|\.{1,2})?/[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)*")
_PATH_REL = re.compile(r"\b[A-Za-z0-9._\-]+/[A-Za-z0-9._\-/]*\.[A-Za-z0-9]+\b")


def _basename(match):
    return match.group(0).rstrip("/").split("/")[-1]


def _strip_symbols(text):
    """Drop emoji and pictographs — `say` either spells them out or stumbles."""
    out = []
    for ch in text:
        if ch in "‍️︎":
            continue
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cf"):
            continue
        out.append(ch)
    return "".join(out)


def clean(md):
    t = md

    # Fenced code blocks go entirely, including an unterminated trailing one.
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"~~~.*?~~~", " ", t, flags=re.S)
    t = re.sub(r"```.*\Z", " ", t, flags=re.S)

    # HTML / comments
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>\n]{1,200}>", " ", t)

    # Links and images -> their text
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "link", t)

    # Inline code keeps its contents — dropping it guts too many sentences.
    t = re.sub(r"`+([^`\n]*)`+", r"\1", t)

    lines = []
    for line in t.split("\n"):
        s = line.strip()
        if s.startswith("|"):                       # table row
            continue
        if re.fullmatch(r"[-*_=]{3,}", s):          # horizontal rule
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)            # heading
        s = re.sub(r"^>+\s*", "", s)                # blockquote
        s = re.sub(r"^[-*+]\s+\[[ xX]\]\s+", "", s)  # task list
        s = re.sub(r"^[-*+]\s+", "", s)             # bullet
        s = re.sub(r"^\d+[.)]\s+", "", s)           # numbered
        lines.append(s)
    t = "\n".join(lines)

    # Emphasis markers
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"~~([^~]+)~~", r"\1", t)
    t = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", t)
    t = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", t)

    # Paths read horribly character by character — keep only the basename.
    t = _PATH_ABS.sub(_basename, t)
    t = _PATH_REL.sub(_basename, t)

    t = _strip_symbols(t)

    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def truncate(text, max_chars):
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max(head.rfind(". "), head.rfind(".\n"), head.rfind("! "),
              head.rfind("? "), head.rfind("\n\n"))
    if cut > max_chars // 3:
        return head[:cut + 1].strip()
    return head.rsplit(" ", 1)[0].strip()


# --------------------------------------------------------------------------
# playback
# --------------------------------------------------------------------------

def _state_path(key, suffix):
    os.makedirs(STATE_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return os.path.join(STATE_DIR, f"{safe}.{suffix}")


def is_playing(key):
    """True if this project is currently speaking."""
    try:
        with open(_state_path(key, "pid")) as f:
            pid = int(f.read().strip())
        os.killpg(os.getpgid(pid), 0)
        return True
    except (OSError, ValueError, ProcessLookupError):
        return False


def stop_playback(key):
    """Kill the process group of anything this project is currently speaking."""
    pidfile = _state_path(key, "pid")
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        killed = True
    except (OSError, ProcessLookupError):
        killed = False
    try:
        os.remove(pidfile)
    except OSError:
        pass
    return killed


def speak(text, voice, cfg, key):
    stop_playback(key)

    txt = _state_path(key, "txt")
    with open(txt, "w") as f:
        f.write(text)

    say = ["say", "-r", str(cfg["rate"])]
    if voice:
        say += ["-v", voice]
    say += ["-f", txt]

    if cfg["volume"] >= 0.999:
        # Full volume: skip the render step and stream straight out.
        cmd = " ".join(_q(a) for a in say)
    else:
        aiff = _state_path(key, "aiff")
        render = " ".join(_q(a) for a in say + ["-o", aiff])
        play = " ".join(_q(a) for a in
                        ["afplay", "-v", str(cfg["volume"]), aiff])
        cmd = f"{render} && {play}; rm -f {_q(aiff)}"

    proc = subprocess.Popen(
        ["/bin/sh", "-c", cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group, so --interrupt can kill it
    )
    with open(_state_path(key, "pid"), "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def _q(arg):
    return "'" + str(arg).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# hook entry point
# --------------------------------------------------------------------------

def _read_payload():
    """The hook JSON Claude Code sends on stdin."""
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def is_muted(cwd):
    if os.environ.get("CLAUDE2SPEECH_DISABLE"):
        return True
    return os.path.exists(os.path.join(cwd, "mute"))


def already_spoken(key, text):
    """Guard against a duplicate Stop firing on the same answer."""
    marker = _state_path(key, "last")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    try:
        with open(marker) as f:
            if f.read().strip() == digest:
                return True
    except OSError:
        pass
    try:
        with open(marker, "w") as f:
            f.write(digest)
    except OSError:
        pass
    return False


def narrate_mode(cfg):
    """PreToolUse: speak text Claude has written but not yet spoken.

    This is what makes narration live — the assistant's "I'll go look at X"
    lands in the transcript before the tool runs, so it can be spoken while the
    work happens rather than being replayed at the end of the turn.
    """
    if not cfg.get("live_narration", True):
        return 0

    payload = _read_payload()
    cwd = (os.environ.get("CLAUDE_PROJECT_DIR")
           or payload.get("cwd") or os.getcwd())
    key = project_key(cwd)

    if is_muted(cwd):
        return 0

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0

    pending = [e for e in turn_entries(transcript)
               if e[0] not in load_spoken(key)]
    if not pending:
        return 0

    # Already talking: leave these unmarked so the next tool call retries them,
    # rather than cutting a sentence in half.
    if is_playing(key):
        return 0

    text = truncate(clean("\n\n".join(t for _, t in pending)), cfg["max_chars"])
    idents = [i for i, _ in pending]
    if text and re.search(r"[A-Za-z0-9]", text):
        speak(text, pick_voice(cfg, key), cfg, key)
    mark_spoken(key, idents)
    return 0


def hook_mode(cfg, dry_run=False):
    payload = _read_payload()
    cwd = (os.environ.get("CLAUDE_PROJECT_DIR")
           or payload.get("cwd") or os.getcwd())
    key = project_key(cwd)

    if is_muted(cwd):
        return 0

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0

    # Stop can fire before Claude's closing message is written; wait for it.
    entries = wait_for_closing_text(transcript)
    if not entries:
        return 0

    if cfg.get("live_narration", True):
        # Narration already spoke the preambles; say only what is left.
        pending = [e for e in entries if e[0] not in load_spoken(key)]
    else:
        # No narration hook, so speak the final answer only — never the
        # preambles, which would arrive long after the work they describe.
        pending = entries[-1:]
    if not pending:
        return 0

    text = truncate(clean("\n\n".join(t for _, t in pending)), cfg["max_chars"])
    if not text or not re.search(r"[A-Za-z0-9]", text):
        return 0

    if dry_run:
        print(f"[{key}] voice: {pick_voice(cfg, key)}\n")
        print(text)
        return 0

    if already_spoken(key, text):
        return 0

    speak(text, pick_voice(cfg, key), cfg, key)
    mark_spoken(key, [i for i, _ in pending])
    return 0


def interrupt_mode(cfg):
    payload = _read_payload()
    cwd = (os.environ.get("CLAUDE_PROJECT_DIR")
           or payload.get("cwd") or os.getcwd())
    stop_playback(project_key(cwd))
    return 0


def main():
    args = sys.argv[1:]
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    key = project_key(cwd)

    if "--interrupt" in args:
        return interrupt_mode(cfg)

    if "--narrate" in args:
        return narrate_mode(cfg)

    if "--voices" in args:
        pool = voice_pool(cfg)
        chosen = pick_voice(cfg, key)
        hq = [v for v in pool if _HQ.search(v)]
        print(f"{len(pool)} voices in {', '.join(cfg['locales'])}"
              f"{' (high quality only)' if hq else ''}:")
        for name in pool:
            print(f"  {'* ' if name == chosen else '  '}{name}")
        if not hq:
            print("\n  No Premium/Enhanced voices installed — these are all "
                  "compact voices.\n  System Settings > Accessibility > Spoken "
                  "Content > System Voice > Manage Voices")
        return 0

    if "--voice" in args:
        print(f"{key}: {pick_voice(cfg, key)}")
        return 0

    if "--demo" in args:
        # Speak each voice in turn, synchronously, so they can be compared.
        for name in voice_pool(cfg):
            print(f"  {name}")
            subprocess.run(["say", "-v", name, "-r", str(cfg["rate"]),
                            f"Hello. This is {name.split('(')[0].strip()}. "
                            f"This is how your project would sound."])
        return 0

    if "--test" in args:
        i = args.index("--test")
        text = " ".join(args[i + 1:]) or \
            "Hello. This is how this project sounds."
        voice = pick_voice(cfg, key)
        print(f"{key}: {voice}")
        speak(clean(text), voice, cfg, key)
        return 0

    return hook_mode(cfg, dry_run="--dry-run" in args)


if __name__ == "__main__":
    sys.exit(main())
