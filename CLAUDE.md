## Debugging

When debugging payment/API failures (especially Razorpay), verify credentials/env vars FIRST with a direct curl test before investigating account-level or code-level theories.

Before assuming a config or workflow (e.g. ticker.yml, cron/LaunchAgent) is broken or 'can never work', check whether it is actually running/updating with real data first.

## Stack / Infrastructure

This project deploys on Vercel with Supabase as the database; check Vercel runtime logs and Supabase SQL directly when diagnosing production issues.

## Version Control

Prefer direct Bash file writes in the correct repo; confirm the working directory/repo before creating worktrees.
