import os
import sys
import shutil
import subprocess
import re
from pathlib import Path
import venv
import stat
import requests
from openai import OpenAI
import json
import logging
from typing import Optional

# Configuration
TEMPLATE_REPO = "https://github.com/bobbyhiddn/Flask2Fly.git"

class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    NC = '\033[0m'

def print_status(message: str) -> None:
    print(f"{Colors.YELLOW}>>> {message}{Colors.NC}")

def print_success(message: str) -> None:
    print(f"{Colors.GREEN}✓ {message}{Colors.NC}")

def print_error(message: str) -> None:
    print(f"{Colors.RED}✗ {message}{Colors.NC}", file=sys.stderr)
    sys.exit(1)

def validate_inputs(project_name: str) -> None:
    if not project_name:
        print("Usage: setup.py <new_project_name> [target_directory]")
        print("Example: setup.py MyNewProject ./projects")
        sys.exit(1)

    if not re.match(r'^[a-zA-Z][a-zA-Z0-9_-]*$', project_name):
        print_error("Project name must start with a letter and contain only letters, numbers, hyphens, and underscores")

def setup_project_directory(project_dir: Path, project_name: str) -> Path:
    project_path = project_dir / project_name
    if project_path.exists():
        print_error(f"Directory {project_path} already exists")
    
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_path

def rename_project_files(project_path: Path, project_name: str) -> None:
    """Rename the app_name directory and update references in files."""
    src_dir = project_path / "src"
    app_dir = src_dir / "app_name"

    if not src_dir.exists():
        print_error(f"Source directory not found at {src_dir}")

    if not app_dir.exists():
        print_error(f"App directory not found at {app_dir}")

    # Rename the app directory
    new_app_dir = src_dir / project_name
    if new_app_dir.exists():
        shutil.rmtree(new_app_dir)
    app_dir.rename(new_app_dir)

def update_python_files(project_path: Path, project_name: str) -> None:
    """Update Python imports and references."""
    # Update main.py specifically
    main_py = project_path / "src" / "main.py"
    if main_py.exists():
        try:
            content = main_py.read_text(encoding='utf-8')
            content = content.replace("from app_name.core", f"from {project_name}.core")
            main_py.write_text(content, encoding='utf-8')
        except UnicodeDecodeError:
            print_status(f"Warning: Could not update {main_py} due to encoding issues")

    # Update all Python files recursively
    for py_file in project_path.rglob("*.py"):
        try:
            content = py_file.read_text(encoding='utf-8')
            content = re.sub(r'from app_name\.', f'from {project_name}.', content)
            content = re.sub(r'import app_name\.', f'import {project_name}.', content)
            content = content.replace("app_name", project_name)
            py_file.write_text(content, encoding='utf-8')
        except UnicodeDecodeError:
            print_status(f"Warning: Could not update {py_file} due to encoding issues")

def generate_theme(project_name: str, project_description: str) -> tuple[dict, bytes]:
    """Generate a theme and logo using OpenAI APIs."""
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        print_error("OPENAI_API_KEY environment variable is required for theme generation")

    client = OpenAI(api_key=api_key)

    try:
        # Generate color scheme
        color_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": f'Create a modern color scheme for a web application named "{project_name}". Description: {project_description}. Return ONLY a JSON object with these colors in hex format: primary-color, secondary-color, background-color, text-color, text-primary'
            }],
            temperature=0.7
        )
        
        raw_content = color_response.choices[0].message.content
        logging.debug(f"Raw API response: {raw_content}")
        
        try:
            clean_content = raw_content.strip()
            if clean_content.startswith("```"):
                clean_content = clean_content.split("\n", 1)[1]
                clean_content = clean_content.rsplit("\n", 1)[0]
            
            colors = json.loads(clean_content)
        except json.JSONDecodeError as e:
            logging.error(f"JSON parsing error: {e}")
            logging.error(f"Failed to parse response: {raw_content}")
            logging.error(f"Cleaned content: {clean_content}")
            raise Exception("Failed to parse theme colors from API response")
        
        # Generate logo
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=f'Create a modern, minimalist logo for "{project_name}". Description: {project_description}. Style: 2D, clean, geometric, professional. NO text, just a simple icon that represents the project. Use {colors["primary-color"]} as the main color. Make it suitable as a website logo. 2D, stylized image.',
            size="1024x1024",
            n=1,
            response_format="url"
        )
        
        logo_url = image_response.data[0].url
        logo_response = requests.get(logo_url)
        if logo_response.status_code != 200:
            raise Exception("Failed to download generated logo")

        return colors, logo_response.content
    except Exception as e:
        logging.error(f"Failed to generate theme: {str(e)}")
        raise

