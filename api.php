<?php
/**
 * AgentTrade Config API Proxy
 * ============================
 * Sits in the webapp's web directory so YunoHost SSOwat trusts it.
 * Forwards requests to the local config server (Flask on port 5111)
 * without going through nginx proxy_pass or SSOwat Lua auth checks.
 *
 * Usage (from settings.html JavaScript):
 * Browser URL may be /AgentTrader/... but YunoHost files live in www root:
 *   fetch('api.php?_path=status')              GET
 *   fetch('api.php?_path=config')              GET / POST
 *   fetch('api.php?_path=config/key')          POST
 *   fetch('api.php?_path=state')               GET
 *   fetch('api.php?_path=history')             GET  (?days=90)
 *   fetch('api.php?_path=trades')               GET  (?days=90)
 *   fetch('api.php?_path=trades/sync')          POST (?days=90)
 *   fetch('api.php?_path=attribution')          GET  (?days=90)
 *   fetch('api.php?_path=export/summary')         GET  (?days=90) CSV
 *   fetch('api.php?_path=export/full')            GET  (?days=90) CSV
 *   fetch('api.php?_path=health')                 GET  pre-cycle checks
 *   fetch('api.php?_path=screener/cache')           GET  cache stats
 *   fetch('api.php?_path=screener/cache/clear')    POST clear cache
 *   fetch('api.php?_path=backtest')               GET  (?days=90&capital=10000&interval=5)
 *   Period summary is included in GET /history as period_summary.
 *   fetch('api.php?_path=test/anthropic')       POST
 *
 * The _path parameter maps directly to the Flask route:
 *   ?_path=config       → http://127.0.0.1:5111/config
 *   ?_path=test/openai  → http://127.0.0.1:5111/test/openai
 */

// Handle CORS preflight
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type');
    http_response_code(204);
    exit;
}

// Get path from query string — default to /status
$raw_path = isset($_GET['_path']) ? $_GET['_path'] : 'status';

// Sanitize path — only allow alphanumeric, slashes, hyphens, underscores
$clean_path = preg_replace('/[^a-zA-Z0-9\/_-]/', '', $raw_path);
if (empty($clean_path)) {
    $clean_path = 'status';
}

$target = 'http://127.0.0.1:5111/' . ltrim($clean_path, '/');

// Forward extra query params (e.g. days=90 for /history) to Flask
$extra = [];
foreach ($_GET as $k => $v) {
    if ($k === '_path') continue;
    $extra[] = rawurlencode($k) . '=' . rawurlencode($v);
}
if ($extra) {
    $target .= '?' . implode('&', $extra);
}

// Get request body and method
$body   = file_get_contents('php://input');
$method = strtoupper($_SERVER['REQUEST_METHOD']);

// Forward via curl
$timeout = ($clean_path === 'backtest') ? 120 : 30;
$ch = curl_init($target);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT        => $timeout,
    CURLOPT_CUSTOMREQUEST  => $method,
    CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
    CURLOPT_FAILONERROR    => false,
]);

if (in_array($method, ['POST', 'PUT', 'PATCH']) && !empty($body)) {
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
}

$response  = curl_exec($ch);
$http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curl_err  = curl_error($ch);
curl_close($ch);

// Return response
header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Cache-Control: no-store');

if ($curl_err || $response === false) {
    http_response_code(502);
    echo json_encode([
        'ok'      => false,
        'message' => 'Config server unreachable. Is trading-agent-config running? Error: ' . $curl_err
    ]);
    exit;
}

http_response_code($http_code ?: 200);
echo $response;
