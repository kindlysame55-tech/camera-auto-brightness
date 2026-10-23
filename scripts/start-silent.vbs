' ============================================================
'  摄像头自动亮度 —— 无窗口启动器
'
'  为什么需要 .vbs：双击 .cmd 会先弹出一个黑底命令框（即使程序本身
'  没有窗口）。用 WScript.Shell.Run 的第 2 个参数 = 0 可以完全隐藏窗口，
'  这样用户只会看到右下角的托盘图标，不会有任何命令框。
'
'  用法：双击本文件即可（也可以放到「启动」文件夹实现开机自启，
'  或由 packaging\install_autostart.ps1 自动创建快捷方式）。
' ============================================================
Option Explicit

Dim fso, shell, root, exeTray, exeCli, pyw, cmd, ps
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set ps = shell.Environment("PROCESS")

' 本文件所在目录的上一级 = 项目根
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

exeTray = root & "\dist\CamabTray.exe"
exeCli = root & "\dist\Camab.exe"
pyw = root & "\.venv\Scripts\pythonw.exe"

If fso.FileExists(exeTray) Then
    cmd = """" & exeTray & """"
ElseIf fso.FileExists(pyw) Then
    ' 源码运行：pythonw 本身没有控制台；PYTHONPATH 指向 src 以便 import camab
    ps("PYTHONPATH") = root & "\src"
    ps("PYTHONUTF8") = "1"
    cmd = """" & pyw & """ -m camab tray"
Else
    ' 两条路都没有：给用户一个能看懂的提示，不要静默消失
    MsgBox "找不到可运行的程序。" & vbCrLf & vbCrLf & _
           "已找过：" & vbCrLf & _
           "  1) " & exeTray & vbCrLf & _
           "  2) " & pyw & vbCrLf & vbCrLf & _
           "如果是刚打包到一半，等打包完成再试；" & vbCrLf & _
           "否则请先在项目目录里运行：" & vbCrLf & _
           "  packaging\build_exe.ps1", _
           16, "摄像头自动亮度"
    WScript.Quit 1
End If

' 第 2 参数 0 = 隐藏窗口，第 3 参数 False = 不等待
shell.CurrentDirectory = root
shell.Run cmd, 0, False
