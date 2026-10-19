"""游戏内文字读取。

用 Windows 自带的 OCR（Windows.Media.Ocr），不额外装 Tesseract。
实测单次调用 0.24s（含 powershell 进程启动），只用来读商品名/价格，
不进抢购热路径，够用。

中文识别有个已知问题：Windows OCR 会把中文按字拆开加空格，而且对
小字号识别率一般。所以这里做了两件事：
  1. 去掉汉字之间的空格
  2. 用模糊匹配把识别结果和任务里登记的商品名对齐
"""
import os
import re
import subprocess
import sys
import tempfile

_PS_SCRIPT = r"""
param([string]$Path, [string]$OutPath)
$ErrorActionPreference = "Stop"
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $asTask.MakeGenericMethod($t).Invoke($null, @($op)).GetAwaiter().GetResult() }

$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$lang   = New-Object Windows.Globalization.Language "zh-Hans-CN"
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $engine) { [System.IO.File]::WriteAllText($OutPath, "", [System.Text.Encoding]::UTF8); exit 0 }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$text = ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
[System.IO.File]::WriteAllText($OutPath, $text, [System.Text.Encoding]::UTF8)
"""

# 带坐标的版本：列表模式要按行切分，必须知道每个词在哪
_PS_WORDS = r"""
param([string]$Path, [string]$OutPath)
$ErrorActionPreference = "Stop"
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $asTask.MakeGenericMethod($t).Invoke($null, @($op)).GetAwaiter().GetResult() }

$file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$lang   = New-Object Windows.Globalization.Language "zh-Hans-CN"
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $engine) { [System.IO.File]::WriteAllText($OutPath, "", [System.Text.Encoding]::UTF8); exit 0 }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$sb = New-Object System.Text.StringBuilder
foreach ($line in $result.Lines) {
  foreach ($w in $line.Words) {
    $r = $w.BoundingRect
    $null = $sb.AppendLine(("{0}`t{1}`t{2}`t{3}`t{4}" -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $w.Text))
  }
}
[System.IO.File]::WriteAllText($OutPath, $sb.ToString(), [System.Text.Encoding]::UTF8)
"""

# 汉字之间被 OCR 插进去的空格
_HAN_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_SPACES = re.compile(r"\s+")

_script_path = None


def _ensure_script():
    global _script_path
    if _script_path and os.path.isfile(_script_path):
        return _script_path
    d = os.path.join(tempfile.gettempdir(), "sjz_ocr")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "ocr.ps1")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write(_PS_SCRIPT)
    _script_path = p
    return p


def available():
    return sys.platform == "win32"


def read_text(img, timeout=6.0):
    """img: BGR ndarray。返回 OCR 出来的原始文本（已清理空格），失败返回 ""。"""
    if not available():
        return ""
    import cv2
    d = os.path.join(tempfile.gettempdir(), "sjz_ocr")
    os.makedirs(d, exist_ok=True)
    png = os.path.join(d, "shot.png")
    txt = os.path.join(d, "shot.txt")
    if os.path.exists(txt):
        try:
            os.remove(txt)
        except OSError:
            pass
    if not cv2.imwrite(png, img):
        return ""
    script = _ensure_script()
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Path", png, "-OutPath", txt],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if not os.path.isfile(txt):
        return ""
    with open(txt, encoding="utf-8-sig") as f:
        raw = f.read()
    return clean(raw)


def clean(text):
    """去掉汉字间空格、合并多余空白。"""
    if not text:
        return ""
    t = text.replace("\r", "\n")
    t = _HAN_GAP.sub("", t)
    t = _SPACES.sub(" ", t)
    return t.strip()


def normalize(s):
    """只留字母数字和汉字，用于模糊比对。"""
    if not s:
        return ""
    return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()


def similarity(a, b):
    """0-1 相似度。字符级 LCS 比例，避免引入额外依赖。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return min(len(na), len(nb)) / max(len(na), len(nb)) * 0.5 + 0.5
    # LCS 长度
    prev = [0] * (len(nb) + 1)
    for ca in na:
        cur = [0]
        for j, cb in enumerate(nb, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / max(len(na), len(nb))


def best_match(text, candidates, floor=0.55):
    """在候选商品名里找最像的。返回 (name, score) 或 (None, 0)。"""
    best, best_score = None, 0.0
    for c in candidates:
        s = similarity(text, c)
        if s > best_score:
            best, best_score = c, s
    if best_score < floor:
        return None, best_score
    return best, best_score


_PRICE = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_NUM = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


def parse_price(text):
    """从 OCR 文本里抠价格。找不到返回 None。"""
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        m = _NUM.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------- 带坐标的 OCR（列表模式用） ----------------

_words_script_path = None


def _ensure_words_script():
    global _words_script_path
    if _words_script_path and os.path.isfile(_words_script_path):
        return _words_script_path
    d = os.path.join(tempfile.gettempdir(), "sjz_ocr")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "ocr_words.ps1")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write(_PS_WORDS)
    _words_script_path = p
    return p


def read_words(img, timeout=10.0):
    """OCR 出每个词和它的包围盒。

    返回 [{"x","y","w","h","text"}]，坐标是相对传入图片左上角的像素。
    列表模式必须知道每个词在哪一行，才能把行聚起来、算出点哪儿。
    """
    if not available() or img is None or img.size == 0:
        return []
    import cv2
    d = os.path.join(tempfile.gettempdir(), "sjz_ocr")
    os.makedirs(d, exist_ok=True)
    png = os.path.join(d, "words.png")
    txt = os.path.join(d, "words.txt")
    if os.path.exists(txt):
        try:
            os.remove(txt)
        except OSError:
            pass
    if not cv2.imwrite(png, img):
        return []
    script = _ensure_words_script()
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Path", png, "-OutPath", txt],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if not os.path.isfile(txt):
        return []
    out = []
    with open(txt, encoding="utf-8-sig") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 5:
                continue
            try:
                x, y, w, h = (int(parts[0]), int(parts[1]),
                              int(parts[2]), int(parts[3]))
            except ValueError:
                continue
            t = parts[4].strip()
            if t:
                out.append({"x": x, "y": y, "w": w, "h": h, "text": t})
    return out


def rows(words, tol_ratio=0.7):
    """把词按纵向位置聚成行。

    容差取「词高中位数 × tol_ratio」：同一行的字高差不多，跨行的间距更大，
    这样不同字号混排（商品名大、价格小）也不会把两行粘成一行。
    """
    if not words:
        return []
    heights = sorted(w["h"] for w in words)
    median_h = heights[len(heights) // 2] or 1
    tol = max(2.0, median_h * tol_ratio)

    lines = []
    for w in sorted(words, key=lambda w: w["y"] + w["h"] / 2.0):
        cy = w["y"] + w["h"] / 2.0
        for line in lines:
            if abs(line["cy"] - cy) <= tol:
                line["words"].append(w)
                line["cy"] = sum(x["y"] + x["h"] / 2.0 for x in line["words"]) / len(line["words"])
                break
        else:
            lines.append({"cy": cy, "words": [w]})

    out = []
    for line in lines:
        ws = sorted(line["words"], key=lambda w: w["x"])
        x0 = min(w["x"] for w in ws)
        y0 = min(w["y"] for w in ws)
        x1 = max(w["x"] + w["w"] for w in ws)
        y1 = max(w["y"] + w["h"] for w in ws)
        out.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "cy": round((y0 + y1) / 2.0, 1),
            "text": clean(" ".join(w["text"] for w in ws)),
            "words": ws,
        })
    out.sort(key=lambda r: r["y0"])
    return out
