Unicode true
Name "Verse Lab"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Verse Lab"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetOverwrite on
Icon "${PAYLOAD}\dist\verse-lab.ico"
UninstallIcon "${PAYLOAD}\dist\verse-lab.ico"

!include "MUI2.nsh"
!include "x64.nsh"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Verse Lab.exe"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "Verse Lab requires 64-bit Windows."
    Abort
  ${EndIf}
  System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w "Local\VerseLabDesktop") p .r0'
  ${If} $0 <> 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_OK|MB_ICONSTOP "Exit Verse Lab from its tray menu, then run Setup again."
    Abort
  ${EndIf}
FunctionEnd

Section "Verse Lab (required)" SEC_APP
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r /x config.json "${PAYLOAD}\*"
  SetOverwrite off
  File "${PAYLOAD}\config.json"
  SetOverwrite on
  DetailPrint "Installing the audio tools from their original publishers..."
  nsExec::ExecToLog '"$INSTDIR\runtime\python\python.exe" "$INSTDIR\setup\runtime_dependencies.py"'
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_OK|MB_ICONSTOP "The audio tools could not be downloaded or verified. Check your connection and run Setup again."
    SetErrorLevel 1
    Abort
  ${EndIf}
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\Verse Lab.lnk" "$INSTDIR\Verse Lab.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab" "DisplayName" "Verse Lab"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab" "DisplayIcon" "$INSTDIR\Verse Lab.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab" "Publisher" "MrBoxik"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd

Section /o "Offline transcription model (3.1 GB download)" SEC_MODEL
  DetailPrint "Downloading and verifying Whisper Large-v3..."
  nsExec::ExecToLog '"$INSTDIR\runtime\python\python.exe" "$INSTDIR\setup_transcription.py" --download-only'
  Pop $0
  ${If} $0 != "0"
    MessageBox MB_OK|MB_ICONEXCLAMATION "Model setup failed. You can still use Verse Lab. Run Setup again to retry the model."
    SetErrorLevel 1
  ${EndIf}
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_APP} "Installs the Windows player, taskbar setup files, and audio tools."
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_MODEL} "Adds local transcription for songs without matching synced lyrics."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Delete "$SMPROGRAMS\Verse Lab.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VerseLab"
  Delete "$INSTDIR\Verse Lab.exe"
  Delete "$INSTDIR\app.py"
  Delete "$INSTDIR\audio_alignment.py"
  Delete "$INSTDIR\audio_tags.py"
  Delete "$INSTDIR\desktop_bridge.py"
  Delete "$INSTDIR\gpu_budget.py"
  Delete "$INSTDIR\lyric_pipeline.py"
  Delete "$INSTDIR\setup_transcription.py"
  Delete "$INSTDIR\transcription.py"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\THIRD_PARTY.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR\dist"
  RMDir /r "$INSTDIR\setup\taskbar"
  Delete "$INSTDIR\setup\runtime_dependencies.py"
  RMDir "$INSTDIR\setup"
  RMDir /r "$INSTDIR\runtime\python"
  RMDir /r "$INSTDIR\runtime\python-deps"
  RMDir /r "$INSTDIR\runtime\ffmpeg"
  RMDir /r "$INSTDIR\runtime\lyrics-gpu"
  Delete "$INSTDIR\runtime\installed-dependencies.json"
  RMDir "$INSTDIR\runtime"
  RMDir "$INSTDIR"
SectionEnd
