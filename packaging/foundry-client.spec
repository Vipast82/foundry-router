# PyInstaller spec: two programs over one bundle.
#   FoundryClient(.exe)  windowless tray app (what the Start menu / autostart run)
#   foundry-client(.exe) console twin for commands and for the coordinator/supervisor
#                        child processes (their output goes to the client's logs)
# Build:  pyinstaller packaging/foundry-client.spec --noconfirm   (from the repo root)
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hidden = (collect_submodules("foundry_coordinator") + collect_submodules("foundry_automation")
          + collect_submodules("foundry_client") + collect_submodules("uvicorn")
          + collect_submodules("keyring.backends") + ["foundry_router", "tomli_w"])
if sys.platform == "win32":
    hidden += ["pystray._win32"]
else:
    hidden += ["pystray._xorg", "pystray._appindicator", "pystray._gtk", "Xlib"]
datas = (collect_data_files("foundry_client") + collect_data_files("foundry_coordinator")
         + collect_data_files("foundry_automation"))

ICON = os.path.join(SPECPATH, "foundry-client.ico")
ICON = ICON if sys.platform == "win32" and os.path.isfile(ICON) else None
a = Analysis([os.path.join(SPECPATH, "launcher.py")], pathex=[os.path.dirname(SPECPATH)], hiddenimports=hidden, datas=datas,
             excludes=["tkinter", "matplotlib", "torch", "transformers", "langchain", "langgraph"])
pyz = PYZ(a.pure)
tray = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FoundryClient", console=False,
           icon=ICON)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="foundry-client", console=True,
          icon=ICON)
COLLECT(tray, cli, a.binaries, a.datas, name="FoundryClient")
