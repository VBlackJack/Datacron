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
#ifndef PayloadVersionVerified
  #error Compile through build_installer.py to validate dist/datacron.exe first
#endif
#if !SameStr(AppVersion, PayloadVersionVerified)
  #error AppVersion does not match the validated dist/datacron.exe version
#endif

; Direct ISCC compilation is intentionally blocked above. The shared local and
; CI wrapper validates dist/datacron.exe --version before supplying the proof.

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
english.VaultPageCaption=Markdown vault (Single-Vault or Dual-Vault)
english.VaultPageDescription=Choose the folder(s) Datacron should serve.
english.VaultPageSubCaption=You can start with an empty folder and add Markdown notes later. Optional Dual-Vault mode: leave the second field empty to keep everything in one folder, or pick a dedicated Memory Vault folder so Datacron stores .datacron/ and _memory/ separately while keeping your documentation folder untouched.
english.PrimaryVaultLabel=Primary documentation vault:
english.MemoryVaultLabel=Dedicated Memory Vault (optional Dual-Vault mode - leave empty for Single-Vault):
english.IndexNow=Index this vault now
english.WritePageCaption=Allow note writing
english.WritePageDescription=Choose whether your AI assistants can create notes.
english.WritePageSubCaption=By default, your AI assistants (Claude, Cursor...) can READ your notes but never change them. Check the first box to also let them create and update notes - only inside three dedicated subfolders (_memory, _drafts, _journal; in Dual-Vault mode, _memory lives in your Memory Vault). Everything else stays untouched. The box shows the choice this installer last recorded; leaving it as it is keeps the current permission, and unchecking a checked box removes it.
english.EnableWriteTools=Let my AI assistants write notes (in 3 dedicated subfolders only)
english.MachineWideWrite=Remember this permission for AI assistants installed later
english.VaultRequiredSilent=/VAULT=<path> is required for a silent Datacron installation.
english.VaultQuoteRejected=The vault path must not contain a double quote character; such a path cannot be passed safely to a command line.
english.SetupFailed=Datacron was installed, but automatic setup failed. Correct the problem, then run Datacron Setup from the Start menu. Setup will return a failure exit code.
english.PathFailed=Datacron could not add its application folder to your user PATH.
english.DirectoryFailed=Datacron could not create the selected vault folder:
english.RegistryFailed=Datacron could not save the selected vault for reinstall and uninstall. Automatic setup was not started.
english.ResetFailed=Datacron could not reset its configuration and generated index. Close every AI application using Datacron, then run the installer again.
english.UnregisterFailed=Datacron could not remove every MCP client entry. The operation will continue; review the client configurations manually.
english.ProtocolFailed=Datacron was registered, but its memory instructions could not be installed in every detected AI client. Run Datacron Setup again or use "datacron protocol install --client all".
english.ProtocolRemoveFailed=Datacron could not remove every installed memory-instruction block. The operation will continue; review the client instruction files manually.
english.SetupStartFailed=Could not start datacron.exe.
english.SetupExitCode=datacron setup returned exit code %1.
english.WriteSwitchConflict=/ENABLEWRITE and /DISABLEWRITE cannot be combined.
english.SingleVaultConflict=/SINGLEVAULT and /MEMORYVAULT= cannot be combined.
french.ReinstallCaption=Configuration Datacron existante
french.ReinstallDescription=Choisissez ce que cette installation doit faire avec le vault selectionne.
french.ReinstallSubCaption=Garder conserve la configuration et l'index genere. Reinitialiser les supprime avant le setup ; les identites de notes, l'audit, les logs et les fichiers Markdown restent inchanges.
french.KeepConfig=Garder ma configuration Datacron actuelle (recommande)
french.ResetConfig=Reinitialiser la configuration et reconstruire l'index genere
french.VaultPageCaption=Vault Markdown (Single-Vault ou Dual-Vault)
french.VaultPageDescription=Choisissez le ou les dossiers que Datacron doit servir.
french.VaultPageSubCaption=Vous pouvez commencer avec un dossier vide et ajouter des notes Markdown plus tard. Mode Dual-Vault (optionnel) : laissez le second champ vide pour tout garder dans un seul dossier, ou choisissez un dossier Vault Memoire dedie pour que Datacron y stocke .datacron/ et _memory/ separement sans toucher a votre dossier de documentation.
french.PrimaryVaultLabel=Vault de documentation principal :
french.MemoryVaultLabel=Vault Memoire dedie (mode Dual-Vault optionnel - laisser vide pour un vault unique) :
french.IndexNow=Indexer ce vault maintenant
french.WritePageCaption=Autoriser l'ecriture de notes
french.WritePageDescription=Choisissez si vos assistants IA peuvent creer des notes.
french.WritePageSubCaption=Par defaut, vos assistants IA (Claude, Cursor...) peuvent LIRE vos notes mais jamais les modifier. Cochez la premiere case pour leur permettre aussi de creer et mettre a jour des notes - uniquement dans trois sous-dossiers dedies (_memory, _drafts, _journal ; en mode Dual-Vault, _memory est place dans votre Vault Memoire). Tout le reste de vos notes reste intouchable. La case reprend le dernier choix enregistre par cet installeur ; la laisser telle quelle conserve l'autorisation actuelle, et decocher une case cochee la retire.
french.EnableWriteTools=Autoriser mes assistants IA a ecrire des notes (dans 3 sous-dossiers dedies uniquement)
french.MachineWideWrite=Retenir cette autorisation pour les assistants IA installes plus tard
french.VaultRequiredSilent=/VAULT=<chemin> est obligatoire pour une installation silencieuse de Datacron.
french.VaultQuoteRejected=Le chemin du vault ne doit pas contenir de guillemet double ; un tel chemin ne peut pas etre transmis sans risque a une ligne de commande.
french.SetupFailed=Datacron a ete installe, mais la configuration automatique a echoue. Corrigez le probleme, puis lancez Datacron Setup depuis le menu Demarrer. Le setup retournera un code d'echec.
french.PathFailed=Datacron n'a pas pu ajouter son dossier d'application au PATH utilisateur.
french.DirectoryFailed=Datacron n'a pas pu creer le dossier de vault selectionne :
french.RegistryFailed=Datacron n'a pas pu memoriser le vault pour la reinstallation et la desinstallation. Le setup automatique n'a pas ete lance.
french.ResetFailed=Datacron n'a pas pu reinitialiser sa configuration et son index genere. Fermez toutes les applications IA utilisant Datacron, puis relancez l'installeur.
french.UnregisterFailed=Datacron n'a pas pu retirer toutes les entrees des clients MCP. L'operation continue ; verifiez manuellement les configurations clientes.
french.ProtocolFailed=Datacron a ete enregistre, mais ses instructions memoire n'ont pas pu etre installees dans tous les clients IA detectes. Relancez Datacron Setup ou utilisez "datacron protocol install --client all".
french.ProtocolRemoveFailed=Datacron n'a pas pu retirer tous les blocs d'instructions memoire installes. L'operation continue ; verifiez manuellement les fichiers d'instructions des clients.
french.SetupStartFailed=Impossible de lancer datacron.exe.
french.SetupExitCode=datacron setup a retourne le code de sortie %1.
french.WriteSwitchConflict=/ENABLEWRITE et /DISABLEWRITE ne peuvent pas etre combines.
french.SingleVaultConflict=/SINGLEVAULT et /MEMORYVAULT= ne peuvent pas etre combines.

