"""Runs the whole tool against a local fake API built from BrickAssist's public schema.

All data here is invented. Run with: python3 -m unittest
"""
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import brickassist_to_brickkeep as tool

EMAIL = "builder@example.com"
GOOD_CODE = 123456
BAD_CODE = 999999
TOKEN = "fake-access-token-abc"


def color(bl_id=11, bl_name="Black", color_code="05131D"):
    return {
        "lego_name": "Black", "lego_id": 26, "bl_name": bl_name, "bl_id": bl_id, "color_code": color_code,
        "timeline": "1950-2026", "is_active": True, "comment": "", "category": "solid", "hsv": 0, "id": 1,
    }


def part_stock(stock_id, location="A-1-1a", quantity=5, bl_ref="3001", bl_name="Brick 2 x 4", the_color=None,
               printed_ref=None, printed_name=None, comment="", needed=False):
    return {
        "id": stock_id, "quantity": quantity, "needed": needed, "location": location, "comment": comment,
        "last_update": None,
        "part_colored": {
            "id": stock_id + 1000, "image_name": "x.jpg", "lego_exact_id": None,
            "printed_part_lego_name": printed_name, "printed_part_bl_ref": printed_ref,
            "printed_part_bl_name": printed_name, "is_printed": bool(printed_ref),
            "part": {
                "lego_name": bl_name, "lego_ref": bl_ref, "bl_name": bl_name, "bl_ref": bl_ref,
                "description": "", "id": stock_id + 2000,
            },
            "color": the_color or color(),
        },
    }


