# OmniLink Desktop — Feature Ideas (50)

A backlog of features that would make OmniLink stand out or are necessary for v1.

## Chat & AI
2. **Voice input/output** — push-to-talk transcription and TTS replies.
3. **Image & file attachments** — drag images/PDFs/code into chat for vision models.
4. **Chat branching** — fork a conversation at any message and explore alternatives.
5. **Message editing & regeneration** — edit a past user message and re-run from there.
6. **Pinned messages** — pin key answers to the top of a chat.
7. **Conversation templates** — start chats from saved templates with system prompts.
8. **Slash commands** — `/search`, `/code`, `/mcp` inline commands in the composer.
9. **Tool-use transparency** — collapsible "thinking" blocks showing model reasoning.
10. **Offline model fallback** — auto-switch to a local Ollama model when offline, switch in settings to toggle on or off auto switch.

## Coding agent
11. **Plan → act → verify loop** — agent proposes a plan, edits files, runs tests, reports.
12. **Diff preview with accept/reject** — per-file diff view before applying edits.
13. **Auto-continue** — agent keeps going until the task is marked done, advanced auto loop detector that can be toggled in settings, on by default.
14. **Risk-tiered permissions** — read (auto), write (confirm), delete/exec (confirm + allowlist). section in settings to auto give permissions to all of them.
15. **Terminal integration** — run shell commands in a sandboxed terminal pane.
16. **Test runner integration** — detect test framework and run tests after edits.
17. **Git integration** — stage, commit, branch, and PR creation from the UI.
18. **Code review mode** — agent reviews a diff and posts inline comments.
19. **Refactor suggestions** — "extract function", "rename symbol" with preview.
20. **Multi-file edits** — atomic apply across several files with rollback.
NEW IDEA. users can toggle an auto completion ai in settings, and select whatever model they want to use, ollama FIM models recommended. 

## MCP & tools
21. **MCP watchdog** — auto-restart crashed stdio servers with backoff.
22. **Tool discovery UI** — browse and invoke MCP tools manually for debugging.
23. **MCP resource browser** — read MCP resources (files, DBs) inline.
24. **Prompt templates from MCP** — expose MCP prompts in the prompt library.
25. **Custom tool definitions** — define local tools (shell, HTTP) without an MCP server.
26. **Tool call audit log** — history of every tool invocation with inputs/outputs.
27. **MCP server marketplace** — one-click install of popular MCP servers.
28. **Per-server permissions** — allow/deny specific tools per server.
Native ollama intergration, section on the side if ollama is installed for finding ollama models, one button install, silimar to the one in the website.

## Sync & collaboration
29. **Cloud sync** — chats, projects, MCP configs, prompts across devices, opt in inside settings.
30. **Conflict resolution UI** — side-by-side merge for divergent files, hard to implement, dont implement if not possible or not practical.
31. **Shared workspaces** — invite others to a workspace with role-based access.
32. **Real-time co-editing** — collaborative code editing with cursors.
33. **Project versioning** — named snapshots with restore and diff.
34. **Export/import workspace** — full backup as a single archive.
35. **Offline-first sync** — queue changes locally, sync when online.

## UX & polish
36. **Command palette search** — fuzzy search across files, chats, prompts, settings.
37. **Keyboard-first navigation** — every action reachable via shortcut.
38. **Themes & accents** — already shipped; add custom CSS and font size.
39. **Split panes** — resizable split views for chat + code side by side.
40. **Floating windows** — detach a chat or file into its own window.
41. **Focus mode** — hide rail/panes for distraction-free editing.
42. **Minimap** — code minimap in the editor.
43. **Breadcrumb navigation** — file path breadcrumbs in coding view.

## Privacy & security
44. **Local-only mode** — guarantee no network calls (air-gapped).
45. **Encrypted workspace** — encrypt the whole SQLite DB at rest.
46. **Secret rotation** — rotate API keys with one click.
47. **Audit log** — who did what, when (for shared workspaces).

## Analytics & growth
48. **Usage dashboard** — tokens, cost, latency per model (beyond event counts).
50. **Crash reports** — opt-in Sentry-style crash reporting.