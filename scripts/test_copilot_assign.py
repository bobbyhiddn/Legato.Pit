#!/usr/bin/env python3
"""
Test script for GitHub Copilot coding agent.

Creates a test repo, creates an issue, and assigns it to Copilot.

Usage:
    export GH_TOKEN=your_token_with_repo_scope
    python test_copilot_assign.py --repo owner/repo-name

    # Or with existing repo and issue:
    python test_copilot_assign.py --repo owner/repo --issue 1 --no-create

Requirements:
    - GH_TOKEN with full repo scope (not just issues)
    - Copilot coding agent enabled on the org/account
"""

import os
import sys
import argparse
import requests
import time


def rest_request(method: str, endpoint: str, token: str, json_data: dict = None) -> dict:
    """Execute a REST API request against GitHub API."""
    url = f"https://api.github.com{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.request(method, url, headers=headers, json=json_data, timeout=30)

    if response.status_code >= 400:
        print(f"REST Error: {response.status_code} - {response.text}", file=sys.stderr)
        return None

    if response.status_code == 204:
        return {}

    return response.json()


def graphql_request(query: str, variables: dict, token: str) -> dict:
    """Execute a GraphQL request against GitHub API."""
    response = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if "errors" in data:
        for error in data["errors"]:
            print(f"GraphQL Error: {error.get('message', error)}", file=sys.stderr)
        if not data.get("data"):
            raise RuntimeError("GraphQL request failed")

    return data


def check_repo_exists(owner: str, repo: str, token: str) -> bool:
    """Check if a repository exists."""
    result = rest_request("GET", f"/repos/{owner}/{repo}", token)
    return result is not None


def create_repo(owner: str, repo: str, token: str) -> bool:
    """Create a new repository."""
    print(f"Creating repository {owner}/{repo}...")

    # Check if creating in org or user account
    user_info = rest_request("GET", "/user", token)
    if not user_info:
        return False

    current_user = user_info.get("login")

    if owner == current_user:
        # Create in user account
        result = rest_request("POST", "/user/repos", token, {
            "name": repo,
            "description": "Test repo for Copilot coding agent",
            "private": False,
            "auto_init": True,  # Creates with README
        })
    else:
        # Create in org
        result = rest_request("POST", f"/orgs/{owner}/repos", token, {
            "name": repo,
            "description": "Test repo for Copilot coding agent",
            "private": False,
            "auto_init": True,
        })

    if result:
        print(f"✓ Repository created: {result.get('html_url')}")
        return True
    return False


def create_issue(owner: str, repo: str, token: str) -> int:
    """Create a test issue in the repository."""
    print("Creating test issue...")

    result = rest_request("POST", f"/repos/{owner}/{repo}/issues", token, {
        "title": "Test: Build a simple greeting function",
        "body": """## Task

Create a simple Python function that returns a greeting message.

## Requirements

1. Create a file called `greet.py`
2. Implement a function `greet(name: str) -> str` that returns "Hello, {name}!"
3. Add a simple test in `test_greet.py`

## Acceptance Criteria

- [ ] `greet.py` exists with the function
- [ ] `test_greet.py` has at least one passing test
- [ ] Code follows PEP 8 style guidelines

---
*This is a test issue for Copilot coding agent*
""",
        "labels": ["copilot"],
    })

    if result:
        issue_num = result.get("number")
        print(f"✓ Issue created: #{issue_num} - {result.get('html_url')}")
        return issue_num
    return None


def get_issue_id(owner: str, repo: str, issue_number: int, token: str) -> str:
    """Get the GraphQL node ID for an issue."""
    query = """
    query GetIssueId($owner: String!, $repo: String!, $number: Int!) {
        repository(owner: $owner, name: $repo) {
            issue(number: $number) {
                id
                title
                state
            }
        }
    }
    """
    variables = {"owner": owner, "repo": repo, "number": issue_number}
    data = graphql_request(query, variables, token)

    issue = data["data"]["repository"]["issue"]
    if not issue:
        raise ValueError(f"Issue #{issue_number} not found in {owner}/{repo}")

    print(f"Found issue: {issue['title']} (state: {issue['state']})")
    return issue["id"]


def get_copilot_actor_id(owner: str, repo: str, token: str) -> str:
    """Get Copilot's actor ID using suggestedActors query on repository."""
    # Query must be on repository, not issue node
    # Copilot's login is "copilot-swe-agent"
    query = """
    query GetCopilotActorId($owner: String!, $repo: String!) {
        repository(owner: $owner, name: $repo) {
            suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
                nodes {
                    login
                    __typename
                    ... on Bot { id }
                    ... on User { id }
                }
            }
        }
    }
    """
    variables = {"owner": owner, "repo": repo}
    data = graphql_request(query, variables, token)

    actors = data["data"]["repository"]["suggestedActors"]["nodes"]
    print(f"Suggested actors: {[a['login'] for a in actors[:10]]}...")

    for actor in actors:
        if actor["login"] == "copilot-swe-agent":
            print(f"Found Copilot actor ID: {actor['id']}")
            return actor["id"]

    return None


