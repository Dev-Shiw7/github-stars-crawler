-- Drop existing tables first (clean slate)
DROP TABLE IF EXISTS repo_stars_history CASCADE;
DROP TABLE IF EXISTS crawl_checkpoints CASCADE;
DROP TABLE IF EXISTS repos CASCADE;

-- Create repositories canonical table
CREATE TABLE repos (
    repo_id BIGINT PRIMARY KEY,
    github_node_id TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT,
    language TEXT,
    stargazers_count INTEGER,
    last_repo_updated_at TIMESTAMP,
    last_crawled_at TIMESTAMPTZ,
    inserted_at TIMESTAMP DEFAULT now(),
    updated_local_at TIMESTAMPTZ
);

-- Append-only daily snapshot of star counts
CREATE TABLE repo_stars_history (
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    stargazers_count INTEGER NOT NULL,
    PRIMARY KEY (repo_id, snapshot_date)
);

-- Checkpointing table for cursors
CREATE TABLE crawl_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_value TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes for performance
CREATE INDEX idx_repos_last_crawled ON repos(last_crawled_at);
CREATE INDEX idx_repos_owner_name ON repos(owner, name);
CREATE INDEX idx_repos_stars ON repos(stargazers_count DESC);
