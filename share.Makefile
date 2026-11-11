# Shared backup targets. Set connection and project variables before inclusion.
CLI_BACKUP ?= cli-backup
CLI_BACKUP_WHERE = $(or $(where),local)
CLI_BACKUP_PATH = $(or $(path),daily)
CLI_BACKUP_FILE = $(file)
CLI_BACKUP_DATABASE = $(db)
CLI_BACKUP_SOURCE_PROJECT = $(source_project)
CLI_BACKUP_SOURCE_DATABASE = $(source_db)
export CLI_BACKUP_WHERE CLI_BACKUP_PATH CLI_BACKUP_FILE CLI_BACKUP_DATABASE CLI_BACKUP_SOURCE_PROJECT CLI_BACKUP_SOURCE_DATABASE

.PHONY: cli-backup@config-check cli-backup@backup cli-backup@list cli-backup@verify cli-backup@restore-test cli-backup@restore

##@ CLI backup
cli-backup@config-check: ## * Validate backup settings
	@$(CLI_BACKUP) scheduled config-check

cli-backup@backup: ## * Back up configured databases
	@$(CLI_BACKUP) scheduled backup

cli-backup@list: ## * List backups [where=local|s3] [path=daily|weekly|monthly] [db=name]
	@set -- --storage "$$CLI_BACKUP_WHERE" --period "$$CLI_BACKUP_PATH"; if [ -n "$$CLI_BACKUP_DATABASE" ]; then set -- "$$@" --database "$$CLI_BACKUP_DATABASE"; fi; $(CLI_BACKUP) scheduled list "$$@"

cli-backup@verify: ## * Verify a backup file=<name> [db=name] [where=local|s3]
	@set -- --storage "$$CLI_BACKUP_WHERE" --period "$$CLI_BACKUP_PATH" --file "$$CLI_BACKUP_FILE"; if [ -n "$$CLI_BACKUP_DATABASE" ]; then set -- "$$@" --database "$$CLI_BACKUP_DATABASE"; fi; $(CLI_BACKUP) scheduled verify "$$@"

cli-backup@restore-test: ## * Rehearse a backup [file=<name>] [db=name] [where=local|s3]
	@set -- --storage "$$CLI_BACKUP_WHERE" --period "$$CLI_BACKUP_PATH"; if [ -n "$$CLI_BACKUP_FILE" ]; then set -- "$$@" --file "$$CLI_BACKUP_FILE"; fi; if [ -n "$$CLI_BACKUP_DATABASE" ]; then set -- "$$@" --database "$$CLI_BACKUP_DATABASE"; fi; $(CLI_BACKUP) scheduled restore-test "$$@"

cli-backup@restore: ## * DESTRUCTIVE: restore a backup file=<name> [db=name]
	@set -- --storage "$$CLI_BACKUP_WHERE" --period "$$CLI_BACKUP_PATH" --file "$$CLI_BACKUP_FILE"; if [ -n "$$CLI_BACKUP_DATABASE" ]; then set -- "$$@" --database "$$CLI_BACKUP_DATABASE"; fi; if [ -n "$$CLI_BACKUP_SOURCE_PROJECT" ]; then set -- "$$@" --source-project-name "$$CLI_BACKUP_SOURCE_PROJECT"; fi; if [ -n "$$CLI_BACKUP_SOURCE_DATABASE" ]; then set -- "$$@" --source-database "$$CLI_BACKUP_SOURCE_DATABASE"; fi; $(CLI_BACKUP) scheduled restore "$$@"
