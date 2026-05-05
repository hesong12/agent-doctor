# Implementation Plan

- [x] 1. Define immutable acceptance criteria
  - Write requirements and design docs for frustration intervention.
  - _Requirement: 1, 2, 3_

- [x] 2. Add local frustration classifier
  - Implement deterministic weighted classifier for insults, profanity,
    trust-break language, repeated corrections, and urgency shape.
  - _Requirement: 1_

- [x] 3. Integrate classifier with scan and autopilot
  - Add `user_frustration_signal` findings.
  - Upgrade high-severity autopilot events to `intervene`.
  - _Requirement: 2, 3_

- [x] 4. Update tests and docs
  - Cover profanity, trust-break, intervention action, cards, and docs.
  - _Requirement: 1, 2, 3_

- [x] 5. Verify
  - Run the full test suite and inspect git diff.
  - _Requirement: 1, 2, 3_
