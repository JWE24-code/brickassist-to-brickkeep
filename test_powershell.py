"""Runs the Python tool's whole test suite against the PowerShell port, then compares
the two tools' files byte for byte.

Each PowerShell found on this machine gets its own copy of the tests: `powershell`
(Windows PowerShell 5.1) and `pwsh` (PowerShell 7). All data here is invented.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import brickassist_to_brickkeep as tool
import test_brickassist_to_brickkeep as base

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "brickassist_to_brickkeep.ps1")
SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]
REAL_API = "$ApiBase = 'https://api.brickassist.com'"


class PowerShellTool:
    """Replaces run_tool so the inherited tests drive the .ps1 instead of the .py."""

    shell = None

    def run_tool(self, answers=(base.EMAIL, str(base.GOOD_CODE)), args=()):
        with open(SCRIPT, encoding="utf-8") as file:
            source = file.read()
        # The API address is swapped in a copy, so the tool itself has no way to be pointed elsewhere.
        self.assertEqual(source.count(REAL_API), 1)
        flags = ["-Email" if arg == "--email" else arg for arg in args]
        with tempfile.TemporaryDirectory() as scratch:
            copy = os.path.join(scratch, "tool.ps1")
            with open(copy, "w", encoding="utf-8", newline="\n") as file:
                file.write(source.replace(REAL_API, f"$ApiBase = '{tool.API_BASE}'"))
            done = subprocess.run(
                [self.shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", copy, "-Output", self.output, *flags],
                input="".join(f"{answer}\n" for answer in answers).encode(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=scratch, timeout=300,
            )
        return done.returncode, done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    def test_login_answer_without_token(self):
        self.api.login_answer = {"nothing": "here"}
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("no access token", printed)

    def test_bad_output_path_fails_before_any_email_is_sent(self):
        self.output = os.path.join(self.folder, "missing", "out.json")
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("Can't write", printed)
        self.assertEqual(self.api.requests, [])

    def test_output_path_that_is_a_folder_fails_before_any_email_is_sent(self):
        self.output = self.folder
        code, printed = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("is a folder", printed)
        self.assertEqual(self.api.requests, [])


class SameFilesAsPython(PowerShellTool, base.ToolTestCase):
    """Awkward data, where a port would differ: both tools must write identical bytes."""

    def awkward_parts(self):
        return [
            base.part_stock(1, location="A-2-3b", comment='quote " slash \\ tab \t line \n ctrl \u0001 del \u007f'),
            base.part_stock(2, location="b-1-1", bl_name="Tuile é 日本語 🧱", comment="2026-01-01T00:00:00Z"),
            base.part_stock(3, location="2026-01-01T00:00:00Z", printed_ref="3001pb01", printed_name="<Printed> & 'x'"),
            base.part_stock(4, location=None, comment=None, the_color=base.color(color_code="#aabbcc"), needed=True),
            base.part_stock(5, location="top shelf", the_color=base.color(color_code=None)),
            base.part_stock(6, quantity=-1),
        ]

    def awkward_set(self):
        entry = base.bundle("Set / é 🧱")
        # Keys PowerShell would confuse with properties, or with each other by case.
        entry["count"] = 7
        entry["keys"] = ["a"]
        entry["Name"] = "upper"
        entry["name"] = "lower"
        entry["price"] = 12.5
        entry["whole_price"] = 12.0
        entry["negative"] = -3
        entry["flags"] = [True, False, None, [], {}, [[]], [1]]
        entry["updated"] = "2026-01-01T00:00:00Z"
        return entry

    def test_both_tools_write_the_same_bytes_and_report(self):
        self.set_parts(*self.awkward_parts())
        self.api.responses["/v1/collection/official_sets"] = (200, [self.awkward_set()])
        extras_path = os.path.join(self.folder, "out-not-imported.json")

        code, python_said = base.ToolTestCase.run_tool(self)
        self.assertEqual(code, 0, python_said)
        with open(self.output, "rb") as file:
            python_file = file.read()
        with open(extras_path, "rb") as file:
            python_extras = file.read()
        os.remove(self.output)
        os.remove(extras_path)

        code, powershell_said = self.run_tool()
        self.assertEqual(code, 0, powershell_said)
        with open(self.output, "rb") as file:
            self.assertEqual(file.read(), python_file)
        with open(extras_path, "rb") as file:
            self.assertEqual(file.read(), python_extras)
        self.assertEqual(powershell_said.split("Logged in.")[1], python_said.split("Logged in.")[1])

    def test_a_relative_output_path_follows_the_current_folder(self):
        self.set_parts(base.part_stock(1))
        wanted = self.output
        self.output = "out.json"
        code, printed = self.run_tool()
        self.assertEqual(code, 0, printed)
        self.assertNotIn(os.path.basename(self.folder), printed)
        self.assertFalse(os.path.exists(wanted))

    # What the base class would otherwise pull in here twice is left out.
    test_login_answer_without_token = None
    test_bad_output_path_fails_before_any_email_is_sent = None
    test_output_path_that_is_a_folder_fails_before_any_email_is_sent = None


class ScriptFile(unittest.TestCase):
    def test_script_is_plain_ascii_so_windows_powershell_reads_it_correctly(self):
        # Windows PowerShell reads a script with no byte-order mark in the local code page.
        with open(SCRIPT, "rb") as file:
            file.read().decode("ascii")

    def test_script_names_only_the_real_api(self):
        with open(SCRIPT, encoding="utf-8") as file:
            source = file.read()
        hosts = {word.split("/")[2] for word in source.replace("'", " ").split() if word.startswith("https://")}
        self.assertEqual(hosts, {"api.brickassist.com", "github.com"})

    @unittest.skipUnless(os.environ.get("CI"), "only CI must have PowerShell")
    def test_ci_really_runs_powershell(self):
        self.assertTrue(SHELLS)
        if os.name == "nt":
            self.assertEqual(SHELLS, ["powershell", "pwsh"])


for shell in SHELLS:
    for parent in (base.FullFlow, base.PartMapping, base.BoxInference, base.WhatBrickkeepCannotHold, base.Failures):
        name = f"{parent.__name__}On{shell.capitalize()}"
        globals()[name] = type(name, (PowerShellTool, parent), {"shell": shell})
    name = f"SameFilesAsPythonOn{shell.capitalize()}"
    globals()[name] = type(name, (SameFilesAsPython,), {"shell": shell})
del SameFilesAsPython


if __name__ == "__main__":
    unittest.main()
