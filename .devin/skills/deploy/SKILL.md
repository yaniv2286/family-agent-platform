---
name: deploy
description: Deploy the Koko platform to production after tests pass and explicit user confirmation.
allowed-tools:
  - exec
  - read
  - ask_user_question
---

You are the deployment assistant for the Koko platform.

The deployment workflow is defined in `deploy.bat` and is part of the project's definition of done.

Steps:

1. **Tests must pass first.** Before any deployment, run:
   ```powershell
   python -m pytest tests -v --ignore=tests/test_live_llm.py
   ```
   If tests fail, stop and report the blocker. Do not deploy.

2. **Ask for explicit confirmation.** `deploy.bat` replaces files and restarts services on the production VPS. You must ask the user for explicit confirmation before running it. Example prompt:
   > Tests pass. `deploy.bat` will replace production files and restart services. Confirm deployment? (yes/no)

3. **Run the deploy script.** After confirmation, execute:
   ```powershell
   .\deploy.bat
   ```

4. **Verify production health.** After deployment succeeds, check the live health endpoint (or the deploy script's output) to confirm services are healthy. If the deploy script does not show health status, query `/api/health` on the production host.

5. **Stop on failure.** If deployment fails, report the exact blocker and keep the task open. Do not mark the task complete until production services are healthy.

Do not use this skill for local-only changes or for running tests in isolation — use `/verify` for that.
