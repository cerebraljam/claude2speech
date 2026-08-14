# claude2speech

Make Claude Code talk to you on macOS.

Claude Code produces a lot of output worth paying attention to, and all of it
has to be read. This speaks the parts that are actually addressed to you —
Claude's chat messages — and stays quiet for everything else.

- **Only the chat.** Tool calls, tool output, and extended thinking are never
  spoken. Just the prose Claude writes to you.
- **A voice per project.** Chosen deterministically by hashing the project
  directory name, so a given project always sounds the same and you learn to
  recognise which window is talking.
- **Per-project mute.** `touch mute` in a project root and it goes quiet there.
- **Per-app volume.** macOS has no per-app volume, but speech is rendered and
  played through `afplay -v`, so this has its own volume independent of the
  system level.
- **Interrupts itself.** Start typing a new prompt and the current sentence
  stops.

## Requirements

macOS and Python 3 (both `say` and `python3` ship with the system). No
third-party packages.

## Install

```bash
git clone https://github.com/cerebraljam/claude2speech.git
cd claude2speech
./install.sh
```

That registers two hooks in `~/.claude/settings.json`, so it works in **every**
project:

| Hook | What it does |
| --- | --- |
| `PreToolUse` | Fires before each tool call — speaks anything Claude has written but not yet said |
| `Stop` | Fires when Claude finishes a turn — speaks whatever is left, usually the final answer |
| `UserPromptSubmit` | Fires when you submit a prompt — stops any speech in progress |

Restart Claude Code (or run `/hooks`) for it to take effect.

The installer backs up your settings to `~/.claude/settings.json.bak` and is
idempotent — rerun it after moving the repo, or after pulling a version that
adds a hook, and it updates in place rather than adding a second copy.

Other install modes:

```bash
./install.sh --local      # this project only, via .claude/settings.json
./install.sh --uninstall  # remove the hooks
```

Keep the clone where it is after installing — the hooks point at `speak.py` by
absolute path.

## Controls

```bash
touch mute    # silence this project
rm mute       # unsilence
```

`mute` is in `.gitignore`, so muting a shared repo doesn't commit the choice
for everyone else.

To silence everything everywhere without uninstalling, export
`CLAUDE2SPEECH_DISABLE=1`.

## Configuration

Edit `claude2speech.json`, in the root of this clone. It is read fresh on every
invocation, so edits take effect on the next turn — no restart, no reinstall.
(Rerun `./install.sh` only if you *move* the clone, since the hooks reference
`speak.py` by absolute path.)

| Key | Default | Meaning |
| --- | --- | --- |
| `volume` | `0.5` | Playback volume, `0.0`–`1.0`, independent of system volume |
| `rate` | `190` | Words per minute |
| `max_chars` | `700` | Long answers are cut at the nearest sentence boundary |
| `locales` | `["en_US","en_GB"]` | Which voices are eligible |
| `voice_overrides` | `{}` | Pin a project to a voice: `{"my-project": "Ava"}` |
| `live_narration` | `true` | Speak text as Claude writes it, rather than only at the end of the turn |
| `prefer_high_quality` | `true` | Restrict the pool to Premium/Enhanced voices when any are installed |

`volume`, `rate`, and `max_chars` can be overridden per-shell with
`CLAUDE2SPEECH_VOLUME`, `CLAUDE2SPEECH_RATE`, `CLAUDE2SPEECH_MAX_CHARS`.
`CLAUDE2SPEECH_VOICE` forces a specific voice.

## Voices

```bash
./speak.py --voices   # list the pool, * marks this project's voice
./speak.py --voice    # just this project's voice
./speak.py --test "how does this sound"
```

The pool is built at runtime from `say -v '?'`, filtered to the configured
locales, minus two families of voices that are technically installed but not
pleasant to listen to:

| Family | Examples | Why excluded |
| --- | --- | --- |
| `com.apple.speech.synthesis.voice.*` | Bells, Zarvox, Bad News | Classic MacinTalk novelty voices — they sing and beep |
| `com.apple.eloquence.*` | Sandy, Rocko, Grandpa, Flo | A 1990s formant synthesizer, shipped for accessibility users who prefer its speed |

What remains are the `com.apple.voice.*` families — actual recorded voices. On a
stock macOS 26 install with `en_US` + `en_GB` that is a thin pool: just
**Samantha** and **Daniel**.

To widen it, either add locales (`en_AU` Karen, `en_IE` Moira, `en_ZA` Tessa,
`en_IN` Rishi/Tara/Aman are all clear, and the accents make projects easy to
tell apart), or install higher-quality voices under **System Settings →
Accessibility → Spoken Content → System Voice → Manage Voices**. Anything
marked Enhanced or Premium there is far better than the compact voices
preinstalled.

