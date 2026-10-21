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

1. Click the Chemotion icon in the top bar and choose **Ask Chemotion…**
   (a middle click on the icon opens it directly). A prompt opens in the
   middle of the screen.
2. Type what you want, e.g. “Which samples are in my collection Main Test?”,
   and press Enter.
3. The window grows to the full screen:
   - **left**: the chat with the agent. Ask follow-up questions (Enter sends,
     Shift+Enter starts a new line); **Stop** or Esc interrupts the agent.
     Short notes show what it read in the documentation;
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

## Safety

- The agent has exactly four tools: `execute_python`, which waits for your
  confirmation in the result panel and is its only way to reach Chemotion,
  and `read_file`, `grep` and `list_files`, which only read the API reference
  and the chemotionapi checkout in its workspace. It cannot run shell
  commands, edit files or fetch web pages.
- Confirmed code runs in its own process with an already logged-in
  `instance`; your password is never shown to the AI model. It is stopped
  after `execution_timeout` seconds and can be cancelled while it runs.
- Your prompts, the code and its results are sent to the AI model's
  provider. Choose a local model for sensitive data.

## Install

```bash
./install.sh
```

Run it as your normal user (it calls `sudo` only when Ubuntu packages are
missing: GTK 3, the AppIndicator library, GtkSourceView, libmagic, git).
It copies the app to `~/.local/share/chem-assistent/` with its own virtual
environment, clones **chemotionapi** (branch `version-3`, with its skills)
into the app's workspace, installs it, generates the API reference for the
agent, asks once for your KI-Toolbox API key (saved as `ai_token` in the
config) and installs and starts the systemd user service, so the icon appears
in the top bar now and at every login.

Re-run `install.sh` to update the app and chemotionapi; the configuration is
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

The app uses GTK 3, which on Windows comes from [MSYS2](https://www.msys2.org),
a free collection of open-source tools and libraries for Windows. Install
MSYS2 (default location `C:\msys64`), then run in PowerShell, in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Options: `-Msys2Root D:\msys64` if MSYS2 is elsewhere, `-NoAutostart` to not
start the app at login. The script installs GTK 3, PyGObject, GtkSourceView,
git and the compiled Python packages chemotion_api needs with `pacman`,
copies the app to `%LOCALAPPDATA%\Programs\chem-assistent` with its own
virtual environment, sets up chemotionapi as on Linux, asks for the
KI-Toolbox API key (saved in the config), creates Start-menu shortcuts
(“ChemAssistent”, “ChemAssistent Settings”) plus a Startup shortcut, and
starts the app.

On Windows the files live in other places:

- configuration: `%APPDATA%\chem-assistent\config.json`
- log: `%LOCALAPPDATA%\chem-assistent\app.log`

Differences from Linux: a left click on the tray icon opens the prompt, a
right click the menu.
Windows support is new and has not been tested much.

Remove: `powershell -ExecutionPolicy Bypass -File uninstall.ps1` (keeps the
configuration).

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
  "prompt_width": 640
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
./uninstall.sh
```

Keeps `~/.config/chem-assistent/`.

## License

ChemAssistent is free software under the
[GNU Affero General Public License v3.0](LICENSE), the license of
chemotion_api, which it uses, and of the Chemotion ELN. You may redistribute
and modify it under the terms of the AGPL-3.0, version 3 only; it comes
without any warranty.
