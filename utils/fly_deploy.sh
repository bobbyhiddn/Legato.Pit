#!/bin/bash

# Legato.Pit Deployment Script for Fly.io
# Loads secrets from .env and deploys to Fly.io

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Function to print status messages
print_status() {
    echo -e "${YELLOW}>>> $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
    exit 1
}

print_info() {
    echo -e "${CYAN}ℹ $1${NC}"
}

# Function to check health endpoint with retries
check_health() {
    local url="$1"
    local max_attempts=10
    local wait_time=10
    local attempt=1

    print_status "Checking deployment health..."

    while [ $attempt -le $max_attempts ]; do
        print_status "Attempt $attempt of $max_attempts"

        if curl -sf "${url}/health" > /dev/null 2>&1; then
            print_success "Application is healthy!"
            return 0
        else
            print_status "Application is still starting up, waiting ${wait_time} seconds..."
            sleep $wait_time
            attempt=$((attempt + 1))
        fi
    done

    print_error "Application failed to respond after $max_attempts attempts"
    return 1
}

# Check for .env file
if [ ! -f .env ]; then
    print_error ".env file not found! Run: python utils/flask_keygen.py"
fi

# Load .env file
print_status "Loading environment variables..."
set -a
source .env
set +a
print_success "Environment variables loaded"

# Validate required variables
REQUIRED_VARS=(
    "FLASK_SECRET_KEY"
    "GITHUB_CLIENT_ID"
    "GITHUB_CLIENT_SECRET"
    "GITHUB_ALLOWED_USERS"
    "SYSTEM_PAT"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        print_error "Missing required variable: $var"
    fi
done
print_success "All required variables present"

# Build secrets command
print_status "Setting secrets on Fly.io..."
SECRETS_CMD="flyctl secrets set \
    FLASK_SECRET_KEY=\"$FLASK_SECRET_KEY\" \
    FLASK_ENV=\"${FLASK_ENV:-production}\" \
    GITHUB_CLIENT_ID=\"$GITHUB_CLIENT_ID\" \
    GITHUB_CLIENT_SECRET=\"$GITHUB_CLIENT_SECRET\" \
    GITHUB_ALLOWED_USERS=\"$GITHUB_ALLOWED_USERS\" \
    SYSTEM_PAT=\"$SYSTEM_PAT\" \
    LEGATO_ORG=\"${LEGATO_ORG:-bobbyhiddn}\" \
    CONDUCT_REPO=\"${CONDUCT_REPO:-Legato.Conduct}\""

# Add optional RAG/Chat secrets if present
if [ -n "$OPENAI_API_KEY" ]; then
    SECRETS_CMD="$SECRETS_CMD OPENAI_API_KEY=\"$OPENAI_API_KEY\""
    print_info "Including OPENAI_API_KEY"
fi

if [ -n "$ANTHROPIC_API_KEY" ]; then
    SECRETS_CMD="$SECRETS_CMD ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\""
    print_info "Including ANTHROPIC_API_KEY"
fi

if [ -n "$CHAT_PROVIDER" ]; then
    SECRETS_CMD="$SECRETS_CMD CHAT_PROVIDER=\"$CHAT_PROVIDER\""
fi

if [ -n "$CHAT_MODEL" ]; then
    SECRETS_CMD="$SECRETS_CMD CHAT_MODEL=\"$CHAT_MODEL\""
fi

# Add Tigris secrets if present
if [ -n "$AWS_ACCESS_KEY_ID" ]; then
    SECRETS_CMD="$SECRETS_CMD AWS_ACCESS_KEY_ID=\"$AWS_ACCESS_KEY_ID\""
    SECRETS_CMD="$SECRETS_CMD AWS_SECRET_ACCESS_KEY=\"$AWS_SECRET_ACCESS_KEY\""
    SECRETS_CMD="$SECRETS_CMD AWS_ENDPOINT_URL_S3=\"${AWS_ENDPOINT_URL_S3:-https://fly.storage.tigris.dev}\""
    SECRETS_CMD="$SECRETS_CMD AWS_REGION=\"${AWS_REGION:-auto}\""
    SECRETS_CMD="$SECRETS_CMD BUCKET_NAME=\"$BUCKET_NAME\""
    print_info "Including Tigris S3 credentials"
fi

# Execute secrets command
eval $SECRETS_CMD

if [ $? -eq 0 ]; then
    print_success "Secrets set successfully!"
else
    print_error "Failed to set secrets"
fi

# Deploy to Fly.io
print_status "Deploying Legato.Pit to Fly.io..."
if fly deploy; then
    print_success "Application deployed!"

    # Get the app name from fly.toml
    APP_NAME=$(grep "^app = " fly.toml | cut -d'"' -f2)
    APP_URL="${APP_NAME}.fly.dev"

    print_info "Application URL: https://$APP_URL"

    # Check health with retries
    check_health "https://$APP_URL"
else
    print_error "Deployment failed"
fi

# Show recent logs
print_status "Recent logs:"
fly logs --no-tail
