; OmniAPI installer hooks (1.3-M5).
;
; Before files are copied (install / update) and before they are removed (uninstall), stop
; everything that runs from the install folder. Without this an update reports success but
; silently keeps the old copy of any file that is in use, and an uninstall leaves running
; programs behind (prototype two, update experiment A/D). Tauri's installer only stops
; OmniAPI.exe itself, not the service it started.
;
; nsis-stop.ps1 (shipped in the install folder) does the work: it stops the shell, asks the
; service to shut down by itself (POST /api/shutdown) when the service runs from the install
; folder, then stops whatever still runs from there. Matching is by path, never by name: a
; python.exe anywhere else is never touched. The user's data (~/.omniapi, Documents\OmniAPI)
; is not in the install folder and is never removed.
;
; The script is run from the folder being updated (the copy of the version installed there).
; When it is not there — first install, or an install older than 1.3-M5 — the plain
; stop-by-path below is used (prototype two's hook; nothing can be asked gracefully then).
; (Tauri copies this .nsh into the build folder, so a script next to it cannot be compiled in.)

!macro OMNI_STOP_INSTALLED
  IfFileExists "$INSTDIR\nsis-stop.ps1" 0 omni_stop_plain
    nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\nsis-stop.ps1" -InstDir "$INSTDIR"'
    Pop $0
    Goto omni_stop_done
  omni_stop_plain:
    nsExec::ExecToLog `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith('$INSTDIR\', [StringComparison]::OrdinalIgnoreCase) -and $$_.Name -notlike 'uninstall*' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
    Pop $0
    Sleep 500
  omni_stop_done:
!macroend

; 1.3-M6: the bundled Python (python\, ~15,000 files) and the GUI (gui\) are replaced as a whole.
; Tauri's installer only overwrites the files of the new version and its uninstaller only deletes
; the files it listed, so a package dropped in an update, or anything written later inside these
; two folders, would stay behind. Both folders are ours alone (nothing the user makes is kept in
; the install folder: data is in ~/.omniapi, works in Documents\OmniAPI), so they are removed
; whole -- only these two, only when OmniAPI.exe is in the same folder (= it is our install).
!macro OMNI_REMOVE_PAYLOAD
  IfFileExists "$INSTDIR\OmniAPI.exe" 0 omni_payload_done
    RMDir /r "$INSTDIR\python"
    RMDir /r "$INSTDIR\gui"
  omni_payload_done:
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro OMNI_STOP_INSTALLED
  !insertmacro OMNI_REMOVE_PAYLOAD
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro OMNI_STOP_INSTALLED
  !insertmacro OMNI_REMOVE_PAYLOAD
!macroend
