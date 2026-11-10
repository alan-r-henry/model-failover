' Starts OmniRoute (free tier), the model-failover gateway and the Claude-limit gateway/proxy
' with NO visible window at all (window style 0 = never shown, so nothing flashes over a game).
' Run once at login by the ModelFailoverGateway scheduled task. There is no repeating timer.
' Each service refuses to start twice (its port is taken), so running this again is harmless.
Option Explicit
Dim sh, dir, py, om
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe")
om = sh.ExpandEnvironmentStrings("%APPDATA%\npm\omniroute.cmd")

sh.Environment("Process")("OMNIROUTE_SERVER_HOST") = "127.0.0.1"   ' never expose OmniRoute to the network
sh.Run "cmd /c """ & om & """ serve --daemon --no-open", 0, False
WScript.Sleep 15000                                                  ' let OmniRoute bind its port
sh.Run """" & py & """ -u """ & dir & "failover.py"" serve --port 20130", 0, False
sh.Run """" & py & """ -u """ & dir & "claude-gateway.py"" serve --port 20135", 0, False
