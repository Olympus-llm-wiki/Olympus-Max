## Purpose

Давать агенту повторяемый локальный разбор речи и видеоряда через подписку Antigravity
с происхождением, возобновлением и явными ограничениями качества.

## Requirements

### Requirement: Scoped subscription invocation
The tool MUST use the existing subscription CLI with additional-credit fallback disabled,
known available quota and selected local media; unrelated tools MUST be excluded.

#### Scenario: Uncertain billing configuration
- **WHEN** settings are unreadable or useG1Credits is enabled
- **THEN** invocation stops before model work and reports the required local configuration.

#### Scenario: Native sparse persistence
- **WHEN** native CLI omits its false-default useG1Credits field from valid settings
- **THEN** the tool treats it as disabled, while still requiring known sufficient quota.

#### Scenario: Unexpected capability
- **WHEN** the launched reader attempts an unapproved tool or file
- **THEN** the pre-tool boundary denies execution and its receipt does not mark success.

### Requirement: Evidence and resumability
The tool SHALL retain input hashes, requested spans, transformations, raw responses and
per-attempt receipts; it MUST reuse only intact successful results for matching requests.

#### Scenario: Interrupted chunked work
- **WHEN** work stops after a completed part
- **THEN** rerun preserves that part, attempts only missing work and requires explicit retry for uncertain attempts.

### Requirement: Independent result checks
The tool MUST distinguish process exit, CLI terminal state, text availability, interval
validity and visual review; model self-report MUST NOT establish semantic correctness.

#### Scenario: Success without audio access
- **WHEN** CLI returns SUCCESS but requested audio is unavailable
- **THEN** the text result is not accepted.

#### Scenario: Impossible timestamps
- **WHEN** model intervals exceed input duration
- **THEN** timestamps fail separately while any returned text remains preserved as a draft.

#### Scenario: Silent video
- **WHEN** a file without an audio track produces a speech transcript
- **THEN** the result is flagged as a modality contradiction.

### Requirement: Coherent skill routing
The skill SHALL route media tasks by requested outcome, prefer captured originals and
retain Notebook workflows without duplicate contradictory Antigravity instructions.

#### Scenario: User requests visual detail
- **WHEN** the request concerns a specific on-screen event
- **THEN** the agent uses the relevant span and checks extracted frames instead of assuming a transcript covers it.
