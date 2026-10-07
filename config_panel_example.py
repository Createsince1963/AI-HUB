# DEPRECATED - NOT USED, kept only for history.
#
# This was an early draft/example for a Qt6 config panel, written before the real
# AI_Launcher architecture (core/config.Config, core/mcp.py, core/procs.py) was reviewed.
# Its API (module-level load_model_catalog()/get_selected_model() etc.) does not match
# how the actual app works (methods take a `Config` instance and a profile name - see
# core/models.py, which IS the real, wired-up implementation).
#
# The real integration lives in:
#   - AI_Launcher/core/models.py           (model + effort catalog, read/write per profile)
#   - AI_Launcher/ui/dashboard.py           (ServiceRow.aimodel button + ai_model_menu())
#   - AI_Launcher/ui/cli_terminal_page.py   (Modell/Stärke QComboBox pair per terminal tab)
#
# This file is not imported by anything and can be deleted.