If any Premium or Enhanced voice is installed, the pool narrows to *only* those
— once a good voice exists there is no reason to roll a compact one. Set
`prefer_high_quality: false` to disable that.

You can check what you have with:

```bash
./speak.py --voices   # the pool
./speak.py --demo     # hear every voice in it, one after another
```

Note that adding or removing voices reshuffles which project gets which — pin
any you have grown attached to in `voice_overrides` first.

### Pinning a voice

Keys in `voice_overrides` are project directory names, values are voice names:

```json
"voice_overrides": { "claude2speech": "Ava", "work-api": "Tom" }
```

You do not need the quality suffix — `"Ava"` resolves to `Ava (Premium)` if
that is what is installed, preferring Premium over Enhanced over compact, and
preferring your configured locales. Matching is case-insensitive.

If the name matches nothing installed, it falls back to the normal hash-based
selection and prints a warning. This matters because `say` itself does *not*
fail on an unknown voice — it silently substitutes a default, which would
otherwise give every misconfigured project the same voice with no indication
why. An override that names an uninstalled voice is therefore a warning, never
a silent surprise.

An exact full name always wins, even outside your configured locales — pinning
`"Karen"` works even with `locales` set to US and UK only.

Voice assignment is `md5(project_dir_name) % len(pool)`, so two checkouts of the
same repo sound the same, and it is stable across machines with the same voices
installed.

## Live narration

With `live_narration` on (the default), Claude speaks as it works. The
"I'll go look at the README" line is said *while* the read happens, not
replayed once the turn is over.

This works because assistant text lands in the transcript before the tool that
follows it runs, so a `PreToolUse` hook can speak it at the right moment. Each
transcript entry has a uuid, and spoken ones are recorded in
`~/.claude/claude2speech/<project>.spoken`, so the `Stop` hook at the end of the
turn says only what is left — usually just the final answer. Nothing is spoken
twice.

If a line arrives while the previous one is still playing, it is left unspoken
rather than cutting the sentence short, and picked up at the next opportunity —
so lines are deferred and batched, never dropped or truncated.

One subtlety worth knowing about: `Stop` can fire a fraction of a second
*before* Claude's closing message is flushed to the transcript, in which case
the newest entry is still the last tool call and the final answer is silently
missed. The `Stop` hook therefore waits (up to 3s) for the newest assistant
entry to be prose rather than a tool call before deciding what to say.

Turning `live_narration` off reverts to speaking only the final answer at the
end of the turn. Preambles are then skipped entirely, since a "let me go look
at X" that arrives after all the work is finished is just noise.

The `PreToolUse` hook runs before *every* tool call, so it has to be cheap.
Enumerating macOS's ~200 voices costs about 400ms, which would be a real tax on
Claude's speed, so the voice list is cached in
`~/.claude/claude2speech/voices.tsv` for an hour. That brings the hook down to
roughly 20ms. `./speak.py --voices` always refreshes, so newly installed voices
show up immediately there.

## How it works

Claude Code's hooks receive the path to the session transcript, a JSONL
file where every entry is tagged by type:

```
assistant ['text']        <- spoken
assistant ['thinking']    <- skipped
assistant ['tool_use']    <- skipped
user      ['tool_result'] <- skipped
```

`speak.py` walks that file backwards from the end, stops at the last real user
message so it never replays an older turn, and keeps only `text` blocks from
`assistant` entries. Subagent messages (`isSidechain`) are skipped too.

The result is then stripped for speech: code fences dropped entirely, links
reduced to their text, bare URLs to the word "link", file paths to their
basename (`/Users/you/project/src/main.py` reads as "main dot pi why"), tables
and horizontal rules removed, emoji removed, and markdown emphasis unwrapped.
Playback runs detached in its own process group so the hook returns immediately
and Claude Code never waits on it.

## Troubleshooting

**Nothing is spoken.** Check for a stray `mute` file, then run the hook by hand
to see what it would say:

```bash
echo '{"transcript_path":"'"$(ls -t ~/.claude/projects/*/*.jsonl | head -1)"'","cwd":"'"$PWD"'"}' \
  | ./speak.py --dry-run
```

`--dry-run` prints the cleaned text and the chosen voice without playing
anything.

**It talks over itself.** Two Claude Code windows in the same project share a
voice and a stop-file. Give one of them a `voice_overrides` entry, or mute it.

**Speech keeps playing after I hit enter.** The `UserPromptSubmit` hook did not
register — rerun `./install.sh` and restart Claude Code.

**I hear the narration but not the final answer.** Check whether the closing
message got marked as spoken:

```bash
python3 -c "import speak,sys; print(speak.load_spoken('YOUR_PROJECT_DIR_NAME'))"
```

`~/.claude/claude2speech/<project>.txt` always holds the exact text last handed
to `say`, which makes it easy to tell whether the wrong message was spoken or
none was.
