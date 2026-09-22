#ifndef AppVersion
  #error AppVersion must be supplied by build_installer.py
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller application folder
#endif
#ifndef ArtifactDir
  #error ArtifactDir must point to the release output folder
#endif

[Setup]
AppId={{179F783B-89DC-419F-95CE-A4DD31A4D542}
AppName=LiDAR HD
AppVersion={#AppVersion}
AppPublisher=LiDAR HD contributors
AppPublisherURL=https://github.com/yann-dubrana/lidar-hd
DefaultDirName={localappdata}\Programs\LiDAR HD
DefaultGroupName=LiDAR HD
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#ArtifactDir}
OutputBaseFilename=lidar-hd-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\lidar-hd.exe
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\lidar-hd.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LiDAR HD"; Filename: "{app}\lidar-hd.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\LiDAR HD"; Filename: "{app}\lidar-hd.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\lidar-hd.exe"; Description: "Launch LiDAR HD"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent