# =============================================================================
#  Pre-publish safety check for a public GitHub repo
#
#  Scans everything that WOULD be committed and reports:
#    - secrets that look like API keys
#    - very large files
#    - model / venv / private-config files that must NOT be public
#
#  Run:  powershell -ExecutionPolicy Bypass -File .\scripts\check-before-push.ps1
#
#  Exit code 0 = safe to push, 1 = problems found (do NOT push).
#
#  NOTE: ASCII-only on purpose. Windows PowerShell 5.1 parses .ps1 as ANSI/GBK
#  when there is no BOM, which corrupts non-ASCII text and breaks the parser.
# =============================================================================

$ErrorActionPreference = 'Continue'

$problems = 0
$warnings = 0

function Head([string]$t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }
function Good([string]$t) { Write-Host "   [OK]   $t" -ForegroundColor Green }
function Bad ([string]$t) { Write-Host "   [FAIL] $t" -ForegroundColor Red;   $script:problems++ }
function Warn([string]$t) { Write-Host "   [WARN] $t" -ForegroundColor Yellow; $script:warnings++ }

Write-Host ''
Write-Host '============================================'
Write-Host '  Pre-publish safety check'
Write-Host '============================================'

# --- make sure we are in a git repo ----------------------------------------
$inside = & git rev-parse --is-inside-work-tree 2>$null
if ($inside -ne 'true') {
    Write-Host ''
    Write-Host 'ERROR: not inside a git repository.' -ForegroundColor Red
    exit 1
}

$root = (& git rev-parse --show-toplevel 2>$null)
Write-Host "Repository: $root"

# --- collect the list of files git would actually track ---------------------
# core.quotepath=false is essential: without it git escapes non-ASCII path
# characters (CJK filenames become \345\212\251.md) and our Test-Path fails.
$tracked = @()
$tracked += (& git -c core.quotepath=false ls-files 2>$null)
$tracked += (& git -c core.quotepath=false ls-files --others --exclude-standard 2>$null)
$tracked = $tracked |
    Where-Object { $_ } |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and $_ -notmatch '[\x00-\x1f]' } |
    Sort-Object -Unique |
    Where-Object { Test-Path -LiteralPath (Join-Path $root $_) -ErrorAction SilentlyContinue }

if (-not $tracked) {
    Write-Host ''
    Write-Host 'No files to check.' -ForegroundColor Yellow
    exit 0
}
Write-Host "Files that would be committed: $($tracked.Count)"

# =============================================================================
Head '1. Private / licensed files must be ignored'
# =============================================================================

$mustIgnore = @(
    @{ Path = 'conf.yaml';         Why = 'contains your API keys' },
    @{ Path = 'mcp_servers.json';  Why = 'contains your local absolute paths' },
    @{ Path = '.env';              Why = 'contains secrets' },
    @{ Path = 'api_keys.py';       Why = 'contains secrets' }
)

foreach ($item in $mustIgnore) {
    $hit = $tracked | Where-Object { $_ -eq $item.Path -or $_ -like "*/$($item.Path)" }
    if ($hit) {
        Bad "$($item.Path) WOULD be committed - $($item.Why)"
    } else {
        Good "$($item.Path) is ignored"
    }
}

# --- directories that must never be published -------------------------------
$mustIgnoreDirs = @(
    @{ Dir = 'live2d-models'; Why = 'Live2D Inc. sample models: license 4.1.1 forbids redistribution' },
    @{ Dir = 'models';        Why = 'large downloaded models; setup.ps1 fetches them instead' },
    @{ Dir = '.venv';         Why = 'virtualenv, ~1.5 GB' },
    @{ Dir = 'cache';         Why = 'runtime artifacts' },
    @{ Dir = 'logs';          Why = 'runtime logs' },
    @{ Dir = 'chat_history';  Why = 'private conversation history' }
)

foreach ($item in $mustIgnoreDirs) {
    $hit = $tracked | Where-Object { $_ -like "$($item.Dir)/*" -or $_ -like "*/$($item.Dir)/*" }
    if ($hit) {
        Bad "$($item.Dir)/ would be committed ($($hit.Count) file(s)) - $($item.Why)"
        $hit | Select-Object -First 3 | ForEach-Object { Write-Host "            e.g. $_" -ForegroundColor DarkGray }
    } else {
        Good "$($item.Dir)/ is ignored"
    }
}

# =============================================================================
Head '2. Secret scan'
# =============================================================================

