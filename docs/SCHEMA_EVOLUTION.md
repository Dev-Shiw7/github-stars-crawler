# Schema Evolution: Adding Metadata (Issues, PRs, Comments, Reviews)

## Design Principles

When evolving the schema to capture additional metadata like issues, pull requests, comments, and reviews, we must balance:

1. **Efficient Updates**: Minimize rows affected when data changes
2. **Historical Tracking**: Preserve time-series data for analytics
3. **Query Performance**: Optimize for common access patterns
4. **Storage Efficiency**: Avoid data duplication

## Current Schema Recap

```sql
repos (
    repo_id BIGINT PRIMARY KEY,
    -- repository metadata
)

repo_stars_history (
    repo_id BIGINT,
    snapshot_date DATE,
    stargazers_count INTEGER,
    PRIMARY KEY (repo_id, snapshot_date)
)
```

**Key insight**: We separate **mutable canonical data** (repos table) from **immutable time-series snapshots** (history table). This pattern applies to all new metadata.

## Proposed Schema Extensions

### 1. Issues Table

```sql
-- Canonical issue data (mutable)
CREATE TABLE issues (
    issue_id BIGINT PRIMARY KEY,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    issue_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    state TEXT NOT NULL,  -- 'open', 'closed'
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    author_id BIGINT,
    author_login TEXT,
    -- Aggregate counts (updated efficiently)
    comments_count INTEGER DEFAULT 0,
    reactions_count INTEGER DEFAULT 0,
    -- Metadata
    labels JSONB,  -- Array of label names
    assignees JSONB,  -- Array of assignee logins
    last_crawled_at TIMESTAMPTZ,
    UNIQUE(repo_id, issue_number)
);

CREATE INDEX idx_issues_repo_id ON issues(repo_id);
CREATE INDEX idx_issues_state ON issues(state) WHERE state = 'open';
CREATE INDEX idx_issues_updated_at ON issues(updated_at DESC);
```

**Efficient update pattern**: When an issue gains new comments:
```sql
-- Only updates 1 row (the issue itself)
UPDATE issues 
SET comments_count = comments_count + 1,
    updated_at = NOW(),
    last_crawled_at = NOW()
WHERE issue_id = $1;
```

### 2. Issue Comments Table (Append-Only)

```sql
-- Immutable comment records
CREATE TABLE issue_comments (
    comment_id BIGINT PRIMARY KEY,
    issue_id BIGINT REFERENCES issues(issue_id) ON DELETE CASCADE,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    author_id BIGINT,
    author_login TEXT,
    body TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    reactions_count INTEGER DEFAULT 0,
    last_crawled_at TIMESTAMPTZ
);

CREATE INDEX idx_issue_comments_issue_id ON issue_comments(issue_id);
CREATE INDEX idx_issue_comments_created_at ON issue_comments(created_at DESC);
```

**Update pattern**: When crawling an issue that had 10 comments yesterday and 20 today:
```sql
-- Step 1: Fetch only NEW comments (after last crawl)
SELECT * FROM issue_comments 
WHERE issue_id = $1 AND created_at > $last_crawl_timestamp;

-- Step 2: Insert only new comments (10 INSERTs, not 20)
INSERT INTO issue_comments (comment_id, issue_id, ...)
VALUES ($1, $2, ...) ON CONFLICT (comment_id) DO UPDATE SET ...;

-- Step 3: Update aggregate count (1 UPDATE)
UPDATE issues SET comments_count = 20 WHERE issue_id = $1;
```

**Result**: 11 total operations (10 inserts + 1 update) instead of updating all 20 rows.

### 3. Pull Requests Table

