# Directory: 

/.dockerignore: 
```
# flyctl launch added from .gitignore
# flyctl launch added from .gitignore
**\**\.env
**\**\.gitignore
**\fly.toml

```
## /.github/

/.github/FUNDING.yml: 
```
# These are supported funding model platforms

github: # Replace with up to 4 GitHub Sponsors-enabled usernames e.g., [user1, user2]
patreon: # Replace with a single Patreon username
open_collective: # Replace with a single Open Collective username
ko_fi: bobbyhiddn
tidelift: # Replace with a single Tidelift platform-name/package-name e.g., npm/babel
community_bridge: # Replace with a single Community Bridge project-name e.g., cloud-foundry
liberapay: # Replace with a single Liberapay username
issuehunt: # Replace with a single IssueHunt username
lfx_crowdfunding: # Replace with a single LFX Crowdfunding project-name e.g., cloud-foundry
polar: # Replace with a single Polar username
buy_me_a_coffee: # Replace with a single Buy Me a Coffee username
thanks_dev: # Replace with a single thanks.dev username
custom: # Replace with up to 4 custom sponsorship URLs e.g., ['link1', 'link2']

```
## /.github/workflows/

/.github/workflows/fly-deploy.yml: 
```
name: Fly Deploy
on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    name: Deploy app
    runs-on: ubuntu-latest
    env:
      FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
    concurrency: deploy-group
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: false
          
      - name: Configure Git
        run: |
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git config --global user.name "github-actions[bot]"
          
      - name: Update Submodules
        run: |
          # Define submodule paths
          SUBMODULES=("src/modules/library")
          
          # Get root directory
          ROOT_DIR=$(pwd)
          
          # Initialize submodules
          git submodule init
          git submodule update
          
          # Loop through each submodule
          for submodule in "${SUBMODULES[@]}"; do
            echo "Processing submodule: $submodule"
            
            if [ ! -d "$submodule" ]; then
              echo "Creating directory: $submodule"
              mkdir -p "$submodule"
            fi
            
            # Enter submodule directory
            cd "$ROOT_DIR/$submodule" || exit 1
            
            echo "Current directory: $(pwd)"
            
            # Configure git locally for the submodule
            git config user.email "github-actions[bot]@users.noreply.github.com"
            git config user.name "github-actions[bot]"
            
            # Fetch and checkout latest main
            git fetch origin main
            git checkout main
            git pull origin main
            
            # Return to root
            cd "$ROOT_DIR" || exit 1
            
            # Update the parent repo's reference
            git add "$submodule"
            git commit -m "Update submodule $submodule to latest main" || echo "No changes to commit for $submodule"
          done
            
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl deploy --remote-only
```
/.github/workflows/miner.yml: 
```

```
/.gitmodules: 
```
[submodule "src/modules/library"]
	path = src/modules/library
	url = https://github.com/bobbyhiddn/Veinity.Library.git

```
/docker-compose.yml: 
```
version: '3.8'

services:
  veinity:
    build: .
    ports:
      - "8888:8888"
    environment:
      - FLASK_SECRET_KEY=${FLASK_SECRET_KEY}
      - NEWSAPI_KEY=${NEWSAPI_KEY}
      - ANALYTICS_ID=${ANALYTICS_ID}
    volumes:
      - ./src:/app/src
      - ./src/modules/library/articles:/app/src/modules/library/articles
    command: python src/main.py
```
/Dockerfile: 
```
FROM python:3.11-slim

# Install git and other dependencies
RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Install gunicorn
RUN pip install gunicorn

# Expose the port
EXPOSE 8888

# Make sure we're in the src directory
WORKDIR /app/src

CMD ["gunicorn", "-w", "4
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8888", "app:app"]]
```
/fly.toml: 
```
# fly.toml app configuration file generated for veinity on 2024-12-08T13:30:40-07:00
#
# See https://fly.io/docs/reference/configuration/ for information about how to use this file.
#

app = 'veinity'
primary_region = 'den'

[build]

[http_service]
  internal_port = 8888
  force_https = true
  auto_stop_machines = 'stop'
  auto_start_machines = true
  min_machines_running = 0
  processes = ['app']

[[vm]]
  memory = '1gb'
  cpu_kind = 'shared'
  cpus = 1

```
## /src/

/src/app.py: 
```
from flask import Flask
from dotenv import load_dotenv
import os
from hub.core import VeinityHub
# TODO: Uncomment when aggregator is built
# from modules.aggregator.core import VeinityAggregator

# Load environment variables
load_dotenv()

def create_app():
    """Initialize and configure the Veinity application"""
    # Initialize components
    hub = VeinityHub()
    # TODO: Uncomment when aggregator is built
    # aggregator = VeinityAggregator()
    
    # Set up routes
    hub.setup_routes()
    # TODO: Uncomment when aggregator is built
    # aggregator.setup_routes()
    
    return hub.app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv('PORT', 8888))
    app.run(host='0.0.0.0', port=port)

```
## /src/hub/

