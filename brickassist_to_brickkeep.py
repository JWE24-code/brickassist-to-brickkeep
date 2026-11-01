#!/usr/bin/env python3
"""Export your own BrickAssist parts into a file Brickkeep can import directly.

Unofficial; not affiliated with BrickAssist or the LEGO Group. Talks only to
api.brickassist.com. Login is an emailed one-time code; the session token stays
in memory, and the email, code and token are never printed or written to disk.
After login this makes GET requests only.
"""
import argparse
import http.client
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

API_BASE = "https://api.brickassist.com"
ISSUES_URL = "https://github.com/JWE24-code/brickassist-to-brickkeep/issues"
TIMEOUT_SECONDS = 120
CODE_ATTEMPTS = 3
DEFAULT_OUTPUT = "brickkeep-import.json"

# Brickkeep's own rules (app/rules.js and src/store.ts). A row outside them makes
# Brickkeep reject the whole batch it arrives in, so such rows must not reach the file.
MAX_ROWS = 50
MAX_COLUMNS = 50
MAX_REF = 60
MAX_NAME = 200
MAX_QUANTITY = 10000000
LOCATION = re.compile(r"([A-Za-z])-([0-9]{1,9})-([0-9]{1,9})-?([A-Za-z]?)")
HEX_COLOR = re.compile(r"#?[0-9a-fA-F]{6}")

# How the API may answer a code it doesn't accept. Its schema documents only 422.
BAD_CODE_STATUSES = {400, 401, 403, 404, 422}


class ExportError(Exception):
    """A problem to explain to the user. Messages never carry the email, code or token."""


class HttpStatus(ExportError):
    def __init__(self, status):
        super().__init__(_status_message(status))
        self.status = status


class ShapeChanged(ExportError):
    def __init__(self, what):
        super().__init__(
            f"BrickAssist's answer doesn't look as expected ({what}). "
            f"Their API may have changed; please report it at {ISSUES_URL}"
        )


def _status_message(status):
    if status in (401, 403):
        return f"BrickAssist refused the session (HTTP {status}). Run the tool again to log in afresh."
    if status == 404:
        return f"BrickAssist no longer has this endpoint (HTTP 404). Their API may have changed; please report it at {ISSUES_URL}"
    if status == 429:
        return "BrickAssist says there were too many requests (HTTP 429). Wait a while and try again."
    if status >= 500:
        return f"BrickAssist is having trouble (HTTP {status}). Try again later."
    return f"BrickAssist answered with an unexpected status (HTTP {status})."


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # urllib would resend the Authorization header to wherever a redirect points.
    def redirect_request(self, *args, **kwargs):
        return None


def _request(path, token=None, body=None):
    headers = {"Accept": "application/json", "User-Agent": "brickassist-to-brickkeep"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API_BASE + path, data=data, headers=headers)
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        # The body is never shown: validation errors echo the submitted email and code.
        error.close()
        if 300 <= error.code < 400:
            raise ExportError(f"BrickAssist tried to redirect the request (HTTP {error.code}); refused.") from None
        raise HttpStatus(error.code) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise ExportError(
                "Couldn't verify BrickAssist's HTTPS certificate. On macOS with Python from "
                "python.org, run 'Install Certificates.command' from its Applications folder."
            ) from None
        raise ExportError(f"Couldn't reach BrickAssist: {error.reason}. Check your connection and try again.") from None
    except (OSError, http.client.HTTPException) as error:
        raise ExportError(f"The connection to BrickAssist failed: {type(error).__name__}. Try again.") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ShapeChanged("the response isn't JSON") from None


def _send_code(email):
    try:
        answer = _request(
            "/v1/auth/email_checker",
            body={"email": email, "language": "en", "is_account_creation": False},
        )
    except HttpStatus as error:
        if error.status in (400, 404, 422):
            raise ExportError(f"BrickAssist didn't accept that email address (HTTP {error.status}).") from None
        raise
    if isinstance(answer, dict) and answer.get("operation") is False:
        print("BrickAssist didn't confirm it sent a code. If no email arrives, check the address.")


def _ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise ExportError("Cancelled.") from None


