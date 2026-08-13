$ErrorActionPreference = 'Stop'
$cutoff = [datetime]'2026-05-08'
$src = '.squad\decisions.md'
$archive = '.squad\decisions-archive.md'

$lines = Get-Content $src
$n = $lines.Count

# Find header preface (everything before first ### YYYY- top-level entry)
$entryStarts = @()
for ($i = 0; $i -lt $n; $i++) {
    if ($lines[$i] -match '^### (\d{4}-\d{2}-\d{2})') {
        $entryStarts += [pscustomobject]@{ Line = $i; Date = [datetime]$matches[1] }
    }
}

$preface = $lines[0..($entryStarts[0].Line - 1)]

$keep = New-Object System.Collections.Generic.List[string]
$arch = New-Object System.Collections.Generic.List[string]

for ($k = 0; $k -lt $entryStarts.Count; $k++) {
    $start = $entryStarts[$k].Line
    $end = if ($k + 1 -lt $entryStarts.Count) { $entryStarts[$k + 1].Line - 1 } else { $n - 1 }
    $block = $lines[$start..$end]
    if ($entryStarts[$k].Date -ge $cutoff) {
        $keep.AddRange([string[]]$block)
    } else {
        $arch.AddRange([string[]]$block)
    }
}

# Write archive (append)
$archHeader = if (Test-Path $archive) { "" } else { "# Squad Decisions Archive`n`n" }
$archBlock = "`n## Archived 2026-05-15 (entries older than 2026-05-08)`n`n" + ($arch -join "`n") + "`n"
if (-not (Test-Path $archive)) { Set-Content -Path $archive -Value $archHeader -Encoding utf8 -NoNewline }
Add-Content -Path $archive -Value $archBlock -Encoding utf8

# Write active (preface + kept entries)
$out = ($preface -join "`n") + "`n" + ($keep -join "`n") + "`n"
Set-Content -Path $src -Value $out -Encoding utf8 -NoNewline

Write-Host "Kept entries: $($entryStarts.Count - ($arch.Count -gt 0 ? ($entryStarts | Where-Object { $_.Date -lt $cutoff }).Count : 0))"
Write-Host "Archived entries: $(($entryStarts | Where-Object { $_.Date -lt $cutoff }).Count)"
Write-Host "Active size: $((Get-Item $src).Length) bytes"
Write-Host "Archive size: $((Get-Item $archive).Length) bytes"