def update_theme_files(project_path: Path, colors: dict, logo_content: bytes) -> None:
    """Update theme files with generated content."""
    # Save the logo
    static_img_path = project_path / "src" / project_path.name / "static" / "img"
    static_img_path.mkdir(parents=True, exist_ok=True)
    (static_img_path / "logo.png").write_bytes(logo_content)

    # Update CSS with new colors
    css_path = project_path / "src" / project_path.name / "static" / "css" / "styles.css"
    if css_path.exists():
        css_content = css_path.read_text(encoding='utf-8')
        for var_name, color in colors.items():
            css_content = re.sub(
                f'--{var_name}: #[0-9a-fA-F]{{6}};',
                f'--{var_name}: {color};',
                css_content
            )
        css_path.write_text(css_content, encoding='utf-8')

def generate_features(project_name: str, project_description: str, client: OpenAI) -> list:
    """Generate customized features using GPT based on project description"""
    try:
        # Craft a prompt that will result in structured feature data
        prompt = f"""Generate 4 key features for a web application named "{project_name}". Description: {project_description}
Return ONLY a JSON object with a 'features' key containing an array of exactly 4 features, where each feature has an 'icon' (single emoji), 'title' (2-3 words), and 'description' (10-15 words).
Features should be specific to the project's purpose."""

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": prompt
            }],
            response_format={ "type": "json_object" },
            temperature=0.7,
        )
        
        # Get the raw response
        raw_content = response.choices[0].message.content
        logging.debug(f"Raw GPT features response: {raw_content}")
        
        try:
            features = json.loads(raw_content)
            # If the response is wrapped in a JSON object, extract the features array
            if isinstance(features, dict) and "features" in features:
                features = features["features"]
            if not isinstance(features, list) or len(features) != 4:
                raise ValueError("Invalid features format")
            
            # Validate each feature has required fields
            for feature in features:
                if not all(key in feature for key in ["icon", "title", "description"]):
                    raise ValueError("Features missing required fields")
            
            return features
            
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"Failed to parse GPT response: {e}")
            logging.error(f"Raw response was: {raw_content}")
            return get_fallback_features(project_name)
            
    except Exception as e:
        logging.error(f"Error generating features: {e}")
        return get_fallback_features(project_name)

def get_fallback_features(project_name: str) -> list:
    """Provide fallback features if GPT generation fails"""
    return [
        {
            "icon": "🚀",
            "title": "Quick Setup",
            "description": f"Get your {project_name} application running in minutes"
        },
        {
            "icon": "⚙️",
            "title": "Easy Configuration",
            "description": "Simple configuration management with environment variables"
        },
        {
            "icon": "🔄",
            "title": "Auto Deployment",
            "description": "Integrated CI/CD pipeline with cloud deployment"
        },
        {
            "icon": "📦",
            "title": "Modular Design",
            "description": "Extensible architecture with support for feature modules"
        }
    ]

