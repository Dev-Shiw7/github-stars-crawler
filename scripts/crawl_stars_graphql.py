#!/usr/bin/env python3
"""
GraphQL-based GitHub stars crawler - OPTIMIZED VERSION
"""

import os
import sys
import time
import argparse
import requests
import datetime
import random
import psycopg2
from psycopg2.extras import execute_values
from datetime import timezone

GITHUB_GRAPHQL = "https://api.github.com/graphql"

GRAPHQL_QUERY = """
query($q:String!, $first:Int!, $after:String) {
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
  search(query: $q, type: REPOSITORY, first: $first, after: $after) {
    repositoryCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Repository {
        id
        databaseId
        name
        owner { login }
        url
        description
        createdAt
        updatedAt
        pushedAt
        stargazerCount
        primaryLanguage { name }
        isArchived
        isDisabled
        defaultBranchRef { name }
      }
    }
  }
}
"""

SEARCH_QUERY = "stars:>0"

def now_utc():
    return datetime.datetime.now(timezone.utc)

def graphql_post(token, query, variables):
    headers = {
        "Authorization": f"bearer {token}",
        "Accept": "application/vnd.github.v4+json",
        "User-Agent": "github-stars-crawler"
    }
    r = requests.post(GITHUB_GRAPHQL, json={"query": query, "variables": variables}, 
                     headers=headers, timeout=60)
    return r

def get_pg_conn(dsn):
    return psycopg2.connect(dsn)

