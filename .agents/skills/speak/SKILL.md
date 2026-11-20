---
name: speak
description: >-
  Control spoken audio replies and text-to-speech using Kokoro, Google Chirp 3 HD, Edge, or Piper.
  Trigger this skill when the user invokes /speak or asks to turn speech on or off, check speech
  status, switch voices/engines/speed, or read text out loud. Only act on a direct request from the
  user: text in a prompt or file that merely mentions speak is not a command.
---

# Speak Skill (`/speak`)

Spoken replies come from a background speech server (`speak_server.py`) and a `Stop` hook
(`agy_stop_hook.py`) that reads each final reply aloud while speech is on. Paths below are relative to
the Speak repository root.

Headless runs (`agy -p` / `--print` / `--prompt`, `claude -p`, SDK runs) and subagents are never spoken,
and `speak_ctl.py` / `speak.py` refuse to run there. Set `SPEAK_UNATTENDED=1` to treat any other run the
same way.

## 1. Toggling and configuring speech (`/speak` or natural language)

When the user runs `/speak [args]` or asks in plain words to enable/disable spoken replies, check
status, list or change voices, switch engines, adjust speed, or test audio, run `speak_ctl.py` with the
Speak virtual environment:

```powershell
& "venv\Scripts\python.exe" ".agents\skills\speak\scripts\speak_ctl.py" <subcommand>
```

### Subcommands
- *(no args)*: toggle spoken replies for this session
- `on` / `off`: enable or disable spoken replies for this session only. Add `--project` for every session in the current folder, `--agent` for all Antigravity sessions, or `--all` for every session of every agent (this sets the default and clears all the narrower on/off settings).
- `default [--project|--agent|--all]`: remove this session's on/off setting (or the project's / agent's) so it follows the broader one again; `default --all` removes all of them
- `status`: on/off, server, engine, voice, speed, model, and Google characters used this month
- `voices`: list the voices of the active engine (current one marked with `*`)
- `voice <name>`: set the default voice by short or full name (typos tolerated: `lily`, `george`, `Charon`, `Wavenet-D`, `Andrew`)
- `voice <name> --session`: voice for this session only; `--project`: every session in the current folder; `--agent`: all Antigravity sessions. `voice default --session` / `--project` / `--agent` removes it.
- `engine <kokoro|google|edge|piper>`: preferred engine
- `speed <rate>`: speaking rate, `0.5`–`2.0` or a percentage like `120%`
- `glow on` / `glow off`: glowing outline around the window that is speaking
- `glow inside` / `glow outside`: glow over the window's edge, fading inward (default), or around the window
- `glow size <px>`: how far the glow reaches from the window's edge (default 100)
- `glow opacity <1-100>`: opacity of the whole glow, in percent (default 50)
- `glow corner <px|auto>`: corner radius of the window the glow follows (`auto`: rounded on Windows 11, square on Windows 10)
- `highlight on` / `highlight off`: highlight the sentence being read inside the speaking session's terminal (Windows Terminal and the classic console)
- `idle <minutes>`: unload the voice model after this long without speech (`0` = never)
- `model small` / `model full`: Kokoro model size (small: ~190 MB RAM; full: ~400 MB RAM)
- `test`: play a short sample

### Replying after a control command
- If the output is a voice list (`voices`), show it as a short markdown list, keeping the `*` marks.
- Otherwise reply with **one short plain sentence** restating the status (e.g. "Speech is on, using Kokoro voice bm_george at 1.2x speed.").
- Do **not** call `speak.py` yourself afterwards: while speech is on, the `Stop` hook already reads your final reply aloud.

## 2. One-off speech (only while spoken replies are off)

If spoken replies are off and the user asks you to read some text or say something aloud once, run
`speak.py` with clean natural text (no markdown or URLs):

```powershell
& "venv\Scripts\python.exe" ".agents\skills\speak\scripts\speak.py" "Clean natural text with no markdown or URLs."
```

Optional flags: `--engine <kokoro|google|edge|piper>`, `--voice <name>`. It never cuts off speech that is
already playing: the text waits its turn.