def update_configuration_files(project_path: Path, project_name: str, project_description: str, client: OpenAI) -> None:
    """Update various configuration files with the project name."""
    features = generate_features(project_name, project_description, client)
    
    # Core configuration files to update
    files_to_update = {
        "fly.toml": (lambda c: re.sub(r'^app = .*$', f"app = '{project_name}'", c, flags=re.MULTILINE)),
        "docker-compose.yml": (lambda c: re.sub(r'^  [a-zA-Z0-9_-]*:', f"  {project_name}:", c, flags=re.MULTILINE)),
        "Dockerfile": (lambda c: c.replace("src/app_name/static", f"src/{project_name}/static")),
        "README.md": (lambda c: c.replace("Flask2Fly", project_name))
    }

    # Update base configuration files
    for filename, update_func in files_to_update.items():
        file_path = project_path / filename
        if file_path.exists():
            try:
                content = file_path.read_text(encoding='utf-8')
                updated_content = update_func(content)
                updated_content = updated_content.replace("Flask2Fly", project_name)
                updated_content = updated_content.replace("flask2fly", project_name.lower())
                updated_content = updated_content.replace("FLASK2FLY", project_name.upper())
                file_path.write_text(updated_content, encoding='utf-8')
            except UnicodeDecodeError:
                print_status(f"Warning: Could not update {filename} due to encoding issues")

    # Update core.py with features
    core_file = project_path / "src" / project_name / "core.py"
    if core_file.exists():
        try:
            content = core_file.read_text(encoding='utf-8')
            
            # Create new context with features
            features_list = []
            for feature in features:
                features_list.append({
                    "icon": feature["icon"],
                    "title": feature["title"],
                    "description": feature["description"]
                })
            
            # Format the features JSON with proper indentation
            features_lines = []
            features_lines.append("[")
            for i, feature in enumerate(features_list):
                feature_json = json.dumps(feature, indent=4, ensure_ascii=False)
                # Indent each line of the feature
                feature_lines = feature_json.splitlines()
                for j, line in enumerate(feature_lines):
                    if j == 0:  # First line
                        features_lines.append("                    " + line)
                    else:
                        features_lines.append("                        " + line.lstrip())
                if i < len(features_list) - 1:
                    features_lines[-1] += ","
            features_lines.append("                ]")
            features_json = "\n".join(features_lines)
            
            # Create a properly indented return statement
            context_str = """            return {
                'now': datetime.datetime.now(),
                'site_name': '%s',
                'app_name': '%s',
                'app_description': '%s',
                'app_purpose': '%s',
                'app_repo_url': f'https://github.com/yourusername/%s',
                'docs_url': f'https://github.com/yourusername/%s/docs',
                'key_features': %s
            }""" % (
                project_name,
                project_name,
                project_description,
                project_description,
                project_name,
                project_name,
                features_json
            )
            
            # Find the inject_globals function and replace its entire content
            inject_pattern = r'def inject_globals\(\):[\s\S]*?return\s*{[\s\S]*?key_features[\s\S]*?\][\s\S]*?}'
            
            replacement = f"""def inject_globals():
            \"\"\"Make common variables available to all templates\"\"\"
{context_str}"""
            
            # Use re.sub with re.MULTILINE and re.DOTALL flags
            content = re.sub(inject_pattern, replacement, content, flags=re.MULTILINE | re.DOTALL)
            
            # Update other references
            content = content.replace("Flask2Fly", project_name)
            content = content.replace("flask2fly", project_name.lower())
            
            core_file.write_text(content, encoding='utf-8')
        except UnicodeDecodeError:
            print_status(f"Warning: Could not update {core_file} due to encoding issues")

    # Update templates and HTML files
    template_dir = project_path / "src" / project_name / "templates"
    if template_dir.exists():
        for template in template_dir.glob("**/*.html"):
            try:
                content = template.read_text(encoding='utf-8')
                content = content.replace("Flask2Fly", project_name)
                content = content.replace("flask2fly", project_name.lower())
                content = content.replace("FLASK2FLY", project_name.upper())
                content = content.replace("flask2fly logo", f"{project_name.lower()} logo")
                template.write_text(content, encoding='utf-8')
            except UnicodeDecodeError:
                print_status(f"Warning: Could not update {template} due to encoding issues")

def initialize_modules(project_path: Path) -> None:
    """Initialize local module directories."""
    modules_dir = project_path / "src" / "modules"
    pages_dir = modules_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Initialize pages as a local Git repository
    subprocess.run(["git", "init"], cwd=pages_dir, check=True)
    
    # Create basic structure
    for subdir in ["docs", "articles", "templates"]:
        (pages_dir / subdir).mkdir(exist_ok=True)

    # Create .gitignore
    gitignore_content = """__pycache__/
*.py[cod]
*$py.class
.env
.venv
env/
venv/
.idea/
.vscode/
"""
    (pages_dir / ".gitignore").write_text(gitignore_content, encoding='utf-8')

    # Create README.md
    readme_content = f"""# Pages Module

This module contains the pages and documentation for {project_path.name}.
It is initialized as a local Git repository that can be synchronized with a remote repository if desired.

## Directory Structure

```
pages/
├── docs/       # Project documentation
├── articles/   # Content articles
└── templates/  # Page templates
```

## Remote Repository Setup (Optional)

To synchronize with a remote repository:

1. Create a new repository on your preferred Git hosting service
2. Add the remote to this repository:
   ```bash
   cd src/modules/pages
   git remote add origin <your-repository-url>
   ```
3. Push your changes:
   ```bash
   git add .
   git commit -m "Initial commit"
   git push -u origin main
   ```
"""
    (pages_dir / "README.md").write_text(readme_content, encoding='utf-8')

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=pages_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial pages module setup"],
        cwd=pages_dir,
        env={**os.environ, 'GIT_AUTHOR_NAME': 'Setup Script', 'GIT_AUTHOR_EMAIL': 'setup@local'},
        check=True
    )

def setup_virtual_environment(project_path: Path) -> None:
    """Set up and configure the virtual environment."""
    venv_path = project_path / "venv"
    venv.create(venv_path, with_pip=True)
    
    pip_path = venv_path / "bin" / "pip" if os.name != 'nt' else venv_path / "Scripts" / "pip"
    requirements_path = project_path / "src" / "requirements.txt"
    
    subprocess.run(
        [str(pip_path), "install", "-r", str(requirements_path)],
        check=True
    )

