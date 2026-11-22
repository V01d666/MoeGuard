from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parents[1]
datas, binaries, hiddenimports = collect_all("cv2")
datas.append((str(root / "resources" / "models"), "resources/models"))

analysis = Analysis(
    [str(root / "moeguard" / "patrol_worker.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
# QtCore imports the Windows ICU ABI. A developer toolchain can put a different
# ICU build on PATH during analysis, which must not shadow Windows' icuuc.dll.
analysis.binaries = [
    entry for entry in analysis.binaries
    if entry[0].lower() not in {"icuuc.dll", "icudt78.dll"}
]
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="MoeGuard-Patrol", console=True)
bundle = COLLECT(exe, analysis.binaries, analysis.datas, name="MoeGuard-Patrol")
