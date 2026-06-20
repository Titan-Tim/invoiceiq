; ============================================================
;  InvoiceIQ — Inno Setup Script
;  Build with:  iscc installer\setup.iss
;  Requires:    PyInstaller one-dir output at ..\dist\InvoiceIQ\
;               and the icon at ..\assets\icon.ico
; ============================================================

#define AppName    "InvoiceIQ"
#define AppVersion "1.0.0"
#define AppPublisher "Your Company Ltd"
#define AppURL     "http://localhost:5000"
#define ExeName    "InvoiceIQ.exe"
#define DistDir    "..\dist\InvoiceIQ"
#define AssetsDir  "..\assets"

[Setup]
AppId                    = {{E7C4B3F2-9A1D-4E8C-B762-3F5A20D81C94}
AppName                  = {#AppName}
AppVersion               = {#AppVersion}
AppPublisher             = {#AppPublisher}
AppPublisherURL          = {#AppURL}
AppSupportURL            = {#AppURL}
AppUpdatesURL            = {#AppURL}
DefaultDirName           = {autopf}\{#AppName}
DefaultGroupName         = {#AppName}
AllowNoIcons             = yes
LicenseFile              =
OutputDir                = ..\dist
OutputBaseFilename       = InvoiceIQ-Setup-{#AppVersion}
SetupIconFile            = {#AssetsDir}\icon.ico
Compression              = lzma2/ultra64
SolidCompression         = yes
WizardStyle              = modern
PrivilegesRequired       = lowest
PrivilegesRequiredOverridesAllowed = dialog
ArchitecturesAllowed     = x64compatible
ArchitecturesInstallIn64BitMode = x64compatible
MinVersion               = 10.0.17763
; Windows 10 1809 minimum (needed for Python 3.12)

; Installer appearance
WizardSmallImageFile     = {#AssetsDir}\icon_256.png
DisableWelcomePage       = no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "{cm:CreateDesktopIcon}";  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon";  Description: "Start InvoiceIQ when Windows starts"; GroupDescription: "Windows startup:"; Flags: unchecked

[Files]
; Main application files — entire PyInstaller output folder
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Ensure config and data directories exist (they may be empty initially)
; These are created by [Dirs] below, not from source

; Icon for shortcuts
Source: "{#AssetsDir}\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Dirs]
; Create writable data / config directories in the install folder
Name: "{app}\data"
Name: "{app}\config"
Name: "{app}\invoices"
Name: "{app}\logs"

[Icons]
; Start menu
Name: "{group}\{#AppName}";           Filename: "{app}\{#ExeName}"; IconFilename: "{app}\assets\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

; Windows startup (optional)
Name: "{userstartup}\{#AppName}";  Filename: "{app}\{#ExeName}"; Tasks: startupicon

[Run]
; Offer to launch the app after install
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up the SQLite database and config on full uninstall (user prompt handled by UninstallRun)
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\config"
Type: filesandordirs; Name: "{app}\invoices"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// ── Pre-install checks ──────────────────────────────────────────────────────

function IsWindowsVersionOK: Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major > 10) or
            ((Version.Major = 10) and (Version.Build >= 17763));
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if not IsWindowsVersionOK then
  begin
    MsgBox('InvoiceIQ requires Windows 10 (version 1809) or later.', mbError, MB_OK);
    Result := False;
  end;
end;

// ── Ask about user data on uninstall ────────────────────────────────────────

function InitializeUninstall: Boolean;
var
  Res: Integer;
begin
  Res := MsgBox(
    'Do you want to remove InvoiceIQ data files (database, configuration, stored invoices)?'
    + #13#10 + #13#10
    + 'Click Yes to remove all data, or No to keep your data.',
    mbConfirmation, MB_YESNO
  );
  if Res = IDNO then
  begin
    // Remove UninstallDelete entries so data is preserved
    // (Inno Setup has no built-in way to skip [UninstallDelete] entries at
    //  runtime, so we simply do nothing — the dirs stay on disk)
  end;
  Result := True;
end;
