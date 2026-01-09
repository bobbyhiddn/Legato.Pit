#!/bin/bash

# Enable strict error handling
set -euo pipefail

# --- Configuration ---
readonly TEMPLATE_REPO="https://github.com/bobbyhiddn/Flask2Fly.git"
readonly PAGES_REPO="https://github.com/bobbyhiddn/Magi.Library.git"
NEW_PROJECT_NAME="${1:-}"
NEW_PROJECT_DIR="${2:-.}"

# --- Color Definitions ---
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly NC='\033[0m'

# --- Helper Functions ---
print_status() {
    echo -e "${YELLOW}>>> $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}" >&2
    exit 1
}

validate_inputs() {
    if [[ -z "$NEW_PROJECT_NAME" ]]; then
        echo "Usage: $0 <new_project_name> [target_directory]"
        echo "Example: $0 MyNewProject ./projects"
        exit 1
    fi

    if [[ ! "$NEW_PROJECT_NAME" =~ ^[a-zA-Z][a-zA-Z0-9_-]*$ ]]; then
        print_error "Project name must start with a letter and contain only letters, numbers, hyphens, and underscores"
    fi
}

setup_project_directory() {
    if [[ -d "$NEW_PROJECT_DIR/$NEW_PROJECT_NAME" ]]; then
        print_error "Directory $NEW_PROJECT_DIR/$NEW_PROJECT_NAME already exists"
    fi
    
    mkdir -p "$NEW_PROJECT_DIR"
    cd "$NEW_PROJECT_DIR"
}

rename_project_files() {
    local src_dir="src"
    local app_dir="app_name"

    if [[ ! -d "$src_dir" ]]; then
        print_error "Source directory not found"
    fi

    if [[ -d "$src_dir/$app_dir" ]]; then
        mv "$src_dir/$app_dir" "$src_dir/$NEW_PROJECT_NAME"
        find "$src_dir" -type f -name "*.py" -exec sed -i "s/$app_dir/$NEW_PROJECT_NAME/g" {} +
    fi
}

update_configuration_files() {
    local files_to_update=(
        "fly.toml"
        "docker-compose.yml"
        ".github/workflows/fly-deploy.yml"
        ".github/workflows/dev-deploy.yml"
    )

    for file in "${files_to_update[@]}"; do
        if [[ -f "$file" ]]; then
            print_status "Updating $file..."
            
            # Update app name in configuration
            if [[ "$file" == "fly.toml" ]]; then
                sed -i "s/^app = .*/app = '$NEW_PROJECT_NAME'/g" "$file"
            fi

            # Update service name in docker-compose
            if [[ "$file" == "docker-compose.yml" ]]; then
                sed -i "s/^  [a-zA-Z0-9_-]*:/  $NEW_PROJECT_NAME:/g" "$file"
            fi

            # Update GitHub workflow configurations
            if [[ "$file" =~ workflows/.+\.yml$ ]]; then
                sed -i "s/flyctl deploy --remote-only$/flyctl deploy --remote-only --app $NEW_PROJECT_NAME/g" "$file"
                sed -i "s/flyctl secrets set\\b/flyctl secrets set --app $NEW_PROJECT_NAME /g" "$file"
                
                # Add dev prefix for non-main branch deployments
                if ! grep -q "branches: \[main\]" "$file"; then
                    sed -i "s/--app $NEW_PROJECT_NAME/--app dev-$NEW_PROJECT_NAME/g" "$file"
                fi
            fi
        fi
    done

    # Update README.md with project name
    if [[ -f "README.md" ]]; then
        print_status "Updating README.md..."
        sed -i "s/Flask2Fly/$NEW_PROJECT_NAME/g" "README.md"
    fi

    # Update Dockerfile
    if [[ -f "Dockerfile" ]]; then
        print_status "Updating Dockerfile..."
        sed -i "s|src/app_name/static|src/$NEW_PROJECT_NAME/static|g" "Dockerfile"
    fi

    # Update templates with project name
    template_dir="src/$NEW_PROJECT_NAME/templates"
    if [[ -d "$template_dir" ]]; then
        print_status "Updating templates..."
        # Update base.html
        if [[ -f "$template_dir/base.html" ]]; then
            sed -i "s/Flask2Fly/$NEW_PROJECT_NAME/g" "$template_dir/base.html"
            sed -i "s/flask2fly logo/$NEW_PROJECT_NAME logo/g" "$template_dir/base.html"
        fi
        
        # Update index.html
        if [[ -f "$template_dir/index.html" ]]; then
            sed -i "s/Welcome to Flask2Fly/Welcome to $NEW_PROJECT_NAME/g" "$template_dir/index.html"
            sed -i "s|https://github.com/bobbyhiddn/Flask2Fly|https://github.com/yourusername/$NEW_PROJECT_NAME|g" "$template_dir/index.html"
        fi
        
        # Update other templates
        find "$template_dir" -type f -name "*.html" -exec sed -i "s/Flask2Fly/$NEW_PROJECT_NAME/g" {} +
    fi

    # Update core.py
    core_file="src/$NEW_PROJECT_NAME/core.py"
    if [[ -f "$core_file" ]]; then
        print_status "Updating core.py..."
        # Update site name in context processor
        sed -i "s/'site_name': 'Flask2Fly'/'site_name': '$NEW_PROJECT_NAME'/g" "$core_file"
        # Update health check and template titles
        sed -i "s/title=\"Welcome to Flask2Fly\"/title=\"Welcome to $NEW_PROJECT_NAME\"/g" "$core_file"
        sed -i "s/- Flask2Fly/- $NEW_PROJECT_NAME/g" "$core_file"
    fi

    # Update fly_deploy.sh
    deploy_script="utils/fly_deploy.sh"
    if [[ -f "$deploy_script" ]]; then
        print_status "Updating fly_deploy.sh..."
        # Update health check verification
        sed -i "s/grep -q \"Flask2Fly\"/grep -q \"$NEW_PROJECT_NAME\"/g" "$deploy_script"
        # Update app URL construction
        sed -i "s/APP_URL=\"flask2fly.fly.dev\"/APP_URL=\"$NEW_PROJECT_NAME.fly.dev\"/g" "$deploy_script"
        # Update success messages
        sed -i "s/Flask2Fly/$NEW_PROJECT_NAME/g" "$deploy_script"
    fi
}

