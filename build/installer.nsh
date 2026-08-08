; Krypt Scout — NSIS customizations for the electron-builder installer.
; Referenced by package.json → build.nsis.include.
;
; The app supervises a PyInstaller backend (krypt-scout-backend.exe, see
; electron/system/python-backend.ts) as a CHILD process. electron-builder's
; own NSIS boilerplate knows how to close "Krypt Scout.exe" before it
; touches the install directory, but it knows nothing about that child —
; and a live backend holds resources\python\krypt-scout-backend\ open, so
; an in-place upgrade or an uninstall fails to replace/delete those files.
;
; Each macro below reaps any lingering backend first. Both are best-effort:
; taskkill returns 128 ("no such process") on the normal path where nothing
; is running, and nsExec::Exec discards the code rather than aborting the
; install.

!macro killBackend
  DetailPrint "Stopping any running Krypt Scout backend..."
  nsExec::Exec 'taskkill /F /T /IM krypt-scout-backend.exe'
  Pop $0
!macroend

; Installer .onInit — runs before files are written (fresh install + upgrade).
!macro customInit
  !insertmacro killBackend
!macroend

; Uninstaller un.onInit — runs before files are removed.
!macro customUnInit
  !insertmacro killBackend
!macroend
