# Chronicle PC tracker - hook de prompt PowerShell (terminal_command).
#
# Charge depuis le profil PowerShell par `python -m pc.tracker shell-hook
# install`. Apres chaque commande, ajoute UNE ligne JSON dans
# %LOCALAPPDATA%\ChroniclePC\spool\ps-<pid>.jsonl, que le tracker lit,
# REDIGE (mots de passe, jetons) et supprime.
#
# Regles : ne jamais casser le prompt (tout est dans try/catch), ne jamais
# ralentir le shell de facon perceptible (une ecriture de fichier).

if (-not $global:__ChroniclePCHook) {
    $global:__ChroniclePCHook = $true
    $global:__ChroniclePCSpool = Join-Path $env:LOCALAPPDATA 'ChroniclePC\spool'
    $global:__ChroniclePCLastId = 0
    $__h = Get-History -Count 1
    if ($__h) { $global:__ChroniclePCLastId = $__h.Id }
    $global:__ChroniclePCPrompt = $function:prompt

    function global:prompt {
        # $? d'abord : toute autre instruction l'ecraserait.
        $__ok = $?
        $__code = $global:LASTEXITCODE
        try {
            $__h = Get-History -Count 1
            if ($__h -and $__h.Id -ne $global:__ChroniclePCLastId) {
                $global:__ChroniclePCLastId = $__h.Id
                $__sortie = 0
                if (-not $__ok) {
                    if ($__code -is [int] -and $__code -ne 0) { $__sortie = $__code } else { $__sortie = 1 }
                }
                $__terminal = 'console'
                if ($env:WT_SESSION) { $__terminal = 'windows-terminal' }
                elseif ($env:TERM_PROGRAM -eq 'vscode') { $__terminal = 'vscode' }
                $__shell = 'powershell'
                if ($PSVersionTable.PSEdition -eq 'Core') { $__shell = 'pwsh' }
                $__ligne = [ordered]@{
                    v         = 1
                    shell     = $__shell
                    pid       = $PID
                    terminal  = $__terminal
                    cwd       = $PWD.ProviderPath
                    command   = $__h.CommandLine
                    start     = $__h.StartExecutionTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ')
                    end       = $__h.EndExecutionTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ')
                    exit_code = $__sortie
                } | ConvertTo-Json -Compress
                if (-not (Test-Path $global:__ChroniclePCSpool)) {
                    New-Item -ItemType Directory -Path $global:__ChroniclePCSpool -Force | Out-Null
                }
                [System.IO.File]::AppendAllText(
                    (Join-Path $global:__ChroniclePCSpool "ps-$PID.jsonl"),
                    $__ligne + "`n",
                    [System.Text.UTF8Encoding]::new($false))
            }
        } catch { }
        $global:LASTEXITCODE = $__code
        if ($global:__ChroniclePCPrompt) { & $global:__ChroniclePCPrompt } else { "PS $($PWD.ProviderPath)> " }
    }
}
