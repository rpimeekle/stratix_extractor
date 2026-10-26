# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller StratixExtractor.spec
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

version = re.search(r'__version__ = "([^"]+)"',
                    Path("stratix_extractor/__init__.py").read_text()).group(1)

# Netmiko loads its device drivers through a dispatcher; pull them all in.
hiddenimports = collect_submodules("netmiko")
datas = collect_data_files("ntc_templates")

a = Analysis(
    ["run_stratix_extractor.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pandas", "numpy", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                     StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    numbers = tuple(int(n) for n in re.findall(r"\d+", version)[:3]) + (0,)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("FileDescription", "Stratix Extractor"),
                StringStruct("ProductName", "Stratix Extractor"),
                StringStruct("FileVersion", version),
                StringStruct("ProductVersion", version),
                StringStruct("OriginalFilename", "StratixExtractor.exe"),
                StringStruct("CompanyName", "rpimeekle"),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StratixExtractor",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    version=version_info,
)
