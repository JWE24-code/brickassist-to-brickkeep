# brickassist-to-brickkeep

Export your own LEGO® parts inventory from [BrickAssist](https://brickassist.com) into a file you can import straight into Brickkeep, an inventory app for storing LEGO® bricks in drawers.

It logs in with a code BrickAssist emails you, downloads your parts, and writes `brickkeep-import.json`.

> **Unofficial.** Not affiliated with BrickAssist or the LEGO Group. For exporting your own data only. It uses BrickAssist's API, which may change and break this tool at any time.
>
> **Status:** tested against a local fake server built from BrickAssist's public API schema, not yet against the real service with a real account. If something fails, please [open an issue](https://github.com/JWE24-code/brickassist-to-brickkeep/issues).

## Usage

Linux and macOS, with Python 3.9 or newer:

```sh
curl -O https://raw.githubusercontent.com/JWE24-code/brickassist-to-brickkeep/main/brickassist_to_brickkeep.py && python3 brickassist_to_brickkeep.py
```

Windows, with nothing to install. In PowerShell:

```powershell
curl.exe -O https://raw.githubusercontent.com/JWE24-code/brickassist-to-brickkeep/main/brickassist_to_brickkeep.ps1; powershell -ExecutionPolicy Bypass -File .\brickassist_to_brickkeep.ps1
```

`-ExecutionPolicy Bypass` applies to this one run, because Windows blocks scripts by default. The PowerShell version does the same as the Python version and writes the same file. It also runs with PowerShell 7 on Linux and macOS. It is slower than Python: a very large collection of 20,000 stocks can take a few minutes.

It asks for your BrickAssist email, then for the code they email you.

| Python | PowerShell | Meaning |
| --- | --- | --- |
| `--email ADDRESS` | `-Email ADDRESS` | Skip the email question. The address may end up in your shell history. |
| `--output PATH` | `-Output PATH` | Where to write the file. Default: `brickkeep-import.json` in the current folder. |

## Importing into Brickkeep

In Brickkeep, open **Settings > Import a file** and choose `brickkeep-import.json`.

You can export and import again later. Each row carries its BrickAssist stock id, so stocks already imported are skipped, and your edits to them in Brickkeep stay as they are. New stocks are added. Changes made in BrickAssist to stocks you already imported are not carried over.

## What ends up in the file

**Parts.** BrickLink part numbers, names and colours, with quantities unchanged. A printed part uses its printed BrickLink number.

**Boxes.** Brickkeep reads a location like `B-8-7a` as box B, row 8, column 7, compartment a. The tool sizes each box from the locations you actually use: the highest row, the highest column, and the highest compartment letter. An empty last row or column can't be seen, so check the sizes under Settings in Brickkeep and adjust them there.

Importing sets the size of every box named in the file. If you have already set up box A in Brickkeep, the import replaces its size with the inferred one, and that also happens when you import again. Check your boxes afterwards.

**Other locations.** Anything that doesn't fit that format, like `top shelf`, stays as text, and Brickkeep lists it under Review for you to fix. So does a stock with no location. The tool prints how many of each it found.

**What is left out.** Brickkeep's format holds loose parts only. If your account has any of the following, the tool warns you with counts:

- Official sets and minifigs are saved as raw JSON in `brickkeep-import-not-imported.json`, next to the main file.
- MOCs and instructions are only counted, not exported.
- Part stocks Brickkeep would reject, such as one with no BrickLink number, are saved in that same file. Brickkeep rejects a whole batch over one such row, so they can't go in the main file.

## Privacy

- The tool talks only to `api.brickassist.com` and sends nothing anywhere else. It refuses redirects to other addresses.
- After login it only reads, and never changes anything in your BrickAssist account.
- It never asks for a password. Your email, the code and the session token are never saved or printed, and the token is forgotten when the tool exits.
- **The output files contain your inventory, so don't share them.** They are set to owner-only permissions on Linux and macOS. On Windows they get the permissions of the folder they are in.

## Development

Tests use only the standard library and a local fake API with invented data. They never contact BrickAssist.

The same tests run against the PowerShell version, once for each of `powershell` and `pwsh` found on the machine, and compare both versions' files byte for byte.

```sh
python3 -m unittest
```

## License and trademarks

[MIT](LICENSE).

LEGO® is a trademark of the LEGO Group of companies, which does not sponsor, authorize or endorse this project.
