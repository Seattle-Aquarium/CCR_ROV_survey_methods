' ---------------------------------------------------------------------------
'  ROV Flight Operations - the quiet way in.
'
'  run_rov_flight_ops.bat is still the thing that does the work. What this adds
'  is the one thing a batch file cannot do for itself: start without leaving a
'  command prompt sitting on the taskbar for the whole survey day. The desktop
'  shortcut points here.
'
'  It is deliberately not a silent launcher.
'
'    * The first run, and any run that has to rebuild the environment, is shown
'      in a visible window -- it takes minutes, it needs the network, and a
'      progress-less wait with nothing on screen is how somebody concludes the
'      program is broken and gives up.
'    * A normal run is hidden, and everything it prints is written to
'      launch.log beside the environment.
'    * A run that fails says so, in a dialog, with the log's path in it -- and
'      offers to start again with the window visible so the error can be read.
'
'  Nothing here needs administrator rights.
' ---------------------------------------------------------------------------
Option Explicit

Dim shell, fso, here, batch, envRoot, logPath, cmd, code, answer
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Where this script is, which is the program's folder in the repository. Taken
' from the script rather than from the working directory, so the shortcut, a
' Run box and a double-click all behave the same.
here = fso.GetParentFolderName(WScript.ScriptFullName)
batch = fso.BuildPath(here, "run_rov_flight_ops.bat")

If Not fso.FileExists(batch) Then
    MsgBox "Could not find:" & vbCrLf & vbCrLf & batch & vbCrLf & vbCrLf & _
           "The shortcut points at a copy of the repository that has moved " & _
           "or been renamed. Run 'Create Desktop Shortcut.bat' from the " & _
           "repository again to point it at the new location.", _
           vbCritical, "ROV Flight Operations"
    WScript.Quit 1
End If

envRoot = fso.BuildPath(shell.ExpandEnvironmentStrings("%LOCALAPPDATA%"), _
                        "CCR_ROV\rov_flight_ops")
If Not fso.FolderExists(envRoot) Then
    CreateFolderTree envRoot
End If
logPath = fso.BuildPath(envRoot, "launch.log")

' A missing environment means a first run: minutes of installing, with output
' worth watching. Show it, and wait, so a second double-click does not start a
' second install on top of the first.
If Not fso.FileExists(fso.BuildPath(envRoot, "venv\Scripts\pythonw.exe")) Then
    shell.CurrentDirectory = here
    code = shell.Run("""" & batch & """", 1, True)
    WScript.Quit code
End If

' The ordinary run: hidden, with everything it says kept.
shell.CurrentDirectory = here
cmd = "cmd /c """"" & batch & """ --silent > """ & logPath & """ 2>&1"""
code = shell.Run(cmd, 0, True)

If code <> 0 Then
    answer = MsgBox("ROV Flight Operations stopped with an error (code " & _
        code & ")." & vbCrLf & vbCrLf & _
        "What it printed is in:" & vbCrLf & logPath & vbCrLf & vbCrLf & _
        "The diagnostics log has more, and the Diagnostics button in the " & _
        "window opens it." & vbCrLf & vbCrLf & _
        "Start again with the window visible, so the error can be read?", _
        vbYesNo + vbExclamation, "ROV Flight Operations")
    If answer = vbYes Then
        shell.Run("""" & batch & """ --console")
    End If
End If

WScript.Quit code


' Makes a folder and every parent it needs. FileSystemObject will not.
Sub CreateFolderTree(path)
    Dim parent
    If fso.FolderExists(path) Then Exit Sub
    parent = fso.GetParentFolderName(path)
    If parent <> "" And Not fso.FolderExists(parent) Then CreateFolderTree parent
    On Error Resume Next
    fso.CreateFolder path
    On Error GoTo 0
End Sub
