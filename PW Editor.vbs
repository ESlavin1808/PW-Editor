' PW Editor.vbs — Запуск PW Editor без консольного окна
' Используйте этот файл для повседневной работы.
' Для отладки (видеть вывод ошибок) используйте PW Editor.bat

Dim shell, fso
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Переходим в папку со скриптом
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

' Запускаем pythonw.exe без консоли (SW_HIDE = 0)
' Если venv есть — используем его, иначе системный pythonw
Dim pywPath
pywPath = fso.GetParentFolderName(WScript.ScriptFullName) & "\venv\Scripts\pythonw.exe"

If fso.FileExists(pywPath) Then
    shell.Run """" & pywPath & """ app.py", 0, False
Else
    shell.Run "pythonw.exe app.py", 0, False
End If
