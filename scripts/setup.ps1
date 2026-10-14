# =============================================================================
#  Setup script for Open-LLM-VTuber companion package
#
#  What it does:
#    1. Checks / installs prerequisites (Python 3.10-3.12, uv, ffmpeg)
#    2. Installs Python dependencies via `uv sync`
#    3. Downloads the offline Chinese TTS model (MIT licensed)
#    4. Downloads a Live2D sample model from its OFFICIAL source
#       (not redistributed here, see README)
#    5. Creates conf.yaml from the template and applies sensible defaults
#
#  Run:  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#
#  Options:
#    -Mirror <url>   Use a PyPI mirror for `uv sync` (does NOT modify the repo).
#                    Handy in mainland China, e.g.:
#                      -Mirror https://mirrors.aliyun.com/pypi/simple
#                    Or just set the env var for one run:
#                      $env:UV_DEFAULT_INDEX='https://mirrors.aliyun.com/pypi/simple'
#    -SkipModels     Skip the TTS / Live2D model downloads
#    -Force          Re-download even if already present
#
#  NOTE: This file is intentionally ASCII-only. Windows PowerShell 5.1 parses
#  .ps1 files as ANSI/GBK when they have no BOM, which corrupts non-ASCII text
#  and breaks the parser. Keeping scripts ASCII avoids that class of bug.
# =============================================================================

param(
    [string]$Mirror = '',     # optional PyPI mirror URL
    [switch]$SkipModels,      # skip TTS + Live2D model downloads
    [switch]$Force            # re-download even if present
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # much faster Invoke-WebRequest

# ----------------------------- paths -----------------------------------------
$Root      = Split-Path -Parent $PSScriptRoot          # repo root
$ModelsDir = Join-Path $Root 'models'
$L2dDir    = Join-Path $Root 'live2d-models'
$ConfYaml  = Join-Path $Root 'conf.yaml'
$TplZh     = Join-Path $Root 'config_templates\conf.ZH.default.yaml'
$TplEn     = Join-Path $Root 'config_templates\conf.default.yaml'

$TtsName   = 'vits-melo-tts-zh_en'
$TtsDir    = Join-Path $ModelsDir $TtsName
$TtsUrl    = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/' +
             'vits-melo-tts-zh_en.tar.bz2'

function Say  ([string]$m) { Write-Host $m }
function Ok   ([string]$m) { Write-Host "  [OK]   $m"   -ForegroundColor Green }
function Warn ([string]$m) { Write-Host "  [WARN] $m"   -ForegroundColor Yellow }
function Fail ([string]$m) { Write-Host "  [FAIL] $m"   -ForegroundColor Red }

function Need ([string]$cmd) {
    return [bool](Get-Command $cmd -ErrorAction SilentlyContinue)
}

# =============================================================================
Say ''
Say '============================================================'
Say '  Open-LLM-VTuber - setup'
Say '============================================================'
Say ''

# ----------------------------- 1. uv -----------------------------------------
Say '[1/5] Checking uv (Python package manager)...'
if (Need 'uv') {
    Ok "uv found: $((Get-Command uv).Source)"
} else {
    Warn 'uv not found - trying to install via winget'
    if (Need 'winget') {
        & winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements --disable-interactivity
        $uvGuess = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        $found = Get-ChildItem $uvGuess -Filter 'uv.exe' -Recurse -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($found) { $env:PATH = "$($found.Directory.FullName);$env:PATH" }
    }
    if (Need 'uv') {
        Ok 'uv installed'
    } else {
        Fail 'Could not install uv automatically.'
        Say  '       Install manually: https://docs.astral.sh/uv/getting-started/installation/'
        exit 1
    }
}

# ----------------------------- 2. ffmpeg -------------------------------------
Say ''
Say '[2/5] Checking ffmpeg (hard dependency for audio)...'
if (Need 'ffmpeg') {
    Ok "ffmpeg found: $((Get-Command ffmpeg).Source)"
} else {
    Warn 'ffmpeg not found - trying to install via winget'
    if (Need 'winget') {
        & winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements --disable-interactivity
        $ffGuess = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        $ffExe = Get-ChildItem $ffGuess -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($ffExe) { $env:PATH = "$($ffExe.Directory.FullName);$env:PATH" }
    }
    if (Need 'ffmpeg') {
        Ok 'ffmpeg installed'
    } else {
        Fail 'Could not install ffmpeg automatically.'
        Say  '       Install manually: https://www.gyan.dev/ffmpeg/builds/'
        exit 1
    }
}

# ----------------------------- 3. dependencies -------------------------------
Say ''
Say '[3/5] Installing Python dependencies (uv sync). This may take a while...'
Push-Location $Root
try {
    if ($Mirror) {
        # Passed as a uv flag so the repository files stay untouched.
        Say "      Using PyPI mirror: $Mirror"
        & uv sync --default-index $Mirror
    } else {
        & uv sync
    }
    if ($LASTEXITCODE -ne 0) {
        Fail 'uv sync failed.'
        Say  '       Behind a proxy?     $env:HTTPS_PROXY="http://127.0.0.1:PORT" then retry'
        Say  '       In mainland China?  re-run with -Mirror, e.g.:'
        Say  '         -Mirror https://mirrors.aliyun.com/pypi/simple'
        exit 1
    }
    Ok 'Dependencies installed'
} finally {
    Pop-Location
}

$PyExe = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $PyExe)) { Fail "venv python not found: $PyExe"; exit 1 }
Ok "Python: $(& $PyExe --version)"

