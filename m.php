<?php
/****************************************************
 *  SIMPLE SECURE FILE MANAGER
 *  Place this file anywhere and open in browser
 ****************************************************/

// ========= OPTIONAL PASSWORD (RECOMMENDED) =========
$password = "ad";  // set like: $password="mysecret";

session_start();
if ($password !== "" && (!isset($_SESSION['AUTH']) || $_SESSION['AUTH'] !== true)) {
    if (isset($_POST['pass'])) {
        if ($_POST['pass'] === $password) $_SESSION['AUTH']=true;
        else $msg="Wrong password";
    }
    if (!isset($_SESSION['AUTH'])) {
        echo '<html><body><h2>Protected</h2>'.
             (!empty($msg)?'<p style="color:red">'.$msg.'</p>':'').
             '<form method="post"><input type="password" name="pass" autofocus>'.
             '<button>Login</button></form></body></html>';
        exit;
    }
}
// ===================================================

// Base dir
$BASE = realpath(__DIR__);
$path = isset($_GET['p']) ? $_GET['p'] : $BASE;
$path = realpath($path);

// block going above base dir
if (!$path || strpos($path, $BASE) !== 0) $path = $BASE;

// Actions
$action = $_GET['a'] ?? null;

// Format size
function fsize($bytes){
    $u=['B','KB','MB','GB','TB'];
    $i=0;
    while($bytes>1024 && $i<4){$bytes/=1024;$i++;}
    return round($bytes,2)." ".$u[$i];
}

// Delete
if ($action==='del' && isset($_GET['t'])) {
    $t = realpath($_GET['t']);
    if ($t && strpos($t,$BASE)===0) {
        if (is_dir($t)) rmdir($t);
        else unlink($t);
    }
    header("Location:?p=".urlencode($path)); exit;
}

// Rename
if ($action==='ren' && isset($_POST['old'],$_POST['new'])) {
    $o=realpath($_POST['old']);
    $n=dirname($o)."/".$_POST['new'];
    if ($o && strpos($o,$BASE)===0) rename($o,$n);
    header("Location:?p=".urlencode($path)); exit;
}

// New folder
if ($action==='mkdir' && isset($_POST['name'])) {
    mkdir($path."/".$_POST['name']);
    header("Location:?p=".urlencode($path)); exit;
}

// Upload
if ($action==='upload' && isset($_FILES['f'])) {
    move_uploaded_file($_FILES['f']['tmp_name'],$path."/".$_FILES['f']['name']);
    header("Location:?p=".urlencode($path)); exit;
}

// Save edited file
if ($action==='save' && isset($_POST['file'],$_POST['content'])) {
    file_put_contents($_POST['file'],$_POST['content']);
    header("Location:?p=".urlencode(dirname($_POST['file']))); exit;
}

// Download raw
if ($action==='download' && isset($_GET['t'])) {
    $t=realpath($_GET['t']);
    if ($t && strpos($t,$BASE)===0) {
        header("Content-Type: application/octet-stream");
        header("Content-Disposition: attachment; filename=".basename($t));
        readfile($t);
        exit;
    }
}

// Open file editor
if ($action==='edit' && isset($_GET['t'])) {
    $t=realpath($_GET['t']);
    if ($t && is_file($t) && strpos($t,$BASE)===0){
        $content=htmlspecialchars(file_get_contents($t));
        echo "<h2>Edit: ".basename($t)."</h2>
        <form method=post action='?a=save'>
        <input type=hidden name=file value='$t'>
        <textarea name=content style='width:100%;height:400px'>$content</textarea><br>
        <button>Save</button>
        </form>";
        exit;
    }
}

// List directory
$items = scandir($path);
$parent = dirname($path);

?>
<!doctype html>
<html>
<head>
<title>File Manager</title>
<style>
body{font-family:Arial;background:#0d1117;color:#ddd}
a{color:#4ea1ff;text-decoration:none}
table{width:100%;border-collapse:collapse;background:#161b22}
td,th{padding:8px;border-bottom:1px solid #30363d}
tr:hover{background:#1f2937}
.btn{padding:4px 8px;background:#238636;border-radius:4px;color:#fff}
.del{background:#da3633}
</style>
</head>
<body>
<h2>📁 File Manager</h2>
<p>Path: <?=htmlspecialchars($path)?></p>

<p>
<a class="btn" href="?p=<?=urlencode($BASE)?>">Root</a>
<?php if ($path!=$BASE): ?>
<a class="btn" href="?p=<?=urlencode($parent)?>">⬆ Up</a>
<?php endif; ?>
</p>

<h3>Create Folder</h3>
<form method="post" action="?a=mkdir&p=<?=urlencode($path)?>">
<input name="name" placeholder="folder name">
<button class="btn">Create</button>
</form>

<h3>Upload File</h3>
<form enctype="multipart/form-data" method="post" action="?a=upload&p=<?=urlencode($path)?>">
<input type="file" name="f">
<button class="btn">Upload</button>
</form>

<table>
<tr><th>Name</th><th>Size</th><th>Actions</th></tr>
<?php foreach ($items as $f):
    if ($f=='.'||$f=='..') continue;
    $full=$path.'/'.$f;
?>
<tr>
<td>
<?php if (is_dir($full)): ?>
📁 <a href="?p=<?=urlencode($full)?>"><?=$f?></a>
<?php else: ?>
📄 <?=$f?>
<?php endif; ?>
</td>
<td><?=is_file($full)?fsize(filesize($full)):"-"?></td>
<td>
<?php if (is_file($full)): ?>
<a class="btn" href="?a=download&t=<?=urlencode($full)?>">Download</a>
<a class="btn" href="?a=edit&t=<?=urlencode($full)?>">Edit</a>
<?php endif; ?>
<a class="btn del" href="?a=del&t=<?=urlencode($full)?>" onclick="return confirm('Delete?')">Delete</a>

<form style="display:inline" method="post" action="?a=ren&p=<?=urlencode($path)?>">
<input type="hidden" name="old" value="<?=$full?>">
<input name="new" value="<?=$f?>" size="10">
<button class="btn">Rename</button>
</form>
</td>
</tr>
<?php endforeach; ?>
</table>
</body>
</html>
