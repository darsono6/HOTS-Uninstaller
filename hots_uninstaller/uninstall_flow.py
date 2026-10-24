import os

from .core_uninstall import list_registry_backups, list_uninstall_history, remove_backup_hash_store
from .core_monitor import list_monitor_logs


def count_saved_items() -> int:
    total = 0
    try:
        total += len(list_uninstall_history())
    except Exception:
        pass
    try:
        total += len(list_monitor_logs())
    except Exception:
        pass
    try:
        total += len(list_registry_backups())
    except Exception:
        pass
    return total


def apply_uninstall_choices(choices: dict) -> dict:
    delete_data = bool(choices.get("delete_saved_data"))
    if delete_data:
        remove_backup_hash_store()
    return {"errors": [], "delete_data": delete_data}