# ----------------------------- 4. TTS model ----------------------------------
Say ''
if ($SkipModels) {
    Say '[4/5] Skipping model downloads (-SkipModels)'
} else {
    Say '[4/5] Downloading offline TTS model (MIT licensed, ~160 MB)...'
    if ((Test-Path (Join-Path $TtsDir 'model.onnx')) -and -not $Force) {
        Ok "TTS model already present: $TtsName"
    } else {
        New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
        $tar = Join-Path $env:TEMP 'vits-melo-tts-zh_en.tar.bz2'
        try {
            Say "      Downloading: $TtsUrl"
            Invoke-WebRequest -Uri $TtsUrl -OutFile $tar -UseBasicParsing
            Say '      Extracting...'
            & tar.exe -xjf $tar -C $ModelsDir
            if ($LASTEXITCODE -ne 0) { throw "tar exited with $LASTEXITCODE" }
            Remove-Item $tar -Force -ErrorAction SilentlyContinue
            Ok "TTS model ready: $TtsDir"
        } catch {
            Fail "TTS model download failed: $($_.Exception.Message)"
            Say  '       You can download it manually from:'
            Say  '       https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models'
            Say  "       then extract so that $TtsDir\model.onnx exists."
        }
    }
}

# ----------------------------- 5. Live2D model -------------------------------
if (-not $SkipModels) {
    Say ''
    Say '[5/5] Checking Live2D model...'
    Say '      NOTE: Live2D sample models are NOT redistributed with this repo.'
    Say '      License: https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html'
    Say '      Clause 4.1.1 forbids redistributing the model files themselves.'

    $haveModel = (Test-Path (Join-Path $L2dDir 'mao_pro\runtime\mao_pro.model3.json')) -or
                 (Test-Path (Join-Path $L2dDir 'shizuku\runtime\shizuku.model3.json'))

    if ($haveModel) {
        Ok 'A Live2D model is already present'
    } else {
        Warn 'No Live2D model found.'
        Say  '       Download ONE of these, then unzip into live2d-models\ :'
        Say  '         Niziiro Mao (recommended, 8 expressions - matches default config):'
        Say  '           https://www.live2d.com/en/learn/sample/niziiro-mao/'
        Say  '         Haru / Mao / Ren and others (8 / 8 / 5 expressions):'
        Say  '           https://github.com/Live2D/CubismWebSamples/tree/develop/Samples/Resources'
        Say  ''
        Say  '       Then rename the folder to match model_dict.json (default: mao_pro)'
        Say  '       or add your own entry to model_dict.json.'
    }
} else {
    Say ''
    Say '[5/5] Skipped (with -SkipModels)'
}

# ----------------------------- conf.yaml -------------------------------------
Say ''
Say '[+] Preparing conf.yaml...'
if ((Test-Path $ConfYaml) -and -not $Force) {
    Ok 'conf.yaml already exists - leaving it untouched'
} else {
    $tpl = if (Test-Path $TplZh) { $TplZh } else { $TplEn }
    Copy-Item $tpl $ConfYaml -Force
    Ok "conf.yaml created from $(Split-Path $tpl -Leaf)"
}

Say ''
Say '============================================================'
Say '  Setup finished.'
Say '============================================================'
Say ''
Say '  Start the server:   .\scripts\start.bat'
Say '  Or run the watchdog: .\scripts\start-watchdog.vbs'
Say ''
Say '  Then open: http://localhost:12393'
Say ''
Say '  IMPORTANT: LM Studio must be running with the local server enabled'
Say '  on port 1234, and the model id in conf.yaml must match exactly.'
Say '  Check with:  (Invoke-RestMethod http://127.0.0.1:1234/v1/models).data.id'
Say ''
