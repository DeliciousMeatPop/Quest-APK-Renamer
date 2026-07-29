"""tkdnd data is selected explicitly by build_support.tkdnd_data_files."""

# Keeping this hook intentionally empty overrides the generic PyInstaller hook,
# which cannot account for the separate Tcl 9 backend shipped by version 0.6.2.
datas = []
binaries = []
