-- Grant permissions to crawler user (for GitHub Actions)
GRANT ALL PRIVILEGES ON DATABASE crawlerdb TO crawler;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO crawler;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO crawler;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO crawler;



-- Drop existing tables
DROP TABLE IF EXISTS repo_stars_history CASCADE;
DROP TABLE IF EXISTS crawl_checkpoints CASCADE;
DROP TABLE IF EXISTS repos CASCADE;

-- Create repositories canonical table
CREATE TABLE IF NOT EXISTS repos (
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
CREATE TABLE IF NOT EXISTS repo_stars_history (
    repo_id BIGINT REFERENCES repos(repo_id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    stargazers_count INTEGER NOT NULL,
    PRIMARY KEY (repo_id, snapshot_date)
);

-- Checkpointing table for cursors
CREATE TABLE IF NOT EXISTS crawl_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_value TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_repos_last_crawled ON repos(last_crawled_at);
CREATE INDEX IF NOT EXISTS idx_repos_owner_name ON repos(owner, name);
CREATE INDEX IF NOT EXISTS idx_repos_stars ON repos(stargazers_count DESC);