/src/hub/core.py: 
```
from flask import Flask, jsonify, render_template, abort, request, url_for
from pathlib import Path
import os
import datetime
import logging
import markdown
import yaml
from typing import Dict, List, Optional
from functools import lru_cache
import time

class VeinityHub:
    def __init__(self):
        # Initialize Flask app with correct template directory
        self.app = Flask(__name__, template_folder='templates')
        self.app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
        
        # Setup logging
        logging.basicConfig(level=logging.DEBUG)
        self.logger = logging.getLogger(__name__)
        
        # Configure paths
        self.base_path = Path(os.path.dirname(os.path.dirname(__file__)))
        self.library = self.base_path / "modules" / "library" / "articles"
        self.cache = self.base_path / "modules" / "library" / "cache"
        
        # Load configurations
        self.ad_config = self._load_ad_config()
        self.site_config = self._load_site_config()
        
        # Initialize cache timestamp
        self._last_cache_update = time.time()
        
    def _load_ad_config(self) -> Dict:
        """Load advertising configuration"""
        config_path = self.base_path / "config" / "ads.yaml"
        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f)
            return {
                'enabled': False,
                'slots': {
                    'header': '',
                    'sidebar': '',
                    'in_content': '',
                    'footer': ''
                }
            }
        except Exception as e:
            self.logger.error(f"Error loading ad config: {str(e)}")
            return {}

    def _load_site_config(self) -> Dict:
        """Load site configuration"""
        config_path = self.base_path / "config" / "site.yaml"
        try:
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f)
            return {
                'site_name': 'Veinity',
                'description': 'Your source for tech news and insights',
                'contact_email': '',
                'social_links': {}
            }
        except Exception as e:
            self.logger.error(f"Error loading site config: {str(e)}")
            return {}

    def _should_update_cache(self, max_age: int = 300) -> bool:
        """Check if cache should be updated (default 5 minutes)"""
        return time.time() - self._last_cache_update > max_age

    def _parse_article_metadata(self, content: str) -> tuple[Dict, str]:
        """Extract YAML frontmatter and markdown content"""
        if content.startswith('---'):
            try:
                _, frontmatter, markdown_content = content.split('---', 2)
                metadata = yaml.safe_load(frontmatter)
                return metadata, markdown_content.strip()
            except Exception as e:
                self.logger.error(f"Error parsing frontmatter: {str(e)}")
        return {}, content

    @lru_cache(maxsize=32)
    def _get_article_preview(self, content: str, length: int = 160) -> str:
        """Generate article preview from content"""
        try:
            # Remove YAML frontmatter if present
            if content.startswith('---'):
                content = content.split('---', 2)[2]
            
            # Convert markdown to plain text
            text = content.replace('#', '').replace('*', '').strip()
            
            # Return truncated preview
            if len(text) > length:
                return text[:length].rsplit(' ', 1)[0] + '...'
            return text
        except Exception as e:
            self.logger.error(f"Error generating preview: {str(e)}")
            return ""

    def _get_recent_articles(self, category: Optional[str] = None, limit: int = 10) -> List[Dict]:
        """Get recent articles, optionally filtered by category"""
        articles = []
        try:
            search_path = self.library / category if category else self.library
            files = sorted(search_path.glob('*.md'), key=os.path.getmtime, reverse=True)
            
            for file_path in files[:limit]:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    metadata, _ = self._parse_article_metadata(content)
                    if metadata:
                        metadata['path'] = str(file_path.relative_to(self.library))
                        articles.append(metadata)
                        
            return articles
        except Exception as e:
            self.logger.error(f"Error getting recent articles: {str(e)}")
            return []

    def _get_categories(self) -> List[str]:
        """Get list of available categories"""
        try:
            return [d.name for d in self.library.iterdir() if d.is_dir()]
        except Exception as e:
            self.logger.error(f"Error getting categories: {str(e)}")
            return []

    def _get_related_articles(self, article_metadata: Dict, limit: int = 3) -> List[Dict]:
        """Get related articles based on category and tags"""
        try:
            related = []
            current_category = article_metadata.get('category')
            current_tags = set(article_metadata.get('tags', []))
            
            for article_file in self.library.rglob('*.md'):
                if len(related) >= limit:
                    break
                    
                with open(article_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    metadata, _ = self._parse_article_metadata(content)
                    
                    # Skip current article
                    if metadata.get('title') == article_metadata.get('title'):
                        continue
                    
                    # Check if related by category or tags
                    if (metadata.get('category') == current_category or 
                        set(metadata.get('tags', [])) & current_tags):
                        metadata['path'] = str(article_file.relative_to(self.library))
                        related.append(metadata)
            
            return related
        except Exception as e:
            self.logger.error(f"Error finding related articles: {str(e)}")
            return []

    def _format_date(self, date_str: str, format: str = '%B %d, %Y') -> str:
        """Format date string for display"""
        try:
            date_obj = datetime.datetime.fromisoformat(date_str)
            return date_obj.strftime(format)
        except Exception as e:
            self.logger.error(f"Error formatting date: {str(e)}")
            return date_str

    def setup_routes(self):
        @self.app.route('/')
        def index():
            """Render the homepage with recent articles"""
            recent_articles = self._get_recent_articles(limit=10)
            categories = self._get_categories()
            
            return render_template('index.html',
                title="Veinity - Latest Tech News",
                articles=recent_articles,
                categories=categories,
                ad_config=self.ad_config)

        @self.app.route('/category/<category>')
        def category_view(category: str):
            """Render category-specific article listing"""
            articles = self._get_recent_articles(category=category)
            if not articles:
                abort(404)
                
            return render_template('category.html',
                title=f"Veinity - {category.title()} News",
                category=category,
                articles=articles,
                ad_config=self.ad_config)

        @self.app.route('/article/<path:article_path>')
        def article_view(article_path: str):
            """Render individual article"""
            try:
                article_file = self.library / article_path
                if not article_file.exists():
                    abort(404)
                    
                with open(article_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                metadata, markdown_content = self._parse_article_metadata(content)
                html_content = markdown.markdown(
                    markdown_content,
                    extensions=['fenced_code', 'codehilite', 'tables', 'toc']
                )
                
                related_articles = self._get_related_articles(metadata)
                
                return render_template('article.html',
                    title=metadata.get('title', 'Article'),
                    metadata=metadata,
                    content=html_content,
                    related_articles=related_articles,
                    ad_config=self.ad_config)
                    
            except Exception as e:
                self.logger.error(f"Error rendering article: {str(e)}")
                abort(500)

        @self.app.route('/search')
        def search():
            """Search articles"""
            query = request.args.get('q', '')
            if not query:
                return render_template('search.html', 
                    title="Search Articles",
                    results=[])
                
            # Basic search implementation
            results = []
            for article_file in self.library.rglob('*.md'):
                try:
                    with open(article_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        metadata, markdown_content = self._parse_article_metadata(content)
                        if query.lower() in content.lower():
                            metadata['path'] = str(article_file.relative_to(self.library))
                            results.append(metadata)
                except Exception as e:
                    self.logger.error(f"Error searching file {article_file}: {str(e)}")
                    
            return render_template('search.html',
                title=f"Search Results for '{query}'",
                query=query,
                results=results,
                ad_config=self.ad_config)

        @self.app.route("/health")
        def health():
            """Health check endpoint"""
            try:
                # Basic check that we can access library
                if not self.library.exists():
                    self.logger.warning("Library directory not found")
                
                return jsonify({
                    "status": "operational",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "version": "0.1.0"
                })
            except Exception as e:
                self.logger.error(f"Health check failed: {str(e)}")
                return jsonify({
                    "status": "error",
                    "error": str(e)
                }), 500

        @self.app.template_filter('format_date')
        def format_date_filter(date_str: str, format: str = '%B %d, %Y') -> str:
            """Template filter for date formatting"""
            return self._format_date(date_str, format)

        @self.app.context_processor
        def inject_globals():
            """Inject global template variables"""
            return {
                'categories': self._get_categories(),
                'current_year': datetime.datetime.now().year,
                'site_name': self.site_config.get('site_name', 'Veinity'),
                'site_description': self.site_config.get('description', ''),
                'social_links': self.site_config.get('social_links', {}),
                'analytics_id': os.getenv('ANALYTICS_ID', ''),
                'get_recent_articles': self._get_recent_articles
            }

        @self.app.template_filter('truncate_html')
        def truncate_html_filter(content: str, length: int = 160) -> str:
            """Template filter for truncating HTML content"""
            return self._get_article_preview(content, length)

        @self.app.errorhandler(404)
        def page_not_found(e):
            """Custom 404 error handler"""
            return render_template('404.html', title='Page Not Found'), 404

        @self.app.errorhandler(500)
        def server_error(e):
            """Custom 500 error handler"""
            return render_template('500.html', title='Server Error'), 500

```
## /src/hub/static/

