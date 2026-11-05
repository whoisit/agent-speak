# Speak

Spoken replies for coding agents on Windows. When Claude Code or Google Antigravity finishes a reply,
Speak reads it aloud in a natural voice while you keep working, with a tray icon to pause, skip, or stop.

- **Local and free by default:** [Kokoro-82M](https://github.com/thewh1teagle/kokoro-onnx) runs on the CPU
  (about 200 MB RAM with the small model). Google Chirp 3 HD, Edge Read Aloud and Piper are optional.
- **Several sessions at once:** replies queue up instead of talking over each other, each session,
  project or agent can have its own voice, and the window that is speaking gets a glowing outline, with
  the sentence being read highlighted in place.
- **Reads replies, not markup:** code blocks, tables, links, paths, emoji and dividers are stripped first.
- **Stays out of the way:** a background server keeps the voice loaded so speech starts in under a
  second, and unloads itself after 20 idle minutes.

## Install

Requires Windows 10/11 and Python 3.11+.

```powershell
git clone https://github.com/whoisit/agent-speak.git
cd agent-speak
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python .agents\skills\speak\scripts\download_models.py
```

`download_models.py` fetches the small Kokoro model and voices (~115 MB) into `kokoro_models/`.
Add `--full` for the full-size model (325 MB, ~400 MB RAM, then `/speak model full`) and `--piper` for
the Piper last-resort fallback.

Then hook it into your agent (both can share one install):

```powershell
venv\Scripts\python .agents\skills\speak\scripts\install_claude.py       # Claude Code: /speak + Stop and prompt hooks
venv\Scripts\python .agents\skills\speak\scripts\install_antigravity.py  # Antigravity: speak skill + Stop and prompt hooks
```

Restart the agent, then run `/speak on` and `/speak test`. Both installers can be re-run safely, for
example after moving the folder; they back up the settings file they change.

**Optional, Google voices:** copy `.env.example` to `.env`, add a Google Cloud Text-to-Speech API key, and
run `/speak engine google`. Replies are read in full; usage is kept under the monthly free tiers by
switching from Chirp 3 HD to WaveNet at 900K characters, and to Kokoro at 3.8M WaveNet characters.

## Commands

| Command | Effect |
|---|---|
| `/speak on` / `off` | Turn spoken replies on / off (also starts / stops the background server) |
| `/speak status` | Server, engine, voice, speed, model, and Google characters used this month |
| `/speak test` | Play a sample in the voice this session would use |
| `/speak voices` | List the voices of the active engine (current one marked) |
| `/speak voice lily` | Default voice, by short name; typos are tolerated (`lili` → `bf_lily`, `charron` → Charon) |
| `/speak voice george --session` | Voice for this session only (until it ends). `--project`: every session in this folder; `--agent`: all Claude Code (or all Antigravity) sessions. Most specific wins: session → project → agent → default |
| `/speak voice default --session` | Remove that override (same with `--project` / `--agent`) |
| `/speak engine kokoro\|google\|edge\|piper` | Preferred engine |
| `/speak speed 1.1` | Speaking rate for every engine (`110%` also works, range 0.5–2.0) |
| `/speak model small\|full` | Kokoro model size: small (int8, ~200 MB RAM) or full (~400 MB RAM, about 20% faster) |
| `/speak glow on\|off` | Glowing outline around the window that is speaking (also in the tray panel) |
| `/speak glow size 27` | How far the glow reaches outside the window, in pixels (1–100) |
| `/speak glow corner 8\|auto` | Corner radius of the window the glow hugs; `auto` is rounded on Windows 11, square on Windows 10. The glow's outer corners are always rounded |
| `/speak highlight on\|off` | Highlight the sentence being read, in place, in the session's terminal (Windows Terminal and the classic console; see below) |
| `/speak idle 20` | Unload the voice model after this many minutes without speech (`0` = keep it loaded) |

## Tray panel

Left-click the tray LED for play / pause, **Paragraph** (skip ahead), **Reply** (move on to the next
queued reply), stop and clear, speed, glow on/off, and **Read clipboard**, which reads the copied text
in a window that highlights each sentence as it is spoken. Right-click for the same controls as a menu.

## How it works

- `speak_server.py` is a hidden background process on `127.0.0.1:47811` that keeps Kokoro loaded and
  owns the tray icon. The Stop hooks (`claude_stop_hook.py`, `agy_stop_hook.py`) clean up each final reply
  and hand it over, starting the server first if it isn't running (after a reboot or an idle unload, the
  first reply takes about 2.5 s longer).
- `/speak` never reaches the model: prompt hooks (`claude_prompt_hook.py`, `agy_prompt_hook.py`) run it
  directly and show the result, so it costs no tokens. The `/speak` command and skill files stay for
  autocomplete and as a fallback.
- Everything plays in the order it arrives and nothing is cut off: replies from any session, window or
  tab, a newer reply from the session already speaking, **Read clipboard**, and one-off `speak.py`
  readings all wait their turn. **Paragraph**, **Reply** and stop are how to move on.
- The hook also finds the window its session is shown in (through the agent's console, since all
  Windows Terminal windows share one process) for the glow. With several tabs in one terminal window,
  the whole window glows.
- The sentence being read is highlighted where it is shown: the terminal's visible text is read through
  UI Automation (the interface screen readers use), the sentence is matched by its words (what is
  spoken has lost its markdown, so the match allows for missing and extra words), and soft blue boxes
  are drawn over it. Only text that is on screen can be highlighted, and only in terminals that expose
  their text this way: Windows Terminal and the classic console do; Antigravity's terminal doesn't yet.
- Text is synthesized in sentence chunks, a short first one so playback starts quickly and the rest while
  it plays, falling back engine by engine:
  - `kokoro`: **Kokoro** → **Piper**
  - `google`: **Chirp 3 HD** → **WaveNet** → Kokoro → Piper
  - `edge`: **Edge Read Aloud** (free, unofficial, sometimes flaky) → Kokoro → Piper
- Settings live in `.agents/skills/speak/state.json`, shared by every agent and session.

## Files

```text
.agents/skills/speak/
├── SKILL.md                 Antigravity skill (install_antigravity.py installs a copy globally)
└── scripts/
    ├── speak_ctl.py         /speak commands
    ├── speak_server.py      background server: socket, tray, model
    ├── player.py            queue, playback, pause / skip
    ├── speak.py             engines, settings, and a one-off command line: speak.py "text"
    ├── textclean.py         markdown → speakable text
    ├── tray.py, ui.py       tray LED, control panel, clipboard reader
    ├── glow.py, windows.py  window outline, finding a session's window
    ├── highlight.py, screentext.py  highlighting the sentence being read in the terminal
    ├── overlay.py           click-through windows the glow and highlight are drawn in
    ├── claude_stop_hook.py, agy_stop_hook.py
    ├── claude_prompt_hook.py, agy_prompt_hook.py  run /speak without a model turn
    ├── install_claude.py, install_antigravity.py
    └── download_models.py
```

## License

MIT, see [LICENSE](LICENSE).
