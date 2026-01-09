#!/usr/bin/env bash
#
# push_fly_secrets.sh - Push secrets from .env to Fly.io app
#
# Usage:
#   ./push_fly_secrets.sh              # Push all secrets
#   ./push_fly_secrets.sh --dry-run    # Preview without pushing
#
# Requirements:
#   - Fly CLI (flyctl) installed and authenticated
#   - Run from project root (where .env and fly.toml are)
#

echo "Pushing secrets to Fly.io..."

ENV_FILE=".env"
DRY_RUN=false

if [[ "$1" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "(Dry run mode - no changes will be made)"
fi

# Check for fly CLI
if ! command -v flyctl &> /dev/null; then
    echo "Error: flyctl not found. Install from https://fly.io/docs/flyctl/install/"
    exit 1
fi

# Check for .env file
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found"
    exit 1
fi

# Keys to skip (not needed on Fly or handled differently)
SKIP_KEYS="FLASK_ENV FLASK_DEBUG DEBUG PORT HOST LOG_LEVEL"

# Build the secrets command
SECRETS=""

while IFS= read -r line || [ -n "$line" ]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    # Parse KEY=VALUE
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        KEY="${BASH_REMATCH[1]}"
        VALUE="${BASH_REMATCH[2]}"

        # Remove surrounding quotes
        VALUE="${VALUE#\"}"
        VALUE="${VALUE%\"}"
        VALUE="${VALUE#\'}"
        VALUE="${VALUE%\'}"

        # Skip certain keys
        if [[ " $SKIP_KEYS " =~ " $KEY " ]]; then
            echo "  SKIP $KEY (non-secret)"
            continue
        fi

        # Skip empty values
        if [ -z "$VALUE" ]; then
            echo "  SKIP $KEY (empty)"
            continue
        fi

        echo "  ADD  $KEY"
        SECRETS="$SECRETS $KEY=\"$VALUE\""
    fi
done < "$ENV_FILE"

echo ""

if [ "$DRY_RUN" = true ]; then
    echo "Would run: flyctl secrets set [secrets...]"
    echo "Run without --dry-run to apply"
else
    echo "Setting secrets on Fly.io..."
    eval "flyctl secrets set $SECRETS"
    echo ""
    echo "Done! Secrets are set. The app will restart automatically."
fi