## /src/hub/static/images/

/src/hub/static/images/logo.png: [non-readable or binary content]
/src/hub/static/images/marble-dark.png: [non-readable or binary content]
/src/hub/static/images/marble-light.png: [non-readable or binary content]
/src/hub/static/styles.css: 
```
/* Variables */
:root {
    /* Light theme (default) */
    --primary-color: #002366;
    --secondary-color: #1e4b94;
    --accent-gold: #d4af37;
    --text-primary: #1a1a1a;
    --text-secondary: rgba(0, 0, 0, 0.85);
    --background-primary: #ffffff;
    --background-secondary: rgba(255, 255, 255, 0.85);
    --border-color: var(--accent-gold);
    --link-color: var(--primary-color);
    --link-hover: var(--accent-gold);
    --bg-transparent: rgba(0, 0, 0, 0.3);
    --bg-transparent-darker: rgba(0, 0, 0, 0.5);
    --bg-transparent-light: rgba(255, 255, 255, 0.3);
    --bg-transparent-light-darker: rgba(255, 255, 255, 0.5);
    --nav-bg: var(--bg-transparent-light);
    --nav-text: #1a1a1a;
    --input-bg: rgba(255, 255, 255, 0.9);
    --input-border: var(--accent-gold);
    --button-bg: var(--primary-color);
    --button-text: #ffffff;
  }
  
  /* Dark theme */
  [data-theme="dark"] {
    --text-primary: #ffffff;
    --text-secondary: rgba(255, 255, 255, 0.85);
    --background-primary: #1a1a1a;
    --background-secondary: rgba(0, 0, 0, 0.85);
    --link-color: var(--accent-gold);
    --link-hover: #ffffff;
    --nav-bg: var(--bg-transparent);
    --nav-text: #ffffff;
    --input-bg: rgba(0, 0, 0, 0.2);
  }
  
  /* Base Styles */
  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }
  
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    line-height: 1.6;
    color: var(--text-primary);
    background-image: url('../static/images/marble-dark.png');
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    background-attachment: fixed;
    background-color: var(--background-primary);
    min-height: 100vh;
    transition: all 0.3s ease;
    position: relative;
    overflow-x: hidden;
  }
  
  [data-theme="light"] body {
    background-image: url('../static/images/marble-light.png');
  }
  
  body::before {
    content: '';
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: var(--background-secondary);
    z-index: -1;
    opacity: 0.7;
  }
  
  a {
    color: var(--link-color);
    text-decoration: none;
    transition: color 0.2s ease;
  }
  
  a:hover {
    color: var(--link-hover);
  }
  
  /* Navigation */
  nav {
    background-color: var(--nav-bg);
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
    padding: 1rem 2rem;
    border-bottom: 1px solid var(--accent-gold);
    transition: all 0.3s ease;
    width: 100%;
    position: relative;
    z-index: 1000;
  }
  
  .nav-container {
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    flex-direction: row-reverse;
    justify-content: space-between;
    align-items: center;
    position: relative;
    z-index: 1001;
  }
  
  /* Navigation Brand */
  .nav-brand {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    position: relative;
    z-index: 1002;
  }

  .nav-brand img {
    height: 120px;
    width: auto;
    transition: transform 0.2s ease;
  }

  .nav-brand a {
    color: var(--text-primary);
    font-size: 2rem;
    font-weight: 600;
    text-decoration: none;
    display: flex;
    align-items: center;
    gap: 1.5rem;
  }
  
  .nav-links {
    display: flex;
    gap: 2rem;
    align-items: center;
    flex-wrap: wrap;
    z-index: 1001;
  }
  
  .nav-links a {
    color: var(--text-primary);
    text-decoration: none;
    font-weight: 500;
    transition: color 0.2s ease;
  }
  
  .nav-links a:hover {
    color: var(--accent-gold);
  }
  
  /* Mobile Menu Toggle */
  .mobile-menu-toggle {
    display: none;
    flex-direction: column;
    justify-content: space-between;
    width: 30px;
    height: 21px;
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 0;
  }
  
  .mobile-menu-toggle span {
    width: 100%;
    height: 3px;
    background-color: var(--text-primary);
    border-radius: 3px;
    transition: all 0.3s ease;
  }
  
  .mobile-menu-toggle.active span:first-child {
    transform: translateY(9px) rotate(45deg);
  }
  
  .mobile-menu-toggle.active span:nth-child(2) {
    opacity: 0;
  }
  
  .mobile-menu-toggle.active span:last-child {
    transform: translateY(-9px) rotate(-45deg);
  }
  
  /* Search Bar */
  .search-container {
    position: relative;
    width: 300px;
  }
  
  .search-input {
    width: 100%;
    padding: 0.5rem 1rem;
    border: 1px solid var(--accent-gold);
    border-radius: 20px;
    background: var(--input-bg);
    color: var(--text-primary);
    transition: all 0.3s ease;
  }
  
  .search-button {
    position: absolute;
    right: 8px;
    top: 50%;
    transform: translateY(-50%);
    background: var(--button-bg);
    color: var(--button-text);
    border: none;
    border-radius: 15px;
    padding: 0.25rem 1rem;
    cursor: pointer;
    transition: all 0.2s ease;
  }
  
  /* Theme Toggle */
  .theme-toggle {
    background: none;
    border: none;
    color: var(--text-primary);
    cursor: pointer;
    padding: 0.5rem;
    display: flex;
    align-items: center;
    transition: all 0.3s ease;
  }
  
  .theme-toggle:hover {
    color: var(--accent-gold);
  }
  
  .theme-toggle svg {
    width: 1.2rem;
    height: 1.2rem;
    fill: currentColor;
  }
  
  /* Hero Section */
  .hero-section {
    background-color: var(--bg-transparent);
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
    padding: 4rem 0;
    margin-bottom: 2rem;
    border-bottom: 1px solid var(--accent-gold);
    position: relative;
    z-index: 1;
  }

  .hero-content {
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 2rem;
    text-align: center;
  }

  .hero-section h1 {
    font-size: 2.5rem;
    font-weight: 700;
    margin-bottom: 1rem;
  }

  .hero-section .lead {
    font-size: 1.25rem;
    color: var(--text-secondary);
  }
  
  /* Article Cards */
  .article-card {
    background-color: var(--bg-transparent-darker);
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
    border: 1px solid var(--border-color);
    border-radius: 1rem;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
    transition: all 0.3s ease;
  }
  
  .article-card:hover {
    transform: translateY(-2px);
    border-color: var(--accent-gold);
    box-shadow: 0 0 20px rgba(212, 175, 55, 0.2);
  }
  
  .article-title {
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 1rem;
  }
  
  .article-meta {
    font-size: 0.9rem;
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }
  
  .article-excerpt {
    color: var(--text-secondary);
    margin-bottom: 1rem;
  }
  
  /* Categories */
  .categories {
    background-color: var(--bg-transparent);
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
    padding: 2rem;
    border-radius: 1rem;
    border: 1px solid var(--accent-gold);
  }
  
  .categories h2 {
    color: var(--text-primary);
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid var(--accent-gold);
  }
  
  /* Footer */
  footer {
    background-color: var(--nav-bg);
    -webkit-backdrop-filter: blur(10px);
    backdrop-filter: blur(10px);
    border-top: 1px solid var(--accent-gold);
    margin-top: 4rem;
    padding: 3rem 2rem;
  }
  
  .footer-content {
    max-width: 1200px;
    margin: 0 auto;
    text-align: center;
  }
  
  /* Mobile Styles */
  @media (max-aspect-ratio: 1/1) {
    /* Mobile Background */
    body {
      background-attachment: scroll;
      min-height: -webkit-fill-available;
    }
  
    /* Mobile Navigation */
    nav {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      padding: 0.5rem;
      height: 80px;
      z-index: 1000;
    }

    /* Adjust content for fixed header */
    main {
      padding-top: 80px;
      position: relative;
      z-index: 1;
    }

    /* Hero Section Adjustments */
    .hero-section {
      position: relative;
      z-index: 1;
      padding: 2rem 1rem;
      margin-top: 0;
    }

    /* Logo positioning */
    .nav-brand {
      position: fixed;
      top: 1rem;
      left: 50%;
      transform: translateX(-50%);
      z-index: 1003;
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
    }

    .mobile-menu-toggle {
      display: flex;
      position: fixed;
      top: 1rem;
      right: 1rem;
      z-index: 1003;
    }
  
    .theme-toggle {
      position: fixed;
      top: 1rem;
      right: 4rem;
      z-index: 1003;
    }
  
    .nav-container {
      padding: 0;
    }
  
    .nav-brand {
      position: fixed;
      top: 1rem;
      left: 50%;
      transform: translateX(-50%);
      z-index: 1003;
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
    }
  
    .nav-brand img {
      height: 50px;
    }
  
    .nav-brand span {
      font-size: 1.5rem;
    }
  
    .nav-links {
      display: none;
    }
  
    .nav-links.active {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      height: 100vh;
      background-color: var(--bg-transparent-darker);
      -webkit-backdrop-filter: blur(10px);
      backdrop-filter: blur(10px);
      z-index: 1002;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 2rem;
      padding: 4rem 1rem;
    }
  
    .nav-links a {
      font-size: 1.5rem;
      padding: 0.75rem 1.5rem;
      width: 100%;
      text-align: center;
    }
  
    /* Mobile Search */
    .search-container {
      width: 100%;
      padding: 0 1rem;
      margin-top: 1rem;
    }
  
    .search-input {
      width: 100%;
    }
  
    /* Mobile Content */
    .hero-section {
      padding: 2rem 1rem;
    }
  
    .article-card {
      margin: 1rem 0;
    }
  
    /* Prevent scroll when menu open */
    body.menu-open {
      overflow: hidden;
      position: fixed;
      width: 100%;
    }
  }
  
  /* iOS Specific */
  @supports (-webkit-touch-callout: none) {
    body {
      min-height: -webkit-fill-available;
    }
  }
  
  /* Mobile adjustments */
  @media (max-aspect-ratio: 1/1) {
    nav {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      padding: 0.5rem;
    }

    .nav-brand {
      position: fixed;
      top: 1rem;
      left: 50%;
      transform: translateX(-50%);
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
    }

    .nav-brand img {
      height: 50px;
    }

    .nav-brand span {
      font-size: 1.5rem;
    }

    main {
      padding-top: 80px;
    }

    .hero-section {
      padding: 2rem 1rem;
    }

    .hero-section h1 {
      font-size: 2rem;
    }
  }
```
## /src/hub/templates/

