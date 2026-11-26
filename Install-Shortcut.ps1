$ErrorActionPreference = 'Stop'

$pythonPath = & python -c 'import sys, tkinter, PIL; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Python with Tkinter and Pillow is required.' }
$pythonWindow = Join-Path ([IO.Path]::GetDirectoryName($pythonPath)) 'pythonw.exe'
$application = Join-Path $PSScriptRoot 'image_prep.py'
if (!(Test-Path -LiteralPath $pythonWindow -PathType Leaf) -or
    !(Test-Path -LiteralPath $application -PathType Leaf)) {
    throw 'Cannot find the launcher or image preparation script.'
}

$desktop = [Environment]::GetFolderPath('DesktopDirectory')
if (!$desktop -or !(Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'Cannot find the Windows desktop.'
}
$shortcutPath = Join-Path $desktop 'Codex Image Prep.lnk'
$arguments = '-B "' + $application + '"'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath -PathType Leaf) -and
    ($shortcut.TargetPath -ne $pythonWindow -or $shortcut.Arguments -ne $arguments)) {
    throw "A different shortcut already exists at $shortcutPath."
}
$shortcut.TargetPath = $pythonWindow
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Prepare screenshots and image files before attaching to Codex'
$shortcut.Hotkey = 'CTRL+ALT+SHIFT+I'
$shortcut.Save()

$saved = $shell.CreateShortcut($shortcutPath)
[pscustomobject]@{
    Shortcut = $shortcutPath
    Target = $saved.TargetPath
    Arguments = $saved.Arguments
    Hotkey = $saved.Hotkey
}
