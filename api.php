<?php

/**

 * ONE FILE TASK SYSTEM FOR src.is-normal.site:7782

 *

 * Endpoints:

 *   api.php?action=start&main_url=...&meme_url=...

 *   api.php?action=status&task_id=...

 *

 * Stores tasks in: /home/container/www/tasks/*.json

 */

header("Content-Type: application/json");

// PATHS

$ROOT = __DIR__;

$TASK_DIR = $ROOT . "/tasks";

// Your existing merge API:

$CR_API = "http://127.0.0.1:10000/cr.php";

// Ensure tasks directory exists

if (!is_dir($TASK_DIR)) mkdir($TASK_DIR, 0777, true);

// JSON response

function j($arr) {

    echo json_encode($arr, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

    exit;

}

// Generate unique ID

function task_id() {

    return "t_" . uniqid() . "_" . bin2hex(random_bytes(3));

}

// Load / Save tasks

function load_task($id) {

    global $TASK_DIR;

    $file = "$TASK_DIR/$id.json";

    return file_exists($file) ? json_decode(file_get_contents($file), true) : null;

}

function save_task($id, $data) {

    global $TASK_DIR;

    file_put_contents("$TASK_DIR/$id.json", json_encode($data, JSON_PRETTY_PRINT));

}

// AUTO process queue every request

process_queue();

// ACTION ROUTER

$action = $_GET["action"] ?? "";

// --------------------------------------------------

// 1) START TASK

// --------------------------------------------------

if ($action === "start") {

    $main = $_GET["main_url"] ?? "";

    $meme = $_GET["meme_url"] ?? "";

    if (!$main || !$meme) {

        j(["status" => "error", "msg" => "main_url & meme_url required"]);

    }

    $id = task_id();

    $task = [

        "id" => $id,

        "main_url" => $main,

        "meme_url" => $meme,

        "status" => "queued",

        "response" => null,

        "error" => null,

        "created" => time()

    ];

    save_task($id, $task);

    j([

        "status" => "queued",

        "task_id" => $id

    ]);

}

// --------------------------------------------------

// 2) TASK STATUS

// --------------------------------------------------

if ($action === "status") {

    $id = $_GET["task_id"] ?? "";

    if (!$id) j(["status"=>"error","msg"=>"task_id required"]);

    $task = load_task($id);

    if (!$task) j(["status"=>"error","msg"=>"Invalid task_id"]);

    // If done return cr.php’s REAL JSON response

    if ($task["status"] === "done") {

        echo $task["response"];

        exit;

    }

    // Otherwise simple status

    j([

        "task_id" => $id,

        "status"  => $task["status"]

    ]);

}

// Unknown action

j(["status"=>"error","msg"=>"Invalid action"]);

// --------------------------------------------------

// BACKGROUND JOB PROCESSOR

// --------------------------------------------------

function process_queue() {

    global $TASK_DIR, $CR_API;

    $files = glob("$TASK_DIR/*.json");

    if (!$files) return;

    foreach ($files as $file) {

        $task = json_decode(file_get_contents($file), true);

        if ($task["status"] !== "queued") continue;

        // Start processing

        $task["status"] = "processing";

        save_task($task["id"], $task);

        // Build cr.php URL

        $url = $CR_API

             . "?main_url=" . urlencode($task["main_url"])

             . "&meme_url=" . urlencode($task["meme_url"]);

        // This may take long (merge)

        $resp = @file_get_contents($url);

        if (!$resp) {

            $task["status"] = "failed";

            $task["error"] = "cr.php did not respond";

            save_task($task["id"], $task);

            return;

        }

        // Save success response (already JSON)

        $task["status"] = "done";

        $task["response"] = $resp;

        save_task($task["id"], $task);

        // process only 1 task per request

        return;

    }

}

?>