/src/hub/templates/404.html: 
```
{% extends "base.html" %}

{% block content %}
<div class="container text-center py-5">
    <h1 class="display-1">404</h1>
    <h2 class="mb-4">Page Not Found</h2>
    <p class="lead mb-4">The page you're looking for doesn't exist or has been moved.</p>
    <div class="mb-4">
        <a href="{{ url_for('index') }}" class="btn btn-primary">
            <i class="fas fa-home mr-2"></i> Return Home
        </a>
    </div>
    
    <div class="mt-5">
        <h3>Recent Articles</h3>
        <div class="row justify-content-center">
            {% set recent_articles = get_recent_articles(limit=3) %}
            {% for article in recent_articles %}
            <div class="col-md-4">
                <div class="article-card">
                    <h4 class="article-title">
                        <a href="{{ url_for('article_view', article_path=article.path) }}">
                            {{ article.title }}
                        </a>
                    </h4>
                    <div class="article-meta">
                        <span class="article-date">{{ article.published_date|format_date }}</span>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
</div>
{% endblock %}
```
/src/hub/templates/article.html: 
```
{% extends "base.html" %}

{% block content %}
<div class="container">
    <div class="row">
        <div class="col-md-8">
            <article class="article-full">
                <header class="article-header">
                    <h1 class="article-title">{{ metadata.title }}</h1>
                    <div class="article-meta">
                        {% if metadata.source %}
                        <span class="article-source">
                            <i class="fas fa-newspaper"></i> {{ metadata.source }}
                        </span>
                        {% endif %}
                        {% if metadata.published_date %}
                        <span class="article-date">
                            <i class="fas fa-calendar"></i> {{ metadata.published_date }}
                        </span>
                        {% endif %}
                        {% if metadata.category %}
                        <a href="{{ url_for('category_view', category=metadata.category) }}" 
                           class="category-tag">
                            <i class="fas fa-tag"></i> {{ metadata.category }}
                        </a>
                        {% endif %}
                    </div>
                </header>

                {% if ad_config.enabled and ad_config.slots.in_content %}
                <div class="ad-slot content-ad-top">
                    <ins class="adsbygoogle"
                         data-ad-client="{{ ad_config.client_id }}"
                         data-ad-slot="{{ ad_config.slots.in_content }}"
                         style="display:block"
                         data-ad-format="rectangle"></ins>
                    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
                </div>
                {% endif %}

                <div class="article-content">
                    {{ content|safe }}
                </div>

                {% if metadata.tags %}
                <div class="article-tags">
                    {% for tag in metadata.tags %}
                    <span class="tag">{{ tag }}</span>
                    {% endfor %}
                </div>
                {% endif %}

                {% if ad_config.enabled and ad_config.slots.in_content %}
                <div class="ad-slot content-ad-bottom">
                    <ins class="adsbygoogle"
                         data-ad-client="{{ ad_config.client_id }}"
                         data-ad-slot="{{ ad_config.slots.in_content }}"
                         style="display:block"
                         data-ad-format="rectangle"></ins>
                    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
                </div>
                {% endif %}
            </article>
        </div>

        <div class="col-md-4">
            {% include '_sidebar.html' %}
        </div>
    </div>
</div>
{% endblock %}
```
/src/hub/templates/base.html: 
```
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}{{ title }} - {{ site_name }}{% endblock %}</title>
    <meta name="description" content="{{ site_description }}">
    <link rel="stylesheet" href="{{ url_for('static', filename='styles.css') }}">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script>
        // Theme handling
        function getTheme() {
            return localStorage.getItem('theme') || 'light';
        }

        function setTheme(theme) {
            localStorage.setItem('theme', theme);
            document.documentElement.setAttribute('data-theme', theme);
        }

        function toggleTheme() {
            const currentTheme = getTheme();
            const newTheme = currentTheme === 'light' ? 'dark' : 'light';
            setTheme(newTheme);
        }

        // Set initial theme
        document.addEventListener('DOMContentLoaded', () => {
            setTheme(getTheme());
        });
    </script>
</head>
<body>
    <header>
        <nav>
            <div class="nav-container">
                <div class="nav-brand">
                    <a href="{{ url_for('index') }}">
                        <img src="{{ url_for('static', filename='images/logo.png') }}" alt="{{ site_name }} logo">
                        <span>{{ site_name }}</span>
                    </a>
                </div>
                
                <button class="mobile-menu-toggle" onclick="toggleMenu()" aria-label="Toggle navigation menu">
                    <span></span>
                    <span></span>
                    <span></span>
                </button>

                <div class="nav-links" id="navLinks">
                    {% for category in categories %}
                    <a href="{{ url_for('category_view', category=category) }}">{{ category }}</a>
                    {% endfor %}
                    
                    <div class="search-container">
                        <form action="{{ url_for('search') }}" method="get">
                            <input type="search" name="q" class="search-input" placeholder="Search articles..." required>
                            <button type="submit" class="search-button">Search</button>
                        </form>
                    </div>
                    
                    <button class="theme-toggle" onclick="toggleTheme()" aria-label="Toggle theme">
                        <svg class="theme-toggle-dark" viewBox="0 0 24 24" width="20" height="20">
                            <path fill="currentColor" d="M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36-.98 1.37-2.58 2.26-4.4 2.26-3.03 0-5.5-2.47-5.5-5.5 0-1.82.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z"/>
                        </svg>
                        <svg class="theme-toggle-light" viewBox="0 0 24 24" width="20" height="20">
                            <path fill="currentColor" d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5z"/>
                        </svg>
                    </button>
                </div>
            </div>
        </nav>
    </header>

    <div class="hero-section">
        <div class="hero-content">
            <h1>Latest Tech News</h1>
            <p class="lead">Your source for the latest tech news and insights</p>
        </div>
    </div>

    <main>
        {% block content %}{% endblock %}
    </main>

    <footer>
        <div class="container">
            <div class="footer-content">
                <p>&copy; {{ current_year }} {{ site_name }}. All rights reserved.</p>
            </div>
        </div>
    </footer>

    <script>
        function toggleMenu() {
            const navLinks = document.getElementById('navLinks');
            const mobileMenuToggle = document.querySelector('.mobile-menu-toggle');
            
            navLinks.classList.toggle('active');
            mobileMenuToggle.classList.toggle('active');
            
            // Toggle body scroll
            document.body.classList.toggle('menu-open');
        }

        // Close menu when clicking outside
        document.addEventListener('click', function(event) {
            const navLinks = document.getElementById('navLinks');
            const mobileMenuToggle = document.querySelector('.mobile-menu-toggle');
            
            if (!event.target.closest('.nav-links') && 
                !event.target.closest('.mobile-menu-toggle') && 
                navLinks.classList.contains('active')) {
                navLinks.classList.remove('active');
                mobileMenuToggle.classList.remove('active');
                document.body.classList.remove('menu-open');
            }
        });

        // Close menu when screen orientation changes
        window.addEventListener('orientationchange', function() {
            const navLinks = document.getElementById('navLinks');
            const mobileMenuToggle = document.querySelector('.mobile-menu-toggle');
            
            if (navLinks.classList.contains('active')) {
                navLinks.classList.remove('active');
                mobileMenuToggle.classList.remove('active');
                document.body.classList.remove('menu-open');
            }
        });
    </script>
</body>
</html>
```
/src/hub/templates/category.html: 
```
{% extends "base.html" %}

{% block content %}
<div class="container">
    <div class="row">
        <div class="col-md-8">
            <header class="category-header">
                <h1>{{ category|title }} News</h1>
                <p class="lead">Latest articles in {{ category|title }}</p>
            </header>

            {% if articles %}
            <div class="article-list">
                {% for article in articles %}
                <article class="article-card">
                    <h2 class="article-title">
                        <a href="{{ url_for('article_view', article_path=article.path) }}">
                            {{ article.title }}
                        </a>
                    </h2>
                    <div class="article-meta">
                        <span class="article-source">{{ article.source }}</span>
                        <span class="article-date">{{ article.published_date }}</span>
                    </div>
                    {% if article.description %}
                    <p class="article-excerpt">{{ article.description[:160] }}...</p>
                    {% endif %}
                </article>
                {% endfor %}
            </div>
            {% else %}
            <p class="text-center mt-5">No articles found in this category.</p>
            {% endif %}
        </div>

        <div class="col-md-4">
            {% include '_sidebar.html' %}
        </div>
    </div>
</div>
{% endblock %}
```
/src/hub/templates/index.html: 
```
{% extends "base.html" %}

{% block content %}
<div class="hero-section text-center py-5">
    <h1 class="display-4">{{ title }}</h1>
    <p class="lead">Your source for the latest tech news and insights</p>
</div>

<div class="container">
    <div class="row">
        <div class="col-md-8">
            <!-- Featured Articles -->
            {% if articles %}
            <section class="mb-5">
                <h2 class="section-title">Latest Articles</h2>
                <div class="article-grid">
                    {% for article in articles %}
                    <article class="article-card">
                        <h3 class="article-title">
                            <a href="{{ url_for('article_view', article_path=article.path) }}">
                                {{ article.title }}
                            </a>
                        </h3>
                        <div class="article-meta">
                            <span class="article-source">{{ article.source }}</span>
                            <span class="article-date">{{ article.published_date }}</span>
                        </div>
                        {% if article.description %}
                        <p class="article-excerpt">{{ article.description[:160] }}...</p>
                        {% endif %}
                        {% if article.category %}
                        <a href="{{ url_for('category_view', category=article.category) }}" 
                           class="category-tag">{{ article.category }}</a>
                        {% endif %}
                    </article>
                    {% endfor %}
                </div>
            </section>
            {% else %}
            <p class="text-center mt-5">No articles found.</p>
            {% endif %}
        </div>
        
        <!-- Sidebar -->
        <div class="col-md-4">
            {% include '_sidebar.html' %}
        </div>
    </div>
</div>
{% endblock %}
```
/src/hub/templates/search.html: 
```
{% extends "base.html" %}

{% block content %}
<div class="container">
    <div class="row">
        <div class="col-md-8">
            <header class="search-header">
                <h1>Search Results</h1>
                {% if query %}
                <p class="lead">Showing results for "{{ query }}"</p>
                {% endif %}
            </header>

            <div class="search-form-large mb-4">
                <form action="{{ url_for('search') }}" method="get">
                    <div class="input-group">
                        <input type="search" name="q" class="form-control" 
                               placeholder="Search articles..." value="{{ query }}">
                        <div class="input-group-append">
                            <button type="submit" class="btn btn-primary">
                                <i class="fas fa-search"></i> Search
                            </button>
                        </div>
                    </div>
                </form>
            </div>

            {% if results %}
            <div class="search-results">
                {% for result in results %}
                <article class="article-card">
                    <h2 class="article-title">
                        <a href="{{ url_for('article_view', article_path=result.path) }}">
                            {{ result.title }}
                        </a>
                    </h2>
                    <div class="article-meta">
                        {% if result.source %}
                        <span class="article-source">{{ result.source }}</span>
                        {% endif %}
                        {% if result.published_date %}
                        <span class="article-date">{{ result.published_date }}</span>
                        {% endif %}
                        {% if result.category %}
                        <a href="{{ url_for('category_view', category=result.category) }}" 
                           class="category-tag">{{ result.category }}</a>
                        {% endif %}
                    </div>
                    {% if result.description %}
                    <p class="article-excerpt">{{ result.description[:160] }}...</p>
                    {% endif %}
                </article>
                {% endfor %}
            </div>
            {% elif query %}
            <p class="text-center mt-5">No results found for "{{ query }}".</p>
            {% endif %}
        </div>

        <div class="col-md-4">
            {% include '_sidebar.html' %}
        </div>
    </div>
</div>
{% endblock %}
```
/src/hub/templates/_sidebar.html: 
```
<aside class="sidebar">
    {% if ad_config.enabled and ad_config.slots.sidebar %}
    <div class="ad-slot sidebar-ad mb-4">
        <ins class="adsbygoogle"
             data-ad-client="{{ ad_config.client_id }}"
             data-ad-slot="{{ ad_config.slots.sidebar }}"
             style="display:block"
             data-ad-format="vertical"></ins>
        <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
    </div>
    {% endif %}

    <div class="sidebar-section">
        <h3>Categories</h3>
        <ul class="category-list">
            {% for category in categories %}
            <li>
                <a href="{{ url_for('category_view', category=category) }}">
                    {{ category|title }}
                </a>
            </li>
            {% endfor %}
        </ul>
    </div>

    <div class="sidebar-section">
        <h3>Recent Articles</h3>
        {% set recent_articles = get_recent_articles(limit=5) %}
        <ul class="recent-articles-list">
            {% for article in recent_articles %}
            <li>
                <a href="{{ url_for('article_view', article_path=article.path) }}">
                    {{ article.title }}
                </a>
                <span class="article-date small">{{ article.published_date }}</span>
            </li>
            {% endfor %}
        </ul>
    </div>
</aside>
```
/src/hub/__init__.py: 
```

```
## /src/modules/