def login(email):
    """Sends the emailed code and returns the access token, which only ever lives in memory."""
    email = email or _ask("BrickAssist account email: ")
    if "@" not in email or len(email) > 255:
        raise ExportError("That doesn't look like an email address.")
    _send_code(email)
    print("BrickAssist is emailing you a login code.")
    for attempts_left in range(CODE_ATTEMPTS - 1, -1, -1):
        code = re.sub(r"[\s-]", "", _ask("Code from the email: "))
        if re.fullmatch(r"[0-9]{1,12}", code):
            try:
                answer = _request("/v1/auth/login", body={"email": email, "code_email_check": int(code)})
            except HttpStatus as error:
                if error.status not in BAD_CODE_STATUSES:
                    raise
                problem = "BrickAssist rejected that code."
            else:
                token = answer.get("access_token") if isinstance(answer, dict) else None
                if not isinstance(token, str) or not token:
                    raise ShapeChanged("the login answer has no access token")
                return token
        else:
            problem = "The code should be digits only."
        if attempts_left:
            print(f"{problem} {attempts_left} more {'try' if attempts_left == 1 else 'tries'}.")
    raise ExportError(f"{problem} Check you used the newest email, or run the tool again for a new code.")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _js_length(text):
    # Brickkeep measures with JavaScript's String.length, in UTF-16 units.
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _valid_text(value, limit):
    return isinstance(value, str) and value.strip() != "" and _js_length(value) <= limit


def _dict_field(record, field):
    value = record.get(field)
    if not isinstance(value, dict):
        raise ShapeChanged(f"a stock has no '{field}' object")
    return value


def to_stock(record):
    """Maps one API part stock to a Brickkeep row. Raises ShapeChanged if it isn't one."""
    if not isinstance(record, dict):
        raise ShapeChanged("a stock isn't an object")
    colored = _dict_field(record, "part_colored")
    part = _dict_field(colored, "part")
    color = _dict_field(colored, "color")
    for source, field in ((part, "bl_ref"), (part, "bl_name"), (color, "bl_id"), (color, "bl_name")):
        if field not in source:
            raise ShapeChanged(f"a stock has no '{field}'")
    if not _is_int(record.get("id")):
        raise ShapeChanged("a stock has no whole-number 'id'")
    if not _is_int(record.get("quantity")):
        raise ShapeChanged("a stock has no whole-number 'quantity'")
    location = record.get("location")
    comment = record.get("comment")
    if not isinstance(location, (str, type(None))) or not isinstance(comment, (str, type(None))):
        raise ShapeChanged("a stock's location or comment isn't text")
    code = color.get("color_code")
    has_hex = isinstance(code, str) and HEX_COLOR.fullmatch(code)
    return {
        "sourceId": record["id"],
        "partRef": colored.get("printed_part_bl_ref") or part["bl_ref"],
        "partName": colored.get("printed_part_bl_name") or part["bl_name"],
        "colorId": color["bl_id"],
        "colorName": color["bl_name"],
        "colorHex": "#" + code.lstrip("#").upper() if has_hex else None,
        "quantity": record["quantity"],
        "location": location or "",
        "comment": comment or "",
    }


def why_unimportable(stock):
    """The reason Brickkeep would reject this row, or None if it would accept it."""
    if not _valid_text(stock["partRef"], MAX_REF):
        return "no usable BrickLink part number"
    if not _valid_text(stock["partName"], MAX_NAME):
        return "no usable BrickLink part name"
    if not _is_int(stock["colorId"]):
        return "no BrickLink colour id"
    if not _valid_text(stock["colorName"], MAX_NAME):
        return "no usable BrickLink colour name"
    if not 0 <= stock["quantity"] <= MAX_QUANTITY:
        return "quantity out of range"
    return None


def parse_location(text):
    """Returns (box, row, column, compartment number or 0), or None if it isn't a usable place."""
    match = LOCATION.fullmatch(text.strip())
    if not match:
        return None
    row, column = int(match[2]), int(match[3])
    if not (1 <= row <= MAX_ROWS and 1 <= column <= MAX_COLUMNS):
        return None
    compartment = ord(match[4].lower()) - ord("a") + 1 if match[4] else 0
    return match[1].upper(), row, column, compartment


def infer_boxes(stocks):
    """Sizes each box to the furthest row, column and compartment that any location uses."""
    boxes = {}
    for stock in stocks:
        place = parse_location(stock["location"])
        if place:
            name, row, column, compartment = place
            box = boxes.setdefault(name, {"name": name, "rows": 0, "columns": 0, "compartments": 0})
            box["rows"] = max(box["rows"], row)
            box["columns"] = max(box["columns"], column)
            box["compartments"] = max(box["compartments"], compartment)
    return [boxes[name] for name in sorted(boxes)]


def build_import(records):
    """Returns the Brickkeep file's content and the rows Brickkeep couldn't accept."""
    if not isinstance(records, list):
        raise ShapeChanged("the parts list isn't a list")
    stocks, skipped = [], []
    for record in records:
        stock = to_stock(record)
        reason = why_unimportable(stock)
        if reason:
            skipped.append({"reason": reason, "stock": record})
        else:
            stocks.append(stock)
    return {"boxes": infer_boxes(stocks), "stocks": stocks}, skipped