$secretPatterns = @(
    @{ Name = 'OpenAI-style key';   Regex = 'sk-[A-Za-z0-9_\-]{20,}' },
    @{ Name = 'Tavily key';         Regex = 'tvly-[A-Za-z0-9_\-]{10,}' },
    @{ Name = 'Anthropic key';      Regex = 'sk-ant-[A-Za-z0-9_\-]{20,}' },
    @{ Name = 'HuggingFace token';  Regex = 'hf_[A-Za-z0-9]{20,}' },
    @{ Name = 'GitHub token';       Regex = 'gh[pousr]_[A-Za-z0-9]{20,}' },
    @{ Name = 'Google API key';     Regex = 'AIza[A-Za-z0-9_\-]{30,}' },
    @{ Name = 'Bearer token';       Regex = 'Bearer\s+[A-Za-z0-9_\-\.]{25,}' },
    # Requires a real-looking value AND a key-ish variable name; excludes
    # template placeholders like token = "<|begin_of_text|>"
    @{ Name = 'assigned secret';    Regex = '(?i)(api[_-]?key|apikey|secret|password|access[_-]?token|auth[_-]?token)\s*[:=]\s*[''"][A-Za-z0-9_\-\.\+/]{20,}[''"]' }
)

$skipExt = @('.png', '.jpg', '.jpeg', '.gif', '.ico', '.wav', '.mp3', '.onnx', '.moc3',
             '.exp3.json', '.motion3.json', '.fst', '.bin', '.zip', '.exe', '.dll', '.woff', '.woff2')

$foundSecrets = 0
foreach ($rel in $tracked) {
    $full = Join-Path $root $rel
    $ext = [System.IO.Path]::GetExtension($rel).ToLower()
    if ($skipExt -contains $ext) { continue }

    # skip binary-ish files by sniffing for a NUL byte in the first chunk
    try {
        $bytes = [System.IO.File]::ReadAllBytes($full)
    } catch { continue }
    if ($bytes.Length -eq 0) { continue }
    $probe = $bytes[0..([Math]::Min(1023, $bytes.Length - 1))]
    if ($probe -contains 0) { continue }

    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    foreach ($p in $secretPatterns) {
        $m = [regex]::Matches($text, $p.Regex)
        if ($m.Count -gt 0) {
            # example/placeholder values are fine - filter the obvious ones
            $real = @($m | Where-Object {
                $v = $_.Value
                $v -notmatch 'YOUR|EXAMPLE|PLACEHOLDER|xxxx|XXXX|your[_-]?key|somethingelse|not-needed'
            })
            if ($real.Count -gt 0) {
                Bad "$rel : $($p.Name) - $($real.Count) match(es)"
                Write-Host "            e.g. $($real[0].Value.Substring(0, [Math]::Min(60, $real[0].Value.Length)))" -ForegroundColor DarkGray
                $foundSecrets++
            }
        }
    }
}
if ($foundSecrets -eq 0) { Good 'No hard-coded secrets found' }

# =============================================================================
Head '3. Large files'
# =============================================================================

$bigCount = 0
foreach ($rel in $tracked) {
    $full = Join-Path $root $rel
    try { $len = (Get-Item $full -ErrorAction Stop).Length } catch { continue }
    if ($len -gt 10MB) {
        Bad ("{0,8:N1} MB  {1}" -f ($len / 1MB), $rel)
        $bigCount++
    } elseif ($len -gt 2MB) {
        Warn ("{0,8:N1} MB  {1}" -f ($len / 1MB), $rel)
    }
}
if ($bigCount -eq 0) { Good 'No files over 10 MB' }

# =============================================================================
Head '4. Attribution / license'
# =============================================================================

if ($tracked -contains 'LICENSE') {
    Good 'LICENSE is present'
} else {
    Bad 'LICENSE is missing - MIT requires keeping the original copyright notice'
}

if (Test-Path (Join-Path $root 'README.md')) {
    $readme = Get-Content (Join-Path $root 'README.md') -Raw
    if ($readme -match 'Open-LLM-VTuber') {
        Good 'README credits the upstream project'
    } else {
        Warn 'README does not mention Open-LLM-VTuber - add attribution'
    }
} else {
    Warn 'No README.md'
}

# =============================================================================
Write-Host ''
Write-Host '============================================'
if ($problems -eq 0) {
    Write-Host "  PASSED - safe to push ($warnings warning(s))" -ForegroundColor Green
} else {
    Write-Host "  $problems PROBLEM(S) FOUND - DO NOT PUSH" -ForegroundColor Red
    Write-Host '  Fix the [FAIL] items above, then run this check again.' -ForegroundColor Red
}
Write-Host '============================================'
Write-Host ''

exit $(if ($problems -gt 0) { 1 } else { 0 })
