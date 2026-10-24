
#define MyAppName "HOTS Uninstaller"
#define MyAppVersion "1.0"
#define MyAppPublisher "Darsono"
#define MyAppExeName "HOTS Uninstaller.exe"
#expr EmitLanguagesSection

[Setup]
AppId={{6F2C7C1E-9C54-4C1E-8B9E-6D3F0B2A5D71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}.0.0
VersionInfoTextVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Setup
DefaultDirName={autopf}\{#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
LicenseFile=LICENSE.txt
OutputDir=Output
OutputBaseFilename=HOTS_Uninstaller_setup
SetupIconFile=icon.ico
SolidCompression=yes
WizardStyle=modern dynamic
; The same mutex the launcher holds while the program runs (session-local namespace, so the
; name must match exactly), so Setup detects a running instance and asks to close it before
; it touches any file in {app}, on install/update as well as on uninstall.
AppMutex=Local\HOTS_Uninstaller_SingleInstance_Mutex

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "explorercontext"; Description: "{cm:ContextMenuTask}"; Flags: unchecked

[CustomMessages]
ContextMenuTask=Add "Remove with HOTS" to the context menu

[Files]
Source: "dist\hots_uninstaller_launcher.dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\hots_uninstaller_launcher.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "hots_uninstall_cleanup.cmd"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; The program runs elevated, so a standard user must not be able to replace its files.
Name: "{app}"; Permissions: admins-full system-full users-readexec

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.exe\shell\HOTSRemove"; ValueType: string; ValueName: ""; ValueData: "{code:GetContextMenuLabel}"; Flags: uninsdeletekey; Tasks: explorercontext
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.exe\shell\HOTSRemove"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: explorercontext
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.exe\shell\HOTSRemove\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" --remove ""%1"""; Tasks: explorercontext

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\hots_uninstall_cleanup.cmd"; Parameters: """{app}\hots_uninst_choices.json"""; Flags: runhidden waituntilterminated

[UninstallDelete]
; hots_uninstall_cleanup.cmd cleans %APPDATA%\HOTS Uninstaller (user data) and runs BEFORE
; this step (UninstallRun finishes before Inno removes {app}). {app} holds no user
; data, only the program's own files, so the whole install folder is deleted,
; including files outside [Files] (logs, cache, etc.).
Type: filesandordirs; Name: "{app}"; Check: IsSafeUninstallDir

[Code]

// The install folder MUST be named like the product. The user can type any
// directory (e.g. Documents, Desktop, C:\ or /DIR=... in silent mode), and below
// the whole {app} is deleted (before an update and on uninstall); without this
// check it would delete everything that lies there.
function IsSafeAppDir(Dir: String): Boolean;
var
  D: String;
begin
  D := RemoveBackslash(Trim(Dir));
  Result := (Length(D) > 3) and (CompareText(ExtractFileName(D), '{#MyAppName}') = 0);
end;

function IsSafeUninstallDir(): Boolean;
begin
  Result := IsSafeAppDir(ExpandConstant('{app}'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if not IsSafeAppDir(WizardDirValue) then
      WizardForm.DirEdit.Text := AddBackslash(RemoveBackslash(Trim(WizardDirValue))) + '{#MyAppName}';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssInstall) and IsSafeAppDir(ExpandConstant('{app}')) then
  begin
    // {app} holds no user data (settings, uninstall history, monitor logs and registry
    // backups are in %APPDATA%\HOTS Uninstaller, outside {app}), so the whole folder is
    // deleted before copying new files, on first install and on update alike. Without
    // this, files removed/renamed in a newer build would stay there forever.
    DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;

function GetAppLanguage(): String;
var
  SettingsPath: String;
  Lines: TArrayOfString;
  I, P1, P2: Integer;
  Line, Lang: String;
begin
  Result := 'en';
  SettingsPath := ExpandConstant('{userappdata}') + '\HOTS Uninstaller\settings.json';
  if not FileExists(SettingsPath) then
    Exit;
  if not LoadStringsFromFile(SettingsPath, Lines) then
    Exit;

  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[I];
    if Pos('"language"', Line) > 0 then
    begin
      P1 := Pos(':', Line);
      if P1 = 0 then
        Continue;
      Line := Copy(Line, P1 + 1, Length(Line) - P1);
      P1 := Pos('"', Line);
      if P1 = 0 then
        Continue;
      Line := Copy(Line, P1 + 1, Length(Line) - P1);
      P2 := Pos('"', Line);
      if P2 = 0 then
        Continue;
      Lang := Copy(Line, 1, P2 - 1);

      if (Lang = 'pl') or (Lang = 'fr') or (Lang = 'de') or (Lang = 'es')
         or (Lang = 'en') then
        Result := Lang;
      Exit;
    end;
  end;
end;

function GetContextMenuLang(): String;
var
  L: String;
begin
  L := Lowercase(ActiveLanguage());
  if (L = 'pl') or (L = 'polish') then Result := 'pl'
  else if (L = 'fr') or (L = 'french') then Result := 'fr'
  else if (L = 'de') or (L = 'german') then Result := 'de'
  else if (L = 'es') or (L = 'spanish') then Result := 'es'
  else if (L = 'en') or (L = 'english') then Result := 'en'
  else Result := GetAppLanguage();
end;

function GetContextMenuLabel(Param: String): String;
var
  Lang: String;
begin
  Lang := GetContextMenuLang();
  if Lang = 'pl' then Result := 'Usuń z HOTS'
  else if Lang = 'fr' then Result := 'Supprimer avec HOTS'
  else if Lang = 'de' then Result := 'Mit HOTS entfernen'
  else if Lang = 'es' then Result := 'Eliminar con HOTS'
  else Result := 'Remove with HOTS';
end;

function GetContextMenuTaskText(): String;
var
  Lang: String;
begin
  Lang := GetContextMenuLang();
  if Lang = 'pl' then
    Result := 'Dodaj „Usuń z HOTS” do menu kontekstowego'
  else if Lang = 'fr' then
    Result := 'Ajouter « Supprimer avec HOTS » au menu contextuel'
  else if Lang = 'de' then
    Result := 'Im Kontextmenü „Mit HOTS entfernen“ hinzufügen'
  else if Lang = 'es' then
    Result := 'Añadir «Eliminar con HOTS» al menú contextual'
  else
    Result := CustomMessage('ContextMenuTask');
end;

procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
begin
  if CurPageID = wpSelectTasks then
  begin
    for I := 0 to WizardForm.TasksList.Items.Count - 1 do
      if WizardForm.TasksList.ItemCaption[I] = CustomMessage('ContextMenuTask') then
        WizardForm.TasksList.ItemCaption[I] := GetContextMenuTaskText();
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Tasks: String;
begin
  Tasks := MemoTasksInfo;
  StringChangeEx(Tasks, CustomMessage('ContextMenuTask'), GetContextMenuTaskText(), True);

  Result := '';
  if MemoUserInfoInfo <> '' then
    Result := Result + MemoUserInfoInfo + NewLine + NewLine;
  if MemoDirInfo <> '' then
    Result := Result + MemoDirInfo + NewLine + NewLine;
  if MemoTypeInfo <> '' then
    Result := Result + MemoTypeInfo + NewLine + NewLine;
  if MemoComponentsInfo <> '' then
    Result := Result + MemoComponentsInfo + NewLine + NewLine;
  if MemoGroupInfo <> '' then
    Result := Result + MemoGroupInfo + NewLine + NewLine;
  if Tasks <> '' then
    Result := Result + Tasks;
end;

function GetUpdateVsUninstallText(): String;
var
  Lang: String;
begin
  Lang := GetAppLanguage();

  if Lang = 'pl' then
    Result :=
      'Jeśli planujesz zainstalować nowszą wersję HOTS Uninstaller, nie musisz najpierw ' +
      'odinstalowywać programu — wystarczy uruchomić nowy instalator bezpośrednio. ' +
      'Zaktualizuje on program w miejscu, bez utraty ustawień ani historii.' + #13#10 + #13#10 +
      'Czy mimo to chcesz kontynuować i całkowicie usunąć HOTS Uninstaller?'
  else if Lang = 'fr' then
    Result :=
      'Si vous êtes sur le point d''installer une version plus récente de HOTS Uninstaller, ' +
      'il n''est pas nécessaire de désinstaller d''abord - lancez simplement le nouvel ' +
      'installateur directement. Il mettra à jour le programme sur place, sans perdre ' +
      'vos paramètres ni votre historique.' + #13#10 + #13#10 +
      'Voulez-vous tout de même continuer et supprimer complètement HOTS Uninstaller ?'
  else if Lang = 'de' then
    Result :=
      'Wenn Sie eine neuere Version von HOTS Uninstaller installieren möchten, müssen Sie ' +
      'das Programm nicht vorher deinstallieren - führen Sie einfach das neue ' +
      'Installationsprogramm direkt aus. Es aktualisiert das Programm an Ort und Stelle, ' +
      'ohne Ihre Einstellungen oder Ihren Verlauf zu verlieren.' + #13#10 + #13#10 +
      'Möchten Sie trotzdem fortfahren und HOTS Uninstaller vollständig entfernen?'
  else if Lang = 'es' then
    Result :=
      'Si estás a punto de instalar una versión más reciente de HOTS Uninstaller, no es ' +
      'necesario desinstalar primero - simplemente ejecuta el nuevo instalador ' +
      'directamente. Actualizará el programa en su lugar, sin perder tu configuración ' +
      'ni tu historial.' + #13#10 + #13#10 +
      '¿Deseas continuar de todos modos y eliminar completamente HOTS Uninstaller?'
  else
    Result :=
      'If you are about to install a newer version of HOTS Uninstaller, you do NOT need to ' +
      'uninstall first - just run the new installer directly. It will update the ' +
      'program in place, without losing your settings or history.' + #13#10 + #13#10 +
      'Do you want to continue and completely remove HOTS Uninstaller?';
end;

function ChoicesFile(): String;
begin
  Result := ExpandConstant('{app}\hots_uninst_choices.json');
end;

function GetWizardFailedText(): String;
var
  Lang: String;
begin
  Lang := GetAppLanguage();

  if Lang = 'pl' then
    Result :=
      'Nie udało się otworzyć okna wyboru, co zrobić z zapisanymi danymi. Jeśli kontynuujesz, ' +
      'program zostanie odinstalowany, ale Twoje ustawienia, historia i kopie zapasowe pozostaną ' +
      'na komputerze.' + #13#10 + #13#10 +
      'Czy mimo to kontynuować?'
  else if Lang = 'fr' then
    Result :=
      'La fenêtre permettant de choisir quoi faire des données enregistrées n''a pas pu s''ouvrir. ' +
      'Si vous continuez, le programme sera désinstallé, mais vos paramètres, votre historique et ' +
      'vos sauvegardes resteront sur cet ordinateur.' + #13#10 + #13#10 +
      'Continuer quand même ?'
  else if Lang = 'de' then
    Result :=
      'Das Fenster zur Auswahl, was mit den gespeicherten Daten geschehen soll, konnte nicht ' +
      'geöffnet werden. Wenn Sie fortfahren, wird das Programm deinstalliert, Ihre Einstellungen, ' +
      'Ihr Verlauf und Ihre Sicherungen bleiben jedoch auf diesem Computer.' + #13#10 + #13#10 +
      'Trotzdem fortfahren?'
  else if Lang = 'es' then
    Result :=
      'No se pudo abrir la ventana para elegir qué hacer con los datos guardados. Si continúas, ' +
      'el programa se desinstalará, pero tu configuración, tu historial y tus copias de seguridad ' +
      'permanecerán en este equipo.' + #13#10 + #13#10 +
      '¿Continuar de todos modos?'
  else
    Result :=
      'The window for choosing what to do with the saved data could not be opened. If you ' +
      'continue, the program will be uninstalled, but your settings, history and backups will ' +
      'stay on this computer.' + #13#10 + #13#10 +
      'Continue anyway?';
end;

// Shows the "keep or delete saved data?" wizard before anything is removed, so closing it or
// pressing Cancel really cancels the uninstall. The choice is applied later by
// hots_uninstall_cleanup.cmd. Exit codes of the wizard: 0 = confirmed, 3 = cancelled.
function RunUninstallWizard(): Boolean;
var
  ResultCode: Integer;
  Launched: Boolean;
begin
  Result := True;
  if UninstallSilent then
    Exit;

  DeleteFile(ChoicesFile());
  Launched := Exec(ExpandConstant('{app}\{#MyAppExeName}'),
                   '--uninstall-wizard "' + ChoicesFile() + '"',
                   '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode);

  if Launched and (ResultCode = 3) then
    Result := False
  else if not (Launched and (ResultCode = 0) and FileExists(ChoicesFile())) then
    Result := MsgBox(GetWizardFailedText(), mbError, MB_YESNO or MB_DEFBUTTON2) = IDYES;

  if not Result then
    DeleteFile(ChoicesFile());
end;

procedure DeinitializeUninstall();
begin
  DeleteFile(ChoicesFile());
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;

  if not UninstallSilent then
  begin
    if MsgBox(GetUpdateVsUninstallText(), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;

  Result := RunUninstallWizard();
end;
