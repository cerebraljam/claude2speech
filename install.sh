#!/bin/bash
# Register (or remove) the claude2speech hooks in ~/.claude/settings.json.
#
#   ./install.sh              install globally, for every project
#   ./install.sh --uninstall  remove
#   ./install.sh --local      install for this project only (.claude/settings.json)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPEAK="$HERE/speak.py"
TARGET="$HOME/.claude/settings.json"
MODE="install"

for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE="uninstall" ;;
    --local)     TARGET="$HERE/.claude/settings.json" ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

chmod +x "$SPEAK"
mkdir -p "$(dirname "$TARGET")"
[ -f "$TARGET" ] || echo '{}' > "$TARGET"
cp "$TARGET" "$TARGET.bak"

SPEAK="$SPEAK" TARGET="$TARGET" MODE="$MODE" python3 <<'PY'
import json, os

speak, target, mode = os.environ["SPEAK"], os.environ["TARGET"], os.environ["MODE"]

with open(target) as f:
    text = f.read().strip()
settings = json.loads(text) if text else {}

hooks = settings.setdefault("hooks", {})

def strip(event):
    """Drop any previously installed claude2speech entries (idempotent)."""
    groups = hooks.get(event, [])
    kept = []
    for group in groups:
        inner = [h for h in group.get("hooks", [])
                 if "speak.py" not in str(h.get("command", ""))]
        if inner:
            group["hooks"] = inner
            kept.append(group)
        elif not group.get("hooks"):
            kept.append(group)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)

for event in ("Stop", "UserPromptSubmit", "PreToolUse"):
    strip(event)

if mode == "install":
    hooks.setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": speak, "timeout": 10}]})
    hooks.setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": speak + " --interrupt",
                    "timeout": 5}]})
    # Runs before every tool call, so it must stay fast — the voice list is
    # cached precisely for this.
    hooks.setdefault("PreToolUse", []).append(
        {"hooks": [{"type": "command", "command": speak + " --narrate",
                    "timeout": 5}]})

if not hooks:
    settings.pop("hooks", None)

with open(target, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"{mode}ed -> {target}")
PY

echo "backup: $TARGET.bak"
if [ "$MODE" = "install" ]; then
  echo
  echo "Restart Claude Code (or /hooks to reload) for this to take effect."
  echo "Silence a project:  touch mute   (in that project's root)"
fi