def bundle(name, stocks=1):
    return {
        "bundle": {
            "identifier": "abc", "type": 1, "name": name, "ref": "1234-1", "description": None,
            "release_year": 2020, "images": [],
        },
        "stocks": [
            {
                "status": "built", "state": "full", "bought_year": None, "bought_price": None, "wanted_price": None,
                "uuid": f"00000000-0000-0000-0000-00000000000{i}", "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            for i in range(stocks)
        ],
    }


class FakeApi:
    def __init__(self):
        self.responses = {
            "/v1/collection/parts": (200, []),
            "/v1/collection/official_sets": (200, []),
            "/v1/collection/minifigs": (200, []),
            "/v1/collection/count_stocks": (200, None),
        }
        self.requests = []
        self.bad_code_status = 400
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=()):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, self.headers.get("Authorization")))
                if self.path == "/v1/auth/email_checker":
                    self._send(200, {"operation": True, "message": "sent"})
                elif self.path == "/v1/auth/login" and body.get("code_email_check") == GOOD_CODE:
                    self._send(200, {
                        "access_token": TOKEN, "refresh_token": "fake-refresh",
                        "token_type": "bearer", "minimum_client_version": 1,
                    })
                elif self.path == "/v1/auth/login":
                    # As FastAPI validation errors do, this echoes what was submitted.
                    self._send(fake.bad_code_status, {"detail": [{"input": body, "msg": "wrong code"}]})
                else:
                    self._send(404, {"detail": "Not Found"})

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"detail": "Not authenticated"})
                    return
                status, body = fake.responses.get(self.path, (404, {"detail": "Not Found"}))
                if status == "drop":
                    self.connection.close()
                elif 300 <= status < 400:
                    self._send(status, b"", [("Location", body)])
                elif self.path.endswith("count_stocks") and body is None:
                    self._send(200, {
                        "parts_stocks_count": len(fake.responses["/v1/collection/parts"][1]),
                        "bundle_officials_stocks_count": 0, "bundle_moc_stocks_count": 0,
                        "bundle_minifigs_stocks_count": 0, "instructions_stocks_count": 0,
                    })
                else:
                    self._send(status, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [path for _, path, _ in self.requests]


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.addCleanup(self.api.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.output = os.path.join(self.folder, "out.json")
        for patcher in (
            mock.patch.object(tool, "API_BASE", self.api.url),
            mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, answers=(EMAIL, str(GOOD_CODE)), args=()):
        """Runs main() with typed answers. Returns the exit code and everything printed."""
        printed = io.StringIO()
        typed = iter(answers)

        def fake_input(prompt=""):
            printed.write(prompt)
            try:
                return next(typed)
            except StopIteration:
                raise EOFError from None

        with mock.patch("builtins.input", fake_input):
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
                code = tool.main(["--output", self.output, *args])
        return code, printed.getvalue()

    def set_parts(self, *parts):
        self.api.responses["/v1/collection/parts"] = (200, list(parts))

    def exported(self):
        with open(self.output, encoding="utf-8") as file:
            return json.load(file)

    def assert_nothing_written(self):
        self.assertEqual(os.listdir(self.folder), [])


class FullFlow(ToolTestCase):
    def test_login_and_export_writes_brickkeep_format(self):
        self.set_parts(part_stock(7, location="A-2-3b", quantity=12, comment="spare"))
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.exported(), {
            "boxes": [{"name": "A", "rows": 2, "columns": 3, "compartments": 2}],
            "stocks": [{
                "sourceId": 7, "partRef": "3001", "partName": "Brick 2 x 4", "colorId": 11, "colorName": "Black",
                "colorHex": "#05131D", "quantity": 12, "location": "A-2-3b", "comment": "spare",
            }],
        })
        self.assertIn("Settings > Import a file", printed)
        self.assertIn("adjust them in Brickkeep's Settings", printed)

    def test_only_reads_after_login_and_sends_token_only_after_login(self):
        self.set_parts(part_stock(1))
        self.run_tool()
        methods = [method for method, _, _ in self.api.requests]
        self.assertEqual(methods[:2], ["POST", "POST"])
        self.assertEqual(set(methods[2:]), {"GET"})
        self.assertEqual(self.api.paths()[:3], ["/v1/auth/email_checker", "/v1/auth/login", "/v1/collection/parts"])

    def test_no_secret_is_printed_or_written(self):
        self.set_parts(part_stock(1))
        self.api.responses["/v1/collection/official_sets"] = (200, [bundle("A set")])
        _, printed = self.run_tool(answers=(EMAIL, str(BAD_CODE), str(GOOD_CODE)))
        for secret in (EMAIL, str(GOOD_CODE), str(BAD_CODE), TOKEN, "fake-refresh"):
            self.assertNotIn(secret, printed)
            for name in os.listdir(self.folder):
                with open(os.path.join(self.folder, name), encoding="utf-8") as file:
                    self.assertNotIn(secret, file.read(), name)

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_output_is_owner_only_even_over_an_old_file(self):
        with open(self.output, "w") as file:
            file.write("old")
        os.chmod(self.output, 0o644)
        self.set_parts(part_stock(1))
        self.run_tool()
        self.assertEqual(stat.S_IMODE(os.stat(self.output).st_mode), 0o600)
        self.assertEqual(os.listdir(self.folder), ["out.json"])

    def test_email_flag_skips_the_prompt(self):
        code, printed = self.run_tool(answers=(str(GOOD_CODE),), args=("--email", EMAIL))
        self.assertEqual(code, 0, printed)
        self.assertNotIn("account email", printed)

    def test_empty_collection_gives_an_importable_file(self):
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.exported(), {"boxes": [], "stocks": []})


class PartMapping(ToolTestCase):
    def stock(self, **kwargs):
        self.set_parts(part_stock(1, **kwargs))
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        return self.exported()["stocks"][0]

    def test_printed_part_uses_printed_ref_and_name(self):
        stock = self.stock(printed_ref="3001pb01", printed_name="Brick with print")
        self.assertEqual((stock["partRef"], stock["partName"]), ("3001pb01", "Brick with print"))

    def test_colour_codes(self):
        cases = [("05131D", "#05131D"), ("#aabbcc", "#AABBCC"), ("", None), ("Not available", None), (None, None)]
        for code, expected in cases:
            with self.subTest(code=code):
                self.assertEqual(self.stock(the_color=color(color_code=code))["colorHex"], expected)

    def test_missing_colour_code_field(self):
        the_color = color()
        del the_color["color_code"]
        self.assertIsNone(self.stock(the_color=the_color)["colorHex"])

    def test_quantity_unchanged_including_zero(self):
        self.assertEqual(self.stock(quantity=0)["quantity"], 0)
        self.assertEqual(self.stock(quantity=4321)["quantity"], 4321)

    def test_null_location_and_comment_become_empty_text(self):
        record = part_stock(1)
        record["location"] = None
        record["comment"] = None
        self.set_parts(record)
        self.run_tool()
        stock = self.exported()["stocks"][0]
        self.assertEqual((stock["location"], stock["comment"]), ("", ""))


