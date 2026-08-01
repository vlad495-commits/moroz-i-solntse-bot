# Task 1 report: static RED contract

## Changes

- Created `project/tests/unit/test_full_project_architecture_visual.py`.
- The test uses Python stdlib `HTMLParser` and accepts
  `FULL_ARCHITECTURE_HTML_PATH`; the host default is
  `docs/moroz-i-solntse-full-architecture.html`.
- It requires the agreed sections, all status-classified nodes, labels, factual
  comparison tokens, git snapshot, static/self-contained HTML, no secret
  assignments or external assets, and the CSS contract.
- Did not create the target HTML or change runtime code and the existing
  `docs/production-v1-architecture.html`.

## Docker evidence

Initial baseline (without a docs mount):

```text
docker compose --env-file <original-repository-.env> run --rm test pytest \
  tests/unit/test_architecture_visual.py tests/unit/test_message_path_visual.py
=> 11 failed: FileNotFoundError for /docs/*.html
```

Corrected baseline (worktree `docs/` mounted at `/repo/docs:ro`, both existing
path overrides supplied):

```text
=> 11 passed in 0.15s
```

RED contract run (worktree `docs/` mounted at `/repo/docs:ro`):

```text
FULL_ARCHITECTURE_HTML_PATH=/repo/docs/moroz-i-solntse-full-architecture.html
pytest /workspace/tests/unit/test_full_project_architecture_visual.py
=> 5 failed: FileNotFoundError for the future HTML
```

Compose interpolation used only the approved dummy values for missing
`RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `RABBITMQ_URL`, and
`TELEGRAM_WEBHOOK_SECRET`; the original repository `.env` was passed by its
absolute path and its values were not printed.

## Files

- `project/tests/unit/test_full_project_architecture_visual.py`
- `changelog.md`
- `.superpowers/sdd/task-1-report.md`

## Self-review

- Checked every required section, node ID, label, factual token and CSS token
  against `task-1-brief.md`.
- The only new product-facing artifact is a failing test; no target HTML was
  created.
- Docker RED is caused by precisely the missing expected artifact, not a test
  collection or container-path error.
- `git diff --check` is run before commit.

## Concerns

- Docker reported pre-existing orphan-container and pytest cache-on-read-only-
  mount warnings. They do not affect the corrected 11-test baseline or the
  expected FileNotFoundError RED outcome.