```sql
CREATE TABLE pull_requests (
    pr_id BIGINT PRIMARY KEY,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    pr_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    state TEXT NOT NULL,  -- 'open', 'closed', 'merged'
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    merged_at TIMESTAMPTZ,
    author_id BIGINT,
    author_login TEXT,
    -- PR-specific fields
    head_ref TEXT,  -- source branch
    base_ref TEXT,  -- target branch
    head_sha TEXT,  -- commit SHA
    -- Aggregate counts
    commits_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    review_comments_count INTEGER DEFAULT 0,
    changed_files_count INTEGER DEFAULT 0,
    additions_count INTEGER DEFAULT 0,
    deletions_count INTEGER DEFAULT 0,
    -- Metadata
    labels JSONB,
    reviewers JSONB,
    requested_reviewers JSONB,
    last_crawled_at TIMESTAMPTZ,
    UNIQUE(repo_id, pr_number)
);

CREATE INDEX idx_prs_repo_id ON pull_requests(repo_id);
CREATE INDEX idx_prs_state ON pull_requests(state);
CREATE INDEX idx_prs_merged_at ON pull_requests(merged_at DESC) WHERE merged_at IS NOT NULL;
```

### 4. PR Commits Table

```sql
CREATE TABLE pr_commits (
    commit_sha TEXT PRIMARY KEY,
    pr_id BIGINT REFERENCES pull_requests(pr_id) ON DELETE CASCADE,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    author_login TEXT,
    author_date TIMESTAMPTZ,
    committer_login TEXT,
    committer_date TIMESTAMPTZ,
    message TEXT,
    additions INTEGER DEFAULT 0,
    deletions INTEGER DEFAULT 0,
    total_changes INTEGER DEFAULT 0,
    last_crawled_at TIMESTAMPTZ
);

CREATE INDEX idx_pr_commits_pr_id ON pr_commits(pr_id);
CREATE INDEX idx_pr_commits_author_date ON pr_commits(author_date DESC);
```

**Efficient update**: When PR gains 5 new commits:
```sql
-- Only insert the 5 new commits (not all commits)
INSERT INTO pr_commits (commit_sha, pr_id, ...)
SELECT unnest($sha_array), $pr_id, ...
ON CONFLICT (commit_sha) DO NOTHING;

-- Update aggregate (1 UPDATE)
UPDATE pull_requests 
SET commits_count = commits_count + 5 
WHERE pr_id = $1;
```

### 5. PR Review Comments Table

```sql
CREATE TABLE pr_review_comments (
    comment_id BIGINT PRIMARY KEY,
    pr_id BIGINT REFERENCES pull_requests(pr_id) ON DELETE CASCADE,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    review_id BIGINT,  -- nullable if standalone comment
    author_id BIGINT,
    author_login TEXT,
    body TEXT,
    path TEXT,  -- file path
    position INTEGER,  -- line number
    commit_sha TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    reactions_count INTEGER DEFAULT 0,
    last_crawled_at TIMESTAMPTZ
);

CREATE INDEX idx_pr_review_comments_pr_id ON pr_review_comments(pr_id);
CREATE INDEX idx_pr_review_comments_review_id ON pr_review_comments(review_id);
```

### 6. PR Reviews Table

```sql
CREATE TABLE pr_reviews (
    review_id BIGINT PRIMARY KEY,
    pr_id BIGINT REFERENCES pull_requests(pr_id) ON DELETE CASCADE,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    reviewer_id BIGINT,
    reviewer_login TEXT,
    state TEXT NOT NULL,  -- 'PENDING', 'COMMENTED', 'APPROVED', 'CHANGES_REQUESTED'
    body TEXT,
    commit_sha TEXT,  -- commit being reviewed
    submitted_at TIMESTAMPTZ,
    comments_count INTEGER DEFAULT 0,
    last_crawled_at TIMESTAMPTZ
);

CREATE INDEX idx_pr_reviews_pr_id ON pr_reviews(pr_id);
CREATE INDEX idx_pr_reviews_state ON pr_reviews(state);
```

### 7. CI Checks Table

