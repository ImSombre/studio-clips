' Studio Clips : c'est ce fichier qu'ouvre le raccourci du Bureau.
' Il lance l'appli sans fenetre noire, ou previent si l'installation n'est pas finie.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dossier = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = dossier & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(dossier & "\.installe") Or Not fso.FileExists(pyw) Then
  MsgBox "Studio Clips est encore en train de s'installer." & vbCrLf & vbCrLf & _
         "Attends la fin de la fenetre d'installation (elle se ferme toute seule et ouvre l'appli)." & vbCrLf & _
         "Si cette fenetre n'est plus ouverte, relance Studio-Clips.exe.", vbInformation, "Studio Clips"
Else
  sh.Run """" & pyw & """ """ & dossier & "\lanceur.py""", 0, False
End If