## /src/modules/library/

## /src/modules/library/.github/

## /src/modules/library/.github/workflows/

/src/modules/library/.github/workflows/hub-sync.yml: 
```
name: Trigger Chamber Sync Job

on:
    push:
      branches:
        - main  # Adjust as needed
jobs:
    trigger_sync:
        env:
            github-token: ${{ secrets.HUB_API_KEY }} # Fine-grained GitHub PAT is saved as action secret.
        runs-on: ubuntu-latest
        steps:
          - name: Trigger Chamber Sync
            uses: actions/github-script@v6
            with:
              github-token: ${{ secrets.HUB_API_KEY }}
              script: |
                await github.rest.actions.createWorkflowDispatch({
                  owner: 'bobbyhiddn',
                  repo: 'Veinity.Hub',
                  workflow_id: 'fly-deploy.yml',
                  ref: 'main'
                })
```
## /src/modules/library/articles/

## /src/modules/library/articles/Business/

/src/modules/library/articles/Business/Startup_Funding.md: 
```
---
title: "Tech Startups See Record Funding Despite Market Challenges"
source: "Business Insider"
url: "https://example.com/startup-funding"
published_date: "2024-12-08"
category: "business"
tags: ["Startups", "Venture Capital", "Technology"]
description: "Despite market uncertainties, tech startups continue to attract unprecedented levels of venture capital."
---

# Tech Startups See Record Funding Despite Market Challenges

Despite ongoing market volatility, tech startups have secured record levels of funding in Q4 2024, with particular focus on AI and sustainable technology ventures.

## Investment Trends

The funding landscape shows strong preference for:
- Artificial Intelligence startups
- Clean technology initiatives
- Healthcare technology solutions

## Key Statistics

- Total funding: $28.5B in Q4
- Average seed round: $3.2M
- Late-stage valuations up 15%

## Market Analysis

Investors remain bullish on technology sectors that demonstrate:
1. Clear path to profitability
2. Strong intellectual property
3. Scalable business models

This trend indicates continued confidence in the tech sector's growth potential.
```
## /src/modules/library/articles/Science/

