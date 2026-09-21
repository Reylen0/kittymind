"""SQLite 持久化的共用底座：版本链迁移 + 生命周期维护（Phase 17）。

两个 SQLite 库（会话库 `sessions.db`、工具审计库 `tool_audit.db`）共用这一层：

  - `migrations`：可审计、失败不提升版本、可重试的 schema 版本链；
  - `maintenance`：自检、在线备份、WAL checkpoint、空间回收、损坏隔离；
  - `atomic`：文本文件原子写（记忆文件、审计 JSONL 这类"不在 SQLite 里"的
    持久化共用，避免两处各写一份实现后慢慢漂移）。

放在独立包而不是塞进 session/store.py，是因为审计库同样需要这套能力，
而 `session/` 不该被 `tools/` 反向依赖。
"""

from .atomic import atomic_write_text
from .maintenance import (
    DbStats,
    backup_name,
    backup_to,
    checkpoint,
    copy_sidecars,
    integrity_check,
    latest_backup_age_hours,
    list_backups,
    prune_backups,
    quarantine_db,
    quarantine_path,
    quick_check,
    read_stats,
    should_vacuum,
    vacuum,
)
from .migrations import (
    Migration,
    MigrationFailedError,
    MigrationOutcome,
    SchemaTooNewError,
    applied_versions,
    run_migrations,
    user_version,
)

__all__ = [
    "DbStats",
    "Migration",
    "MigrationFailedError",
    "MigrationOutcome",
    "SchemaTooNewError",
    "applied_versions",
    "atomic_write_text",
    "backup_name",
    "backup_to",
    "checkpoint",
    "copy_sidecars",
    "integrity_check",
    "latest_backup_age_hours",
    "list_backups",
    "prune_backups",
    "quarantine_db",
    "quarantine_path",
    "quick_check",
    "read_stats",
    "run_migrations",
    "should_vacuum",
    "user_version",
    "vacuum",
]
