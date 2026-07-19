; Copyright 2026 Julien Bombled
;
; Licensed under the Apache License, Version 2.0 (the "License");
; you may not use this file except in compliance with the License.
; You may obtain a copy of the License at
;
;     http://www.apache.org/licenses/LICENSE-2.0
;
; Unless required by applicable law or agreed to in writing, software
; distributed under the License is distributed on an "AS IS" BASIS,
; WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
; See the License for the specific language governing permissions and
; limitations under the License.

#ifndef AppVersion
  #error AppVersion must be provided with ISCC /DAppVersion=<version>
#endif

[Setup]
AppId=Datacron
AppName=Datacron
AppPublisher=Julien Bombled
AppVersion={#AppVersion}
AppVerName=Datacron {#AppVersion}
DefaultDirName={localappdata}\Programs\Datacron
DefaultGroupName=Datacron
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
UninstallDisplayName=Datacron
UninstallDisplayIcon={app}\datacron.exe
OutputDir=..\..\dist-installer
OutputBaseFilename=Datacron-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=force
CloseApplicationsFilter=datacron.exe
RestartApplications=no
#ifdef InstallerSignTool
SignTool={#InstallerSignTool}
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[CustomMessages]
english.ReinstallCaption=Existing Datacron configuration
english.ReinstallDescription=Choose what this installation should do with the selected vault.
english.ReinstallSubCaption=Keep preserves your configuration and generated index. Reset removes them before setup; note identities, audit data, logs, and Markdown files remain unchanged.
english.KeepConfig=Keep my current Datacron configuration (recommended)
english.ResetConfig=Reset configuration and rebuild the generated index
english.VaultPageCaption=Markdown vault
english.VaultPageDescription=Choose the folder Datacron should serve.
english.VaultPageSubCaption=You can start with an empty folder and add Markdown notes later.
english.IndexNow=Index this vault now
english.VaultRequiredSilent=/VAULT=<path> is required for a silent Datacron installation.
english.SetupFailed=Datacron was installed, but automatic setup failed. Correct the problem, then run Datacron Setup from the Start menu. Setup will return a failure exit code.
english.PathFailed=Datacron could not add its application folder to your user PATH.
english.DirectoryFailed=Datacron could not create the selected vault folder:
english.RegistryFailed=Datacron could not save the selected vault for reinstall and uninstall. Automatic setup was not started.
english.ResetFailed=Datacron could not reset its configuration and generated index. Close every AI application using Datacron, then run the installer again.
english.UnregisterFailed=Datacron could not remove every MCP client entry. The operation will continue; review the client configurations manually.
english.ProtocolFailed=Datacron was registered, but its memory instructions could not be installed in every detected AI client. Run Datacron Setup again or use "datacron protocol install --client all".
english.ProtocolRemoveFailed=Datacron could not remove every installed memory-instruction block. The operation will continue; review the client instruction files manually.
french.ReinstallCaption=Configuration Datacron existante
french.ReinstallDescription=Choisissez ce que cette installation doit faire avec le vault selectionne.
french.ReinstallSubCaption=Garder conserve la configuration et l'index genere. Reinitialiser les supprime avant le setup ; les identites de notes, l'audit, les logs et les fichiers Markdown restent inchanges.
french.KeepConfig=Garder ma configuration Datacron actuelle (recommande)
french.ResetConfig=Reinitialiser la configuration et reconstruire l'index genere
french.VaultPageCaption=Vault Markdown
french.VaultPageDescription=Choisissez le dossier que Datacron doit servir.
french.VaultPageSubCaption=Vous pouvez commencer avec un dossier vide et ajouter des notes Markdown plus tard.
french.IndexNow=Indexer ce vault maintenant
french.VaultRequiredSilent=/VAULT=<chemin> est obligatoire pour une installation silencieuse de Datacron.
french.SetupFailed=Datacron a ete installe, mais la configuration automatique a echoue. Corrigez le probleme, puis lancez Datacron Setup depuis le menu Demarrer. Le setup retournera un code d'echec.
french.PathFailed=Datacron n'a pas pu ajouter son dossier d'application au PATH utilisateur.
french.DirectoryFailed=Datacron n'a pas pu creer le dossier de vault selectionne :
french.RegistryFailed=Datacron n'a pas pu memoriser le vault pour la reinstallation et la desinstallation. Le setup automatique n'a pas ete lance.
french.ResetFailed=Datacron n'a pas pu reinitialiser sa configuration et son index genere. Fermez toutes les applications IA utilisant Datacron, puis relancez l'installeur.
french.UnregisterFailed=Datacron n'a pas pu retirer toutes les entrees des clients MCP. L'operation continue ; verifiez manuellement les configurations clientes.
french.ProtocolFailed=Datacron a ete enregistre, mais ses instructions memoire n'ont pas pu etre installees dans tous les clients IA detectes. Relancez Datacron Setup ou utilisez "datacron protocol install --client all".
french.ProtocolRemoveFailed=Datacron n'a pas pu retirer tous les blocs d'instructions memoire installes. L'operation continue ; verifiez manuellement les fichiers d'instructions des clients.

[Files]
Source: "..\..\dist\datacron.exe"; DestDir: "{app}"; DestName: "datacron.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\Datacron Status"; Filename: "{cmd}"; Parameters: "{code:StatusShortcutParameters}"
Name: "{group}\Datacron Setup"; Filename: "{cmd}"; Parameters: "{code:SetupShortcutParameters}"

[Code]
const
  InstallerStateKey = 'Software\Datacron';
  InstallerVaultValue = 'VaultRoot';

var
  VaultPage: TInputDirWizardPage;
  ReinstallPage: TInputOptionWizardPage;
  IndexNowCheckBox: TNewCheckBox;
  PreviousVaultPath: String;
  LastDetectedVaultPath: String;
  VaultPath: String;
  ExistingConfigDetected: Boolean;
  SetupFailed: Boolean;

function CommandLineSwitchPresent(const Name: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), '/' + Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function CommandLineValue(const Name: String): String;
begin
  Result := Trim(ExpandConstant('{param:' + Name + '|}'));
end;

function SelectedVaultPath: String;
begin
  if WizardSilent then
    Result := CommandLineValue('VAULT')
  else
    Result := Trim(VaultPage.Values[0]);
end;

function EffectiveVaultPath: String;
begin
  if VaultPath <> '' then
    Result := VaultPath
  else
    Result := SelectedVaultPath;
end;

function ResetConfigurationRequested: Boolean;
begin
  if CommandLineSwitchPresent('RESETCONFIG') then
    Result := True
  else if WizardSilent then
    Result := False
  else
    Result := ExistingConfigDetected and (ReinstallPage.SelectedValueIndex = 1);
end;

function ShouldIndexNow: Boolean;
begin
  if WizardSilent then
    Result := CommandLineSwitchPresent('INDEX')
  else
    Result := IndexNowCheckBox.Checked;
end;

function ShortcutParameters(const Subcommand: String): String;
var
  Command: String;
begin
  Command := AddQuotes(ExpandConstant('{app}\datacron.exe')) +
    ' ' + Subcommand + ' --vault ' + AddQuotes(EffectiveVaultPath);
  Result := '/k "' + Command + '"';
end;

function StatusShortcutParameters(Param: String): String;
begin
  Result := ShortcutParameters('status');
end;

function SetupShortcutParameters(Param: String): String;
begin
  Result := ShortcutParameters('setup');
end;

procedure InitializeWizard;
var
  InitialPath: String;
begin
  PreviousVaultPath := '';
  RegQueryStringValue(
    HKCU,
    InstallerStateKey,
    InstallerVaultValue,
    PreviousVaultPath
  );

  InitialPath := CommandLineValue('VAULT');
  if InitialPath = '' then
    InitialPath := Trim(PreviousVaultPath);
  if InitialPath = '' then
    InitialPath := ExpandConstant('{userdocs}\Datacron-Vault');

  VaultPage := CreateInputDirPage(
    wpWelcome,
    CustomMessage('VaultPageCaption'),
    CustomMessage('VaultPageDescription'),
    CustomMessage('VaultPageSubCaption'),
    False,
    ''
  );
  VaultPage.Add('');
  VaultPage.Values[0] := InitialPath;

  IndexNowCheckBox := TNewCheckBox.Create(VaultPage);
  IndexNowCheckBox.Parent := VaultPage.Surface;
  IndexNowCheckBox.Caption := CustomMessage('IndexNow');
  IndexNowCheckBox.Checked := True;
  IndexNowCheckBox.Top :=
    VaultPage.Edits[0].Top + VaultPage.Edits[0].Height + ScaleY(16);
  IndexNowCheckBox.Left := VaultPage.Edits[0].Left;
  IndexNowCheckBox.Width := VaultPage.Edits[0].Width;

  ReinstallPage := CreateInputOptionPage(
    VaultPage.ID,
    CustomMessage('ReinstallCaption'),
    CustomMessage('ReinstallDescription'),
    CustomMessage('ReinstallSubCaption'),
    True,
    False
  );
  ReinstallPage.Add(CustomMessage('KeepConfig'));
  ReinstallPage.Add(CustomMessage('ResetConfig'));
  if CommandLineSwitchPresent('RESETCONFIG') then
    ReinstallPage.SelectedValueIndex := 1
  else
    ReinstallPage.SelectedValueIndex := 0;

  ExistingConfigDetected := False;
  LastDetectedVaultPath := '';
  VaultPath := '';
  SetupFailed := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  CandidateVault: String;
begin
  Result := True;
  if CurPageID <> VaultPage.ID then
    Exit;

  CandidateVault := Trim(VaultPage.Values[0]);
  if CompareText(CandidateVault, LastDetectedVaultPath) <> 0 then
    ReinstallPage.SelectedValueIndex := 0;
  ExistingConfigDetected := (CandidateVault <> '') and FileExists(
    AddBackslash(CandidateVault) + '.datacron\VAULT.yaml'
  );
  if not ExistingConfigDetected then
    ReinstallPage.SelectedValueIndex := 0;
  LastDetectedVaultPath := CandidateVault;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if WizardSilent then
    Result := (PageID = VaultPage.ID) or (PageID = ReinstallPage.ID)
  else if PageID = ReinstallPage.ID then
    Result := not ExistingConfigDetected
  else
    Result := False;
end;

procedure MarkSetupFailure(const MessageText: String);
begin
  SetupFailed := True;
  Log('Datacron post-install setup failed: ' + MessageText);
  SuppressibleMsgBox(
    MessageText + #13#10#13#10 + CustomMessage('SetupFailed'),
    mbError,
    MB_OK,
    IDOK
  );
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  VaultPath := SelectedVaultPath;
  if VaultPath = '' then
  begin
    if WizardSilent then
      MarkSetupFailure(CustomMessage('VaultRequiredSilent'));
    Result := CustomMessage('VaultRequiredSilent');
    Exit;
  end;

  if not DirExists(VaultPath) and not ForceDirectories(VaultPath) then
    Result := CustomMessage('DirectoryFailed') + #13#10 + VaultPath;
end;

function NormalizePathEntry(const Value: String): String;
begin
  Result := Trim(Value);
  if (Length(Result) >= 2) and (Result[1] = '"') and
     (Result[Length(Result)] = '"') then
  begin
    Delete(Result, Length(Result), 1);
    Delete(Result, 1, 1);
  end;
  StringChangeEx(Result, '/', '\', True);
  while (Length(Result) > 3) and (Result[Length(Result)] = '\') do
    Delete(Result, Length(Result), 1);
end;

function UserPathContains(const UserPath, Entry: String): Boolean;
var
  Remaining: String;
  Part: String;
  SeparatorPosition: Integer;
begin
  Result := False;
  Remaining := UserPath;
  while True do
  begin
    SeparatorPosition := Pos(';', Remaining);
    if SeparatorPosition = 0 then
    begin
      Part := Remaining;
      Remaining := '';
    end
    else
    begin
      Part := Copy(Remaining, 1, SeparatorPosition - 1);
      Delete(Remaining, 1, SeparatorPosition);
    end;
    if CompareText(NormalizePathEntry(Part), NormalizePathEntry(Entry)) = 0 then
    begin
      Result := True;
      Exit;
    end;
    if SeparatorPosition = 0 then
      Exit;
  end;
end;

function AddAppToUserPath: Boolean;
var
  CurrentPath: String;
  AppPath: String;
  NewPath: String;
begin
  AppPath := ExpandConstant('{app}');
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    CurrentPath := '';
  if UserPathContains(CurrentPath, AppPath) then
  begin
    Result := True;
    Exit;
  end;
  if CurrentPath = '' then
    NewPath := AppPath
  else if CurrentPath[Length(CurrentPath)] = ';' then
    NewPath := CurrentPath + AppPath
  else
    NewPath := CurrentPath + ';' + AppPath;
  Result := RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
  if Result then
    Log('Added Datacron application directory to the user PATH.');
end;

function PathWithoutEntry(
  const UserPath, Entry: String;
  var Removed: Boolean
): String;
var
  Remaining: String;
  Part: String;
  SeparatorPosition: Integer;
  Finished: Boolean;
  WrotePart: Boolean;
begin
  Result := '';
  Removed := False;
  Remaining := UserPath;
  Finished := False;
  WrotePart := False;
  while not Finished do
  begin
    SeparatorPosition := Pos(';', Remaining);
    if SeparatorPosition = 0 then
    begin
      Part := Remaining;
      Remaining := '';
      Finished := True;
    end
    else
    begin
      Part := Copy(Remaining, 1, SeparatorPosition - 1);
      Delete(Remaining, 1, SeparatorPosition);
    end;

    if CompareText(NormalizePathEntry(Part), NormalizePathEntry(Entry)) = 0 then
      Removed := True
    else
    begin
      if WrotePart then
        Result := Result + ';';
      Result := Result + Part;
      WrotePart := True;
    end;
  end;
end;

procedure RemoveAppFromUserPath;
var
  CurrentPath: String;
  NewPath: String;
  Removed: Boolean;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    Exit;
  NewPath := PathWithoutEntry(CurrentPath, ExpandConstant('{app}'), Removed);
  if not Removed then
    Exit;
  if NewPath = '' then
    RegDeleteValue(HKCU, 'Environment', 'Path')
  else
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
  Log('Removed Datacron application directory from the user PATH.');
end;

procedure RunDatacronSetup;
var
  ExecutablePath: String;
  Parameters: String;
  ResetConfiguration: Boolean;
  ResultCode: Integer;
begin
  if not AddAppToUserPath then
  begin
    MarkSetupFailure(CustomMessage('PathFailed'));
    Exit;
  end;

  ExecutablePath := ExpandConstant('{app}\datacron.exe');
  if (Trim(PreviousVaultPath) <> '') and
     (CompareText(
       NormalizePathEntry(PreviousVaultPath),
       NormalizePathEntry(VaultPath)
     ) <> 0) then
  begin
    Parameters :=
      'unregister --yes --client all --scope both --vault ' +
      AddQuotes(PreviousVaultPath);
    if not Exec(
      ExecutablePath,
      Parameters,
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
      MarkSetupFailure(CustomMessage('UnregisterFailed'))
    else if ResultCode <> 0 then
    begin
      Log(
        'Datacron old-vault unregistration returned exit code ' +
        IntToStr(ResultCode) + '.'
      );
      MarkSetupFailure(CustomMessage('UnregisterFailed'));
    end;
  end;

  if not RegWriteStringValue(
    HKCU,
    InstallerStateKey,
    InstallerVaultValue,
    VaultPath
  ) then
  begin
    MarkSetupFailure(CustomMessage('RegistryFailed'));
    Exit;
  end;

  ResetConfiguration := ResetConfigurationRequested;
  Parameters :=
    'setup --yes --client all --scope both --vault ' + AddQuotes(VaultPath);
  if ResetConfiguration then
    Parameters := Parameters + ' --reset';
  if not ShouldIndexNow then
    Parameters := Parameters + ' --no-index';

  if not Exec(
    ExecutablePath,
    Parameters,
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    MarkSetupFailure('Could not start datacron.exe.')
  else if ResultCode <> 0 then
  begin
    if ResetConfiguration then
      MarkSetupFailure(CustomMessage('ResetFailed'))
    else
      MarkSetupFailure(
        'datacron setup returned exit code ' + IntToStr(ResultCode) + '.'
      );
  end
  else
  begin
    Parameters := 'protocol install --client all --scope user';
    if not Exec(
      ExecutablePath,
      Parameters,
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
      MarkSetupFailure(CustomMessage('ProtocolFailed'))
    else if ResultCode <> 0 then
    begin
      Log(
        'Datacron protocol installation returned exit code ' +
        IntToStr(ResultCode) + '.'
      );
      MarkSetupFailure(CustomMessage('ProtocolFailed'));
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunDatacronSetup;
end;

function GetCustomSetupExitCode: Integer;
begin
  if SetupFailed then
    Result := 20
  else
    Result := 0;
end;

procedure ShowUnregisterFailure(const LogMessage: String);
begin
  Log(LogMessage);
  SuppressibleMsgBox(
    CustomMessage('UnregisterFailed'),
    mbError,
    MB_OK,
    IDOK
  );
end;

procedure ShowProtocolRemoveFailure(const LogMessage: String);
begin
  Log(LogMessage);
  SuppressibleMsgBox(
    CustomMessage('ProtocolRemoveFailed'),
    mbError,
    MB_OK,
    IDOK
  );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExecutablePath: String;
  Parameters: String;
  StoredVault: String;
  HasStoredVault: Boolean;
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    StoredVault := '';
    HasStoredVault := RegQueryStringValue(
      HKCU,
      InstallerStateKey,
      InstallerVaultValue,
      StoredVault
    ) and (Trim(StoredVault) <> '');

    ExecutablePath := ExpandConstant('{app}\datacron.exe');
    if FileExists(ExecutablePath) then
    begin
      if HasStoredVault then
        Parameters :=
          'unregister --yes --client all --scope both --vault ' +
          AddQuotes(StoredVault)
      else
        Parameters := 'unregister --yes --client all --scope user';

      if not Exec(
        ExecutablePath,
        Parameters,
        ExpandConstant('{app}'),
        SW_HIDE,
        ewWaitUntilTerminated,
        ResultCode
      ) then
        ShowUnregisterFailure(
          'Datacron client unregistration could not start during uninstall.'
        )
      else if ResultCode <> 0 then
        ShowUnregisterFailure(
          'Datacron client unregistration returned exit code ' +
          IntToStr(ResultCode) + ' during uninstall.'
        );

      Parameters := 'protocol uninstall --client all --scope user';
      if not Exec(
        ExecutablePath,
        Parameters,
        ExpandConstant('{app}'),
        SW_HIDE,
        ewWaitUntilTerminated,
        ResultCode
      ) then
        ShowProtocolRemoveFailure(
          'Datacron protocol removal could not start during uninstall.'
        )
      else if ResultCode <> 0 then
        ShowProtocolRemoveFailure(
          'Datacron protocol removal returned exit code ' +
          IntToStr(ResultCode) + ' during uninstall.'
        );
    end;

    RemoveAppFromUserPath;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if RegDeleteKeyIncludingSubkeys(HKCU, InstallerStateKey) then
      Log('Removed Datacron installer state from the user registry.');
  end;
end;