def assign_to_copilot(issue_id: str, copilot_id: str, token: str) -> bool:
    """Assign the issue to Copilot using GraphQL mutation."""
    mutation = """
    mutation AssignToCopilot($issueId: ID!, $actorIds: [ID!]!) {
        replaceActorsForAssignable(input: {
            assignableId: $issueId,
            actorIds: $actorIds
        }) {
            assignable {
                ... on Issue {
                    assignees(first: 5) {
                        nodes {
                            login
                        }
                    }
                }
            }
        }
    }
    """
    variables = {"issueId": issue_id, "actorIds": [copilot_id]}
    data = graphql_request(mutation, variables, token)

    assignees = data["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
    assigned_logins = [a["login"] for a in assignees]
    print(f"Issue now assigned to: {assigned_logins}")

    return "copilot" in [login.lower() for login in assigned_logins]


def main():
    parser = argparse.ArgumentParser(
        description="Test GitHub Copilot coding agent - creates repo, issue, and assigns to Copilot"
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository in owner/repo format",
    )
    parser.add_argument(
        "--issue",
        type=int,
        help="Existing issue number (skips repo/issue creation)",
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Don't create repo/issue, just assign existing issue to Copilot",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN"),
        help="GitHub token (or set GH_TOKEN env var)",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: GitHub token required. Set GH_TOKEN or use --token", file=sys.stderr)
        sys.exit(1)

    if "/" not in args.repo:
        print("Error: Repository must be in owner/repo format", file=sys.stderr)
        sys.exit(1)

    owner, repo = args.repo.split("/", 1)

    print(f"\n{'='*60}")
    print(f"Testing Copilot Coding Agent: {owner}/{repo}")
    print(f"{'='*60}\n")

    try:
        issue_number = args.issue

        if not args.no_create:
            # Step 1: Check/create repo
            print("Step 1: Checking repository...")
            if check_repo_exists(owner, repo, args.token):
                print(f"✓ Repository {owner}/{repo} already exists")
            else:
                if not create_repo(owner, repo, args.token):
                    print("✗ Failed to create repository", file=sys.stderr)
                    sys.exit(1)
                # Wait for GitHub to fully provision the repo
                print("  Waiting for repo to be ready...")
                time.sleep(3)

            # Step 2: Create issue
            if not issue_number:
                print("\nStep 2: Creating test issue...")
                issue_number = create_issue(owner, repo, args.token)
                if not issue_number:
                    print("✗ Failed to create issue", file=sys.stderr)
                    sys.exit(1)
                time.sleep(2)  # Give GitHub a moment
        else:
            if not issue_number:
                print("Error: --issue required when using --no-create", file=sys.stderr)
                sys.exit(1)

        # Step 3: Get issue ID
        print(f"\nStep 3: Getting issue #{issue_number} GraphQL ID...")
        issue_id = get_issue_id(owner, repo, issue_number, args.token)

        # Step 4: Get Copilot's actor ID
        print("\nStep 4: Finding Copilot in suggested actors...")
        copilot_id = get_copilot_actor_id(owner, repo, args.token)

        if not copilot_id:
            print("\n" + "="*60)
            print("⚠ Copilot not found in suggested actors!")
            print("="*60)
            print("""
This usually means one of:
1. Copilot coding agent is not enabled for this repo/org
2. Your token doesn't have sufficient permissions
3. Copilot coding agent isn't available for your account

To enable Copilot coding agent:
1. Go to github.com/settings/copilot (or org settings)
2. Enable "Copilot coding agent" feature
3. Make sure the repo has Copilot access

The repo and issue have been created - you can try assigning
Copilot manually via the GitHub UI to test if it works there.
""")
            sys.exit(1)

        # Step 5: Assign to Copilot
        print("\nStep 5: Assigning issue to Copilot...")
        success = assign_to_copilot(issue_id, copilot_id, args.token)

        print("\n" + "="*60)
        if success:
            print("✓ SUCCESS! Issue assigned to Copilot")
            print("="*60)
            print(f"""
Copilot will now:
1. Analyze the issue
2. Create a branch
3. Implement the solution
4. Open a PR for review

Check the issue at:
  https://github.com/{owner}/{repo}/issues/{issue_number}
""")
        else:
            print("✗ Assignment may have failed")
            print("="*60)
            sys.exit(1)

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
