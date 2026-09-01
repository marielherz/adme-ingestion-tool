$ErrorActionPreference = 'Stop'

$python = 'C:\Users\marielherzog\adme-ingestion-tool\.venv\Scripts\python.exe'
$sdutil = 'C:\Users\marielherzog\adme-tools\seismic-store-sdutil\sdutil'
$localRoot = 'C:\Users\marielherzog\osdu-data\volve\datasets\seismic'
$logPath = 'C:\Users\marielherzog\adme-ingestion-tool\scripts\volve_sdms_batch.log'
$baseUrl = 'https://osdu-seismic-test-data.s3.amazonaws.com/'
$sdPrefix = 'sd://opendes/volve-seismic/'

$env:AZURE_TENANT_ID = '72f988bf-86f1-41af-91ab-2d7cd011db47'
$env:AZURE_CLIENT_ID = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'
$env:AZURE_CLIENT_SECRET = 'idtoken-mode'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Log([string] $Message) {
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -Path $logPath -Value $line
    Write-Output $line
}

function Get-SdmsInventory {
    $token = az account get-access-token --resource https://energy.azure.com --query accessToken -o tsv
    $output = & $python $sdutil ls 'sd://opendes/volve-seismic' -r -l --idtoken=$token 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Unable to read SDMS inventory: $($output -join ' ')" }
    return @($output | Where-Object { $_ -is [string] -and $_ -like 'sd://opendes/volve-seismic/*' })
}

New-Item -ItemType Directory -Force -Path $localRoot | Out-Null
Write-Log 'START remaining Volve seismic batch'
$inventory = Get-SdmsInventory
Write-Log "Existing SDMS datasets: $($inventory.Count)"

$xml = [xml](Invoke-WebRequest -UseBasicParsing ($baseUrl + '?list-type=2&prefix=volve/seismic/')).Content
$items = @($xml.ListBucketResult.Contents | Where-Object { $_.Key -notlike '*/' })
Write-Log "Source objects: $($items.Count)"

foreach ($item in $items) {
    $sourceKey = [string]$item.Key
    $relative = $sourceKey.Substring('volve/seismic/'.Length)
    $destinationRelative = $relative.Replace('+', '-')
    $destination = $sdPrefix + $destinationRelative
    if ($inventory -contains $destination) {
        Write-Log "SKIP already finalized: $destination"
        continue
    }

    $local = Join-Path $localRoot ($relative.Replace('/', [IO.Path]::DirectorySeparatorChar))
    New-Item -ItemType Directory -Force -Path (Split-Path $local) | Out-Null
    $urlKey = $sourceKey.Replace('+', '%2B')
    $url = $baseUrl + $urlKey
    $expected = [int64]$item.Size

    try {
        if (-not (Test-Path $local) -or (Get-Item $local).Length -ne $expected) {
            Write-Log "DOWNLOAD $sourceKey expected=$expected"
            curl.exe -L --fail --retry 10 --retry-all-errors --retry-delay 5 $url -o $local
        }
        $actual = (Get-Item $local).Length
        if ($actual -ne $expected) { throw "Size mismatch expected=$expected actual=$actual" }

        Write-Log "UPLOAD $destination bytes=$actual"
        $token = az account get-access-token --resource https://energy.azure.com --query accessToken -o tsv
        $output = & $python $sdutil cp $local $destination --idtoken=$token --chunk-size=32 2>&1
        $exitCode = $LASTEXITCODE
        $output | ForEach-Object { Add-Content -Path $logPath -Value $_ }
        if ($exitCode -ne 0) {
            Write-Log "UPLOAD_EXIT_NONZERO $destination exit=$exitCode; verify with stat before retry"
        } else {
            Write-Log "UPLOADED $destination"
        }
    } catch {
        Write-Log "ERROR $sourceKey :: $($_.Exception.Message)"
    }
}

Write-Log 'END remaining Volve seismic batch'
