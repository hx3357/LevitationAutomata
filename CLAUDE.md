# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Hermes platform adapter plugin** that bridges the WeChat desktop client to the
Hermes agent gateway. It lives at `~/.hermes/plugins/wx-auto-platform/` and is loaded
by Hermes' plugin system via `register(ctx)` in `adapter.py` — **no Hermes core code is
modified** (a hard design constraint, see `docs/levi_agent.md`). The end goal is "Levi
Agent": a human-feeling agent that participates in WeChat DMs and group chats.

There is no build, no test suite, and no package install for the plugin itself — Hermes
discovers and imports it at startup. Hermes provides `gateway.platforms.base` and
`gateway.config` (imported in `adapter.py`); those modules are **not** in this repo and
are only resolvable when running inside a Hermes checkout.

## Layout

| Path | Role |
|------|------|
| `PLUGIN.yaml` | Plugin manifest: slug `wx-auto`, declares env vars surfaced in `hermes config`. |
| `adapter.py` | The adapter class `WxautoPlatformAdapter` + `register(ctx)` entry point. |
| `throttled_wechat.py` | `ThrottledWeChat(WeChat)` subclass — anti-detection delay layer. |
| `wxauto/` | **Vendored** third-party library (Cluic's wxauto 3.9 archive). Keep unmodified. |
| `docs/levi_agent.md` | Product/requirements doc for the agent being built (Chinese). |

## The two layers and how they must connect

**Layer 1 — `wxauto` (synchronous, blocking, Windows-only).**
`wxauto/wxauto/wxauto.py` drives the WeChat 3.9.x Windows desktop client through
UIAutomation/COM (`uiautomation.py`). It requires Windows 10/11, WeChat 3.9.x, and the
deps in `wxauto/pyproject.toml` (`pywin32`, `comtypes`, `pyperclip`, `pillow`, `psutil`,
etc.). The public entry is `from wxauto import WeChat`. Key methods on `WeChat`:
- RPC / outbound (hit WeChat servers): `SendMsg`, `SendFiles`, `AtAll`, `AddNewFriend`.
- Read / local UI (no server effect): `GetAllMessage`, `GetNextNewMessage`,
  `AddListenChat` + `GetListenMessage` (per-chat listen loop), `ChatWith`,
  `GetSessionList`. See `wxauto/helloworld.py` for the canonical listen-loop pattern.

**`throttled_wechat.py`** subclasses `WeChat` and wraps exactly the four RPC methods
with a random `time.sleep(min_delay, max_delay)` to mimic human pacing. The throttle is
intentionally **synchronous** and uses a `_delay_depth` reentrancy counter so nested
calls (e.g. `AtAll` → `SendMsg`) only pause once. To keep the vendored package on
`sys.path` without editing it, this file injects its own `wxauto/` dir into `sys.path`
at import time.

**Layer 2 — the Hermes adapter (asynchronous).** `BasePlatformAdapter.connect/send/
disconnect` are all `async def`. wxauto is blocking COM. **The correct bridge is in
`adapter.py`**: wrap throttle + blocking UI calls in `await asyncio.to_thread(...)`,
ideally pinned to a single `CoInitialize`-d worker thread, so the event loop never
freezes. The module docstring in `throttled_wechat.py` spells out this rule:
*wxauto layer stays sync-blocking; the adapter layer does the async bridging.* Do not
make the wxauto subclass `async` — it breaks the parent's synchronous call chains.

## Inbound/outbound flow (Hermes side)

`User <-> WeChat <-> WxautoPlatformAdapter <-> Gateway <-> AIAgent`. Inbound: build a
`MessageEvent` (via `self.build_source(...)`) and call `self.handle_message(event)`.
Outbound arrives through `send(chat_id, content, ...)` returning a `SendResult`.
`register(ctx)` wires config parsing, user auth, cron delivery, `send_message` routing,
chunking, and the `hermes config` UI — all for free.

## ⚠️ Current state: adapter is a half-converted scaffold

`adapter.py` still carries scaffold placeholders and is **not yet functional**. Before it
works you must:

1. **Replace the `WeChat`/`ThrottledWeChat` integration** — `connect`, `_fetch_updates`,
   `_build_event`, and `send` are still TODO stubs (`send` returns
   `"REPLACE_WITH_REAL_ID"`). Wire them to `ThrottledWeChat` via `asyncio.to_thread`.
2. **Fix the env-var name mismatch.** The class body reads `WX_AUTO_TOKEN` /
   `WX_AUTO_CHANNEL`, but `register(...)`, `check_requirements`, `validate_config`, and
   `_env_enablement` still reference the scaffold names `MY_PLATFORM_TOKEN` /
   `MY_PLATFORM_CHANNEL` / `MY_PLATFORM_HOME_CHANNEL` / `MY_PLATFORM_ALLOWED_USERS` /
   `MY_PLATFORM_ALLOW_ALL_USERS`. `PLUGIN.yaml` also still declares `MY_PLATFORM_*`.
   These three files must agree on one prefix (`WX_AUTO_*`) or the platform will never
   auto-enable. Note WeChat needs no token/channel the way the scaffold assumes — adapt
   the required-env model to what wxauto actually needs (a running WeChat client).

When editing `adapter.py`, keep `PLATFORM_NAME`, `register(name=...)`, and `PLUGIN.yaml`
`name:` all equal to `wx-auto`.

## Running the raw library (no Hermes)

To smoke-test wxauto against a live WeChat client on Windows:

```powershell
pip install tenacity pywin32 pyperclip pillow psutil colorama comtypes
python wxauto/helloworld.py   # listens to CONTACTS, appends messages to msgs.json
```

## Conventions

- Code comments and the requirements doc are in Chinese; match that when editing existing
  files. New adapter wiring should mirror the surrounding bilingual style.
- Treat `wxauto/` as a read-only vendored dependency. Extend behavior by subclassing
  (as `ThrottledWeChat` does), not by patching files under `wxauto/`.
