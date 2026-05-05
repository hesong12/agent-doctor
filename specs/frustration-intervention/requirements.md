# Requirements Document

## Introduction

Agent Doctor autopilot should make user frustration visibly actionable without
waiting for the host agent to remember to diagnose itself. The improvement must
preserve the local-first privacy boundary while detecting stronger real-world
signals such as insults, trust breakdown, repeated corrections, and unverified
success claims.

## Requirements

### Requirement 1 - Local Frustration Detection

**User Story:** As a user of a memoryful AI agent, I want Agent Doctor to notice
when I am clearly angry, insulting the agent, or losing trust, so diagnosis
starts without me asking for a postmortem.

#### Acceptance Criteria

1. When a user message contains direct insults, profanity, direct dumb-feedback
   phrases such as "Why are you so dumb?", "Are you stupid?", or
   "你怎么这么笨的？", Agent Doctor shall classify it as a high-severity
   user frustration signal.
2. When a user message contains direct quality complaints, repeated-correction
   language, or trust-break language, Agent Doctor shall classify it with a
   deterministic local classifier and expose the matched signal labels.
3. When Agent Doctor scans production transcripts, the classifier shall not call
   a remote LLM or any network service.
4. When Chinese words such as "笨重" are used as technical/product
   descriptions, Agent Doctor shall not classify them as user frustration.

### Requirement 2 - Visible Autopilot Intervention

**User Story:** As a user, I want Agent Doctor to be visibly present when the
agent quality breaks down, so I can tell the product is doing something useful.

#### Acceptance Criteria

1. When a high-severity user frustration signal is detected, Agent Doctor
   autopilot shall emit an `intervene` event instead of a passive notification.
2. When an intervention card is written, the card shall tell the host agent to
   pause the normal success path, acknowledge the concrete problem, and answer
   with evidence-backed next steps.
3. When a notify command is configured, Agent Doctor shall expose the event
   action through environment variables so delivery adapters can render
   intervention events differently.

### Requirement 3 - Durable Product Fixes

**User Story:** As an operator, I want frustration events to turn into patch and
eval candidates, so the same quality failure can be prevented later.

#### Acceptance Criteria

1. When `scan` detects user frustration signals, Agent Doctor shall include them
   in normal findings and reports.
2. When `apply` stages patches from frustration findings, Agent Doctor shall
   produce identity, SOP, and eval guidance suitable for review.
3. When tests run, they shall cover profanity/insult detection, trust-break
   detection, intervention cards, and non-LLM local operation.
