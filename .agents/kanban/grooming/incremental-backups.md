# Incremental backups for databases and files

Stage: grooming

## Context

Database and file backups default to one full backup per day. Incremental backups are deferred until needed, particularly for large databases and limited storage capacity.

## Scope to clarify

- Choose supported database engines and file sources, and define consistency guarantees, full-backup cadence and incremental chain format.
- Define recovery point objectives, retention, storage sizing, integrity verification and behavior when a chain is incomplete.
- Preserve file ownership, permissions and symbolic links when file recovery requires them.

## Acceptance criteria

- An approved design covers backup, recovery, retention, operational monitoring and migration from existing full backups.
- A recovery drill proves a full backup plus increments restores the expected data, including failure handling for missing or corrupt increments.
- Daily full backups remain the default until explicitly configured otherwise.
