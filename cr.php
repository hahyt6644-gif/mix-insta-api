<?php

// cr.php - robust version for your container environment

// Requirements: /home/container/ffmpeg and /home/container/ffprobe must exist

// Usage: cr.php?main_url=...&meme_url=...

set_time_limit(0);

ob_start();

header('Content-Type: application/json; charset=utf-8');

// ---------- CONFIG ----------

$FFMPEG  = '/home/container/ffmpeg';

$FFPROBE = '/home/container/ffprobe';

$ROOT = __DIR__ . '/';

$UPLOAD_DIR = $ROOT . 'Upload/';

$MEME_DIR   = $ROOT . 'meme/';

@mkdir($UPLOAD_DIR, 0777, true);

@mkdir($MEME_DIR,   0777, true);

// ---------- HELPERS ----------

function json_exit($arr) {

    if (ob_get_length()) ob_clean();

    echo json_encode($arr, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

    exit;

}

function safe_filename($prefix='file') {

    return $prefix . '_' . time() . '_' . bin2hex(random_bytes(4)) . '.mp4';

}

function download_file($url, $dest, &$err=null) {

    $err = null;

    $ch = curl_init($url);

    $fp = fopen($dest, 'w');

    if (!$fp) { $err = "Failed to open $dest for writing"; return false; }

    curl_setopt($ch, CURLOPT_FILE, $fp);

    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);

    curl_setopt($ch, CURLOPT_FAILONERROR, true);

    curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 15);

    curl_setopt($ch, CURLOPT_TIMEOUT, 180);

    curl_setopt($ch, CURLOPT_USERAGENT, 'Mozilla/5.0 (compatible)');

    curl_exec($ch);

    $curlErr = curl_error($ch);

    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    curl_close($ch);

    fclose($fp);

    if ($curlErr) { @unlink($dest); $err = "cURL error: $curlErr"; return false; }

    if ($httpCode >= 400) { @unlink($dest); $err = "HTTP error code: $httpCode"; return false; }

    if (!file_exists($dest) || filesize($dest) < 1024) { @unlink($dest); $err = "Downloaded file too small or missing"; return false; }

    return true;

}

function run_cmd($cmd, &$out = null, &$ret = null) {

    // capture both stdout and stderr

    $full = $cmd . ' 2>&1';

    exec($full, $out, $ret);

    return $ret;

}

function ffprobe_has_audio($ffprobe, $file) {

    // returns true if ffprobe finds any audio stream

    $cmd = escapeshellcmd($ffprobe) . ' -v error -select_streams a -show_entries stream=index -of csv=p=0 ' . escapeshellarg($file);

    $out = [];

    $ret = 0;

    run_cmd($cmd, $out, $ret);

    $s = trim(implode("\n", $out));

    return ($s !== '');

}

// ---------- INPUT ----------

$main_url = isset($_GET['main_url']) ? trim($_GET['main_url']) : null;

$meme_url = isset($_GET['meme_url']) ? trim($_GET['meme_url']) : null;

if (!$main_url || !$meme_url) json_exit(['status'=>'error','msg'=>'Missing main_url or meme_url']);

// ---------- CHECK FFMPEG/FFPROBE ----------

if (!file_exists($FFMPEG) || !is_executable($FFMPEG)) json_exit(['status'=>'error','msg'=>"FFmpeg binary not found or not executable at $FFMPEG"]);

if (!file_exists($FFPROBE) || !is_executable($FFPROBE)) json_exit(['status'=>'error','msg'=>"FFprobe binary not found or not executable at $FFPROBE"]);

// ---------- DOWNLOAD ----------

$mainPath = $UPLOAD_DIR . safe_filename('main');

$memePath = $MEME_DIR . safe_filename('meme');

$err = '';

if (!download_file($main_url, $mainPath, $err)) {

    json_exit(['status'=>'error','msg'=>'Failed to download main video','detail'=>$err]);

}

if (!download_file($meme_url, $memePath, $err)) {

    @unlink($mainPath);

    json_exit(['status'=>'error','msg'=>'Failed to download meme video','detail'=>$err]);

}

// ---------- PROBE main width ----------

$probeCmd = escapeshellcmd($FFPROBE) . " -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 " . escapeshellarg($mainPath);

