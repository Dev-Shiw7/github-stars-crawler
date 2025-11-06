# Scaling to 500 Million Repositories

## Current Architecture Limitations

At 100,000 repositories, our single-threaded GraphQL crawler works well (~10 repos/sec). However, scaling to 500 million repositories (5000x increase) requires fundamental architectural changes.

## Proposed Architecture for 500M Scale

### 1. Distributed Crawling System

**Problem**: Single crawler would take ~580 days to fetch 500M repos at current rate.

**Solution**: Distributed worker pool
```
┌─────────────┐
│ Job Queue   │ ← Master scheduler assigns work
│ (RabbitMQ)  │
└──────┬──────┘
       │
   ┌───┴────┬────────┬────────┐
   ▼        ▼        ▼        ▼
[Worker1][Worker2][Worker3]...[Worker100]
   │        │        │        │
   └────────┴────────┴────────┘
              ▼
      ┌──────────────┐
      │ Database     │
      │ Cluster      │
      └──────────────┘
```

**Implementation**:
- 100+ worker nodes (Kubernetes pods or EC2 instances)
- Each worker processes a shard of repositories
- Message queue (RabbitMQ/AWS SQS) distributes work
- Target: 1000 repos/sec aggregate throughput = ~6 days for full crawl

### 2. Database Sharding Strategy

**Problem**: Single PostgreSQL instance can't handle 500M rows efficiently.

**Solution**: Horizontal sharding by `repo_id`

```sql
-- Create 10 database shards
CREATE TABLE repos_shard_0 PARTITION OF repos 
  FOR VALUES WITH (MODULUS 10, REMAINDER 0);
  
CREATE TABLE repos_shard_1 PARTITION OF repos 
  FOR VALUES WITH (MODULUS 10, REMAINDER 1);
-- ... etc to shard_9
```

**Benefits**:
- Each shard handles 50M repos
- Parallel writes across shards
- Independent backup/maintenance windows
- Can scale to 100+ shards if needed

**Alternative**: Use Cassandra or ScyllaDB for write-heavy workloads with automatic sharding.

### 3. API Token Pool & Rate Limit Management

**Problem**: GitHub GraphQL API limits to 5,000 points/hour per token.

**Solution**: Token rotation pool
- Acquire 20-50 GitHub Apps or organization tokens
- Rotate requests across token pool
- Centralized rate limit tracker (Redis)
- Effective rate: 100,000-250,000 points/hour

```python
class TokenPool:
    def get_available_token(self):
        # Returns token with highest remaining quota
        return redis.zrevrange('token_quota', 0, 0)[0]
    
    def update_quota(self, token, remaining):
        redis.zadd('token_quota', {token: remaining})
```

### 4. Incremental Crawling Strategy

**Problem**: Can't re-crawl 500M repos daily - waste of API quota.

**Solution**: Tiered update frequency

```
Priority Tier 1 (Hot): Updated in last 7 days  → Crawl daily   (5M repos)
Priority Tier 2 (Warm): Updated 7-30 days ago  → Crawl weekly  (20M repos)  
Priority Tier 3 (Cold): Updated 30-90 days ago → Crawl monthly (75M repos)
Priority Tier 4 (Archived): No updates >90 days → Crawl quarterly (400M repos)
```

**Implementation**:
```sql
-- Queue jobs based on staleness
SELECT repo_id FROM repos 
WHERE last_crawled_at < NOW() - INTERVAL '1 day'
  AND last_repo_updated_at > NOW() - INTERVAL '7 days'
ORDER BY stargazers_count DESC  -- Prioritize popular repos
LIMIT 100000;
```

### 5. Caching Layer

**Problem**: Repeated reads of same repo metadata waste DB resources.

**Solution**: Multi-tier caching
- **L1 Cache**: Redis for top 100K most-accessed repos (hot data)
- **L2 Cache**: Worker-local LRU cache (reduces network calls)
- **CDN**: Serve read-only API from Cloudflare Workers

```
User Request → CDN Cache (Cloudflare) → Redis → PostgreSQL
              (99% hits)   (0.9% hits)   (0.1% hits)
```

### 6. Data Pipeline Architecture

**Problem**: Need to process, validate, and enrich 500M records.

**Solution**: Stream processing pipeline

