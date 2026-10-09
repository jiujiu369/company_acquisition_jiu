# Plan: 2026-09-29-collection-only-tool
Ruling: Direct edits in the requested directory; no Git metadata, so source snapshot replaces commits.
Ruling: Keep synchronous worker and use interruptible polling for operation pacing; no async browser migration required.
Task 1: RED: 3 tests failed, including confirmed login-resume regression.
Task 1: delay/login regression GREEN: 8 passed.
Task 2/3/4 RED: 5 end-to-end requirements failed before implementation; integrated implementation in progress.
Ruling: Download main, detail and SKU originals, excluding videos and recommendations, because the user requested original product images without a detail-only restriction.
Ruling: Keep existing SQLite tables intact, but remove unused business ORM modules; collection ORM has no dependencies on those modules.
Ruling: Retire tests and commands for removed features, and replace old network-mode tests with unified-strategy tests.
Task 2/3/4: complete; unified strategy, mixed input, idempotent checkpoints, removed routes and services. Targeted suite 55 passed, final project suite 73 passed.
Task 5: complete; dependencies and README updated, real Chrome isolated UI flow and exact-byte ZIP download passed. Source database copy preserves 19 tables; original data retains 6 products, 467 assets, 217 collection jobs, 232 tasks; all 329 original hashes unchanged.
Final review: independent reviewer found 6 Important issues. Added 7 failing reproductions, fixed dynamic detail sufficiency, per-job access mode, manual browser ownership, fresh metadata, page cursors, and child public-first policy.
Review boundaries: added dynamic partial shop, real browser cursor and parent/child manual tests; added failing empty-public-shop ownership and JS button pagination reproductions, fixed both. Independent limited recheck passed.
Final regression: 86 passed, one external Starlette/AnyIO deprecation warning, 78.84 seconds. JS syntax passed. Isolated real Chrome UI acceptance passed with exact-byte original and ZIP downloads and zero page errors.
Final data audit: 329 original hashes unchanged; counts 6 products, 467 assets, 217 collection jobs, 232 tasks; all 19 legacy table counts preserved in migrated copy.
Ruling: Retain live client-side catalog state across pause; on process restart rebuild necessary JS pagination state while reusing durable discovered products and originals. Documented in README and final report.
Cleanup limitation: automatic approval rejected removal of generated node_modules cache (blocked by policy); no active references or dependency declarations remain. Existing virtual environment and compiled caches retained.
Final report: reports/collection-only-validation.md. Source backup: reports/collection-only-source-before.zip. No real Taobao or credentialed ScraperAPI requests performed.
