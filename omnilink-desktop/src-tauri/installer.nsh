; OmniLink desktop installer additions.
; The per-machine install writes shortcuts for all users. Tauri's template
; already creates the Start Menu entry; we add the desktop shortcut explicitly
; so it is always present regardless of installer page choices.

!macro NSIS_HOOK_POSTINSTALL
  CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  Delete "$DESKTOP\${PRODUCTNAME}.lnk"
!macroend