```sql
CREATE TABLE ci_checks (
    check_id BIGINT PRIMARY KEY,
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    pr_id BIGINT REFERENCES pull_requests(pr_id) ON DELETE SET NULL,
    commit_sha TEXT NOT NULL,
    name TEXT NOT NULL,  -- e.g., "CI/Travis", "Build"
    status TEXT NOT NULL,  -- 'queued', 'in_progress', 'completed'
    conclusion TEXT,  -- 'success', 'failure', 'cancelled', 'neutral'
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_seconds INTEGER,  -- computed
    external_id TEXT,  -- GitHub check run ID
    details_url TEXT,
    last_crawled_at TIMESTAMPTZ
);

CREATE INDEX idx_ci_checks_repo_id ON ci_checks(repo_id);
CREATE INDEX idx_ci_checks_pr_id ON ci_checks(pr_id);
CREATE INDEX idx_ci_checks_commit_sha ON ci_checks(commit_sha);
CREATE INDEX idx_ci_checks_conclusion ON ci_checks(conclusion);
```

## Efficient Update Strategies

### Strategy 1: Incremental Crawling with Timestamps

**Problem**: PR had 10 comments yesterday, 20 today. Don't want to re-fetch all 20.

**Solution**: Track `last_crawled_at` and use GitHub's `since` parameter:

```python
def crawl_pr_comments(pr_id, last_crawl_timestamp):
    # Only fetch comments created/updated since last crawl
    comments = github.get_pr_comments(
        pr_id, 
        since=last_crawl_timestamp
    )
    
    for comment in comments:
        upsert_comment(comment)  # ON CONFLICT DO UPDATE
    
    # Update aggregate count (1 UPDATE, not 20)
    update_pr_comment_count(pr_id, total_count=20)
```

### Strategy 2: Separate Mutable vs Immutable Data

**Mutable** (update in-place):
- Issue/PR state, title, labels
- Aggregate counts
- Last updated timestamps

**Immutable** (append-only):
- Comments (never change after creation)
- Commits (immutable by nature)
- Review submissions

### Strategy 3: Batch Upserts for Efficiency

```python
def upsert_comments_batch(comments):
    values = [(c['id'], c['issue_id'], c['body'], ...) for c in comments]
    
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO issue_comments 
            (comment_id, issue_id, body, created_at, updated_at)
            VALUES %s
            ON CONFLICT (comment_id) DO UPDATE SET
                body = EXCLUDED.body,
                updated_at = EXCLUDED.updated_at
        """, values, page_size=1000)
```

**Result**: 1000 upserts in a single round-trip instead of 1000 separate queries.

### Strategy 4: Materialized Views for Analytics

For expensive queries (e.g., "repos with most PR activity this week"):

```sql
CREATE MATERIALIZED VIEW repo_pr_activity_weekly AS
SELECT 
    repo_id,
    COUNT(*) as prs_opened,
    COUNT(*) FILTER (WHERE merged_at IS NOT NULL) as prs_merged,
    AVG(comments_count) as avg_comments
FROM pull_requests
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY repo_id;

CREATE UNIQUE INDEX idx_repo_pr_activity_repo_id 
ON repo_pr_activity_weekly(repo_id);

-- Refresh daily (or on-demand)
REFRESH MATERIALIZED VIEW CONCURRENTLY repo_pr_activity_weekly;
```

## Example: Handling a PR with Growing Comments

### Scenario
- **Day 1**: PR created with 10 comments
- **Day 2**: PR now has 20 comments (10 new)

### Inefficient Approach (❌)
```sql
-- Delete all comments
DELETE FROM pr_review_comments WHERE pr_id = $1;

-- Re-insert all 20 comments
INSERT INTO pr_review_comments (...) VALUES (...);  -- 20 INSERTs
```
**Cost**: 1 DELETE + 20 INSERTs = **21 operations, 20 rows affected**

### Efficient Approach (✅)
```sql
-- Fetch only new comments (GitHub API: since=last_crawl_at)
-- Insert only the 10 new comments
INSERT INTO pr_review_comments (comment_id, pr_id, ...)
VALUES ($1, $2, ...) -- Repeat for 10 new comments
ON CONFLICT (comment_id) DO UPDATE SET updated_at = EXCLUDED.updated_at;

-- Update aggregate count
UPDATE pull_requests 
SET review_comments_count = 20,
    last_crawled_at = NOW()
WHERE pr_id = $1;
```
**Cost**: 10 INSERTs + 1 UPDATE = **11 operations, 11 rows affected**

