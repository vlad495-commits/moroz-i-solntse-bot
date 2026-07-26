# Task 4 report — shared SecurityPipeline

Base HEAD: `c9ab47417da9d59218d2f45a013cbbce7fa009ef`

## Delivered

- Added the shared in-process `SecurityPipeline`.
- Added local zero-call block, stop, rate-limit and medical escalation results.
- Added invocation-only masking for allowed history and current input before
  external guard/answer requests.
- Added strict masked guard handling, deterministic route metadata, source-owned
  output validation, one bounded answer retry and current-turn-only restore.
- Added exact usage aggregation across actual guard/answer responses.
- Wired legacy `generate_response` and production `init_llm` to the shared
  primary/reserve SDK gateway while preserving prompt hot reload.
- Kept the owned prompt, route metadata and validator retry code in one
  machine-owned privileged system block for OpenAI/Anthropic compatibility.

## TDD and Docker evidence

- Two pre-build invocation errors were root-caused before implementation:
  fallback cwd resolution and an incomplete process-only Redis credential pair.
  Neither created persistent resources; both are recorded safely in changelog.
- Fresh RED: no-cache build exit `0`; pytest collection exit `2` on the expected
  missing `moroz.security.pipeline`; cleanup `0/0/0/0`.
- First GREEN: `258 passed / 1 failed`; the only failure was a hard-coded
  placeholder ordinal in the test. One session correctly assigned the current
  email after the history email. The assertion was corrected without production
  changes.
- Corrected gate: `259 passed / 0 failed`; compile exit `0`; cleanup
  `0/0/0/0`.
- Requirements audit added a focused multiple-system regression. RED was
  `2 failed / 19 passed`, proving the existing request could let Anthropic keep
  metadata instead of the owned prompt.
- Final fresh no-cache gate after the source fix: `259 passed / 0 failed`,
  compile exit `0`, build/test/compile/cleanup/image-removal exits all `0`,
  remaining containers/volumes/networks/images `0/0/0/0`.
- Task-local empty env-file was removed.

## Safety

- Tests used scripted in-memory gateways only; no provider call was made.
- No raw fixture, mapping, provider response, endpoint, credential or exception
  message is included in this report or changelog evidence.
- No staging/production mutation, push or merge was performed.

## Concerns

- None within Task 4 scope.
