#!/usr/bin/env bash
# Install the Memra memory provider into Hermes Agent.
#
#   curl -fsSL https://raw.githubusercontent.com/usememra/hermes-memra/main/install.sh | bash
#
# Then run:  hermes memory setup   (pick "memra")
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/usememra/hermes-memra/main/memra"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Hermes discovers user-installed providers one level deep under
# $HERMES_HOME/plugins/<name>/ and loads each provider's init.py. So the
# provider goes directly in plugins/memra/ (NOT plugins/memory/memra/, which
# is the in-tree bundled layout), and the entry module is named init.py.
DEST="$HERMES_HOME/plugins/memra"

say() { printf '\033[36m[memra]\033[0m %s\n' "$1"; }

say "Installing Memra memory provider into: $DEST"
mkdir -p "$DEST"

# Prefer local files when run from inside the repo; otherwise download.
# The source package file is __init__.py (its in-tree bundled name); a user
# install deploys it as init.py, which is what the user-plugin loader imports.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)/memra"
if [[ -f "$SRC_DIR/__init__.py" ]]; then
  say "Copying from local checkout"
  cp "$SRC_DIR/__init__.py" "$DEST/init.py"
  cp "$SRC_DIR/plugin.yaml" "$DEST/plugin.yaml"
  cp "$SRC_DIR/README.md"   "$DEST/README.md"
else
  say "Downloading init.py"
  curl -fsSL "$REPO_RAW/__init__.py" -o "$DEST/init.py"
  say "Downloading plugin.yaml"
  curl -fsSL "$REPO_RAW/plugin.yaml" -o "$DEST/plugin.yaml"
  say "Downloading README.md"
  curl -fsSL "$REPO_RAW/README.md" -o "$DEST/README.md"
fi

cat <<EOF

$(say "Installed.")

Next steps:
  1. hermes memory setup        # select "memra", paste your API key + project id
                                # (get them at https://usememra.com)
  - or configure manually -
  2. hermes config set memory.provider memra
     echo "MEMRA_API_KEY=memra_live_xxx" >> "$HERMES_HOME/.env"
     echo '{"project_id": "proj_xxx"}'   >  "$HERMES_HOME/memra.json"

Verify:  hermes memory status
EOF
