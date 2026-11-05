# App wakeup audit — November 4, 2026

Read-only live investigation around 14:45–15:00 Stockholm. Evidence is in the
ignored `.azure/wakeup-audit-20261104/` directory. Queries cover roughly the prior
six hours; times in raw evidence are UTC. No cloud configuration, queues, source
records or application code were modified by this investigation.

## Current state

At the replica/queue snapshot, Vision, clustering and indexing all had zero
replicas. Every photostore work/deferred/expiry/dead-letter queue reported zero
messages. The tag, index-maintenance and wave-stage pending partitions were
empty. Core had one replica; the remaining seven app roles had zero. All apps
had minReplicas=0 and successful provisioning. Queue-count snapshots are
approximate and do not prove absence of future producer traffic.

## Findings

### Clustering: upload-expiry checks repeatedly start the expensive app

System logs show 13 activations triggered solely by the upload-expiry scaler
between 11:40:59 and 13:36:00 Stockholm, plus one activation triggered by both
expiry and clustering. These are distinct from deployment starts.

Per-process telemetry identifies 13 lifetimes whose only jobs were expiry
checks, with zero cluster-wave summaries. Across these 13 lifetimes, 34 expiry
messages were started, 21 expiry deferrals occurred, and two sessions emitted
actual expiry-completion events. Another mixed clustering/expiry process
expired a third session. These counts describe observed telemetry, not unique
sessions per delivery.

Examples of pure expiry-only lifetimes completed one check with 0.037–0.493
seconds processing and roughly 34 seconds idle. The service allocates 2 vCPU
and 4 GiB. The checks are intentional safety work, but assigning them to the
clustering app creates disproportionate startup/idle overhead. The expiry
consumer also handles terminal parents by driving handoff repair; absence of
an expiry-completion event alone does not prove a delivery was useless.

`upload_session_lease.expire` reschedules an active session's check, retries
finalization conflicts after 30 seconds, and checks closed parents for handoff
repair. Checks are not cancelled when an upload closes. A small dedicated
consumer and safely cancellable checks would reduce this overhead.

### Indexing: confirmed repeated no-progress deliveries

Between 14:34:04 and 14:44:10 Stockholm, the current indexer revision emitted
132 `index_message_outcome=retry` records: 95 vector and 37 lexical. These are
retry outcomes, not 132 separate replica starts or successful index builds.
Logs repeatedly retain the same queue deliveries, including dequeue counts
above the displayed maximum of five. At 14:44:12 and 14:44:18 the replacement
vector and lexical intents completed; stale deliveries were then classified
obsolete and the replica scaled to zero at 14:44:57.

The corresponding worker lifetime recorded about 664 seconds elapsed, 502
seconds idle and 162 seconds processing. Some actual rebuilding occurred;
calling the entire activation empty would be inaccurate.

The dispatcher has a concrete path that can sustain this churn: an early
receipt cannot claim a future `notBefore` intent; `notify` returns false while
an existing wake reservation is valid; the dispatcher returns `retry`; the
queue handler changes visibility to ten seconds. The main queue scaler counts
all messages, including invisible messages, so retained early receipts keep
demand alive while a delayed replacement already exists. Outcome telemetry
omits the retry reason, so the exact branch for all 132 records cannot be
proven from these outcome records alone. Fixing this should preserve durable
continuation before acknowledging redundant receipts.

Over six hours, KEDA recorded 23 deferred-queue indexer activations and three
HTTP activations, alongside real wave and maintenance completions. Older
barrier retries and deployment changes are included in that historical window.
The three-hour HTTP sample contained five index-status requests and six
build requests; it did not show the old continuous `/api/jobs/status` storm.
Indexer API and worker remain sidecars in the same 2-vCPU/4-GiB replica, so a
status-only request can still start the full allocation.

### Vision: maintenance-only starts warm unrelated models

Seven six-hour activations were triggered by the deferred Vision queue, with
six by the main queue. Some starts have no per-image or Vision-wave work but
do execute maintenance; zero photo processing is not equivalent to zero useful
work.

A concrete short-message example at 14:32:53–14:33:54 Stockholm on the current
revision warmed the full model set for 17.36 seconds, then handled one message
in 3.70 seconds with no image/wave summary. Another current-revision maintenance
activation at 14:39:45–14:42:53 warmed models for 11.77 seconds and handled a
message for 119.20 seconds, again without image processing. The latter cannot
be described as an empty wake from the available telemetry.

Both starts warm face detection, face embedding, face landmarks, CLIP, ONNX
CLIP, vocabulary, geo index and geocoder regardless of the queued task. Lazy
initialization by message type would avoid unrelated model startup for tag
maintenance or stale deliveries. Keep main-queue `all` scaling: it protects
long-running invisible in-flight Vision work. Deferred queues already use
visible-only demand and all three workers now have a 60-second cooldown.

## Priorities

1. Eliminate redundant index-maintenance receipts retrying every ten seconds,
   and record the actual retry reason/intent deadline in telemetry.
2. Move upload-expiry checks off the clustering allocation; cancel closed-session
   checks only while preserving close-to-handoff crash recovery.
3. Warm Vision models only when the dispatched work requires them.
4. Separate status HTTP demand from the index-maintenance worker allocation.

Deployment startup also produced empty lifetimes (including the latest cluster
revision); those were excluded from queue-only activation claims. Empty polling
inside an already-running process does not independently wake a scaled-to-zero
app. No recurring cron scaler was present in the live app rules.
