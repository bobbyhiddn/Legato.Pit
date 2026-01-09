#!/usr/bin/env bash
#
# push_secrets.sh - Push secrets from .env to GitHub repository secrets
#
# Usage:
#   ./push_secrets.sh                    # Push all secrets from .env
#   ./push_secrets.sh --dry-run          # Show what would be pushed without pushing
#   ./push_secrets.sh --file custom.env  # Use a custom env file
#   ./push_secrets.sh KEY1 KEY2          # Push only specific keys
#
# Requirements:
#   - GitHub CLI (gh) installed and authenticated
#   - Must be run from within a git repository
#

echo "Starting push_secrets.sh..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
ENV_FILE=".env"
DRY_RUN=false
SPECIFIC_KEYS=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --file)
            ENV_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [options] [KEY1 KEY2 ...]"
            echo ""
            echo "Options:"
            echo "  --dry-run        Show what would be pushed without pushing"
            echo "  --file FILE      Use a custom env file (default: .env)"
            echo "  -h, --help       Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                          # Push all secrets from .env"
            echo "  $0 --dry-run                # Preview what would be pushed"
            echo "  $0 OPENAI_API_KEY           # Push only OPENAI_API_KEY"
            echo "  $0 --file prod.env          # Use prod.env instead of .env"
            exit 0
            ;;
        *)
            SPECIFIC_KEYS+=("$1")
            shift
            ;;
    esac
done

# Check if gh CLI is installed
if ! command -v gh &> /dev/null; then
    echo -e "${RED}Error: GitHub CLI (gh) is not installed${NC}"
    echo "Install it from: https://cli.github.com/"
    exit 1
fi

# Check if gh is authenticated
if ! gh auth status &> /dev/null; then
    echo -e "${RED}Error: GitHub CLI is not authenticated${NC}"
    echo "Run: gh auth login"
    exit 1
fi

# Get repository info
REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null)
if [ -z "$REPO" ]; then
    echo -e "${RED}Error: Not in a GitHub repository${NC}"
    exit 1
fi

echo -e "${BLUE}Repository: ${REPO}${NC}"
echo ""

# Check if env file exists
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}Error: ${ENV_FILE} not found${NC}"
    exit 1
fi

# Keys to skip (not secrets, or handled differently)
SKIP_KEYS=(
    "FLASK_ENV"
    "FLASK_DEBUG"
    "DEBUG"
    "PORT"
    "HOST"
    "LOG_LEVEL"
)

# Function to check if key should be skipped
should_skip() {
    local key=$1
    for skip in "${SKIP_KEYS[@]}"; do
        if [ "$key" == "$skip" ]; then
            return 0
        fi
    done
    return 1
}

# Function to check if key is in specific keys list
is_specific_key() {
    local key=$1
    if [ ${#SPECIFIC_KEYS[@]} -eq 0 ]; then
        return 0  # No specific keys means all keys
    fi
    for specific in "${SPECIFIC_KEYS[@]}"; do
        if [ "$key" == "$specific" ]; then
            return 0
        fi
    done
    return 1
}

# Count secrets
TOTAL=0
PUSHED=0
SKIPPED=0
FAILED=0

echo -e "${YELLOW}Reading secrets from ${ENV_FILE}...${NC}"
echo ""

# Read and process .env file
while IFS= read -r line || [ -n "$line" ]; do
    # Skip empty lines and comments
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    # Parse KEY=VALUE
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
        KEY="${BASH_REMATCH[1]}"
        VALUE="${BASH_REMATCH[2]}"

        # Remove surrounding quotes if present
        VALUE="${VALUE#\"}"
        VALUE="${VALUE%\"}"
        VALUE="${VALUE#\'}"
        VALUE="${VALUE%\'}"

        ((TOTAL++))

        # Check if we should skip this key
        if should_skip "$KEY"; then
            echo -e "  ${YELLOW}SKIP${NC} ${KEY} (non-secret)"
            ((SKIPPED++))
            continue
        fi

        # Check if this is in the specific keys list
        if ! is_specific_key "$KEY"; then
            continue
        fi

        # Check if value is empty
        if [ -z "$VALUE" ]; then
            echo -e "  ${YELLOW}SKIP${NC} ${KEY} (empty value)"
            ((SKIPPED++))
            continue
        fi

        if [ "$DRY_RUN" = true ]; then
            # Mask the value for display
            MASKED="${VALUE:0:4}...${VALUE: -4}"
            echo -e "  ${BLUE}WOULD PUSH${NC} ${KEY} = ${MASKED}"
            ((PUSHED++))
        else
            # Push the secret
            if echo -n "$VALUE" | gh secret set "$KEY" --repo "$REPO" 2>/dev/null; then
                echo -e "  ${GREEN}PUSHED${NC} ${KEY}"
                ((PUSHED++))
            else
                echo -e "  ${RED}FAILED${NC} ${KEY}"
                ((FAILED++))
            fi
        fi
    fi
done < "$ENV_FILE"

echo ""
echo -e "${BLUE}========================================${NC}"
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}DRY RUN COMPLETE${NC}"
    echo -e "Would push: ${PUSHED} secrets"
else
    echo -e "${GREEN}COMPLETE${NC}"
    echo -e "Pushed: ${GREEN}${PUSHED}${NC}"
fi
echo -e "Skipped: ${YELLOW}${SKIPPED}${NC}"
if [ $FAILED -gt 0 ]; then
    echo -e "Failed: ${RED}${FAILED}${NC}"
fi
echo -e "${BLUE}========================================${NC}"

# List current secrets
echo ""
echo -e "${BLUE}Current repository secrets:${NC}"
gh secret list --repo "$REPO" 2>/dev/null || echo "  (unable to list secrets)"
