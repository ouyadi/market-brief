' run-hidden.vbs -- launch a command invisibly from Task Scheduler.
'
' Why this exists: Task Scheduler tasks with Action.Execute=powershell.exe
' flash a console window for ~1 second every time they fire, even when
' the .ps1 immediately calls Start-Process -WindowStyle Hidden. The
' flashing window belongs to the parent powershell.exe, not the child
' it spawns -- -WindowStyle Hidden takes effect AFTER PowerShell has
' already created its console.
'
' wscript.exe + this VBS is the historical Windows fix:
'   - wscript.exe itself has no console (it's a windowless host).
'   - WshShell.Run(cmd, 0, True) spawns the child with windowStyle=Hidden
'     from the moment of CreateProcess, so PowerShell never gets a console,
'     while still waiting for the child to exit. This lets Task Scheduler's
'     running/success/failure state reflect the actual child process.
'
' Usage from a Task Scheduler action:
'   Execute:  wscript.exe
'   Argument: "<repo>\run-hidden.vbs" "<exe>" "<arg1>" "<arg2>" ...
'
' This script quote-wraps every received argument and concatenates them
' into a single command line, then runs that invisibly. Args containing
' spaces survive intact because each is rewrapped in double quotes.

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

Dim q, cmdLine, i
q = Chr(34)
cmdLine = q & WScript.Arguments(0) & q
For i = 1 To WScript.Arguments.Count - 1
    cmdLine = cmdLine & " " & q & WScript.Arguments(i) & q
Next

WScript.Quit CreateObject("WScript.Shell").Run(cmdLine, 0, True)