[Files]
Source: "..\..\dist\datacron.exe"; DestDir: "{app}"; DestName: "datacron.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\Datacron Companion"; Filename: "{app}\datacron.exe"; Parameters: "{code:CompanionShortcutParameters}"
Name: "{group}\Datacron Status"; Filename: "{cmd}"; Parameters: "{code:StatusShortcutParameters}"
Name: "{group}\Datacron Setup"; Filename: "{cmd}"; Parameters: "{code:SetupShortcutParameters}"

[Code]
const
  InstallerStateKey = 'Software\Datacron';
  InstallerVaultValue = 'VaultRoot';
  InstallerMemoryVaultValue = 'MemoryVaultRoot';
  InstallerWriteValue = 'WriteToolsEnabled';
  InstallerWriteEnvValue = 'WriteEnvAdded';
  UserEnvironmentKey = 'Environment';
  WriteEnvName = 'DATACRON_WRITE_PATHS';
  WriteChoiceKeep = 0;
  WriteChoiceEnable = 1;
  WriteChoiceDisable = 2;

var
  VaultPage: TInputDirWizardPage;
  ReinstallPage: TInputOptionWizardPage;
  WritePage: TInputOptionWizardPage;
  IndexNowCheckBox: TNewCheckBox;
  PreviousVaultPath: String;
  PreviousMemoryVaultPath: String;
  LastDetectedVaultPath: String;
  VaultPath: String;
  MemoryVaultPath: String;
  ExistingConfigDetected: Boolean;
  SetupFailed: Boolean;
  PathsPrepared: Boolean;
  UnregisterWarning: Boolean;
  ResetSwitchDefault: Integer;
  InitialWriteEnabled: Boolean;

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

