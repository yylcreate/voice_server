<?php
declare(strict_types=1);
// 真实代理响应形状的碎片化回归，同时拒绝含混的格式/分块头；不访问业务或网络。--yyl
if (empty($argv[1]) || !is_file($argv[1])) throw new InvalidArgumentException('Pass the HHT VoiceHttpStreamDecoder.php path');
require $argv[1];
use plugin\ysimplghhtapi\app\service\VoiceHttpStreamDecoder;

$base = "HTTP/1.1 200 OK\r\nContent-Type: application/vnd.yilian.xiaoya-voice\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n";
$body = "8\r\nXYVOICE1\r\n4\r\nABCD\r\n0\r\n\r\n";
$proxy = $base . "Server-Timing: cfCache;desc=\"DYNAMIC\"\r\nserver-timing: cfEdge;dur=1\r\nCache-Control: no-store\r\n\r\n";
$checks = 0;
foreach ([$base . "\r\n", $proxy, str_replace('Cache-Control:', "SERVER-TIMING: cfOrigin;dur=2\r\nCache-Control:", $proxy)] as $header) {
    $wire = $header . $body;
    for ($split = 1; $split < strlen($wire); $split++) {
        $decoder = new VoiceHttpStreamDecoder();
        $parts = array_merge($decoder->push(substr($wire, 0, $split)), $decoder->push(substr($wire, $split)));
        if (implode('', $parts) !== 'XYVOICE1ABCD' || !$decoder->complete()) throw new RuntimeException('Fragmented proxy response failed');
        $checks++;
    }
    $decoder = new VoiceHttpStreamDecoder(); $parts = [];
    foreach (str_split($wire) as $byte) $parts = array_merge($parts, $decoder->push($byte));
    if (implode('', $parts) !== 'XYVOICE1ABCD' || !$decoder->complete()) throw new RuntimeException('Byte-fragmented proxy response failed');
    $checks++;
}
foreach ([
    'Transfer-Encoding: chunked',
    'tRaNsFeR-EnCoDiNg: chunked',
    'Content-Type: application/vnd.yilian.xiaoya-voice',
    "Content-Length: 4\r\nContent-Length: 4",
    "Content-Encoding: identity\r\nContent-Encoding: identity",
    'Cache-Control: no-store',
    'Connection: close',
] as $duplicate) {
    $bad = str_replace("\r\n\r\n", "\r\n" . $duplicate . "\r\n\r\n", $proxy) . $body;
    try { (new VoiceHttpStreamDecoder())->push($bad); throw new LogicException('Ambiguous upstream headers accepted'); }
    catch (RuntimeException $expected) { $checks++; }
}
$decoder = new VoiceHttpStreamDecoder(); $decoder->push(substr($proxy . $body, 0, -1));
if ($decoder->complete()) throw new RuntimeException('Truncated proxy response marked complete');
$checks++;
echo json_encode(['passed'=>true, 'checks'=>$checks, 'scope'=>'fragmented proxy HTTP responses and strict framing headers; no business/device checks']), PHP_EOL;
