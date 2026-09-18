$targets = @(
    "C:\Users\Shafah\AppData\Local\Temp",
    "C:\Users\Shafah\AppData\Local\pip",
    "C:\Users\Shafah\AppData\Local\Microsoft\Windows\INetCache",
    "C:\Windows\Temp",
    "C:\Users\Shafah\AppData\Local\CrashDumps",
    "C:\Users\Shafah\AppData\Local\Diagnostics",
    "C:\Users\Shafah\.cache",
    "C:\Users\Shafah\Downloads"
)
foreach ($p in $targets) {
    if (Test-Path $p) {
        $size = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        "{0,8:N2} MB  {1}" -f ($size/1MB), $p
    } else {
        "(missing) $p"
    }
}
