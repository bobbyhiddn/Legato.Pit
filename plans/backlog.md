# Legato.Pit Backlog

## Planned Features

### Clarify Note/Chord Ontology in Conduct
**Priority:** High
**Context:** The ontology is:
- **Note** = Library/Knowledge entry (stored in Legato.Library)
- **Chord** = Lab repo spawned by an agent (Lab.ProjectName.Chord)

The Conduct classifier has `ProjectScope.NOTE` and `ProjectScope.CHORD` which affects repo naming (`Lab.X.Note` vs `Lab.X.Chord`). This may be incorrect - if Notes are always library entries, there should be no `Lab.X.Note` repos.

**Questions to resolve:**
- Should all Lab repos be `.Chord` suffix only?
- Is `ProjectScope` about complexity (quick vs complex) or type (library vs repo)?
- Should the classifier only output PROJECT type for things that become Chords?

**Files involved:**
- `Legato.Conduct/package/legato/classifier.py` - ProjectScope enum
- `Legato.Conduct/prompts/classifier.md` - Scope determination section
- `Legato/.github/workflows/spawn-from-pit.yml` - Repo naming logic

---

### Improve Conduct Classifier for Direct Project Requests
**Priority:** Medium
**Context:** Classifier missed a direct request to "create an agent with a dummy readme" - classified as KNOWLEDGE instead of PROJECT.

**Problem:** The classifier prompt looks for specific PROJECT indicators like "I want to build..." or technical specs. Conversational requests may not match.

**Possible solutions:**
- Add more PROJECT indicators to classifier.md prompt (e.g., "create a...", "make a...", "set up a...")
- Lower the threshold for PROJECT classification when action verbs are present
- Add a MIXED type handling that queues as both KNOWLEDGE and PROJECT

**Files involved:**
- `Legato.Conduct/prompts/classifier.md`

**Workaround:** Use "Generate Chord" button on library entries to manually queue as project.

---

### Chord Lifecycle Tracking (Agent → PR → Merge)
**Priority:** High
**Context:** Track the full lifecycle of a Chord from agent approval through PR merge.

**Lifecycle States:**
1. `queued` - Waiting for approval in Pit
2. `approved` - User approved, spawning repo
3. `spawning` - Repo being created
4. `assigned` - Issue created and assigned to Copilot
5. `working` - Copilot is working (issue open, no PR yet)
6. `pr_created` - PR opened by Copilot, ready for review
7. `pr_changes_requested` - User requested changes
8. `pr_approved` - PR approved, waiting for merge
9. `completed` - PR merged, Chord is done
10. `failed` - Something went wrong

**Database Changes:**
```sql
ALTER TABLE agent_queue ADD COLUMN chord_repo TEXT;
ALTER TABLE agent_queue ADD COLUMN issue_number INTEGER;
ALTER TABLE agent_queue ADD COLUMN issue_url TEXT;
ALTER TABLE agent_queue ADD COLUMN pr_number INTEGER;
ALTER TABLE agent_queue ADD COLUMN pr_url TEXT;
ALTER TABLE agent_queue ADD COLUMN pr_state TEXT;
ALTER TABLE agent_queue ADD COLUMN last_checked DATETIME;
```

**Implementation:**
1. **Spawn workflow** reports back to Pit with repo/issue info
2. **Status checker** polls GitHub for issue/PR updates
3. **Agents tab** shows lifecycle with progress indicator
4. **Webhook endpoint** (future) for real-time updates

**Agents Tab UI:**
- Show current state with visual indicator
- Link to issue when working
- Link to PR when created
- "Check Status" button to refresh
- "View Chord" link to repo

**API Endpoints:**
- `POST /agents/api/report-spawn` - Called by workflow after spawn
- `GET /agents/api/{queue_id}/status` - Check current status
- `POST /agents/api/{queue_id}/refresh` - Force status refresh from GitHub
- `POST /agents/webhook` - GitHub webhook receiver (future)

**Files involved:**
- `agents.py` - New endpoints, status checking
- `agents.html` - Lifecycle UI
- `database.py` - Schema changes
- `spawn-from-pit.yml` - Report back to Pit after spawn

---

### Retry Failed Queue-to-Pit from Dashboard
**Priority:** Medium
**Context:** When Conduct's queue-to-pit step fails, user currently must re-run the entire workflow.

**Proposed solution:**
- Add "Retry Queue" button on failed workflow runs in dashboard
- API endpoint fetches `routing-decisions` artifact from the failed GitHub workflow
- Parse routing.json for PROJECT items
- Insert directly into agent_queue table

**Files involved:**
- `dashboard.py` - Add retry button and API endpoint
- `agents.py` - Reuse queue insertion logic

**Workaround:** Re-run failed job from GitHub Actions UI

---

## Completed

- [x] Agent queue system with Pit approval gateway
- [x] Library entry editor with GitHub commit
- [x] Auto-sync library on startup
- [x] Dedicated Agents tab
- [x] Generate Chord button on library entries (manual project queue)
