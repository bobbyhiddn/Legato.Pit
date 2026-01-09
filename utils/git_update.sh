#!/bin/bash

# Update all submodules to their latest versions
git submodule update --remote --merge

# Check if there are any changes
if ! git diff --quiet; then
    echo "Changes detected in submodules. Committing..."
    git add .
    git commit -m "Update submodules to latest versions"
    echo "Changes committed successfully."
else
    echo "No changes in submodules."
fi
