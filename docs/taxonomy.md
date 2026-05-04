# Failure Taxonomy

Agent Doctor uses deterministic failure modes. Each finding includes evidence quotes with file, line, role, diagnosis, recommendation proposals, an eval case, and confidence.

## repeated_user_correction

Signals include phrases such as "I already told you", "我刚才说过", "你又", "不是这个", "not what I asked", and "again".

Patch targets:

- `memory`: capture stable user preferences only when transcript evidence supports them.
- `sop`: clarify how to handle corrections and conflicting instructions.

## execution_discipline

Signals include an assistant promising to check, run, test, verify, create, update, inspect, read, search, fix, or write, with no tool message before the next assistant or user message. It also catches user complaints about planning instead of acting.

Patch targets:

- `sop`: require the promised action before reporting progress or results.
- `eval`: add cases that fail when the agent promises execution but only narrates.

## verification_failure

Signals include phrases such as "did you test", "你测了吗", "你验证了吗", "without verifying", "没验证", and "not actually tested".

Patch targets:

- `sop`: require explicit verification status.
- `eval`: fail confident claims that lack a concrete verification step or a clear disclosure that verification was not run.

## memory_failure

Signals include phrases such as "you forgot", "你忘了", "remember", "我说过", and "last time".

Patch targets:

- `memory`: create a reviewable memory candidate from evidence, without inferring beyond the quoted transcript.

## tool_failure_or_hidden_error

Signals include tool output containing error, failed, failure, timeout, unauthorized, 401, 403, 500, traceback, or exception, followed by an assistant response that does not acknowledge the failure.

Patch targets:

- `sop`: acknowledge tool failures, summarize the observed error, and change course before claiming success.
- `tool_discipline`: block success summaries when recent tool results contain failure signals.

## communication_mismatch

Signals include phrases such as "too verbose", "stop explaining", "别废话", "直接做", "不要只给计划", and "don't just plan".

Patch targets:

- `memory`: store a concise communication preference only after repeated evidence.
- `identity`: guide the agent toward direct, action-oriented updates.

Single instances can be situational, so recommendations warn reviewers not to overfit one-off communication complaints.
