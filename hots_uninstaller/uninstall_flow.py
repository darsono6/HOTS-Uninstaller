from .core_uninstall import list_registry_backups, list_uninstall_history, remove_backup_hash_store
from .core_monitor import list_monitor_logs


def count_settings_items() -> int:
    total = 0
    try:
        total += len(list_uninstall_history())
    except Exception:
        pass
    try:
        total += len(list_monitor_logs())
    except Exception:
        pass
    return total


def count_backup_items() -> int:
    try:
        return len(list_registry_backups())
    except Exception:
        return 0


def apply_uninstall_choices(choices: dict) -> dict:
    delete_settings = bool(choices.get("delete_settings"))
    delete_backups = bool(choices.get("delete_backups"))
    if delete_settings or delete_backups:
        remove_backup_hash_store(backups=delete_backups, monitor=delete_settings)
    return {"errors": [], "delete_settings": delete_settings, "delete_backups": delete_backups}