def setup_git_hooks(project_path: Path) -> None:
    """Set up Git hooks."""
    hooks_dir = project_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    
    shutil.copy2(
        project_path / "utils" / "pre-push",
        hooks_dir / "pre-push"
    )
    (hooks_dir / "pre-push").chmod(0o755)

def initialize_project(project_path: Path) -> None:
    """Initialize the project with Git and virtual environment."""
    subprocess.run(["git", "init"], cwd=project_path, check=True)
    
    env_file = project_path / ".env"
    env_file.touch()

    subprocess.run(
        ["python", "utils/flask_keygen.py"],
        cwd=project_path,
        check=True
    )

    setup_virtual_environment(project_path)
    setup_git_hooks(project_path)

def safe_remove_git_dir(path: Path) -> None:
    """Safely remove a git directory on Windows."""
    if not path.exists():
        return

    def on_rm_error(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
        except Exception:
            pass

    try:
        # Clean the git repo first
        try:
            subprocess.run(["git", "clean", "-fd"], cwd=path, check=False, capture_output=True)
            subprocess.run(["git", "gc"], cwd=path, check=False, capture_output=True)
        except Exception:
            pass

        # Try standard removal first
        shutil.rmtree(path, onerror=on_rm_error)
        
        # If directory still exists, try more aggressive cleanup
        if path.exists():
            for p in path.rglob('*'):
                try:
                    p.chmod(stat.S_IWRITE)
                except Exception:
                    pass
            shutil.rmtree(path, ignore_errors=True)
        
        # Last resort: use system commands
        if path.exists():
            try:
                if os.name == 'nt':
                    subprocess.run(['rmdir', '/S', '/Q', str(path)], check=False, capture_output=True)
                else:
                    subprocess.run(['rm', '-rf', str(path)], check=False, capture_output=True)
            except Exception:
                pass

    except Exception as e:
        print_status(f"Warning: Could not fully clean up {path}. You may want to remove it manually.")

def main() -> None:
    """Main entry point for the setup script."""
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) < 2:
        print_error("Project name is required")
    
    project_name = sys.argv[1]
    project_dir = Path(sys.argv[2] if len(sys.argv) > 2 else ".")
    
    print_status("Please provide a brief description of your project for theme generation:")
    project_description = input("> ")
    
    print_status(f"Starting project setup for {project_name}")
    
    validate_inputs(project_name)
    project_path = setup_project_directory(project_dir, project_name)
    
    print_status("Cloning Flask2Fly template...")
    
    # Create temporary directory for cloning
    temp_clone_path = Path("..") / "temp_clone"
    
    # Clean up existing temp_clone directory if it exists
    if temp_clone_path.exists():
        try:
            shutil.rmtree(temp_clone_path)
        except Exception as e:
            safe_remove_git_dir(temp_clone_path)
    
    subprocess.run(["git", "clone", TEMPLATE_REPO, str(temp_clone_path)], check=True)
    
    # Move contents to actual project directory
    project_path.mkdir(parents=True, exist_ok=True)
    
    # Copy everything except .git
    for item in temp_clone_path.iterdir():
        if item.name != '.git':
            if item.is_dir():
                shutil.copytree(str(item), str(project_path / item.name), dirs_exist_ok=True)
            else:
                shutil.copy2(str(item), str(project_path))
    
    # Clean up temporary directory
    safe_remove_git_dir(temp_clone_path)
    
    # Change to project directory for remaining operations
    os.chdir(project_path)
    
    # Perform all updates
    rename_project_files(project_path, project_name)
    update_python_files(project_path, project_name)
    
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        print_error("OPENAI_API_KEY environment variable is required for theme generation")
    
    client = OpenAI(api_key=api_key)
    
    update_configuration_files(project_path, project_name, project_description, client)
    initialize_modules(project_path)
    initialize_project(project_path)
    
    # Generate and apply theme
    print_status("Generating custom theme and logo...")
    colors, logo_content = generate_theme(project_name, project_description)
    update_theme_files(project_path, colors, logo_content)
    print_success("Theme and logo generated successfully!")
    
    print_success(f"Project '{project_name}' has been successfully created!")
    print_status("Next steps:")
    print_status("1. Review and update the .env file with your configuration")
    print_status("2. Review the generated fly.toml configuration")
    print_status("3. Run 'fly launch' to initialize your Fly.io application")
    print_status("4. Update the README.md with your project-specific information")

if __name__ == "__main__":
    main()