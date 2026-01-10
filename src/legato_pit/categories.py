"""
Categories Blueprint - User-defined category management.

Provides API and UI for managing knowledge categories.
Categories are stored per-user and can be customized from defaults.
"""

import re
import logging
from flask import Blueprint, request, jsonify, render_template, g

from .core import login_required

logger = logging.getLogger(__name__)

categories_bp = Blueprint('categories', __name__, url_prefix='/categories')


def get_db():
    """Get legato database connection."""
    if 'legato_db_conn' not in g:
        from .rag.database import init_db
        g.legato_db_conn = init_db()
    return g.legato_db_conn


def get_user_id():
    """Get current user ID (or 'default' for single-user mode)."""
    # For now, use 'default' - can be extended for multi-user
    return 'default'


# ============ API Endpoints ============

@categories_bp.route('/api/list', methods=['GET'])
@login_required
def api_list_categories():
    """List all active categories for the current user.

    Response:
    {
        "categories": [
            {
                "id": 1,
                "name": "epiphany",
                "display_name": "Epiphany",
                "description": "Major breakthrough...",
                "folder_name": "epiphanys",
                "sort_order": 1
            },
            ...
        ]
    }
    """
    from .rag.database import get_user_categories

    db = get_db()
    user_id = get_user_id()
    categories = get_user_categories(db, user_id)

    return jsonify({'categories': categories})


@categories_bp.route('/api/create', methods=['POST'])
@login_required
def api_create_category():
    """Create a new category.

    Request body:
    {
        "name": "research",           -- slug (required)
        "display_name": "Research",   -- human readable (required)
        "description": "...",         -- optional
        "folder_name": "research"     -- optional, defaults to {name}s
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    name = data.get('name', '').lower().strip()
    display_name = data.get('display_name', '').strip()
    description = data.get('description', '').strip()
    folder_name = data.get('folder_name', '').strip()

    if not name or not display_name:
        return jsonify({'error': 'name and display_name are required'}), 400

    # Validate name (slug format)
    if not re.match(r'^[a-z][a-z0-9-]*$', name):
        return jsonify({'error': 'name must start with a letter and contain only lowercase letters, numbers, and hyphens'}), 400

    if len(name) > 30:
        return jsonify({'error': 'name must be 30 characters or less'}), 400

    # Default folder_name
    if not folder_name:
        folder_name = f"{name}s"

    try:
        db = get_db()
        user_id = get_user_id()

        # Get next sort_order
        max_order = db.execute(
            "SELECT MAX(sort_order) FROM user_categories WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0] or 0

        cursor = db.execute("""
            INSERT INTO user_categories (user_id, name, display_name, description, folder_name, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, name, display_name, description, folder_name, max_order + 1))

        db.commit()
        category_id = cursor.lastrowid

        logger.info(f"Created category: {name} (id={category_id})")

        return jsonify({
            'success': True,
            'id': category_id,
            'name': name,
        })

    except Exception as e:
        if 'UNIQUE constraint' in str(e):
            return jsonify({'error': f'Category "{name}" already exists'}), 409
        logger.error(f"Failed to create category: {e}")
        return jsonify({'error': str(e)}), 500


@categories_bp.route('/api/<int:category_id>', methods=['PUT'])
@login_required
def api_update_category(category_id: int):
    """Update an existing category.

    Request body (all optional):
    {
        "display_name": "New Display Name",
        "description": "New description",
        "folder_name": "new-folder",
        "sort_order": 5
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    try:
        db = get_db()
        user_id = get_user_id()

        # Build update query dynamically
        updates = []
        params = []

        if 'display_name' in data:
            updates.append('display_name = ?')
            params.append(data['display_name'].strip())

        if 'description' in data:
            updates.append('description = ?')
            params.append(data['description'].strip())

        if 'folder_name' in data:
            updates.append('folder_name = ?')
            params.append(data['folder_name'].strip())

        if 'sort_order' in data:
            updates.append('sort_order = ?')
            params.append(int(data['sort_order']))

        if not updates:
            return jsonify({'error': 'No fields to update'}), 400

        updates.append('updated_at = CURRENT_TIMESTAMP')
        params.extend([category_id, user_id])

        result = db.execute(
            f"UPDATE user_categories SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params
        )
        db.commit()

        if result.rowcount == 0:
            return jsonify({'error': 'Category not found'}), 404

        logger.info(f"Updated category id={category_id}")

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Failed to update category: {e}")
        return jsonify({'error': str(e)}), 500


@categories_bp.route('/api/<int:category_id>', methods=['DELETE'])
@login_required
def api_delete_category(category_id: int):
    """Delete (soft-delete) a category.

    Note: This sets is_active=0, not a hard delete.
    Entries using this category will keep their current category.
    """
    try:
        db = get_db()
        user_id = get_user_id()

        # Get category name for logging
        cat = db.execute(
            "SELECT name FROM user_categories WHERE id = ? AND user_id = ?",
            (category_id, user_id)
        ).fetchone()

        if not cat:
            return jsonify({'error': 'Category not found'}), 404

        db.execute(
            "UPDATE user_categories SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (category_id,)
        )
        db.commit()

        logger.info(f"Deleted category: {cat['name']} (id={category_id})")

        return jsonify({'success': True, 'name': cat['name']})

    except Exception as e:
        logger.error(f"Failed to delete category: {e}")
        return jsonify({'error': str(e)}), 500


@categories_bp.route('/api/reorder', methods=['POST'])
@login_required
def api_reorder_categories():
    """Reorder categories.

    Request body:
    {
        "order": [3, 1, 2, 5, 4]  -- category IDs in new order
    }
    """
    data = request.get_json()
    if not data or 'order' not in data:
        return jsonify({'error': 'order array required'}), 400

    try:
        db = get_db()
        user_id = get_user_id()

        for idx, category_id in enumerate(data['order']):
            db.execute(
                "UPDATE user_categories SET sort_order = ? WHERE id = ? AND user_id = ?",
                (idx + 1, category_id, user_id)
            )

        db.commit()

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Failed to reorder categories: {e}")
        return jsonify({'error': str(e)}), 500


# ============ UI Route ============

@categories_bp.route('/')
@login_required
def index():
    """Category management page."""
    from .rag.database import get_user_categories

    db = get_db()
    user_id = get_user_id()
    categories = get_user_categories(db, user_id)

    return render_template('categories.html', categories=categories)
