; -*- coding: utf-8 -*-
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"
!include "x64.nsh"
!include "${STAGE}\config.nsh"
Name "CrossDesk ${VERSION}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\CrossDesk"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetDatablockOptimize on
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${NUMERIC_VERSION}"
VIAddVersionKey /LANG=2052 "ProductName" "CrossDesk"
VIAddVersionKey /LANG=2052 "FileDescription" "CrossDesk 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "CrossDesk contributors"
!define REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\CrossDesk"

Var VerifyMode
Var ConfigureLAN
Var Checkbox
Var Dialog
Var FirewallStatus
Var Result
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "安装 CrossDesk ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "共享 Mac 与 Windows 的键盘和鼠标。$\r$\n$\r$\n安装向导会创建快捷方式，并可自动配置专用局域网连接。日常使用不需要管理员权限。$\r$\n$\r$\n本版本为预览版。已有 CrossDesk 的电脑请优先使用应用内签名更新。"
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDirectory
!insertmacro MUI_PAGE_DIRECTORY
Page custom NetworkPage NetworkPageLeave
!insertmacro MUI_PAGE_INSTFILES
!define MUI_PAGE_CUSTOMFUNCTION_PRE FinishText
!define MUI_FINISHPAGE_TEXT "$FirewallStatus$\r$\n$\r$\n安装说明中包含 Mac 权限设置和两端配对步骤。"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\INSTALL.html"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "查看安装与权限说明"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
    SetShellVarContext current
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP "此安装包需要 64 位 Windows。" /SD IDOK
        SetErrorLevel 10
        Abort
    ${EndIf}
    SetRegView 64
    StrCpy $ConfigureLAN ${BST_CHECKED}
    StrCpy $VerifyMode 0
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/VERIFY=" $1
    ${If} $1 == "1"
        StrCpy $VerifyMode 1
        StrCpy $ConfigureLAN ${BST_UNCHECKED}
    ${EndIf}
    ClearErrors
    ${GetOptions} $0 "/NOFIREWALL" $1
    ${IfNot} ${Errors}
        StrCpy $ConfigureLAN ${BST_UNCHECKED}
    ${EndIf}
    ${If} $VerifyMode != 1
        ReadRegStr $0 HKCU "${REGKEY}" "InstallLocation"
        ${If} $0 != ""
            MessageBox MB_ICONSTOP "此用户已通过安装器安装 CrossDesk。请使用签名更新，或先从系统设置卸载旧安装。" /SD IDOK
            SetErrorLevel 16
            Abort
        ${EndIf}
    ${EndIf}
FunctionEnd

Function CheckDirectory
    ; Refuse replacement of an existing host and any nonempty destination.
    FindFirst $0 $1 "$INSTDIR\*"
    loop:
        ${If} $1 == ""
            Goto empty
        ${EndIf}
        ${If} $1 != "."
        ${AndIf} $1 != ".."
            FindClose $0
            MessageBox MB_ICONSTOP "此目录已有文件。请选择一个空目录。已有 CrossDesk 请使用签名更新，避免覆盖现有程序。" /SD IDOK
            SetErrorLevel 11
            Abort
        ${EndIf}
        FindNext $0 $1
        Goto loop
    empty:
    FindClose $0
FunctionEnd

Function NetworkPage
    !insertmacro MUI_HEADER_TEXT "局域网连接" "安装时自动配置 Windows 防火墙"
    nsDialogs::Create 1018
    Pop $Dialog
    ${NSD_CreateCheckbox} 0 5u 100% 20u "自动配置 CrossDesk 的局域网连接（推荐）"
    Pop $Checkbox
    ${NSD_SetState} $Checkbox $ConfigureLAN
    ${NSD_CreateLabel} 0 36u 100% 90u "勾选后会弹出一次 Windows 管理员确认。$\r$\n$\r$\n仅允许此应用在专用网络上接收同一子网的连接：TCP 48193、UDP 48194。$\r$\n$\r$\n若取消管理员确认，应用仍会安装，并提示网络配置未完成。公共网络不会自动改为专用网络。"
    Pop $0
    nsDialogs::Show
FunctionEnd

Function NetworkPageLeave
    ${NSD_GetState} $Checkbox $ConfigureLAN
FunctionEnd

Function FinishText
    ; MUI reads FirewallStatus at page creation, including skipped/failed UAC.
FunctionEnd