def write_private_json(path, content):
    """Writes atomically; the temporary file is owner-only where the OS supports that."""
    directory = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".brickassist-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(content, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def _optional_fetch(path, token, warnings, what):
    try:
        return _request(path, token=token)
    except ExportError:
        warnings.append(f"Couldn't check {what}, so this account may hold some that aren't in the file.")
        return None


def _bundle_stock_count(aggregated):
    return sum(len(entry.get("stocks") or []) for entry in aggregated if isinstance(entry, dict))


def _collect_extras(token, part_count, warnings):
    """Everything Brickkeep's format can't hold is reported here, not dropped silently."""
    extras = {}
    for key, path, what in (
        ("official_sets", "/v1/collection/official_sets", "official sets"),
        ("minifigs", "/v1/collection/minifigs", "minifigs"),
    ):
        answer = _optional_fetch(path, token, warnings, what)
        if answer is None:
            continue
        if not isinstance(answer, list):
            warnings.append(f"Couldn't read {what}, so this account may hold some that aren't in the file.")
            continue
        count = _bundle_stock_count(answer)
        if count:
            extras[key] = answer
            warnings.append(f"{what}: {count} NOT in the import file, because Brickkeep's format can't hold them.")

    counts = _optional_fetch("/v1/collection/count_stocks", token, warnings, "the account's totals")
    if isinstance(counts, dict):
        reported = counts.get("parts_stocks_count")
        if _is_int(reported) and reported != part_count:
            warnings.append(f"BrickAssist reports a total of {reported} part stocks but returned {part_count}.")
        for field, what in (("bundle_moc_stocks_count", "MOCs"), ("instructions_stocks_count", "instructions")):
            if _is_int(counts.get(field)) and counts[field] > 0:
                warnings.append(f"{what}: {counts[field]} NOT in the import file, and not exported by this tool.")
    return extras


def _extras_path(output):
    root, _ = os.path.splitext(output)
    return f"{root}-not-imported.json"


def _report(output, content, records, skipped, warnings, extras_written):
    stocks = content["stocks"]
    print(f"\nPart stocks written: {len(stocks)}, to {output}")
    print("It holds your inventory, so don't share it.")

    if content["boxes"]:
        print("\nBoxes inferred from your locations (adjust them in Brickkeep's Settings):")
        for box in content["boxes"]:
            print(f"  {box['name']}: {box['rows']} rows x {box['columns']} columns, {box['compartments']} compartments")

    empty = sum(1 for stock in stocks if not stock["location"].strip())
    not_understood = sum(1 for stock in stocks if stock["location"].strip() and not parse_location(stock["location"]))
    if not_understood or empty:
        print(f"\nLocations not understood: {not_understood}. Without a location: {empty}.")
        print("They are kept as they are; Brickkeep lists them under Review.")

    needed = sum(1 for record in records if record.get("needed") is True)
    if needed:
        print(f"\nStocks marked 'needed' in BrickAssist: {needed}. Brickkeep has no such mark; they import like the rest.")

    if skipped:
        warnings.insert(0, f"part stocks: {len(skipped)} NOT in the import file, because Brickkeep would reject them.")
    if warnings:
        print("\nWARNING, not everything could be included:")
        for warning in warnings:
            print(f"  - {warning}")
    if extras_written:
        print(f"The sets, minifigs and rejected stocks listed above are saved as raw JSON in {extras_written}")

    print("\nNext: in Brickkeep, open Settings > Import a file and choose the file.")


def run(email, output):
    output = os.path.abspath(os.path.expanduser(output))
    directory = os.path.dirname(output)
    # Checked first, so a bad path doesn't cost the user a login email.
    if os.path.isdir(output):
        raise ExportError(f"The output path is a folder: {output}")
    if not os.path.isdir(directory) or not os.access(directory, os.W_OK):
        raise ExportError(f"Can't write to the folder: {directory}")

    token = login(email)
    print("Logged in. Fetching your parts...")
    records = _request("/v1/collection/parts", token=token)
    content, skipped = build_import(records)

    warnings = []
    extras = _collect_extras(token, len(records), warnings)
    if skipped:
        extras["parts_not_imported"] = skipped

    try:
        write_private_json(output, content)
        extras_path = None
        if extras:
            extras_path = _extras_path(output)
            write_private_json(extras_path, extras)
    except OSError as error:
        raise ExportError(f"Couldn't write the file: {error.strerror or error}") from None
    _report(output, content, records, skipped, warnings, extras_path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export your own BrickAssist parts into a file Brickkeep can import. "
        "Unofficial; not affiliated with BrickAssist or the LEGO Group."
    )
    parser.add_argument("--email", help="your BrickAssist account email; asked for if left out")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"where to write the file (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)
    try:
        run(args.email, args.output)
    except ExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
