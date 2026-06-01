#!/usr/bin/env bash
# Install the Memra memory provider into Hermes Agent.
#
#   curl -fsSL https://raw.githubusercontent.com/usememra/hermes-memra/main/install.sh | bash
#
# Then run:  hermes memory setup   (pick "memra")
#
# Tenant scoping:
#   Memra rows are partitioned by tenant_id. Single-user installs should pin
#   tenant_id in $HERMES_HOME/memra.json so every platform (CLI, Telegram,
#   Discord, cron) writes to and reads from the same store. Multi-user
#   gateways should leave tenant_id unset so each session is scoped to its
#   per-user gateway identity. Override the suggested default with:
#       MEMRA_DEFAULT_TENANT=my-tenant curl ... | bash
#   Opt out of pinning entirely:
#       MEMRA_DEFAULT_TENANT="" curl ... | bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/usememra/hermes-memra/main/memra"
FILES=(__init__.py plugin.yaml README.md)
# Tenant tools live under memra/scripts/. memra_doctor.py reports any
# fragmentation that already exists in your project; migrate_tenant.py
# merges rows between tenants when fragmentation needs repairing.
SCRIPT_FILES=(memra_doctor.py migrate_tenant.py)

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Hermes discovers user-installed providers one level deep under
# $HERMES_HOME/plugins/<name>/, so the provider goes directly in plugins/memra/
# — NOT plugins/memory/memra/, which is the in-tree bundled layout. The loader
# imports each provider directory's __init__.py, so keep that filename.
DEST="$HERMES_HOME/plugins/memra"
DEFAULT_TENANT="${MEMRA_DEFAULT_TENANT-hermes-user}"

say() { printf '\033[36m[memra]\033[0m %s\n' "$1"; }

say "Installing Memra memory provider into: $DEST"
mkdir -p "$DEST/scripts"

# Prefer local files when run from inside the repo; otherwise download.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)/memra"
if [[ -f "$SRC_DIR/__init__.py" ]]; then
  say "Copying from local checkout"
  cp "$SRC_DIR"/{__init__.py,plugin.yaml,README.md} "$DEST/"
  for f in "${SCRIPT_FILES[@]}"; do
    if [[ -f "$SRC_DIR/scripts/$f" ]]; then
      cp "$SRC_DIR/scripts/$f" "$DEST/scripts/$f"
      chmod +x "$DEST/scripts/$f"
    fi
  done
else
  for f in "${FILES[@]}"; do
    say "Downloading $f"
    curl -fsSL "$REPO_RAW/$f" -o "$DEST/$f"
  done
  for f in "${SCRIPT_FILES[@]}"; do
    say "Downloading scripts/$f"
    curl -fsSL "$REPO_RAW/scripts/$f" -o "$DEST/scripts/$f"
    chmod +x "$DEST/scripts/$f"
  done
fi

# Suggest a memra.json template that includes tenant_id when one isn't
# already present. Don't overwrite an existing config — `hermes memory setup`
# (or the user) will fill in project_id later.
if [[ ! -f "$HERMES_HOME/memra.json" ]] && [[ -n "$DEFAULT_TENANT" ]]; then
  printf '{\n  "project_id": "",\n  "tenant_id": "%s"\n}\n' "$DEFAULT_TENANT" \
    > "$HERMES_HOME/memra.json.template"
  say "Wrote $HERMES_HOME/memra.json.template (rename to memra.json after filling project_id)"
fi

cat <<EOF

$(say "Installed.")

Next steps:
  1. hermes memory setup        # select "memra", paste your API key + project id
                                # (get them at https://usememra.com)
  - or configure manually -
  2. hermes config set memory.provider memra
     echo "MEMRA_API_KEY=memra_live_xxx" >> "$HERMES_HOME/.env"
     # Single-user install (pin tenant so every platform shares one store):
     echo '{"project_id": "proj_xxx", "tenant_id": "${DEFAULT_TENANT:-hermes-user}"}' \\
       > "$HERMES_HOME/memra.json"
     # Multi-user gateway (per-user scoping via gateway user_id):
     # echo '{"project_id": "proj_xxx"}' > "$HERMES_HOME/memra.json"

Tenant scoping (IMPORTANT):
  Memra rows are partitioned by tenant_id. Without an explicit pin,
  Telegram/Discord sessions silently fragment into separate stores keyed by
  per-platform user_id. The single-user example above pins it; multi-user
  gateways should leave tenant_id unset and let the plugin fall back to the
  gateway user_id.

  Override the suggested default:  MEMRA_DEFAULT_TENANT=my-tenant curl ... | bash
  Opt out of pinning:              MEMRA_DEFAULT_TENANT=""        curl ... | bash

Verify:                            hermes memory status
Detect existing fragmentation:     $DEST/scripts/memra_doctor.py
Repair fragmentation when found:   $DEST/scripts/migrate_tenant.py \\
                                     --from-tenant <orphan> \\
                                     --to-tenant ${DEFAULT_TENANT:-hermes-user}
EOF
