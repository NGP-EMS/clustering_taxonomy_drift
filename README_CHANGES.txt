Updated scripts for hourly/weekly taxonomy setup.

Implemented:
1. Hourly actor-aware directionality guard during centroid matching.
   - Blocks reversed actor-target matches such as agent insulted customer vs customer insulted agent.
   - Applies to standard cluster matching and known anomaly cluster matching.
   - Keeps the current short field+meaning+label embedding text so existing centroids remain compatible.

2. Weekly actor-aware guard.
   - Adds actor_direction_conflict into deterministic safe-map checks.
   - Blocks NEW_CLUSTER_CANDIDATE safe-map when top standard candidate reverses actor/target direction.
   - Blocks attaching NEW_CLUSTER_CANDIDATE to an existing anomaly cluster if the nearest anomaly has reversed actor/target direction.

3. Weekly known-anomaly accumulation and promotion path.
   - Loads KNOWN_TRUE_ANOMALY rows from taxonomy_call_cluster_outputs.
   - Refreshes their existing anomaly cluster label map/counters.
   - Updates promotion counters on taxonomy_clusters.
   - Promotes recurring known anomaly clusters to standard when configured thresholds are met.
   - Updates mapper outputs cluster-wide from KNOWN_TRUE_ANOMALY to EXISTING_CLUSTER after promotion.

No change made to pipeline.py, because changing offline embedding text would create a new vector space and should only happen during the later Voyage/backfill rebuild.