```
GitHub API → Kafka → Stream Processor → Database
                    ↓
                  [Validation]
                  [Deduplication]
                  [Enrichment]
```

**Technologies**:
- Apache Kafka for event streaming
- Apache Flink/Spark for stream processing
- Debezium for Change Data Capture (CDC)

### 7. Storage Optimization

**Problem**: 500M repos × ~2KB/row = ~1TB of data (with indexes: ~3TB).

**Solutions**:
- **Compression**: Enable PostgreSQL table compression (TOAST)
- **Archival**: Move repos inactive >1 year to cold storage (S3)
- **Denormalization**: Store frequently accessed fields redundantly to avoid joins
- **Columnar Storage**: Use Parquet files for analytics workloads

```sql
-- Archive old data
CREATE TABLE repos_archive (LIKE repos) 
PARTITION BY RANGE (last_crawled_at);

-- Move to archive partition
INSERT INTO repos_archive 
SELECT * FROM repos 
WHERE last_crawled_at < NOW() - INTERVAL '1 year';
```

### 8. Monitoring & Observability

**Critical metrics**:
- Crawl rate (repos/sec per worker)
- API quota consumption per token
- Database write throughput (rows/sec)
- Error rates by error type
- Queue depth and processing lag

**Tools**:
- Prometheus + Grafana for metrics
- ELK stack for log aggregation
- PagerDuty for alerting
- Custom dashboard showing:
  * Total repos crawled
  * Estimated completion time
  * Per-worker health status
  * Database shard utilization

### 9. Cost Optimization

**Estimated monthly costs at 500M scale**:
- Compute (100 workers): ~$2,000/month (spot instances)
- Database (10 shards): ~$5,000/month (RDS multi-AZ)
- Network/API: ~$500/month
- Storage (3TB): ~$300/month
- **Total: ~$8,000/month**

**Optimizations**:
- Use spot instances for workers (70% cost savings)
- Compress data (50% storage savings)
- Use Aurora Serverless for variable workloads
- Cache aggressively to reduce DB load

### 10. Fault Tolerance & Recovery

**Challenges**:
- Worker failures mid-crawl
- Database connectivity issues
- API rate limit exceeded
- Partial data corruption

**Solutions**:
- Checkpoint every 1000 repos (already implemented ✓)
- Dead letter queue for failed jobs
- Automatic worker restart (Kubernetes liveness probes)
- Database transaction logs for point-in-time recovery
- Multi-region replication for disaster recovery

## Migration Path: 100K → 500M

### Phase 1: Vertical Scaling (100K → 1M repos)
- Optimize existing code
- Add connection pooling (PgBouncer)
- Increase database resources

### Phase 2: Horizontal Scaling (1M → 10M repos)
- Deploy 10 worker instances
- Implement job queue (RabbitMQ)
- Add Redis caching

### Phase 3: Sharding (10M → 100M repos)
- Shard database into 10 partitions
- Deploy token rotation pool
- Implement incremental crawling

### Phase 4: Enterprise Scale (100M → 500M repos)
- Scale to 100+ workers
- Add stream processing pipeline
- Implement cold storage archival
- Multi-region deployment

## Performance Projections

| Scale | Workers | Time to Crawl | Daily Update Capacity |
|-------|---------|---------------|----------------------|
| 100K  | 1       | 2.7 hours     | 880K repos/day       |
| 1M    | 10      | 2.7 hours     | 8.8M repos/day       |
| 10M   | 50      | 5.5 hours     | 44M repos/day        |
| 100M  | 100     | 27 hours      | 88M repos/day        |
| 500M  | 200     | 6 days        | 176M repos/day       |

With incremental crawling, daily updates require only:
- Tier 1 (5M): 1 hour
- Tier 2 (20M): 4 hours  
- Total: **~5 hours/day** for keeping 500M repos fresh

## Conclusion

Scaling from 100K to 500M repositories requires:
1. **Architecture**: Move from single-node to distributed system
2. **Database**: Shard data across multiple instances
3. **API**: Rotate multiple tokens to multiply rate limits
4. **Strategy**: Incremental updates instead of full re-crawls
5. **Infrastructure**: Robust monitoring and fault tolerance

The core crawler code is solid - the main changes are infrastructure and orchestration, not algorithm redesign.