Section "CrossDesk" Main
    Call CheckDirectory
    SetOutPath "$INSTDIR"
    ClearErrors
    !include "${STAGE}\files-install.nsh"
    ${If} ${Errors}
        MessageBox MB_ICONSTOP "文件安装未完成，请检查目录权限和可用空间后重试。" /SD IDOK
        SetErrorLevel 12
        Abort
    ${EndIf}
    WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "Version" "${VERSION}"
    WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "Path" "$INSTDIR"
    WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "VerifyMode" "$VerifyMode"
    WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "FirewallAttempted" "0"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    ${If} $VerifyMode != 1
        CreateDirectory "$SMPROGRAMS\CrossDesk"
        CreateShortcut "$SMPROGRAMS\CrossDesk\CrossDesk.lnk" "$INSTDIR\CrossDesk.exe"
        CreateShortcut "$SMPROGRAMS\CrossDesk\安装与权限说明.lnk" "$INSTDIR\INSTALL.html"
        CreateShortcut "$SMPROGRAMS\CrossDesk\修复局域网连接.lnk" "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\Repair-Network.ps1"'
        CreateShortcut "$DESKTOP\CrossDesk.lnk" "$INSTDIR\CrossDesk.exe"
        WriteRegStr HKCU "${REGKEY}" "DisplayName" "CrossDesk"
        WriteRegStr HKCU "${REGKEY}" "DisplayVersion" "${VERSION}"
        WriteRegStr HKCU "${REGKEY}" "InstallLocation" "$INSTDIR"
        WriteRegStr HKCU "${REGKEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
        WriteRegStr HKCU "${REGKEY}" "DisplayIcon" "$INSTDIR\CrossDesk.exe"
        WriteRegDWORD HKCU "${REGKEY}" "NoModify" 1
        WriteRegDWORD HKCU "${REGKEY}" "NoRepair" 1
    ${EndIf}
    StrCpy $FirewallStatus "CrossDesk 已安装。局域网规则未配置；可从开始菜单运行“修复局域网连接”。"
    ${If} $ConfigureLAN == ${BST_CHECKED}
    ${AndIf} $VerifyMode != 1
        WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "FirewallAttempted" "1"
        DetailPrint "正在配置局域网连接，请处理 Windows 管理员确认…"
        StrCpy $Result -1
        ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\Configure-Firewall.ps1" -Action Install' $Result
        ${If} $Result == 0
            StrCpy $FirewallStatus "CrossDesk 已安装，专用局域网防火墙规则已配置。两台电脑连接同一可信局域网后即可配对。"
        ${ElseIf} $Result == 3
            StrCpy $FirewallStatus "CrossDesk 已安装，防火墙规则已添加。当前没有专用网络：请仅将你信任的家庭/办公室网络设为“专用”，再进行连接。"
        ${Else}
            StrCpy $FirewallStatus "CrossDesk 已安装，但管理员授权被取消或网络配置失败。请从开始菜单运行“修复局域网连接”；详情见安装目录的 firewall-result.txt（如有）。"
        ${EndIf}
    ${EndIf}
    DetailPrint "$FirewallStatus"
    WriteINIStr "$INSTDIR\install.ini" "CrossDesk" "NetworkResult" "$FirewallStatus"
SectionEnd

Function un.onInit
    SetShellVarContext current
    SetRegView 64
    ReadINIStr $0 "$INSTDIR\install.ini" "CrossDesk" "Path"
    ${If} $0 != $INSTDIR
        MessageBox MB_ICONSTOP "无法确认安装目录，已停止卸载。" /SD IDOK
        SetErrorLevel 13
        Abort
    ${EndIf}
    ReadINIStr $VerifyMode "$INSTDIR\install.ini" "CrossDesk" "VerifyMode"
FunctionEnd

Section "Uninstall"
    ; Detect a running host before removing its files or firewall rules.
    ${If} ${FileExists} "$INSTDIR\CrossDesk.exe"
        ClearErrors
        FileOpen $0 "$INSTDIR\CrossDesk.exe" a
        ${If} ${Errors}
            MessageBox MB_ICONSTOP "请先退出此安装目录中的 CrossDesk，再卸载。" /SD IDOK
            SetErrorLevel 14
            Abort
        ${EndIf}
        FileClose $0
    ${EndIf}
    ${If} $VerifyMode != 1
        ReadINIStr $0 "$INSTDIR\install.ini" "CrossDesk" "FirewallAttempted"
        ${If} $0 == 1
            DetailPrint "正在移除本安装的防火墙规则，请处理管理员确认…"
            StrCpy $Result -1
            ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$INSTDIR\Configure-Firewall.ps1" -Action Remove' $Result
            ${If} $Result != 0
                MessageBox MB_ICONEXCLAMATION "防火墙规则尚未移除，卸载已停止。完成管理员确认后重试，以免遗留规则。" /SD IDOK
                SetErrorLevel 15
                Abort
            ${EndIf}
        ${EndIf}
        ReadRegStr $0 HKCU "${REGKEY}" "InstallLocation"
        ${If} $0 == $INSTDIR
            DeleteRegKey HKCU "${REGKEY}"
            Delete "$DESKTOP\CrossDesk.lnk"
            Delete "$SMPROGRAMS\CrossDesk\CrossDesk.lnk"
            Delete "$SMPROGRAMS\CrossDesk\安装与权限说明.lnk"
            Delete "$SMPROGRAMS\CrossDesk\修复局域网连接.lnk"
            RMDir "$SMPROGRAMS\CrossDesk"
        ${EndIf}
    ${EndIf}
    SetOutPath "$TEMP"
    !include "${STAGE}\files-uninstall.nsh"
    Delete "$INSTDIR\firewall-result.txt"
    Delete "$INSTDIR\install.ini"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
    DetailPrint "卸载完成。用户配置、配对记录和用户自行添加的文件已保留。"
SectionEnd
