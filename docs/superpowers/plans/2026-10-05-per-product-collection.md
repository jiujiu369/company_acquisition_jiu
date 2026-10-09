# Per-product collection implementation plan

Use superpowers:executing-plans inline with systematic-debugging, test-driven-development and verification-before-completion.

User-approved behavior: each fresh product attempt makes one separate ScraperAPI request, then parses information, downloads originals and saves its package before the next product. Shop listing discovery is separate. Keep one configured API key; do not promise avoidance of verification. Reuse mode and durable product download checkpoints remain available. Never resume a paused live task automatically.

1. Back up changed files and SQLite using its backup API. Add failing integration tests for incremental shop discovery, existing discovered links, independent product requests, download ordering, no inherited shop authentication, and restart/pause.
2. Enqueue shop products during discovery, yield discovery while child work exists, drain saved discovered links first. Preserve pagination checkpoints and complete discovery only after iterator exhaustion.
3. For fresh product acquisition use rendered ScraperAPI with one HTTP attempt; record actual request count and browser fallback reason on each task. Continue an already verified product in its browser; never let shop login force every new child to the browser. Local download failures reuse saved parsing checkpoints.
4. Show child progress before discovery completion and show request counts separately from package/required-field state. Document render costs and original-image CDN downloads.
5. Run related tests and full suite; inspect live paused task before/after safely restarting local service. Do not spend real API credits in tests.

Ledger: root cause confirmed: shop discovery created children only after exhausting all pages, and copied parent authentication_required to every child's browser_required flag. Individual product handler already performs information, downloads and package sequentially.

Implemented: streamed child enqueue/drain, atomic public-page cursor/link persistence, rendered single product HTTP attempts, durable per-task counters, independent UI connection probe, narrow migration for inherited legacy browser flags, status/usage UI, refresh selected by default. Explicit disabled-API browser mode remains compatible.

Verification ledger: initial 4 strategy tests failed for the expected missing behavior; cursor atomicity, connection probe isolation and legacy-child regressions also failed before fixes. Focused suite 42 passed; subsequent focused review/checkpoint suite 28 passed; final full suite pending. First full pass: 159 passed, one old cursor test needed product-before-next-page order; adapted without weakening restart assertions.

Independent read-only review found two important issues (shared connection-test task hooks and old inherited flags), now fixed and independently verified with 3 passing mock tests. No remaining important findings.

Live state before deployment: configured enabled ScraperAPI; no RUNNING/PENDING tasks. Historical parent remains CANCELED with 271 known links and zero child tasks; original target preserved. Tests never use production credentials or real paid requests.

Final verification: 162 passed, 1 existing dependency deprecation warning, 92.19 seconds. Live read-only browser checks passed (refresh default, native-folder field, request counters, no JavaScript errors/overflow), service healthy. Actual project database is data/database/app.db; initial guessed backup path was corrected, real backup saved and 24 tables verified. 60 products, 3501 assets and 77 captures match prior backup row-for-row. Task status counts unchanged across restart.

Ruling: 271-link historical parent has a pre-existing removed_at marker, explaining absence from live list — preserve user's deletion rather than restore it. New collection uses the strategy; retained paused tasks will drain saved links when the user resumes them. Corrected report and communication accordingly.