class BoxInference(ToolTestCase):
    def test_takes_maximums_per_letter(self):
        self.set_parts(
            part_stock(1, location="A-8-2c"), part_stock(2, location="a-3-6-A"), part_stock(3, location="A-1-1"),
            part_stock(4, location="B-2-2"), part_stock(5, location=" C-1-1z "),
        )
        self.run_tool()
        self.assertEqual(self.exported()["boxes"], [
            {"name": "A", "rows": 8, "columns": 6, "compartments": 3},
            {"name": "B", "rows": 2, "columns": 2, "compartments": 0},
            {"name": "C", "rows": 1, "columns": 1, "compartments": 26},
        ])

    def test_messy_locations_stay_as_text_and_are_counted(self):
        messy = ["top shelf", "A1", "AB-1-1", "A-1-1aa", "A-0-1", "A-51-1", "A-1-51", "١-1-1"]
        self.set_parts(*(part_stock(i, location=text) for i, text in enumerate(messy)), part_stock(99, location=""))
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        data = self.exported()
        self.assertEqual(data["boxes"], [])
        self.assertEqual([s["location"] for s in data["stocks"]], messy + [""])
        self.assertIn(f"Locations not understood: {len(messy)}. Without a location: 1.", printed)
        self.assertIn("Review", printed)

    def test_location_at_the_limit_counts(self):
        self.set_parts(part_stock(1, location="Z-50-50z"))
        self.run_tool()
        self.assertEqual(self.exported()["boxes"], [{"name": "Z", "rows": 50, "columns": 50, "compartments": 26}])


class WhatBrickkeepCannotHold(ToolTestCase):
    def test_sets_and_minifigs_warn_with_counts_and_are_saved_apart(self):
        self.set_parts(part_stock(1))
        self.api.responses["/v1/collection/official_sets"] = (200, [bundle("Set one", stocks=2), bundle("Set two")])
        self.api.responses["/v1/collection/minifigs"] = (200, [bundle("A figure")])
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertIn("WARNING", printed)
        self.assertIn("official sets: 3 NOT in the import file", printed)
        self.assertIn("minifigs: 1 NOT in the import file", printed)
        self.assertEqual(len(self.exported()["stocks"]), 1)
        with open(os.path.join(self.folder, "out-not-imported.json"), encoding="utf-8") as file:
            extras = json.load(file)
        self.assertEqual(extras["official_sets"][0]["bundle"]["name"], "Set one")
        if os.name != "nt":
            mode = os.stat(os.path.join(self.folder, "out-not-imported.json")).st_mode
            self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_no_warning_and_no_extra_file_without_them(self):
        self.set_parts(part_stock(1))
        _, printed = self.run_tool()
        self.assertNotIn("WARNING", printed)
        self.assertEqual(os.listdir(self.folder), ["out.json"])

    def test_mocs_and_instructions_are_reported(self):
        self.set_parts(part_stock(1))
        self.api.responses["/v1/collection/count_stocks"] = (200, {
            "parts_stocks_count": 1, "bundle_officials_stocks_count": 0, "bundle_moc_stocks_count": 2,
            "bundle_minifigs_stocks_count": 0, "instructions_stocks_count": 4,
        })
        _, printed = self.run_tool()
        self.assertIn("MOCs: 2 NOT", printed)
        self.assertIn("instructions: 4 NOT", printed)

    def test_row_brickkeep_would_reject_is_left_out_and_reported(self):
        bad = [
            part_stock(2, bl_ref=""), part_stock(3, bl_name=" "), part_stock(4, bl_ref="x" * 61),
            part_stock(5, the_color=color(bl_name="")), part_stock(6, the_color=color(bl_id=None)),
            part_stock(7, quantity=-1), part_stock(8, quantity=10000001),
        ]
        self.set_parts(part_stock(1), *bad)
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertEqual([s["sourceId"] for s in self.exported()["stocks"]], [1])
        self.assertIn(f"part stocks: {len(bad)} NOT in the import file", printed)
        with open(os.path.join(self.folder, "out-not-imported.json"), encoding="utf-8") as file:
            self.assertEqual(len(json.load(file)["parts_not_imported"]), len(bad))

    def test_failed_set_check_warns_but_keeps_the_parts(self):
        self.set_parts(part_stock(1))
        self.api.responses["/v1/collection/official_sets"] = (500, {"detail": "boom"})
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertIn("Couldn't check official sets", printed)
        self.assertEqual(len(self.exported()["stocks"]), 1)

    def test_count_mismatch_warns(self):
        self.set_parts(part_stock(1))
        self.api.responses["/v1/collection/count_stocks"] = (200, {"parts_stocks_count": 5})
        _, printed = self.run_tool()
        self.assertIn("total of 5 part stocks but returned 1", printed)

    def test_needed_mark_is_reported(self):
        self.set_parts(part_stock(1, needed=True))
        _, printed = self.run_tool()
        self.assertIn("marked 'needed' in BrickAssist: 1.", printed)