### Even More Efficient (Batch)
```python
# Batch all 10 inserts into 1 query
execute_values(cur, """
    INSERT INTO pr_review_comments (comment_id, pr_id, body, created_at)
    VALUES %s
    ON CONFLICT (comment_id) DO NOTHING
""", new_comments_data)
```
**Cost**: 1 batch INSERT + 1 UPDATE = **2 operations, 11 rows affected**

## Performance Considerations

### Partitioning Large Tables

For tables with billions of rows (e.g., comments):

```sql
CREATE TABLE issue_comments (
    comment_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
    ...
) PARTITION BY RANGE (created_at);

-- Create monthly partitions
CREATE TABLE issue_comments_2024_11 
PARTITION OF issue_comments
FOR VALUES FROM ('2024-11-01') TO ('2024-12-01');

CREATE TABLE issue_comments_2024_12 
PARTITION OF issue_comments
FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');
```

**Benefits**:
- Queries on recent data only scan recent partitions
- Can drop old partitions for archival
- Parallel queries across partitions

### Indexes for Common Queries

```sql
-- Query: "Show all open PRs for a repo"
CREATE INDEX idx_prs_repo_state ON pull_requests(repo_id, state) 
WHERE state = 'open';

-- Query: "Show recent comments on a PR"
CREATE INDEX idx_pr_comments_pr_created 
ON pr_review_comments(pr_id, created_at DESC);

-- Query: "Find PRs with failed CI"
CREATE INDEX idx_ci_checks_conclusion 
ON ci_checks(pr_id, conclusion) 
WHERE conclusion = 'failure';
```

## Migration Path

### Phase 1: Add Issues
1. Create `issues` table
2. Create `issue_comments` table
3. Backfill top 10K most active repos
4. Add incremental crawler for issues

### Phase 2: Add Pull Requests
1. Create `pull_requests` table
2. Create `pr_commits`, `pr_review_comments`, `pr_reviews` tables
3. Backfill recent PRs (last 6 months)

### Phase 3: Add CI Checks
1. Create `ci_checks` table
2. Integrate with GitHub Checks API
3. Track check runs for active PRs

### Phase 4: Optimize
1. Add materialized views for analytics
2. Partition large tables by date
3. Archive old data to cold storage

## Storage Estimates

For 500M repositories:

| Entity | Avg per Repo | Total Records | Storage per Row | Total Storage |
|--------|--------------|---------------|-----------------|---------------|
| Issues | 20 | 10B | 1KB | 10TB |
| Issue Comments | 100 | 50B | 500B | 25TB |
| Pull Requests | 50 | 25B | 1.5KB | 37.5TB |
| PR Commits | 200 | 100B | 500B | 50TB |
| PR Comments | 100 | 50B | 500B | 25TB |
| Reviews | 30 | 15B | 300B | 4.5TB |
| CI Checks | 100 | 50B | 400B | 20TB |
| **TOTAL** | | **300B records** | | **~172TB** |

**With compression and indexes**: ~250-300TB total

**Cost (AWS RDS Aurora)**:
- Storage: ~$30,000/month (300TB × $0.10/GB)
- IOPS: ~$10,000/month
- **Total: ~$40,000/month** for full dataset

**Optimization**: Archive data older than 2 years → reduce to ~$10,000/month

## Conclusion

Key principles for schema evolution:

1. **Separate canonical vs historical data** - updates touch minimal rows
2. **Use aggregate counters** - avoid expensive COUNT(*) queries
3. **Batch operations** - reduce network round-trips
4. **Incremental crawling** - only fetch what changed
5. **Partition by time** - manage growth efficiently
6. **Index strategically** - optimize for query patterns, not writes

The schema design scales to billions of records while maintaining efficient updates through careful separation of mutable aggregates from immutable event data.