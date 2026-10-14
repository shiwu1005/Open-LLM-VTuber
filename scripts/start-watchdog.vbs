' ============================================================================
'  Start Open-LLM-VTuber in the background (no console window)
'
'  Use this for daily use. To stop it, end the python.exe process in
'  Task Manager, or run start.bat once to see the console.
'
'  For FIRST-TIME setup use this order instead:
'     1. powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
'     2. scripts\start.bat        (foreground - so you can see any errors)
'
'  NOTE: this launcher does NOT auto-restart the server if it crashes.
'  A restart-on-crash watchdog needs machine-specific absolute paths, so it
'  is not shipped here. If you want one, see docs\服务守护操作卡.md.
' ============================================================================

Option Explicit

Dim shell, fso, here, batPath, cmd
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

' resolve paths relative to this script's location
here    = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = fso.BuildPath(here, "start.bat")

If Not fso.FileExists(batPath) Then
    MsgBox "Cannot find start.bat next to this script:" & vbCrLf & batPath, 16, "Open-LLM-VTuber"
    WScript.Quit 1
End If

cmd = "cmd.exe /c """ & batPath & """"

' 0 = hidden window, False = do not wait for it to finish
shell.Run cmd, 0, False
