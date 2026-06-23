# Repository Guidelines

## Project Structure & Module Organization

This repository is a Windows-only Hermes platform adapter for WeChat Desktop. `adapter.py` is the plugin entry point and connects Hermes' asynchronous gateway to the blocking WeChat COM/UIAutomation layer. Core project modules live in `src/`: configuration, filtering, commands, directives, background processes, and the single-threaded `WeChatWorker`.

`src/wxauto_plugin/` is vendored third-party code. Treat it as read-only; extend it through project modules such as `src/throttled_wechat.py`. Operational documentation is under `docs/`, helper utilities are under `scripts/`, and plugin metadata is in `PLUGIN.yaml`. Use `config.example.yaml` as the configuration template; do not commit local secrets from `config.yaml`.

## Build, Test, and Development Commands

The plugin has no build step and is imported by Hermes at startup.

```powershell
python -m pip install -r requirements.txt
python -m compileall adapter.py src scripts
pyright
```

The first command installs Windows automation and project dependencies. `compileall` provides a quick syntax/import-shape check without requiring Hermes. `pyright` uses `pyrightconfig.json`, which adds `src/` to the import path. Full runtime verification requires Windows 10/11, WeChat 3.9.x, and a Hermes installation; restart Hermes after changing the adapter.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, and `from __future__ import annotations` in new Python modules. Follow standard Python naming: `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_CASE` for constants. Keep async gateway methods non-blocking; route all wxauto/COM calls through `WeChatWorker` rather than calling them on the event loop. Preserve the existing bilingual Chinese/English documentation style where practical.

Keep `PLATFORM_NAME`, `register(name=...)`, and `PLUGIN.yaml`'s `name` aligned as `wx-auto`.

## Testing Guidelines

No automated test suite or coverage threshold is currently present. For each change, run `compileall` and `pyright`, then smoke-test affected message flows against a disposable WeChat account. Verify connection, inbound filtering, command authorization, outbound text/files, and clean shutdown as applicable. Add future tests under `tests/` using names such as `test_filters.py`.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout, so no repository-specific commit convention can be confirmed. Use short, imperative subjects such as `Fix duplicate inbound delivery`. Pull requests should describe behavior changes, configuration impacts, manual test steps, and Windows/WeChat versions used. Include sanitized logs or screenshots for UIAutomation failures and link relevant issues.
