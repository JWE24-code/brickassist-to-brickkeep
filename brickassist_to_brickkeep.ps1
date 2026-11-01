#requires -Version 5.1
<#
.SYNOPSIS
Export your own BrickAssist parts into a file Brickkeep can import directly.

.DESCRIPTION
Unofficial; not affiliated with BrickAssist or the LEGO Group. Talks only to
api.brickassist.com. Login is an emailed one-time code; the session token stays
in memory, and the email, code and token are never printed or written to disk.
After login this makes GET requests only.

.PARAMETER Email
Your BrickAssist account email; asked for if left out.

.PARAMETER Output
Where to write the file. Default: brickkeep-import.json in the current folder.
#>
[CmdletBinding()]
param(
    [string]$Email,
    [string]$Output = 'brickkeep-import.json'
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$ApiBase = 'https://api.brickassist.com'
$IssuesUrl = 'https://github.com/JWE24-code/brickassist-to-brickkeep/issues'
$TimeoutMilliseconds = 120000
$CodeAttempts = 3

# Brickkeep's own rules (app/rules.js and src/store.ts). A row outside them makes
# Brickkeep reject the whole batch it arrives in, so such rows must not reach the file.
$MaxRows = 50
$MaxColumns = 50
$MaxRef = 60
$MaxName = 200
$MaxQuantity = 10000000
$LocationPattern = New-Object regex '\A([A-Za-z])-([0-9]{1,9})-([0-9]{1,9})-?([A-Za-z]?)\z'
$HexColorPattern = New-Object regex '\A#?[0-9a-fA-F]{6}\z'

# How the API may answer a code it doesn't accept. Its schema documents only 422.
$BadCodeStatuses = 400, 401, 403, 404, 422

# ConvertFrom-Json differs between PowerShell versions and turns date-like text into
# dates, so the tool reads and writes JSON itself and both versions give the same file.
$JsonSpace = New-Object regex '\G[ \t\r\n]*'
$JsonString = New-Object regex '\G"[^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*)*"'
$JsonNumber = New-Object regex '\G-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?'
$JsonEscape = New-Object regex '\\(?:u([0-9a-fA-F]{4})|(.))'
$JsonNeedsEscape = New-Object regex '["\\\x00-\x1f]'
$JsonText = ''
$JsonPosition = 0

if ($PSVersionTable.PSEdition -ne 'Core') {
    # Windows PowerShell may not offer TLS 1.2 by default, and would add an Expect header.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    [Net.ServicePointManager]::Expect100Continue = $false
}


# A problem to explain to the user. Messages never carry the email, code or token.
function New-ExportError([string]$Message, $Status = $null) {
    $problem = New-Object System.Exception $Message
    $problem.Data['ExportError'] = $true
    $problem.Data['Status'] = $Status
    return $problem
}

function New-ShapeChanged([string]$What) {
    return New-ExportError "BrickAssist's answer doesn't look as expected ($What). Their API may have changed; please report it at $IssuesUrl"
}

function Test-ExportError($Problem) {
    return $Problem.Exception.Data.Contains('ExportError')
}

function Get-StatusMessage([int]$Status) {
    if ($Status -eq 401 -or $Status -eq 403) {
        return "BrickAssist refused the session (HTTP $Status). Run the tool again to log in afresh."
    }
    if ($Status -eq 404) {
        return "BrickAssist no longer has this endpoint (HTTP 404). Their API may have changed; please report it at $IssuesUrl"
    }
    if ($Status -eq 429) {
        return 'BrickAssist says there were too many requests (HTTP 429). Wait a while and try again.'
    }
    if ($Status -ge 500) {
        return "BrickAssist is having trouble (HTTP $Status). Try again later."
    }
    return "BrickAssist answered with an unexpected status (HTTP $Status)."
}


function Stop-BadJson {
    throw (New-Object System.FormatException 'Not JSON')
}

function Skip-JsonSpace {
    $script:JsonPosition += $JsonSpace.Match($script:JsonText, $script:JsonPosition).Length
}

function Read-JsonString {
    $match = $JsonString.Match($script:JsonText, $script:JsonPosition)
    if (-not $match.Success) { Stop-BadJson }
    $script:JsonPosition += $match.Length
    $inner = $match.Value.Substring(1, $match.Length - 2)
    if ($inner.IndexOf([char]'\') -lt 0) { return $inner }
    return $JsonEscape.Replace($inner, [System.Text.RegularExpressions.MatchEvaluator] {
        param($found)
        if ($found.Groups[1].Success) {
            return [string][char][Convert]::ToInt32($found.Groups[1].Value, 16)
        }
        switch -CaseSensitive ($found.Groups[2].Value) {
            'b' { return [string][char]8 }
            'f' { return [string][char]12 }
            'n' { return [string][char]10 }
            'r' { return [string][char]13 }
            't' { return [string][char]9 }
            default { return $_ }
        }
    })
}

function Read-JsonNumber {
    $match = $JsonNumber.Match($script:JsonText, $script:JsonPosition)
    if (-not $match.Success) { Stop-BadJson }
    $script:JsonPosition += $match.Length
    $invariant = [Globalization.CultureInfo]::InvariantCulture
    $whole = 0L
    if (-not $match.Groups[1].Success -and -not $match.Groups[2].Success -and
        [long]::TryParse($match.Value, [Globalization.NumberStyles]::AllowLeadingSign, $invariant, [ref]$whole)) {
        return $whole
    }
    return [double]::Parse($match.Value, [Globalization.NumberStyles]::Float, $invariant)
}

function Test-JsonWord([string]$Word) {
    if ([string]::CompareOrdinal($script:JsonText, $script:JsonPosition, $Word, 0, $Word.Length) -ne 0) { return $false }
    $script:JsonPosition += $Word.Length
    return $true
}

function Read-JsonCharacter([char]$Expected) {
    Skip-JsonSpace
    if ($script:JsonPosition -ge $script:JsonText.Length -or $script:JsonText[$script:JsonPosition] -cne $Expected) { Stop-BadJson }
    $script:JsonPosition++
}

function Test-JsonNext([char]$Expected) {
    Skip-JsonSpace
    return $script:JsonPosition -lt $script:JsonText.Length -and $script:JsonText[$script:JsonPosition] -ceq $Expected
}

# Objects keep their order and, unlike a PowerShell hashtable, tell keys apart by case.
function Read-JsonObject {
    $object = New-Object System.Collections.Specialized.OrderedDictionary
    Read-JsonCharacter '{'
    if (Test-JsonNext '}') {
        $script:JsonPosition++
        return , $object
    }
    while ($true) {
        Skip-JsonSpace
        $key = Read-JsonString
        Read-JsonCharacter ':'
        $object[[object]$key] = Read-JsonValue
        if (Test-JsonNext '}') {
            $script:JsonPosition++
            return , $object
        }
        Read-JsonCharacter ','
    }
}

function Read-JsonArray {
    $list = New-Object System.Collections.ArrayList
    Read-JsonCharacter '['
    if (Test-JsonNext ']') {
        $script:JsonPosition++
        return , $list
    }
    while ($true) {
        $item = Read-JsonValue
        [void]$list.Add($item)
        if (Test-JsonNext ']') {
            $script:JsonPosition++
            return , $list
        }
        Read-JsonCharacter ','
    }
}

function Read-JsonValue {
    Skip-JsonSpace
    if ($script:JsonPosition -ge $script:JsonText.Length) { Stop-BadJson }
    $next = $script:JsonText[$script:JsonPosition]
    if ($next -ceq '"') { return Read-JsonString }
    if ($next -ceq '{') { return , (Read-JsonObject) }
    if ($next -ceq '[') { return , (Read-JsonArray) }
    if (Test-JsonWord 'true') { return $true }
    if (Test-JsonWord 'false') { return $false }
    if (Test-JsonWord 'null') { return $null }
    return Read-JsonNumber
}

function ConvertFrom-JsonText([string]$Text) {
    $script:JsonText = $Text
    $script:JsonPosition = 0
    $value = Read-JsonValue
    Skip-JsonSpace
    if ($script:JsonPosition -ne $script:JsonText.Length) { Stop-BadJson }
    return , $value
}

function Add-JsonString([System.Text.StringBuilder]$Builder, [string]$Text) {
    [void]$Builder.Append('"')
    if ($JsonNeedsEscape.IsMatch($Text)) {
        $Text = $JsonNeedsEscape.Replace($Text, [System.Text.RegularExpressions.MatchEvaluator] {
            param($found)
            $code = [int][char]$found.Value
            switch ($code) {
                8 { return '\b' }
                9 { return '\t' }
                10 { return '\n' }
                12 { return '\f' }
                13 { return '\r' }
                34 { return '\"' }
                92 { return '\\' }
                default { return '\u' + $code.ToString('x4') }
            }
        })
    }
    [void]$Builder.Append($Text).Append('"')
}

function Add-JsonValue([System.Text.StringBuilder]$Builder, $Value, [int]$Depth) {
    $invariant = [Globalization.CultureInfo]::InvariantCulture
    if ($null -eq $Value) {
        [void]$Builder.Append('null')
    } elseif ($Value -is [string]) {
        Add-JsonString $Builder $Value
    } elseif ($Value -is [bool]) {
        [void]$Builder.Append($(if ($Value) { 'true' } else { 'false' }))
    } elseif ($Value -is [int] -or $Value -is [long]) {
        [void]$Builder.Append($Value.ToString($invariant))
    } elseif ($Value -is [double]) {
        $text = $Value.ToString('R', $invariant)
        if ($text -notmatch '[.E]') { $text += '.0' }
        [void]$Builder.Append($text)
    } elseif ($Value -is [System.Collections.IDictionary]) {
        # psbase: a plain .Count or .Keys would read a key of that name from the data.
        if ($Value.psbase.Count -eq 0) {
            [void]$Builder.Append('{}')
            return
        }
        $indent = ' ' * (2 * ($Depth + 1))
        $separator = '{'
        foreach ($key in @($Value.psbase.Keys)) {
            [void]$Builder.Append($separator).Append("`n").Append($indent)
            Add-JsonString $Builder $key
            [void]$Builder.Append(': ')
            Add-JsonValue $Builder $Value[[object]$key] ($Depth + 1)
            $separator = ','
        }
        [void]$Builder.Append("`n").Append(' ' * (2 * $Depth)).Append('}')
    } elseif ($Value -is [System.Collections.IList]) {
        if ($Value.Count -eq 0) {
            [void]$Builder.Append('[]')
            return
        }
        $indent = ' ' * (2 * ($Depth + 1))
        $separator = '['
        foreach ($item in $Value) {
            [void]$Builder.Append($separator).Append("`n").Append($indent)
            Add-JsonValue $Builder $item ($Depth + 1)
            $separator = ','
        }
        [void]$Builder.Append("`n").Append(' ' * (2 * $Depth)).Append(']')
    } else {
        throw "Can't write a $($Value.GetType().Name) as JSON"
    }
}

function ConvertTo-JsonText($Value) {
    $builder = New-Object System.Text.StringBuilder
    Add-JsonValue $builder $Value 0
    return $builder.ToString()
}


function Find-InnerException($Problem, [type]$Type) {
    $current = $Problem
    while ($null -ne $current) {
        if ($current -is $Type) { return $current }
        $current = $current.InnerException
    }
    return $null
}

function Invoke-BrickAssist([string]$Path, [string]$Token = '', $Body = $null) {
    # WebRequest, not Invoke-RestMethod: that follows redirects by default in some
    # versions, and throws away the difference between its error shapes.
    $request = [System.Net.WebRequest]::Create($ApiBase + $Path)
    $request.AllowAutoRedirect = $false
    $request.Timeout = $TimeoutMilliseconds
    $request.ReadWriteTimeout = $TimeoutMilliseconds
    $request.Accept = 'application/json'
    $request.UserAgent = 'brickassist-to-brickkeep'
    if ($Token) { $request.Headers.Add('Authorization', "Bearer $Token") }

    $status = 0
    $raw = $null
    $connected = $false
    $response = $null
    try {
        if ($null -ne $Body) {
            $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes((ConvertTo-JsonText $Body))
            $request.Method = 'POST'
            $request.ContentType = 'application/json'
            $stream = $request.GetRequestStream()
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Close()
        }
        $response = $request.GetResponse()
        $connected = $true
        $status = [int]$response.StatusCode
        if ($status -ge 200 -and $status -lt 300) {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
            $raw = $reader.ReadToEnd()
        }
    } catch {
        $failure = Find-InnerException $_.Exception ([System.Net.WebException])
        if ($null -ne $failure -and $null -ne $failure.Response) {
            # The body is never shown: validation errors echo the submitted email and code.
            $status = [int]$failure.Response.StatusCode
            $failure.Response.Close()
        } elseif ($null -ne (Find-InnerException $_.Exception ([System.Security.Authentication.AuthenticationException])) -or
            ($null -ne $failure -and $failure.Status -eq [System.Net.WebExceptionStatus]::TrustFailure)) {
            throw (New-ExportError "Couldn't verify BrickAssist's HTTPS certificate. Check your computer's date, and any antivirus or proxy that inspects HTTPS.")
        } elseif ($connected) {
            throw (New-ExportError 'The connection to BrickAssist failed. Try again.')
        } else {
            throw (New-ExportError "Couldn't reach BrickAssist. Check your connection and try again.")
        }
    } finally {
        if ($null -ne $response) { $response.Close() }
    }

    if ($status -ge 300 -and $status -lt 400) {
        throw (New-ExportError "BrickAssist tried to redirect the request (HTTP $status); refused.")
    }
    if ($status -lt 200 -or $status -ge 300) {
        throw (New-ExportError (Get-StatusMessage $status) $status)
    }
    try {
        return , (ConvertFrom-JsonText $raw)
    } catch [System.FormatException] {
        throw (New-ShapeChanged "the response isn't JSON")
    }
}


function Read-Answer([string]$Prompt) {
    if ([Console]::IsInputRedirected) {
        # Read-Host would echo piped input, and so the email and code, into any captured output.
        [Console]::Out.Write("${Prompt}: ")
        $answer = [Console]::In.ReadLine()
    } else {
        $answer = Read-Host $Prompt
    }
    if ($null -eq $answer) { throw (New-ExportError 'Cancelled.') }
    return $answer.Trim()
}

function Send-LoginCode([string]$Address) {
    try {
        $answer = Invoke-BrickAssist '/v1/auth/email_checker' -Body ([ordered]@{ email = $Address; language = 'en'; is_account_creation = $false })
    } catch {
        $status = $_.Exception.Data['Status']
        if ($status -eq 400 -or $status -eq 404 -or $status -eq 422) {
            throw (New-ExportError "BrickAssist didn't accept that email address (HTTP $status).")
        }
        throw
    }
    if ($answer -is [System.Collections.IDictionary] -and $answer['operation'] -is [bool] -and -not $answer['operation']) {
        Write-Host "BrickAssist didn't confirm it sent a code. If no email arrives, check the address."
    }
}

# Sends the emailed code and returns the access token, which only ever lives in memory.
function Get-AccessToken([string]$Address) {
    if (-not $Address) { $Address = Read-Answer 'BrickAssist account email' }
    if ($Address.IndexOf([char]'@') -lt 0 -or $Address.Length -gt 255) {
        throw (New-ExportError "That doesn't look like an email address.")
    }
    Send-LoginCode $Address
    Write-Host 'BrickAssist is emailing you a login code.'
    $problem = ''
    for ($attemptsLeft = $CodeAttempts - 1; $attemptsLeft -ge 0; $attemptsLeft--) {
        $code = [regex]::Replace((Read-Answer 'Code from the email'), '[\s-]', '')
        if ($code -cmatch '\A[0-9]{1,12}\z') {
            $accepted = $false
            try {
                $answer = Invoke-BrickAssist '/v1/auth/login' -Body ([ordered]@{ email = $Address; code_email_check = [long]$code })
                $accepted = $true
            } catch {
                if ($BadCodeStatuses -notcontains $_.Exception.Data['Status']) { throw }
                $problem = 'BrickAssist rejected that code.'
            }
            if ($accepted) {
                $token = $null
                if ($answer -is [System.Collections.IDictionary]) { $token = $answer['access_token'] }
                if ($token -isnot [string] -or -not $token) {
                    throw (New-ShapeChanged 'the login answer has no access token')
                }
                return $token
            }
        } else {
            $problem = 'The code should be digits only.'
        }
        if ($attemptsLeft -gt 0) {
            $tries = if ($attemptsLeft -eq 1) { 'try' } else { 'tries' }
            Write-Host "$problem $attemptsLeft more $tries."
        }
    }
    throw (New-ExportError "$problem Check you used the newest email, or run the tool again for a new code.")
}


function Test-ValidText($Value, [int]$Limit) {
    # .Length counts UTF-16 units, as Brickkeep's JavaScript String.length does.
    return $Value -is [string] -and $Value.Trim().Length -gt 0 -and $Value.Length -le $Limit
}

function Get-DictField($Record, [string]$Field) {
    $value = $Record[[object]$Field]
    if ($value -isnot [System.Collections.IDictionary]) {
        throw (New-ShapeChanged "a stock has no '$Field' object")
    }
    return , $value
}

# Maps one API part stock to a Brickkeep row. Throws if it isn't one.
function ConvertTo-Stock($Record) {
    if ($Record -isnot [System.Collections.IDictionary]) {
        throw (New-ShapeChanged "a stock isn't an object")
    }
    $colored = Get-DictField $Record 'part_colored'
    $part = Get-DictField $colored 'part'
    $color = Get-DictField $colored 'color'
    foreach ($check in @(@($part, 'bl_ref'), @($part, 'bl_name'), @($color, 'bl_id'), @($color, 'bl_name'))) {
        if (-not $check[0].Contains($check[1])) {
            throw (New-ShapeChanged "a stock has no '$($check[1])'")
        }
    }
    if ($Record['id'] -isnot [long]) { throw (New-ShapeChanged "a stock has no whole-number 'id'") }
    if ($Record['quantity'] -isnot [long]) { throw (New-ShapeChanged "a stock has no whole-number 'quantity'") }
    $location = $Record['location']
    $comment = $Record['comment']
    if (($null -ne $location -and $location -isnot [string]) -or ($null -ne $comment -and $comment -isnot [string])) {
        throw (New-ShapeChanged "a stock's location or comment isn't text")
    }
    $code = $color['color_code']
    $hex = $null
    if ($code -is [string] -and $HexColorPattern.IsMatch($code)) {
        $hex = '#' + $code.TrimStart('#').ToUpperInvariant()
    }
    $partRef = if ($colored['printed_part_bl_ref']) { $colored['printed_part_bl_ref'] } else { $part['bl_ref'] }
    $partName = if ($colored['printed_part_bl_name']) { $colored['printed_part_bl_name'] } else { $part['bl_name'] }
    return [ordered]@{
        sourceId = $Record['id']
        partRef = $partRef
        partName = $partName
        colorId = $color['bl_id']
        colorName = $color['bl_name']
        colorHex = $hex
        quantity = $Record['quantity']
        location = $(if ($location) { $location } else { '' })
        comment = $(if ($comment) { $comment } else { '' })
    }
}

# The reason Brickkeep would reject this row, or $null if it would accept it.
function Get-UnimportableReason($Stock) {
    if (-not (Test-ValidText $Stock['partRef'] $MaxRef)) { return 'no usable BrickLink part number' }
    if (-not (Test-ValidText $Stock['partName'] $MaxName)) { return 'no usable BrickLink part name' }
    if ($Stock['colorId'] -isnot [long]) { return 'no BrickLink colour id' }
    if (-not (Test-ValidText $Stock['colorName'] $MaxName)) { return 'no usable BrickLink colour name' }
    if ($Stock['quantity'] -lt 0 -or $Stock['quantity'] -gt $MaxQuantity) { return 'quantity out of range' }
    return $null
}

# Returns the box, row, column and compartment (or 0), or $null if it isn't a usable place.
function ConvertFrom-Location([string]$Text) {
    $match = $LocationPattern.Match($Text.Trim())
    if (-not $match.Success) { return $null }
    $row = [int]$match.Groups[2].Value
    $column = [int]$match.Groups[3].Value
    if ($row -lt 1 -or $row -gt $MaxRows -or $column -lt 1 -or $column -gt $MaxColumns) { return $null }
    $compartment = 0
    if ($match.Groups[4].Length -gt 0) {
        $compartment = [int][char]::ToLowerInvariant($match.Groups[4].Value[0]) - [int][char]'a' + 1
    }
    return @{ Box = $match.Groups[1].Value.ToUpperInvariant(); Row = $row; Column = $column; Compartment = $compartment }
}

# Sizes each box to the furthest row, column and compartment that any location uses.
function Get-InferredBoxes($Stocks) {
    $boxes = @{}
    foreach ($stock in $Stocks) {
        $place = ConvertFrom-Location $stock['location']
        if ($null -eq $place) { continue }
        if (-not $boxes.ContainsKey($place.Box)) {
            $boxes[$place.Box] = [ordered]@{ name = $place.Box; rows = 0; columns = 0; compartments = 0 }
        }
        $box = $boxes[$place.Box]
        $box['rows'] = [Math]::Max($box['rows'], $place.Row)
        $box['columns'] = [Math]::Max($box['columns'], $place.Column)
        $box['compartments'] = [Math]::Max($box['compartments'], $place.Compartment)
    }
    $names = [string[]]@($boxes.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    $sorted = New-Object System.Collections.ArrayList
    foreach ($name in $names) { [void]$sorted.Add($boxes[$name]) }
    return , $sorted
}

# Returns the Brickkeep file's content and the rows Brickkeep couldn't accept.
function New-ImportContent($Records) {
    if ($Records -isnot [System.Collections.IList]) {
        throw (New-ShapeChanged "the parts list isn't a list")
    }
    $stocks = New-Object System.Collections.ArrayList
    $skipped = New-Object System.Collections.ArrayList
    foreach ($record in $Records) {
        $stock = ConvertTo-Stock $record
        $reason = Get-UnimportableReason $stock
        if ($reason) {
            [void]$skipped.Add([ordered]@{ reason = $reason; stock = $record })
        } else {
            [void]$stocks.Add($stock)
        }
    }
    return @{ Content = [ordered]@{ boxes = (Get-InferredBoxes $stocks); stocks = $stocks }; Skipped = $skipped }
}


# Writes atomically; the temporary file is owner-only where the OS supports that.
function Write-PrivateJson([string]$Path, $Content) {
    $temporary = Join-Path ([System.IO.Path]::GetDirectoryName($Path)) ('.brickassist-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $encoding = New-Object System.Text.UTF8Encoding $false
    try {
        [System.IO.File]::WriteAllText($temporary, '', $encoding)
        if ($env:OS -cne 'Windows_NT') {
            & chmod 600 $temporary
            if ($LASTEXITCODE -ne 0) { throw "chmod failed" }
        }
        [System.IO.File]::WriteAllText($temporary, (ConvertTo-JsonText $Content) + "`n", $encoding)
        if ([System.IO.File]::Exists($Path)) {
            [System.IO.File]::Replace($temporary, $Path, [NullString]::Value)
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } catch {
        if ([System.IO.File]::Exists($temporary)) { [System.IO.File]::Delete($temporary) }
        $cause = $_.Exception
        while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
        throw (New-ExportError "Couldn't write the file: $($cause.Message)")
    }
}

function Get-OptionalAnswer([string]$Path, [string]$Token, $Warnings, [string]$What) {
    try {
        return , (Invoke-BrickAssist $Path -Token $Token)
    } catch {
        if (-not (Test-ExportError $_)) { throw }
        $Warnings.Add("Couldn't check $What, so this account may hold some that aren't in the file.")
        return $null
    }
}

function Get-BundleStockCount($Aggregated) {
    $count = 0
    foreach ($entry in $Aggregated) {
        if ($entry -is [System.Collections.IDictionary] -and $entry['stocks'] -is [System.Collections.IList]) {
            $count += $entry['stocks'].Count
        }
    }
    return $count
}

# Everything Brickkeep's format can't hold is reported here, not dropped silently.
function Get-Extras([string]$Token, [int]$PartCount, $Warnings) {
    $extras = [ordered]@{}
    foreach ($kind in @(
            @('official_sets', '/v1/collection/official_sets', 'official sets'),
            @('minifigs', '/v1/collection/minifigs', 'minifigs'))) {
        $what = $kind[2]
        $answer = Get-OptionalAnswer $kind[1] $Token $Warnings $what
        if ($null -eq $answer) { continue }
        if ($answer -isnot [System.Collections.IList]) {
            $Warnings.Add("Couldn't read $what, so this account may hold some that aren't in the file.")
            continue
        }
        $count = Get-BundleStockCount $answer
        if ($count -gt 0) {
            $extras[$kind[0]] = $answer
            $Warnings.Add("${what}: $count NOT in the import file, because Brickkeep's format can't hold them.")
        }
    }

    $counts = Get-OptionalAnswer '/v1/collection/count_stocks' $Token $Warnings "the account's totals"
    if ($counts -is [System.Collections.IDictionary]) {
        $reported = $counts['parts_stocks_count']
        if ($reported -is [long] -and $reported -ne $PartCount) {
            $Warnings.Add("BrickAssist reports a total of $reported part stocks but returned $PartCount.")
        }
        foreach ($total in @(@('bundle_moc_stocks_count', 'MOCs'), @('instructions_stocks_count', 'instructions'))) {
            $value = $counts[[object]$total[0]]
            if ($value -is [long] -and $value -gt 0) {
                $Warnings.Add("$($total[1]): $value NOT in the import file, and not exported by this tool.")
            }
        }
    }
    return , $extras
}

function Write-Report([string]$Path, $Content, $Records, $Skipped, $Warnings, [string]$ExtrasWritten) {
    $stocks = $Content['stocks']
    Write-Host "`nPart stocks written: $($stocks.Count), to $Path"
    Write-Host "It holds your inventory, so don't share it."

    if ($Content['boxes'].Count -gt 0) {
        Write-Host "`nBoxes inferred from your locations (adjust them in Brickkeep's Settings):"
        foreach ($box in $Content['boxes']) {
            Write-Host "  $($box['name']): $($box['rows']) rows x $($box['columns']) columns, $($box['compartments']) compartments"
        }
    }

    $empty = 0
    $notUnderstood = 0
    foreach ($stock in $stocks) {
        if ($stock['location'].Trim().Length -eq 0) {
            $empty++
        } elseif ($null -eq (ConvertFrom-Location $stock['location'])) {
            $notUnderstood++
        }
    }
    if ($notUnderstood -gt 0 -or $empty -gt 0) {
        Write-Host "`nLocations not understood: $notUnderstood. Without a location: $empty."
        Write-Host 'They are kept as they are; Brickkeep lists them under Review.'
    }

    $needed = 0
    foreach ($record in $Records) {
        if ($record['needed'] -is [bool] -and $record['needed']) { $needed++ }
    }
    if ($needed -gt 0) {
        Write-Host "`nStocks marked 'needed' in BrickAssist: $needed. Brickkeep has no such mark; they import like the rest."
    }

    if ($Skipped.Count -gt 0) {
        $Warnings.Insert(0, "part stocks: $($Skipped.Count) NOT in the import file, because Brickkeep would reject them.")
    }
    if ($Warnings.Count -gt 0) {
        Write-Host "`nWARNING, not everything could be included:"
        foreach ($warning in $Warnings) { Write-Host "  - $warning" }
    }
    if ($ExtrasWritten) {
        Write-Host "The sets, minifigs and rejected stocks listed above are saved as raw JSON in $ExtrasWritten"
    }

    Write-Host "`nNext: in Brickkeep, open Settings > Import a file and choose the file."
}

function Invoke-Export([string]$Address, [string]$Path) {
    $directory = [System.IO.Path]::GetDirectoryName($Path)
    # Checked first, so a bad path doesn't cost the user a login email.
    if (Test-Path -LiteralPath $Path -PathType Container) {
        throw (New-ExportError "The output path is a folder: $Path")
    }
    $probe = Join-Path $directory ('.brickassist-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($probe, '')
        [System.IO.File]::Delete($probe)
    } catch {
        throw (New-ExportError "Can't write to the folder: $directory")
    }

    $token = Get-AccessToken $Address
    Write-Host 'Logged in. Fetching your parts...'
    $records = Invoke-BrickAssist '/v1/collection/parts' -Token $token
    $built = New-ImportContent $records

    $warnings = New-Object 'System.Collections.Generic.List[string]'
    $extras = Get-Extras $token $records.Count $warnings
    if ($built.Skipped.Count -gt 0) { $extras['parts_not_imported'] = $built.Skipped }

    Write-PrivateJson $Path $built.Content
    $extrasPath = ''
    if ($extras.psbase.Count -gt 0) {
        $extrasPath = Join-Path $directory ([System.IO.Path]::GetFileNameWithoutExtension($Path) + '-not-imported.json')
        Write-PrivateJson $extrasPath $extras
    }
    Write-Report $Path $built.Content $records $built.Skipped $warnings $extrasPath
}


try {
    # Relative paths and ~ follow PowerShell's current folder, which .NET doesn't track.
    Invoke-Export $Email $PSCmdlet.GetUnresolvedProviderPathFromPSPath($Output)
} catch {
    if (Test-ExportError $_) {
        [Console]::Error.WriteLine("Error: $($_.Exception.Message)")
    } else {
        # Only the type and line: an unexpected error's text could quote what was typed.
        [Console]::Error.WriteLine("Error: something unexpected went wrong ($($_.Exception.GetType().Name), line $($_.InvocationInfo.ScriptLineNumber)). Please report it at $IssuesUrl")
    }
    exit 1
}
exit 0