/src/modules/library/articles/Science/Quantum Computing.md: 
```
---
title: "Quantum Computing Achieves New Milestone"
source: "Science Today"
url: "https://example.com/quantum-milestone"
published_date: "2024-12-08"
category: "science"
tags: ["Quantum Computing", "Physics", "Technology"]
description: "Scientists demonstrate new quantum computing breakthrough with potential to revolutionize cryptography."
---

# Quantum Computing Achieves New Milestone

Scientists have achieved a significant breakthrough in quantum computing stability, maintaining quantum coherence for record durations at room temperature.

## Technical Achievement

The breakthrough involves:
- Extended coherence time
- Room temperature operation
- Scalable architecture

## Implications

This development could revolutionize:
1. Cryptography
2. Drug discovery
3. Climate modeling

## Future Prospects

The research team suggests commercial applications could be viable within:
- 3-5 years for specialized applications
- 5-7 years for general computing tasks

This represents a major step toward practical quantum computing.
```
## /src/modules/library/articles/Tech/

/src/modules/library/articles/Tech/AI Article Test.md: 
```
---
title: "Major AI Breakthrough in Natural Language Processing"
source: "Tech Daily"
url: "https://example.com/ai-breakthrough"
published_date: "2024-12-08"
category: "tech"
tags: ["AI", "NLP", "Machine Learning"]
description: "Researchers announce significant advancement in natural language understanding capabilities."
---

# Major AI Breakthrough in Natural Language Processing

Researchers at leading AI labs have announced a significant breakthrough in natural language processing, demonstrating unprecedented accuracy in understanding context and nuance in human communication.

## Key Developments

The new system shows remarkable capabilities in:
- Understanding complex context
- Processing multiple languages simultaneously
- Maintaining coherence in long-form conversations

## Impact on Industry

This breakthrough is expected to have far-reaching implications for:
1. Customer service automation
2. Educational technology
3. Healthcare communication systems

## Technical Details

The system utilizes a novel architecture that combines:
- Advanced transformer models
- Innovative attention mechanisms
- Efficient processing algorithms

This represents a major step forward in AI capabilities and sets new benchmarks for the industry.
```
/src/modules/library/articles/Tech/Blockchain.md: 
```
---
title: "Revolutionary Blockchain Solution Reduces Energy Consumption by 90%"
source: "Crypto Weekly"
url: "https://example.com/blockchain-energy"
published_date: "2024-12-08"
category: "tech"
tags: ["Blockchain", "Green Technology", "Cryptocurrency", "Innovation"]
description: "A new blockchain architecture promises to slash energy consumption while maintaining security and decentralization."
---

# Revolutionary Blockchain Solution Reduces Energy Consumption by 90%

In a groundbreaking development for the blockchain industry, researchers have unveiled a new consensus mechanism that dramatically reduces the energy footprint of blockchain networks while preserving their core security features.

## The Innovation

The new system, dubbed "EcoChain," introduces several key innovations:

- Adaptive power scaling based on network load
- Smart node hibernation during low-activity periods
- Efficient proof-of-stake variant with enhanced security guarantees

## Environmental Impact

Initial testing shows promising results:
- 90% reduction in energy consumption
- Carbon footprint equivalent to traditional database systems
- Scalable to millions of transactions per second

## Industry Response

Major blockchain platforms are already expressing interest in adopting the technology:

1. Three major cryptocurrencies plan to implement the system by 2025
2. Several financial institutions are running pilot programs
3. Government regulators have praised the environmental considerations

## Technical Implementation

The system achieves its efficiency through:
- Dynamic resource allocation
- Advanced node coordination
- Optimized consensus protocols

## Future Prospects

This development could mark a turning point in blockchain adoption, particularly in:
- Enterprise systems
- Government applications
- Environmental certification

Experts predict widespread adoption could begin as early as mid-2025.
```
## /src/modules/library/cache/

