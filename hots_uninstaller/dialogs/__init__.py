from .programs_page   import ProgramsPage
from .appx_page       import AppxPage
from .startup_page    import StartupPage
from .junk_page       import JunkPage
from .monitor_page    import MonitorPage
from .backups_page    import BackupsPage
from .leftover_dialog import LeftoverDialog
from .uninstall_wizard import UninstallWizardDialog, AppxUninstallWizardDialog
from .self_uninstall_wizard import SelfUninstallWizardDialog
from .support_page    import SupportPage
from .about_page      import AboutPage
from .language_dialog import LanguageDialog
from .accent_dialog   import AccentColorDialog

__all__ = [
    "ProgramsPage", "AppxPage", "StartupPage", "JunkPage", "MonitorPage", "BackupsPage", "LeftoverDialog",
    "UninstallWizardDialog", "AppxUninstallWizardDialog",
    "SelfUninstallWizardDialog",
    "SupportPage", "AboutPage",
    "LanguageDialog", "AccentColorDialog",
]
