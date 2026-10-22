# ChemAssistent

A small desktop app for Ubuntu (and Windows, see
[Install on Windows](#install-on-windows)) that puts a **Chemotion** icon in
the top bar (the tray on Windows) and
lets you talk to your [Chemotion ELN](https://chemotion.net) through an AI
agent. The agent is part of the app (`agent.py`) and talks to any
OpenAI-compatible API, by default the
[KIT KI-Toolbox](https://ki-toolbox.scc.kit.edu). It uses the Python package
[chemotion_api](https://github.com/Chemotion/chemotionapi) (branch
`version-3`) and knows its API from the package documentation and skills.

**Nothing runs against Chemotion without your confirmation**: every piece of
code the agent wants to run is shown to you first, you can edit it, and it runs
only after you click **Run**.

## How it works

1. Press **Ctrl+Space twice** quickly, or click the Chemotion icon in the
   top bar and choose **Ask Chemotion…** (a middle click on the icon opens it
   directly). A prompt opens in the middle of the screen.
2. Type what you want, e.g. “Which samples are in my collection Main Test?”,
   and press Enter.
3. The window grows to the full screen:
   - **left**: the chat with the agent. Ask follow-up questions (Enter sends,
     Shift+Enter starts a new line); **Stop** or Esc interrupts the agent.
     Short notes show what it read in the documentation. The paperclip button
     (or dropping files on the chat) attaches files or folders to your next
     message: the agent may read them and use them in its code, e.g. to
     upload a measurement file to a sample;
   - **right**: the results. Each time the agent wants to call the Chemotion
     API, a card appears with its purpose and the Python code. **Run** executes
     it (Ctrl+Enter in the code also works), **Reject** refuses it; the optional
     reason is sent to the agent. You can edit the code before running it; the
     agent is told what actually ran. Only the newest code is shown.
4. The output appears in a tab below the code (the latest ten are kept):
   tables for lists and dicts, text otherwise, the error traceback when it
   fails. Chemotion elements (samples, reactions, …) are rows you can click to
   open the element in the browser. “Copy JSON” copies the result.

The header buttons start a new conversation, open the settings, minimize the
window (the top-bar icon brings it back) or end the conversation.

Conversations are saved after every answer in
`~/.local/state/chem-assistent/history/` (the newest 200). The prompt lists
the latest ones: click one to continue it (the chat comes back, the result
tabs of earlier runs do not), the bin icon deletes it. **Clear history** in
the settings deletes them all.

## Safety

- The agent has exactly four tools: `execute_python`, which waits for your
  confirmation in the result panel and is its only way to reach Chemotion,
  and `read_file`, `grep` and `list_files`, which only read the API reference
  and the chemotionapi checkout in its workspace plus the files and folders
  you attached. It cannot run shell
  commands, edit files or fetch web pages.
- Confirmed code runs in its own process with an already logged-in
  `instance`; your password is never shown to the AI model. It is stopped
  after `execution_timeout` seconds and can be cancelled while it runs.
- Your prompts, the code and its results are sent to the AI model's
  provider. Choose a local model for sensitive data.

## Install

One command, no download needed:

```bash
curl -fsSL https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.sh | bash
```

The script fetches the app from GitHub by itself. From a checkout,
`./install.sh` installs that checkout instead. `CHEM_ASSISTENT_REF=<branch>`
installs another branch.

Run it as your normal user (it calls `sudo` only when Ubuntu packages are
missing: GTK 3, the AppIndicator library, GtkSourceView, libmagic, git).
It copies the app to `~/.local/share/chem-assistent/` with its own virtual
environment, clones **chemotionapi** (branch `version-3`, with its skills)
into the app's workspace, installs it, generates the API reference for the
agent, asks once for your KI-Toolbox API key (saved as `ai_token` in the
config) and installs and starts the systemd user service, so the icon appears
in the top bar now and at every login.

Run it again to update the app and chemotionapi; the configuration is
kept.

Requirements:

- Ubuntu with GNOME and the “Ubuntu AppIndicators” extension (enabled by
  default on Ubuntu).
- Python 3.13 or 3.14 (`/usr/bin/python3`; chemotion_api needs it).
- An API key of the [KIT KI-Toolbox](https://ki-toolbox.scc.kit.edu), or
  the URL and token of another OpenAI-compatible API.

Then open **ChemAssistent Settings** from the application menu, enter the
Chemotion URL, user and password, check the AI settings (API URL, token,
model; “Load models” lists the models and tests the token) and save.
“Test connection” logs in to Chemotion without saving.

## Install on Windows

On Windows ChemAssistent is one self-contained folder: Python, GTK 3 and
chemotion_api are bundled with [PyInstaller](https://pyinstaller.org). The
installer only unpacks that folder and creates shortcuts: no admin rights,
no MSYS2, no Python, no git, nothing added to `PATH` or the registry. In
PowerShell:

```powershell
irm https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.ps1 | iex
```

The script

1. downloads `ChemAssistent-windows-x64.zip` from the GitHub release
   `windows-main` (see [Build the Windows version](#build-the-windows-version)),
2. unpacks it to `%LOCALAPPDATA%\Programs\chem-assistent`, replacing an
   older version (a running app is stopped first),
3. asks once for your KI-Toolbox API key (saved as `ai_token` in the
   config; `KI_TOOLBOX_API_KEY` from your user environment is taken over),
4. creates the Start-menu shortcuts “ChemAssistent” and “ChemAssistent
   Settings” plus a Startup shortcut, and starts the app.

Options: `-NoAutostart` to not start the app at login, `-Package <zip>` to
install a locally built zip instead of downloading one. With the one-liner:
`& ([scriptblock]::Create((irm https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.ps1))) -NoAutostart`.
From a checkout: `powershell -ExecutionPolicy Bypass -File install.ps1`.
Another branch: set `$env:CHEM_ASSISTENT_REF = "<branch>"` first (the script
then downloads the release `windows-<branch>`).

Re-run the installer to update the app and chemotionapi; the configuration
and the conversation history are kept.

Requirements:

- Windows 10 or 11, 64-bit.
- (*only on some systems, not vertified yet*):**Smart App Control off.** The build is not code-signed, and Smart App
  Control blocks unsigned programs and has no way to exclude them. Turn it
  off in Settings > Privacy & security > Windows Security > App & browser
  control > Smart App Control settings (Windows keeps this setting
  permanently; it can only be re-enabled by resetting Windows).
- An API key of the [KIT KI-Toolbox](https://ki-toolbox.scc.kit.edu), or
  the URL and token of another OpenAI-compatible API.

Then open **ChemAssistent Settings** from the Start menu and enter the
Chemotion URL, user and password as on Linux.

If the installation fails:

- `Could not download …`: there is no build for that branch yet; see
  [Build the Windows version](#build-the-windows-version).
- `An application control policy has blocked this file` (on German Windows:
  „Eine Anwendungssteuerungsrichtlinie hat diese Datei blockiert“): Smart
  App Control is still on, see above.
- `Could not delete the old version`: the app is still running. Quit it
  (right-click the tray icon) and run the script again.
- An antivirus program reports or quarantines `ChemAssistent.exe`:
  PyInstaller programs are occasionally flagged by mistake. Allow the folder
  `%LOCALAPPDATA%\Programs\chem-assistent`.

Upgrading from the earlier MSYS2-based version: just run the new installer.
It replaces the app folder (including the old virtual environment) and the
shortcuts and keeps the configuration. MSYS2 itself is left installed; if
nothing else uses it, remove it under Settings > Apps (“MSYS2”) or with
`winget uninstall MSYS2.MSYS2`. Git for Windows and a GitHub login are no
longer needed.

On Windows the files live in other places:

- the app: `%LOCALAPPDATA%\Programs\chem-assistent` (`build.json` there
  says which commit of the app and of chemotionapi it was built from,
  `License.md` lists the licenses of all bundled third-party software)
- configuration: `%APPDATA%\chem-assistent\config.json`
- log: `%LOCALAPPDATA%\chem-assistent\app.log`
- conversation history: `%LOCALAPPDATA%\chem-assistent\history\`

Differences from Linux:

- a left click on the tray icon opens the prompt, a right click the menu;
- chemotionapi is part of the build (the newest `version-3` when it was
  built) instead of being cloned at install time;
- confirmed code runs in the bundled Python: the standard library,
  chemotion_api and its dependencies (requests, PyYAML, jsonschema, rdflib,
  …); other packages such as numpy or pandas are not available;
- the MIME type of an uploaded file is guessed from its file name
  (`windows/magic.py` replaces python-magic, which needs libmagic).

Windows support is new and has not been tested much.

Remove: `powershell -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\chem-assistent\uninstall.ps1"`
(keeps the configuration and the history).

### Build the Windows version

The GitHub workflow `.github/workflows/windows.yml` builds the zip on
`windows-latest` and publishes it as the release `windows-<branch>` (slashes
become `-`): on every push that changes the app, every Monday (to pick up
chemotionapi changes) and on demand (Actions > Windows build > Run
workflow). The release is replaced by each build; `install.ps1` always
downloads the newest one.

To build it yourself on Windows you need Python 3.14 (e.g. from
[python.org](https://www.python.org/downloads/windows/)); it must be that
version because gvsbuild's PyGObject wheel is built for it:

```powershell
py -3.14 windows\build.py
powershell -ExecutionPolicy Bypass -File install.ps1 -Package dist\ChemAssistent-windows-x64.zip
```

`windows\build.py` downloads GTK 3, GtkSourceView and PyGObject from
[gvsbuild](https://github.com/wingtk/gvsbuild) (about 315 MB, kept for the
next build) and chemotionapi into `build\windows\`, installs everything into
a virtual environment there, generates the API reference, runs PyInstaller
with `windows\chem-assistent.spec`, checks the result with
`chem-assistent-runner.exe --self-test` and writes
`dist\ChemAssistent-windows-x64.zip`. Nothing is installed outside the
checkout.

The zip contains `LICENSE.txt` (ChemAssistent's AGPL-3.0) and `License.md`,
which `windows\licenses.py` generates from what is really in the build:
every bundled DLL with its component, version, license and source, every
Python package with the license files from its metadata, PyInstaller's
bootloader, the icon theme and the full license texts. It takes them from
gvsbuild's `share\doc` and `share\licenses`, the packages' `dist-info`,
Python's `LICENSE.txt` and `windows\licenses\` (FreeType, libxml2 and
OpenSSL, which gvsbuild ships without license text). If a DLL is not known
to `COMPONENTS` in `windows\licenses.py` or a license text is missing, the
build fails; add the component there.

`License.md` also contains the written offer for the source code of the
LGPL/GPL components. Its contact is `SOURCE_CONTACT` in
`windows\licenses.py`, for now a placeholder (the build warns about it):
fill it in before distributing builds.

## Configuration

`~/.config/chem-assistent/config.json` (mode `0600`):

```json
{
  "url": "https://chemotion.example.com",
  "user": "",
  "password": "",
  "verify_tls": true,
  "ai_url": "https://ki-toolbox.scc.kit.edu/api/v1",
  "ai_token": "",
  "ai_model": "alias.complex-local",
  "execution_timeout": 300,
  "prompt_width": 640,
  "hotkey": "<Control>space"
}
```

- `url`, `user`, `password`: your Chemotion server and login (abbreviation
  or e-mail). `CHEMOTION_PASSWORD` in the environment overrides the password.
- `verify_tls`: set to `false` only for test servers with self-signed
  certificates.
- `ai_url`: base URL of an OpenAI-compatible API (the app uses
  `GET <ai_url>/models` and `POST <ai_url>/chat/completions` with streaming
  and tool calls). Default: the KIT KI-Toolbox. Others: e.g.
  `https://api.openai.com/v1`, Ollama's `http://localhost:11434/v1`, vLLM or
  LiteLLM.
- `ai_token`: the API key, sent as `Authorization: Bearer`. If empty,
  `KI_TOOLBOX_API_KEY` from the environment is used.
- `ai_model`: the model id, e.g. `alias.complex-local` (the KI-Toolbox's
  local GLM), `alias.complex-gpt` or `alias.complex-claude`. The model must
  support tool calls.
- `execution_timeout`: maximum run time of one confirmed snippet, in seconds.
- `prompt_width`: width of the prompt window.
- `hotkey`: the global hotkey; pressed twice within 0.6 s it opens the
  prompt. GTK syntax (`<Control>space`, `<Super>c`, `<Control><Alt>k`), empty
  turns it off. On GNOME it is a custom keyboard shortcut (Settings → Keyboard
  → Custom Shortcuts, “ChemAssistent”), so a single press no longer reaches
  other programs, e.g. completion in editors; pick another key if you need
  Ctrl+Space there. On Windows a change applies after restarting the app.

Changes apply to the next conversation; no restart needed.

## Run from the source tree

```bash
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
./setup_workspace.sh "$PWD" "$PWD/.venv/bin/python"
.venv/bin/python app.py --show
```

`--show` opens the prompt right away. Starting the app while it already runs
opens the prompt in the running instance (the **ChemAssistent** menu entry does
this too).

## Service

```bash
systemctl --user status chem-assistent.service
journalctl --user -u chem-assistent.service -f
```

“Stop app” / “Start app” in the settings stops or starts the service.

## Remove

```bash
~/.local/share/chem-assistent/uninstall.sh
```

(or `./uninstall.sh` in a checkout). Keeps `~/.config/chem-assistent/`.

## License

ChemAssistent is free software under the
[GNU Affero General Public License v3.0](LICENSE), the license of
chemotion_api, which it uses, and of the Chemotion ELN. You may redistribute
and modify it under the terms of the AGPL-3.0, version 3 only; it comes
without any warranty.