/src/modules/library/cache/test: 
```

```
/src/modules/library/README.md: 
```

```
## /src/modules/library/Test/

/src/modules/library/Test/test.md: 
```
# Testing

## Test

This is a test
    - This is a test
    - This is a test

```python   
print("Hello, world!")
```
```
/src/requirements.txt: 
```
Flask==3.0.0
Werkzeug==3.0.1
Jinja2==3.1.2
MarkupSafe==2.1.3
PyYAML==6.0.1
Markdown==3.5.1
python-dotenv==1.0.0
gunicorn==21.2.0
click==8.1.7
itsdangerous==2.1.2
requests==2.31.0
feedparser==6.0.10
python-dateutil==2.8.2
Pygments==2.15.1
```
## /utils/

/utils/curl_update.sh: 
```
WEBHOOK_SECRET='d9ded68b21e8ab8a5a135918554897f11c13784301b77f61a6ad93ba7c6a6927'
WEBHOOK_URL='https://magi-chamber.fly.dev/webhook' 
# WEBHOOK_URL='https://localhost:8888/webhook'

PAYLOAD='{}'  # Empty JSON payload
SIGNATURE='sha256='$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
curl -X POST \
-H "Content-Type: application/json" \
-H "X-Hub-Signature-256: $SIGNATURE" \
-d "$PAYLOAD" \
"$WEBHOOK_URL"
```
/utils/flask_keygen.py: 
```
import secrets

def generate_flask_key():
    # Generate a secure 32-byte (256-bit) random key and convert to hex
    return secrets.token_hex(32)

if __name__ == "__main__":
    key = generate_flask_key()
    print(f"FLASK_SECRET_KEY=\"{key}\"")
```
/utils/fly_deploy.sh: 
```
#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Function to check health endpoint
check_health() {
    local url="$1"
    local max_attempts=10
    local wait_time=10
    local attempt=1

    print_status "Checking deployment health..."
    
    while [ $attempt -le $max_attempts ]; do
        print_status "Attempt $attempt of $max_attempts"
        
        if curl -s "${url}/health" | grep -q "operational"; then
            print_success "Veinity is operational!"
            return 0
        else
            print_status "Veinity is still starting up, waiting ${wait_time} seconds..."
            sleep $wait_time
            attempt=$((attempt + 1))
        fi
    done

    print_error "Veinity failed to respond after $max_attempts attempts"
    return 1
}

# Load environment variables
if [ -f .env ]; then
    print_status "Loading environment variables..."
    export $(grep -v '^#' .env | xargs)
    print_success "Environment variables loaded"
else
    print_error ".env file not found!"
fi

# Update git repository and submodules
print_status "Updating repository and submodules..."
git pull --recurse-submodules
git submodule update --init --recursive
print_success "Repository updated"

# Set secrets on Fly.io
print_status "Setting secrets on Fly.io..."
flyctl secrets set \
    FLASK_SECRET_KEY="$FLASK_SECRET_KEY" \
    NEWSAPI_KEY="$NEWSAPI_KEY" \
    ANALYTICS_ID="$ANALYTICS_ID" \
    WEBHOOK_SECRET="$WEBHOOK_SECRET"

print_success "Secrets set successfully on Fly.io"

# Deploy to Fly.io
print_status "Deploying Veinity to Fly.io..."
if flyctl deploy; then
    print_success "Veinity has been deployed!"
    
    # Get the app URL
    APP_URL="veinity.fly.dev"
    print_status "Your application is available at: https://$APP_URL"
    
    # Check health with retries
    check_health "https://$APP_URL"
else
    print_error "Deployment failed"
fi

# Show recent logs
print_status "Recent logs from Veinity:"
flyctl logs
```
/utils/git_update.sh: 
```
#!/bin/sh
echo "Pulling latest changes and updating submodules..."
git pull origin main --recurse-submodules
git submodule init
git submodule update --remote --merge
```
/utils/pre-push: 
```
#!/bin/sh

remote="$1"
url="$2"

# Get the current branch name
current_branch=$(git symbolic-ref HEAD | sed -e 's,.*/\(.*\),\1,')

# If we're pushing to main branch, update submodules first
if [ "$current_branch" = "main" ]; then
    echo "Pushing to main branch - updating submodules first..."
    
    # Store initial commit hash
    initial_commit=$(git rev-parse HEAD)

    # Update submodules similar to what's done in [utils/git_update.sh](utils/git_update.sh)
    if ! git pull --recurse-submodules; then
        echo "Error: Failed to pull latest changes"
        exit 1
    fi
    
    if ! git submodule update --remote --merge; then
        echo "Error: Failed to update submodules"
        exit 1
    fi

    # Check if there are changes in the parent repo
    if git status --porcelain | grep -q '^.M'; then
        echo "Changes detected in parent repo after submodule update"
        
        # Stage all changes
        if ! git add -A; then
            echo "Error: Failed to stage changes"
            exit 1
        fi

        # Create commit with automatic message
        if ! git commit -m "Auto-commit: Update submodule references"; then
            echo "Error: Failed to create automatic commit"
            exit 1
        fi

        echo "Created automatic commit for submodule updates"
    fi
fi

zero=$(git hash-object --stdin </dev/null | tr '[0-9a-f]' '0')
final_commit=$(git rev-parse HEAD)

while read local_ref local_oid remote_ref remote_oid
do
    if test "$local_oid" = "$zero"
    then
        # Handle delete
        :
    else
        if test "$remote_oid" = "$zero"
        then
            # New branch, examine all commits
            range="$local_oid"
        else
            # Update to existing branch, examine new commits
            if [ "$initial_commit" != "$final_commit" ]; then
                # Include the auto-commit in the range
                range="$remote_oid..$final_commit"
            else
                range="$remote_oid..$local_oid"
            fi
        fi
    fi
done

exit 0
```
