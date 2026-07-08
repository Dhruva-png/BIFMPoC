' Launches the BIFM auto-processor completely invisibly (no black window,
' no taskbar icon). Double-clicking this file, or Task Scheduler running
' it, both start the watcher silently in the background.
'
' Assumes this file sits in the project root, next to streamlit_app.py.
' If you move it, update PROJECT_DIR below to the full path of the folder
' that contains streamlit_app.py.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

PROJECT_DIR = fso.GetParentFolderName(WScript.ScriptFullName)

' --- EDIT THIS LINE ---
' Full path to pythonw.exe on this machine. Find it by running:
'   python -c "import sys; print(sys.executable)"
' and swapping "python.exe" for "pythonw.exe" in the path it prints.
PYTHONW_PATH = "C:\Users\dhruv\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\pythonw.exe"

' 0 = hidden window, False = don't wait for it to finish (run forever).
shell.CurrentDirectory = PROJECT_DIR
shell.Run """" & PYTHONW_PATH & """ -m app.scripts.watch_and_process", 0, False
