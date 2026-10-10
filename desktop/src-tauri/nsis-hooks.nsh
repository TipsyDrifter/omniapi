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
;
; 1.4: the exe is ${MAINBINARYNAME}.exe (Tauri's define, resolved where the macros are inserted):
; OmniAPI.exe for the released app, OmniAPI-Test.exe for the test build (package.ps1 -TestIdentity),
; so a test install never looks for, or removes payload next to, the released exe.

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
; whole -- only these two, only when our exe is in the same folder (= it is our install).
!macro OMNI_REMOVE_PAYLOAD
  IfFileExists "$INSTDIR\${MAINBINARYNAME}.exe" 0 omni_payload_done
    RMDir /r "$INSTDIR\python"
    RMDir /r "$INSTDIR\gui"
  omni_payload_done:
!macroend

; 1.4.1: "start at logon" survives a covering install.
; Double-clicking a newer installer first runs the OLD version's uninstaller, which deletes
; HKCU\...\Run\<name> unless Tauri's own updater started it (/UPDATE) -- and our hooks run too
; late (after that) to save the value. So the shell keeps the user's choice in the data home, a
; place no install or uninstall touches: <data home>\autostart.pref, first line "autostart=on" or
; "autostart=off" (src/autostart_pref.rs; plain text because NSIS has no JSON parser). When it
; says on, the post-install hook writes the Run value back at once, so the logon entry works even
; if the app is not started after installing. The shell repairs a lost value at its own start too.
;
; The value is exactly what tauri-plugin-autostart 2.5.1 writes (src/autostart.rs my_command):
;   name = ${MAINBINARYNAME}  (OmniAPI / OmniAPI-Test = the identity's run value; the same define
;          that names the exe, so the test installer can never write the released app's value)
;   data = <install folder>\${MAINBINARYNAME}.exe --background   (exe path unquoted, one space)
; plus, as the plugin does when that key exists, StartupApproved\Run = "enabled" for Task Manager.
;
; Data home = OMNIAPI_HOME when the environment has it (the shell's own rule when its config sets
; none; the installed configs set none, the install tests set it), else %USERPROFILE%\.omniapi
; (.omniapi-test for the test build; same as identity.rs HOME_DIR).
;
; A genuine uninstall leaves autostart.pref (it is in the data home, like every other setting), so
; a later reinstall turns the logon entry on again. Turning it off in the app (or deleting the
; file) before uninstalling gives the other behaviour. The uninstaller itself is not changed: it
; deletes the Run value as before; nothing there can tell "real uninstall" from "first half of an
; upgrade" except how the installer happened to start it.
!macro OMNI_RESTORE_AUTOSTART
  Push $0
  Push $1
  Push $2
  ReadEnvStr $0 OMNIAPI_HOME
  StrCmp $0 "" 0 omni_as_home_known
    ReadEnvStr $0 USERPROFILE
    StrCmp $0 "" 0 omni_as_profile_known
      StrCpy $0 "$PROFILE"
    omni_as_profile_known:
    !if "${MAINBINARYNAME}" == "OmniAPI-Test"
      StrCpy $0 "$0\.omniapi-test"
    !else
      StrCpy $0 "$0\.omniapi"
    !endif
  omni_as_home_known:

  StrCpy $2 "no file"
  IfFileExists "$0\autostart.pref" 0 omni_as_decided
    StrCpy $2 "unreadable"
    ClearErrors
    FileOpen $1 "$0\autostart.pref" r
    IfErrors omni_as_decided
    FileRead $1 $2
    FileClose $1
  omni_as_decided:

  ; the first line, with or without its line ending (StrCmp ignores case)
  StrCmp $2 "autostart=on" omni_as_on
  StrCmp $2 "autostart=on$\r$\n" omni_as_on
  StrCmp $2 "autostart=on$\n" omni_as_on
  Goto omni_as_log

  omni_as_on:
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${MAINBINARYNAME}" "$INSTDIR\${MAINBINARYNAME}.exe --background"
    StrCpy $2 "autostart=on, Run value written"
    ; the plugin marks the entry enabled for Task Manager only when that key exists (and so do we)
    ClearErrors
    EnumRegValue $1 HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" 0
    IfErrors omni_as_log
      WriteRegBin HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "${MAINBINARYNAME}" 020000000000000000000000

  omni_as_log:
  ; one line next to nsis-stop.ps1's, for the install tests and for support
  ClearErrors
  FileOpen $1 "$TEMP\omniapi-installer-stop.log" a
  IfErrors omni_as_end
    FileSeek $1 0 END
    FileWrite $1 "autostart-restore: $0\autostart.pref: $2$\r$\n"
    FileClose $1
  omni_as_end:
  Pop $2
  Pop $1
  Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro OMNI_STOP_INSTALLED
  !insertmacro OMNI_REMOVE_PAYLOAD
!macroend

!macro NSIS_HOOK_POSTINSTALL
  !insertmacro OMNI_RESTORE_AUTOSTART
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro OMNI_STOP_INSTALLED
  !insertmacro OMNI_REMOVE_PAYLOAD
!macroend