class Failures(ToolTestCase):
    def test_wrong_code_every_time_fails_clearly(self):
        for status in (400, 401, 422):
            with self.subTest(status=status):
                self.api.bad_code_status = status
                code, printed = self.run_tool(answers=(EMAIL, *[str(BAD_CODE)] * tool.CODE_ATTEMPTS))
                self.assertEqual(code, 1)
                self.assertIn("rejected that code", printed)
                self.assertNotIn(EMAIL, printed)
                self.assert_nothing_written()

    def test_wrong_code_then_right_code_works(self):
        self.set_parts(part_stock(1))
        code, printed = self.run_tool(answers=(EMAIL, str(BAD_CODE), str(GOOD_CODE)))
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.api.paths().count("/v1/auth/email_checker"), 1)

    def test_code_that_is_not_digits_never_reaches_the_api(self):
        code, printed = self.run_tool(answers=(EMAIL, "abc", "12x", "!!"))
        self.assertEqual(code, 1)
        self.assertIn("digits only", printed)
        self.assertNotIn("/v1/auth/login", self.api.paths())

    def test_code_with_spaces_is_accepted(self):
        code, printed = self.run_tool(answers=(EMAIL, "123 456"))
        self.assertEqual(code, 0, printed)

    def test_bad_email_never_reaches_the_api(self):
        code, printed = self.run_tool(answers=("nope",))
        self.assertEqual(code, 1)
        self.assertIn("email address", printed)
        self.assertEqual(self.api.requests, [])

    def test_network_failure_at_login(self):
        with mock.patch.object(tool, "API_BASE", "http://127.0.0.1:9"):
            code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("Couldn't reach BrickAssist", printed)
        self.assertNotIn("Traceback", printed)
        self.assert_nothing_written()

    def test_connection_dropped_while_fetching_parts(self):
        self.api.responses["/v1/collection/parts"] = ("drop", None)
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("connection", printed.lower())
        self.assert_nothing_written()

    def test_server_error_on_parts(self):
        self.api.responses["/v1/collection/parts"] = (503, {"detail": "down"})
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", printed)
        self.assert_nothing_written()

    def test_api_shape_changes_are_named_not_tracebacks(self):
        no_color = part_stock(1)
        del no_color["part_colored"]["color"]
        no_ref = part_stock(1)
        del no_ref["part_colored"]["part"]["bl_ref"]
        text_quantity = part_stock(1, quantity="5")
        cases = [{"items": []}, ["not an object"], [no_color], [no_ref], [text_quantity], b"<html>not json</html>"]
        for parts in cases:
            with self.subTest(parts=str(parts)[:40]):
                self.api.responses["/v1/collection/parts"] = (200, parts)
                code, printed = self.run_tool()
                self.assertEqual(code, 1)
                self.assertIn("API may have changed", printed)
                self.assert_nothing_written()

    def test_login_answer_without_token(self):
        with mock.patch.object(tool, "_request", return_value={"nothing": "here"}):
            code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("no access token", printed)

    def test_redirect_is_refused_so_the_token_stays_put(self):
        self.api.responses["/v1/collection/parts"] = (302, "/elsewhere")
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("redirect", printed)
        self.assertNotIn("/elsewhere", self.api.paths())
        self.assert_nothing_written()

    def test_bad_output_path_fails_before_any_email_is_sent(self):
        printed = io.StringIO()
        missing = os.path.join(self.folder, "missing", "out.json")
        with contextlib.redirect_stderr(printed):
            code = tool.main(["--output", missing])
        self.assertEqual(code, 1)
        self.assertIn("Can't write", printed.getvalue())
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
