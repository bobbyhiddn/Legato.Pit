#!/usr/bin/env python3
"""Check and optionally clear agent queue."""
import sqlite3
import sys

DB_PATH = '/data/agents.db'

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Show current agents
    rows = conn.execute('SELECT queue_id, status, project_name FROM agent_queue').fetchall()
    print(f"Found {len(rows)} agents in queue:")
    for row in rows:
        print(f"  {row['queue_id']} | {row['status']} | {row['project_name']}")

    # If --clear flag, delete all
    if len(sys.argv) > 1 and sys.argv[1] == '--clear':
        conn.execute('DELETE FROM agent_queue')
        conn.commit()
        print(f"\nCleared all {len(rows)} agents from queue.")
    else:
        print("\nTo clear all, run: python3 check_agents.py --clear")

if __name__ == '__main__':
    main()