setup_git_submodules() {
    print_status "Setting up Git submodules..."
    
    # Create modules directory if it doesn't exist
    mkdir -p "src/modules"
    
    # Add pages submodule
    git submodule add "$PAGES_REPO" "src/modules/pages"
    
    # Update .gitmodules with proper configuration
    if [[ -f ".gitmodules" ]]; then
        print_status "Configuring .gitmodules..."
        sed -i "s|url = .*|url = $PAGES_REPO|g" ".gitmodules"
    fi
    
    # Initialize and update submodules
    git submodule init
    git submodule update
}

setup_virtual_environment() {
    print_status "Setting up Python virtual environment..."
    python -m venv venv

    # Source the virtual environment based on platform
    if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
        source venv/Scripts/activate
    else
        source venv/bin/activate
    fi

    print_status "Installing project dependencies..."
    pip install -r src/requirements.txt
}

setup_git_hooks() {
    print_status "Setting up Git hooks..."
    if [ ! -d ".git/hooks" ]; then
        mkdir -p .git/hooks
    fi
    cp utils/pre-push .git/hooks/
    chmod +x .git/hooks/pre-push
}

initialize_project() {
    print_status "Initializing git repository..."
    rm -rf .git
    git init
    
    setup_git_submodules

    print_status "Creating environment file..."
    if [[ ! -f ".env" ]]; then
        touch .env
    fi

    print_status "Generating Flask secret key..."
    python utils/flask_keygen.py

    setup_virtual_environment
    setup_git_hooks
}

# --- Main Script ---
main() {
    print_status "Starting project setup for $NEW_PROJECT_NAME"
    
    validate_inputs
    setup_project_directory
    
    print_status "Cloning Flask2Fly template..."
    git clone "$TEMPLATE_REPO" "$NEW_PROJECT_NAME"
    cd "$NEW_PROJECT_NAME"
    
    rename_project_files
    update_configuration_files
    initialize_project
    
    print_success "Project '$NEW_PROJECT_NAME' has been successfully created!"
    print_status "Next steps:"
    print_status "1. Review and update the .env file with your configuration"
    print_status "2. Review the generated fly.toml configuration"
    print_status "3. Run 'fly launch' to initialize your Fly.io application"
    print_status "4. Update the README.md with your project-specific information"
}

main "$@"