function SelectedVaultPath: String;
begin
  if WizardSilent then
    Result := CommandLineValue('VAULT')
  else
    Result := Trim(VaultPage.Values[0]);
end;

function SelectedMemoryVaultPath: String;
begin
  { A silent upgrade without /MEMORYVAULT= used to run setup as Single-Vault
    against a Dual-Vault install. The recorded memory vault is the default;
    /SINGLEVAULT is the explicit way back. }
  if WizardSilent then
  begin
    if CommandLineSwitchPresent('SINGLEVAULT') then
      Result := ''
    else
    begin
      Result := CommandLineValue('MEMORYVAULT');
      { The recorded memory vault belongs to the recorded docs vault: a
        different /VAULT= must not inherit it. }
      if (Result = '') and (Trim(PreviousVaultPath) <> '') and
         (CompareText(
           NormalizePathEntry(CommandLineValue('VAULT')),
           NormalizePathEntry(PreviousVaultPath)
         ) = 0) then
        Result := Trim(PreviousMemoryVaultPath);
    end;
  end
  else
    Result := Trim(VaultPage.Values[1]);
end;

function EffectiveVaultPath: String;
begin
  if VaultPath <> '' then
    Result := VaultPath
  else
    Result := SelectedVaultPath;
end;

function EffectiveMemoryVaultPath: String;
begin
  { Once PrepareToInstall has settled the paths, an empty memory vault is the
    Single-Vault answer, the same one setup and the registry receive: the
    shortcuts must not fall back to a field or a record that says otherwise. }
  if PathsPrepared or (MemoryVaultPath <> '') then
    Result := MemoryVaultPath
  else
    Result := SelectedMemoryVaultPath;
end;

function SingleVaultRequested: Boolean;
begin
  { An empty memory field alone is not a choice: a Dual-Vault install made with
    "datacron setup" is not in the registry, and setup keeps the memory vault
    its client entry names. Clearing a field this installer had filled is the
    choice, and so is /SINGLEVAULT. }
  if WizardSilent then
    Result := CommandLineSwitchPresent('SINGLEVAULT')
  else
    Result := (Trim(PreviousMemoryVaultPath) <> '') and
      (Trim(VaultPage.Values[1]) = '');
end;

function ResetConfigurationRequested: Boolean;
begin
  { /RESETCONFIG seeds the wizard page; it does not override what the operator
    then chooses on it. Answering it first and ignoring the page destroyed a
    custom VAULT.yaml for anyone who passed the switch out of habit and then
    picked "Keep my current configuration", which is the one choice the page
    exists to offer. In silent mode there is no page, so the switch decides. }
  if WizardSilent then
    Result := CommandLineSwitchPresent('RESETCONFIG')
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

function WriteToolsChecked: Boolean;
begin
  if WizardSilent then
    Result := CommandLineSwitchPresent('ENABLEWRITE')
  else
    Result := WritePage.Values[0];
end;

