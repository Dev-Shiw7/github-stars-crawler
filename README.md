## Scaling to 500 Million Repositories
If this crawler were scaled to handle 500 million repositories:
- We would switch to a distributed job queue (e.g., Celery + Redis or Kafka) with multiple worker nodes.
- The repository table would be sharded by `repo_id` or `owner` to distribute load.
- Rate limiting would require multiple GitHub tokens from a token pool, automatically rotated.
- We would use streaming ingestion (e.g., PostgreSQL partitioned tables or a data lake sink like S3 + Athena).
- Historical snapshots would move from per-day inserts to append-only columnar storage (Parquet/BigQuery).

## Schema Evolution for Metadata Expansion
To support issues, PRs, comments, and reviews:
- Each new entity would have its own table: `issues`, `pull_requests`, `comments`, `reviews`, etc.
- Each table references `repo_id` or `pr_id`.
- Updates (e.g., new comments) are handled via upserts keyed by `id` and timestamp.
- We’d adopt an “append-only” pattern with `*_history` tables for time-series data (just like `repo_stars_history`).
- This ensures minimal rows are updated and avoids table-wide locks.