def read_checkpoint(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT checkpoint_value FROM crawl_checkpoints WHERE checkpoint_key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None

def write_checkpoint(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO crawl_checkpoints (checkpoint_key, checkpoint_value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (checkpoint_key) DO UPDATE SET
              checkpoint_value = EXCLUDED.checkpoint_value,
              updated_at = EXCLUDED.updated_at
        """, (key, value))
    conn.commit()

def upsert_repos_and_snapshots(conn, rows):
    """Batch upsert repos and snapshots"""
    if not rows:
        return
    
    today = datetime.date.today()
    now = now_utc()
    
    with conn.cursor() as cur:
        repo_values = []
        snapshot_values = []
        
        for r in rows:
            repo_id = r["repo_id"]
            repo_values.append((
                repo_id,
                r["github_node_id"],
                r["owner"],
                r["name"],
                r["full_name"],
                r["url"],
                r.get("description"),
                r.get("language"),
                r.get("stargazerCount", 0),
                r.get("updatedAt"),
                now,
                now
            ))
            snapshot_values.append((repo_id, today, r.get("stargazerCount", 0)))

        # Batch insert repos
        execute_values(cur, """
            INSERT INTO repos (repo_id, github_node_id, owner, name, full_name, url, 
                             description, language, stargazers_count, last_repo_updated_at, 
                             last_crawled_at, updated_local_at)
            VALUES %s
            ON CONFLICT (repo_id) DO UPDATE SET
              github_node_id = EXCLUDED.github_node_id,
              owner = EXCLUDED.owner,
              name = EXCLUDED.name,
              full_name = EXCLUDED.full_name,
              url = EXCLUDED.url,
              description = EXCLUDED.description,
              language = EXCLUDED.language,
              stargazers_count = EXCLUDED.stargazers_count,
              last_repo_updated_at = EXCLUDED.last_repo_updated_at,
              last_crawled_at = EXCLUDED.last_crawled_at,
              updated_local_at = EXCLUDED.updated_local_at
        """, repo_values, page_size=200)

        # Batch insert snapshots
        execute_values(cur, """
            INSERT INTO repo_stars_history (repo_id, snapshot_date, stargazers_count)
            VALUES %s
            ON CONFLICT (repo_id, snapshot_date) DO UPDATE SET
              stargazers_count = EXCLUDED.stargazers_count
        """, snapshot_values, page_size=200)

    conn.commit()

def safe_sleep(seconds):
    time.sleep(seconds + random.random() * 0.3)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=100000)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--pg-dsn", type=str, required=True)
    parser.add_argument("--checkpoint-key", type=str, default="global_search_cursor")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN missing in environment")

    conn = get_pg_conn(args.pg_dsn)
    fetched = 0
    page_size = min(100, max(10, args.page_size))
    after = read_checkpoint(conn, args.checkpoint_key)
    backoff_base = 1.0
    
    start_time = time.time()
    print(f"Starting crawl: target={args.total}, page_size={page_size}")

    while fetched < args.total:
        variables = {"q": SEARCH_QUERY, "first": page_size, "after": after}
        
        try:
            resp = graphql_post(token, GRAPHQL_QUERY, variables)
        except Exception as e:
            print(f"Network error: {e}")
            safe_sleep(backoff_base)
            backoff_base = min(backoff_base * 2, 60)
            continue

        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (502, 503, 504, 429):
                safe_sleep(backoff_base)
                backoff_base = min(backoff_base * 2, 60)
                continue
            time.sleep(2)
            continue

        data = resp.json()
        if data.get("errors"):
            print(f"GraphQL errors: {data['errors']}")
            safe_sleep(2)
            continue

        # Check rate limit
        rl = data.get("data", {}).get("rateLimit", {})
        remaining = rl.get("remaining", 5000)
        
        if remaining < 500:
            resetAt = rl.get("resetAt")
            try:
                reset_ts = datetime.datetime.fromisoformat(resetAt.replace("Z", "+00:00"))
                sleep_seconds = (reset_ts - datetime.datetime.now(datetime.timezone.utc)).total_seconds() + 5
                if sleep_seconds > 0:
                    print(f"Rate limit low ({remaining}), sleeping {int(sleep_seconds)}s")
                    time.sleep(max(0, sleep_seconds))
            except Exception:
                safe_sleep(30)

        search = data.get("data", {}).get("search", {})
        nodes = search.get("nodes", [])
        pageInfo = search.get("pageInfo", {})
        has_next = pageInfo.get("hasNextPage", False)
        endCursor = pageInfo.get("endCursor")

        # Parse nodes
        rows = []
        for n in nodes:
            if not n:
                continue
            
            owner = (n.get("owner") or {}).get("login")
            name = n.get("name")
            dbid = n.get("databaseId")
            
            if not dbid:
                print(f"Warning: Missing databaseId for {n.get('id')}, skipping")
                continue
                
            rows.append({
                "repo_id": int(dbid),
                "github_node_id": n.get("id"),
                "owner": owner,
                "name": name,
                "full_name": f"{owner}/{name}",
                "url": n.get("url"),
                "description": n.get("description"),
                "language": (n.get("primaryLanguage") or {}).get("name"),
                "stargazerCount": n.get("stargazerCount") or 0,
                "updatedAt": n.get("updatedAt")
            })

        if rows:
            upsert_repos_and_snapshots(conn, rows)
            fetched += len(rows)
            elapsed = time.time() - start_time
            rate = fetched / elapsed if elapsed > 0 else 0
            eta = (args.total - fetched) / rate if rate > 0 else 0
            print(f"Progress: {fetched}/{args.total} repos | Rate: {rate:.1f}/s | ETA: {eta/60:.1f}min")
        else:
            print("No nodes returned")
            break

        # Save checkpoint
        if endCursor:
            write_checkpoint(conn, args.checkpoint_key, endCursor)
            after = endCursor

        backoff_base = 1.0
        
        if not has_next:
            print("No more pages")
            break

        # OPTIMIZED: Only sleep if rate limit is getting low
        if remaining < 1000:
            safe_sleep(0.5)
        # Otherwise continue immediately for maximum speed

    conn.close()
    total_time = time.time() - start_time
    print(f"\n✓ Crawl finished: {fetched} repos in {total_time/60:.2f} minutes ({fetched/total_time:.1f} repos/sec)")

if __name__ == "__main__":
    main()