function WriteChoice: Integer;
begin
  { Tri-state. Passing --no-write whenever the box was unchecked removed write
    access from every client on any upgrade that did not tick it again, and the
    silent path, which has no box, did the same without /ENABLEWRITE. Only a
    change the operator made reaches setup; anything else keeps the entry. }
  Result := WriteChoiceKeep;
  if WizardSilent then
  begin
    if CommandLineSwitchPresent('ENABLEWRITE') then
      Result := WriteChoiceEnable
    else if CommandLineSwitchPresent('DISABLEWRITE') then
      Result := WriteChoiceDisable;
  end
  else if WritePage.Values[0] and not InitialWriteEnabled then
    Result := WriteChoiceEnable
  else if InitialWriteEnabled and not WritePage.Values[0] then
    Result := WriteChoiceDisable;
end;

function MachineWideWriteRequested: Boolean;
begin
  { Fail-safe: machine-wide is only honored when write tools are enabled. }
  if not WriteToolsChecked then
    Result := False
  else if WizardSilent then
    Result := CommandLineSwitchPresent('MACHINEWIDEWRITE')
  else
    Result := WritePage.Values[1];
end;

function QuotedArgument(const Value: String): String;
begin
  { Quote unconditionally, and double a trailing backslash. A drive root such
    as G:\ keeps its backslash through normalization, and under the Windows
    command-line rules a backslash before the closing quote escapes it: the
    program then reads the quote and the rest of the line as part of the path. }
  if (Length(Value) > 0) and (Value[Length(Value)] = '\') then
    Result := '"' + Value + '\"'
  else
    Result := '"' + Value + '"';
end;

function VaultArguments: String;
begin
  Result := ' --vault ' + QuotedArgument(EffectiveVaultPath);
  if EffectiveMemoryVaultPath <> '' then
    Result := Result + ' --memory-vault ' + QuotedArgument(EffectiveMemoryVaultPath);
end;

function ShortcutParameters(const Subcommand: String): String;
var
  Command: String;
begin
  { Quote unconditionally. AddQuotes only quotes a path containing a space, so a
    space-free vault path carrying an ampersand reached cmd unquoted: cmd splits
    on the ampersand and runs whatever follows it as a second command, from a
    Start menu shortcut the installer wrote. /s tells cmd to strip exactly the
    outer pair of quotes, which is what makes the inner quoting reliable. }
  Command := '"' + ExpandConstant('{app}\datacron.exe') + '"' +
    ' ' + Subcommand + VaultArguments;
  Result := '/s /k "' + Command + '"';
end;

function CompanionShortcutParameters(Param: String): String;
begin
  { The Companion shortcut launched without a vault, so it opened whatever
    DATACRON_VAULT_ROOT or the working directory pointed at, not the vault this
    installer configured. It starts datacron.exe directly, without cmd. }
  Result := 'companion' + VaultArguments;
end;

function StatusShortcutParameters(Param: String): String;
begin
  Result := ShortcutParameters('status');
end;

function SetupShortcutParameters(Param: String): String;
begin
  Result := ShortcutParameters('setup');
end;

procedure WritePageClickCheck(Sender: TObject);
begin
  { Machine-wide propagation is meaningless without the write tools opt-in. }
  if WritePage.Values[1] and not WritePage.Values[0] then
    WritePage.Values[1] := False;
end;

procedure InitializeWizard;
var
  InitialPath: String;
  InitialMemoryPath: String;
  PreviousWriteChoice: String;
begin
  { The switch chooses which option the page opens on; the operator still decides. }
  if CommandLineSwitchPresent('RESETCONFIG') then
    ResetSwitchDefault := 1
  else
    ResetSwitchDefault := 0;
  UnregisterWarning := False;
  PreviousVaultPath := '';
  PreviousMemoryVaultPath := '';
  RegQueryStringValue(
    HKCU,
    InstallerStateKey,
    InstallerVaultValue,
    PreviousVaultPath
  );
  RegQueryStringValue(
    HKCU,
    InstallerStateKey,
    InstallerMemoryVaultValue,
    PreviousMemoryVaultPath
  );

  InitialPath := CommandLineValue('VAULT');
  if InitialPath = '' then
    InitialPath := Trim(PreviousVaultPath);
  if InitialPath = '' then
    InitialPath := ExpandConstant('{userdocs}\Datacron-Vault');

  InitialMemoryPath := CommandLineValue('MEMORYVAULT');
  if InitialMemoryPath = '' then
    InitialMemoryPath := Trim(PreviousMemoryVaultPath);

  VaultPage := CreateInputDirPage(
    wpWelcome,
    CustomMessage('VaultPageCaption'),
    CustomMessage('VaultPageDescription'),
    CustomMessage('VaultPageSubCaption'),
    False,
    ''
  );
  VaultPage.Add(CustomMessage('PrimaryVaultLabel'));
  VaultPage.Values[0] := InitialPath;
  VaultPage.Add(CustomMessage('MemoryVaultLabel'));
  VaultPage.Values[1] := InitialMemoryPath;

  IndexNowCheckBox := TNewCheckBox.Create(VaultPage);
  IndexNowCheckBox.Parent := VaultPage.Surface;
  IndexNowCheckBox.Caption := CustomMessage('IndexNow');
  IndexNowCheckBox.Checked := True;
  IndexNowCheckBox.Top :=
    VaultPage.Edits[1].Top + VaultPage.Edits[1].Height + ScaleY(16);
  IndexNowCheckBox.Left := VaultPage.Edits[1].Left;
  IndexNowCheckBox.Width := VaultPage.Edits[1].Width;

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

  WritePage := CreateInputOptionPage(
    ReinstallPage.ID,
    CustomMessage('WritePageCaption'),
    CustomMessage('WritePageDescription'),
    CustomMessage('WritePageSubCaption'),
    False,
    False
  );
  WritePage.Add(CustomMessage('EnableWriteTools'));
  WritePage.Add(CustomMessage('MachineWideWrite'));
  { The first box opens on the choice the last run of this installer recorded,
    so leaving the page alone keeps it; without a record it starts unchecked,
    and leaving it unchecked then changes nothing either. The machine-wide
    opt-in always starts unchecked. }
  PreviousWriteChoice := '';
  RegQueryStringValue(HKCU, InstallerStateKey, InstallerWriteValue, PreviousWriteChoice);
  InitialWriteEnabled := Trim(PreviousWriteChoice) = '1';
  WritePage.Values[0] := InitialWriteEnabled;
  WritePage.Values[1] := False;
  WritePage.CheckListBox.OnClickCheck := @WritePageClickCheck;

  ExistingConfigDetected := False;
  LastDetectedVaultPath := '';
  VaultPath := '';
  MemoryVaultPath := '';
  SetupFailed := False;
  PathsPrepared := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  CandidateVault: String;
  CandidateMemoryVault: String;
  SidecarRoot: String;
begin
  Result := True;
  if CurPageID <> VaultPage.ID then
    Exit;

  CandidateVault := Trim(VaultPage.Values[0]);
  CandidateMemoryVault := Trim(VaultPage.Values[1]);
  if CandidateMemoryVault <> '' then
    SidecarRoot := CandidateMemoryVault
  else
    SidecarRoot := CandidateVault;
  if CompareText(SidecarRoot, LastDetectedVaultPath) <> 0 then
    ReinstallPage.SelectedValueIndex := ResetSwitchDefault;
  ExistingConfigDetected := (SidecarRoot <> '') and FileExists(
    AddBackslash(SidecarRoot) + '.datacron\VAULT.yaml'
  );
  if not ExistingConfigDetected then
    ReinstallPage.SelectedValueIndex := ResetSwitchDefault;
  LastDetectedVaultPath := SidecarRoot;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  if WizardSilent then
    Result := (PageID = VaultPage.ID) or (PageID = ReinstallPage.ID)
      or (PageID = WritePage.ID)
  else if PageID = ReinstallPage.ID then
    Result := not ExistingConfigDetected
  else
    Result := False;
end;

procedure MarkSetupWarning(const MessageText: String);
begin
  { A step that failed without leaving the product unconfigured. It is reported
    on its own, because the SetupFailed suffix tells the operator the install did
    not work and exit 20 tells their deployment tool the same, when in fact
    everything the install had to do was done. }
  UnregisterWarning := True;
  Log('Datacron post-install warning: ' + MessageText);
  SuppressibleMsgBox(MessageText, mbInformation, MB_OK, IDOK);
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
  if WizardSilent and CommandLineSwitchPresent('ENABLEWRITE') and
     CommandLineSwitchPresent('DISABLEWRITE') then
  begin
    MarkSetupFailure(CustomMessage('WriteSwitchConflict'));
    Result := CustomMessage('WriteSwitchConflict');
    Exit;
  end;
  if WizardSilent and CommandLineSwitchPresent('SINGLEVAULT') and
     (CommandLineValue('MEMORYVAULT') <> '') then
  begin
    MarkSetupFailure(CustomMessage('SingleVaultConflict'));
    Result := CustomMessage('SingleVaultConflict');
    Exit;
  end;
  { Normalize once, here, so every later use agrees: a path ending in a backslash
    would otherwise escape the closing quote of every Exec() and every shortcut
    command line built from it. A path carrying its own double quote cannot be
    quoted safely at all and is refused. }
  VaultPath := NormalizePathEntry(SelectedVaultPath);
  MemoryVaultPath := NormalizePathEntry(SelectedMemoryVaultPath);
  PathsPrepared := True;
  if (Pos('"', VaultPath) > 0) or (Pos('"', MemoryVaultPath) > 0) then
  begin
    Result := CustomMessage('VaultQuoteRejected');
    Exit;
  end;
  if VaultPath = '' then
  begin
    if WizardSilent then
      MarkSetupFailure(CustomMessage('VaultRequiredSilent'));
    Result := CustomMessage('VaultRequiredSilent');
    Exit;
  end;

  if not DirExists(VaultPath) and not ForceDirectories(VaultPath) then
  begin
    Result := CustomMessage('DirectoryFailed') + #13#10 + VaultPath;
    Exit;
  end;
  if (MemoryVaultPath <> '') and (not DirExists(MemoryVaultPath)) and
     (not ForceDirectories(MemoryVaultPath)) then
    Result := CustomMessage('DirectoryFailed') + #13#10 + MemoryVaultPath;
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
  begin
    { Record that this install is the one that added the entry. Uninstall used to
      remove the directory whenever it appeared in the user PATH, including when
      the user had put it there themselves, or when a second install had added
      it: removing one product took a line out of someone else's environment. }
    RegWriteStringValue(HKCU, InstallerStateKey, 'PathEntryAdded', AppPath);
    Log('Added Datacron application directory to the user PATH.');
  end;
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
  AddedPath: String;
  Removed: Boolean;
begin
  { Only remove what this installer added. Without the receipt, uninstalling
    deleted a PATH entry the user had written by hand. }
  if not RegQueryStringValue(HKCU, InstallerStateKey, 'PathEntryAdded', AddedPath) then
  begin
    Log('Leaving the user PATH alone: this installer did not add the entry.');
    Exit;
  end;
  if CompareText(
    NormalizePathEntry(AddedPath), NormalizePathEntry(ExpandConstant('{app}'))
  ) <> 0 then
  begin
    Log('Leaving the user PATH alone: the recorded entry is a different directory.');
    Exit;
  end;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', CurrentPath) then
    Exit;
  NewPath := PathWithoutEntry(CurrentPath, ExpandConstant('{app}'), Removed);
  if not Removed then
    Exit;
  if NewPath = '' then
    RegDeleteValue(HKCU, 'Environment', 'Path')
  else
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
  RegDeleteValue(HKCU, InstallerStateKey, 'PathEntryAdded');
  Log('Removed Datacron application directory from the user PATH.');
end;

procedure RecordWriteEnvReceipt(const HadWriteEnv: Boolean);
var
  WrittenValue: String;
begin
  { /MACHINEWIDEWRITE has setup create DATACRON_WRITE_PATHS in the user
    environment, and uninstall left it behind, still granting write access to
    any client started later. The receipt names what this install created, so
    uninstall removes that value and nothing the user set themselves. }
  if HadWriteEnv then
    Exit;
  if RegQueryStringValue(HKCU, UserEnvironmentKey, WriteEnvName, WrittenValue) then
  begin
    RegWriteStringValue(HKCU, InstallerStateKey, InstallerWriteEnvValue, WrittenValue);
    Log('Recorded the user write allowlist created by this installation.');
  end;
end;

procedure RemoveWriteEnvAdded;
var
  AddedValue: String;
  CurrentValue: String;
begin
  if not RegQueryStringValue(HKCU, InstallerStateKey, InstallerWriteEnvValue, AddedValue) then
  begin
    Log('Leaving the user write allowlist alone: this installer did not create it.');
    Exit;
  end;
  if not RegQueryStringValue(HKCU, UserEnvironmentKey, WriteEnvName, CurrentValue) then
    Exit;
  if CurrentValue <> AddedValue then
  begin
    Log('Leaving the user write allowlist alone: it changed after this installer created it.');
    Exit;
  end;
  RegDeleteValue(HKCU, UserEnvironmentKey, WriteEnvName);
  RegDeleteValue(HKCU, InstallerStateKey, InstallerWriteEnvValue);
  Log('Removed the user write allowlist created by this installation.');
end;

procedure RunDatacronSetup;
var
  ExecutablePath: String;
  Parameters: String;
  ResetConfiguration: Boolean;
  Choice: Integer;
  MachineWide: Boolean;
  HadWriteEnv: Boolean;
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
      QuotedArgument(NormalizePathEntry(PreviousVaultPath));
    if not Exec(
      ExecutablePath,
      Parameters,
      ExpandConstant('{app}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
      MarkSetupWarning(CustomMessage('UnregisterFailed'))
    else if ResultCode <> 0 then
    begin
      Log(
        'Datacron old-vault unregistration returned exit code ' +
        IntToStr(ResultCode) + '.'
      );
      MarkSetupWarning(CustomMessage('UnregisterFailed'));
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
  if MemoryVaultPath <> '' then
    RegWriteStringValue(
      HKCU,
      InstallerStateKey,
      InstallerMemoryVaultValue,
      MemoryVaultPath
    )
  else
    RegDeleteValue(HKCU, InstallerStateKey, InstallerMemoryVaultValue);

  ResetConfiguration := ResetConfigurationRequested;
  { User scope only: project scope writes five client configs and their backups
    into the vault root, which the installer promises never to touch. Uninstall
    still unregisters both scopes, so files left by an earlier release go too. }
  Parameters :=
    'setup --yes --client all --scope user --vault ' + QuotedArgument(VaultPath);
  if MemoryVaultPath <> '' then
    Parameters := Parameters + ' --memory-vault ' + QuotedArgument(MemoryVaultPath)
  else if SingleVaultRequested then
    Parameters := Parameters + ' --single-vault';
  if ResetConfiguration then
    Parameters := Parameters + ' --reset';
  if not ShouldIndexNow then
    Parameters := Parameters + ' --no-index';
  Choice := WriteChoice;
  MachineWide := MachineWideWriteRequested;
  { setup accepts --machine-wide-write only together with --enable-write. }
  if MachineWide then
    Choice := WriteChoiceEnable;
  if Choice = WriteChoiceEnable then
    Parameters := Parameters + ' --enable-write'
  else if Choice = WriteChoiceDisable then
    Parameters := Parameters + ' --no-write';
  if MachineWide then
    Parameters := Parameters + ' --machine-wide-write';
  HadWriteEnv := RegValueExists(HKCU, UserEnvironmentKey, WriteEnvName);

  if not Exec(
    ExecutablePath,
    Parameters,
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    MarkSetupFailure(CustomMessage('SetupStartFailed'))
  else if ResultCode <> 0 then
  begin
    if ResetConfiguration then
      MarkSetupFailure(CustomMessage('ResetFailed'))
    else
      MarkSetupFailure(
        FmtMessage(CustomMessage('SetupExitCode'), [IntToStr(ResultCode)])
      );
  end
  else
  begin
    if Choice = WriteChoiceEnable then
      RegWriteStringValue(HKCU, InstallerStateKey, InstallerWriteValue, '1')
    else if Choice = WriteChoiceDisable then
      RegWriteStringValue(HKCU, InstallerStateKey, InstallerWriteValue, '0');
    if MachineWide then
      RecordWriteEnvReceipt(HadWriteEnv);
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
          QuotedArgument(NormalizePathEntry(StoredVault))
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
    RemoveWriteEnvAdded;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    if RegDeleteKeyIncludingSubkeys(HKCU, InstallerStateKey) then
      Log('Removed Datacron installer state from the user registry.');
  end;
end;