$probeOut = []; $probeRet = 0;

run_cmd($probeCmd, $probeOut, $probeRet);

$mainWidth = intval(trim(implode("\n",$probeOut)));

if ($mainWidth <= 0) $mainWidth = 720; // fallback

// ---------- SCALE meme to main width (use -2 for even height) ----------

$fixedMeme = $MEME_DIR . 'meme_fixed_' . time() . '_' . bin2hex(random_bytes(4)) . '.mp4';

$scaleCmd = escapeshellcmd($FFMPEG) . ' -y -i ' . escapeshellarg($memePath)

          . ' -vf ' . escapeshellarg("scale={$mainWidth}:-2")

          . ' -preset ultrafast -pix_fmt yuv420p ' . escapeshellarg($fixedMeme);

$scaleOut = []; $scaleRet = 0;

run_cmd($scaleCmd, $scaleOut, $scaleRet);

if ($scaleRet !== 0) {

    @unlink($mainPath); @unlink($memePath);

    json_exit(['status'=>'error','msg'=>'Meme scaling failed','ffmpeg'=>$scaleOut]);

}

// ---------- CHECK MAIN AUDIO ----------

$mainHasAudio = ffprobe_has_audio($FFPROBE, $mainPath);

// ---------- BUILD OVERLAY COMMAND ----------

$outputFile = 'final_' . time() . '_' . bin2hex(random_bytes(4)) . '.mp4';

$outputPath = $ROOT . $outputFile;

// overlay filter: simple bottom overlay

$filter_complex = "[0:v][1:v] overlay=0:main_h-overlay_h:format=auto [v]";

if ($mainHasAudio) {

    $overlayCmd = escapeshellcmd($FFMPEG) . ' -y -i ' . escapeshellarg($mainPath) . ' -i ' . escapeshellarg($fixedMeme)

                . ' -filter_complex ' . escapeshellarg($filter_complex)

                . ' -map ' . escapeshellarg('[v]') . ' -map 0:a '

                . ' -c:v libx264 -preset ultrafast -pix_fmt yuv420p -c:a aac '

                . escapeshellarg($outputPath);

} else {

    // produce video-only output (no audio)

    $overlayCmd = escapeshellcmd($FFMPEG) . ' -y -i ' . escapeshellarg($mainPath) . ' -i ' . escapeshellarg($fixedMeme)

                . ' -filter_complex ' . escapeshellarg($filter_complex)

                . ' -map ' . escapeshellarg('[v]') . ' -an '

                . ' -c:v libx264 -preset ultrafast -pix_fmt yuv420p '

                . escapeshellarg($outputPath);

}

// ---------- RUN OVERLAY (capture output) ----------

$ovOut = []; $ovRet = 0;

run_cmd($overlayCmd, $ovOut, $ovRet);

if ($ovRet !== 0 || !file_exists($outputPath) || filesize($outputPath) < 1024) {

    // cleanup scaled meme as well if exists

    @unlink($fixedMeme);

    @unlink($mainPath);

    @unlink($memePath);

    json_exit([

        'status'=>'error',

        'msg'=>'Overlay failed',

        'ffmpeg'=> $ovOut,

        'ret' => $ovRet

    ]);

}

// ---------- CLEANUP input files (success) ----------

@unlink($mainPath);

@unlink($memePath);

@unlink($fixedMeme);

// ---------- AUTO DELETE OLD OUTPUTS (15 minutes) ----------

$maxAge = 900;

$now = time();

foreach (glob($ROOT . 'final_*.mp4') as $f) {

    if ($now - filemtime($f) > $maxAge) @unlink($f);

}

// ---------- BUILD DOWNLOAD URL ----------

$scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';

$host = $_SERVER['HTTP_HOST'] ?? ($_SERVER['SERVER_NAME'] ?? 'localhost');

$scriptDir = rtrim(dirname($_SERVER['SCRIPT_NAME']), '/');

$base = $scheme . '://' . $host . $scriptDir;

$downloadUrl = $base . '/' . basename($outputPath);

// ---------- RETURN SUCCESS ----------

json_exit([

    'status'=>'success',

    'download_url'=>$downloadUrl,

    'has_audio'=>$mainHasAudio,

    'file_size_bytes' => filesize($outputPath)

]);