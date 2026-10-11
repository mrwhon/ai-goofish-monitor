# Generic Xianyu monitor CLI

This fork retains upstream Web/API code, but the `src.cubi_monitor` mode is independent. It runs no Web server, exports no cookie state and does not use the upstream scraper's skip-old-ID pipeline. Required runtime dependencies are Python 3.11+ and existing Playwright; JSON parsing and keyword normalization reuse upstream pure modules. A host Reader adapter provides one owned background CDP target and closes only that target. No new external dependencies were added.

Copy [the synthetic two-category example](../examples/monitor.example.json) to an owner-private location, replace example endpoints/baselines, and keep it out of Git. Price ceilings, exclusions, preferences and comparison references belong to each task's `criteria`/`baseline_refs`. References are dated observed/negotiated claims, not proof of sale or hardware health. Model image URLs are not downloaded: text-only analysis always reports pictures unchecked.

```
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB register
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB --monitor TASK scan --prepare
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB --monitor TASK activate
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB run
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB --monitor TASK status
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB --monitor TASK stop
python3 -m src.cubi_monitor.cli --config PRIVATE_CONFIG --database PRIVATE_DB --monitor TASK resume
```

`register` is not activation. `activate` creates one immutable expiry; restart/config changes cannot extend it. Prepare reads are explicitly requested, consume their own bounded analysis allocation, and do not advance live cadence/health/expiry or send recommendations. `run` processes configured tasks sequentially under one database file lock. New tasks quietly establish the initial baseline by default. Daily scheduling is the host's job; one persistent timer catch-up does not replay every missed day. Disabled/stopped/expired tasks perform no collection or AI.

Search uses overlapping recent publish windows, every third successful live round native new-price search, and every seventh a bounded latest-page sweep. ID identifies a listing; dates restrict search coverage only. Page limits and unknown pagination are reported as truncation. This is not full-market coverage, nor proof that the seller has not deleted/reposted an item under another ID.

Facts are shared in `(platform,item_id)` items; task relationships store separate decisions, manual quotes and watches. Observations retain source and price. Assessments bind item fingerprint, semantic criteria/baseline hash and evaluator version; browsing counts/relative times/thumbnail changes do not trigger AI. Meaningful price changes compare against last assessment, with task-configured fraction or crossing its hard maximum price. Partial lists preserve detailed facts and unknown price never becomes zero. A rotating watch refresh and changed/plausible/unknown queue lanes share bounded detail/analysis allocations. AI failure retains pending status and consumes a run's call allocation; there is no automatic unlimited retry.

Schema1 migration first validates input identities/FKs, makes a mode-0600 SQLite backup beside the private database, then transactionally copies all facts, observations, old decisions, quotes, run/expiry values and original notification payload/status. Mixed legacy purchase history belongs to a disabled `legacy-purchase-review`; only explicit current-task IDs create new task relationships. No old writer remains. Failed migration rolls back and unknown schemas fail closed. Stop the wrapper/timer before restoring a backup; preserve both files and delivery receipts. Restoring an old format requires running its pinned old CLI, not the current CLI against guessed tables.

Notifications are independent outbox records. Sender return 0 acknowledges delivery, 1 proves preflight failed and permits retry, and ambiguous errors/timeouts become `uncertain`; interrupted claims are uncertain as well. Old facts/criteria events become `superseded`. Expiry emits one final summary without more browsing/AI. `reconcile-send EVENT --result sent|retry` only resolves an uncertain event after operator verification; it does not repeat analysis. Exactly-once delivery across a network boundary is not promised.

Login/challenge/rate-limit evidence blocks future capture globally. Recover the same account manually, then explicitly `--monitor TASK unblock`; no rotation/bypass. Routine API/schema failures remain recorded failures, distinct from true empty results. Independent health checks must monitor the timer and last successful **live** run; OnFailure alone cannot detect a timer that never fires.

Host API keys come from environment, never a config file. `runtime.ai` supplies an HTTPS endpoint, configured model, explicit verified price ceilings and a cumulative USD cap (max 3 in this implementation). UTF-8 byte/input and output-token bounds reserve cost before each request, including ambiguous failures; no refunds/retries assume success. Verify provider pricing/eligibility before using other providers. Trial integration uses the existing ordinary DeepSeek channel; [official pricing](https://api-docs.deepseek.com/quick_start/pricing/) was checked 2026-10-11 and the private profile sets ceilings at twice peak Flash rates. Its existing `deepseek-v4-flash` alias is currently served as V4.1 Flash; actual model/usage receipt is stored without secrets.

Focused checks, without upstream Web fixtures:

```
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/cubi --confcutdir=tests/cubi -q -p no:cacheprovider -o addopts='--tb=short'
```

Only synthetic test inputs/examples are public. Personal comparisons, real item snapshots, negotiation prices, browser evidence, databases and migration backups belong in ignored/private storage. CLI summaries omit API errors and credentials. Cubi owns its wrapper, systemd units, chosen private configuration and existing Telegram sender; this repository owns the core.
