# Portable, sandboXed Components (PXC)

PXC ("pixie") is a new standard for online learning activities. It aims at improving other standards such as [SCORM](https://en.wikipedia.org/wiki/Sharable_Content_Object_Reference_Model), [LTI](https://en.wikipedia.org/wiki/Learning_Tools_Interoperability) or [H5P](https://h5p.org).

As a high-level overview: the PXC standard supports running arbitrary code both on the client (for the learner UI) _and_ the server. Server code is sandboxed in WebAssembly. Activities are portable, which means that they can be transferred from one LMS to another. Activities are also secure, as unsafe PXC capabilities (such as network access) are granted by platform administrators on a case-by-case basis.

Offline mode is supported, with two possible options:

1. Sandboxed code is shipped to the offline device (typically a mobile phone) and runs there. If the client decompiles the wasm binaries, they have access to the grading logic. This is acceptable when the client is trusted and the sandboxed code does not need network access.
2. Communication between the frontend and the backend is performed in an event-driven architecture  (see [event sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)). When offline, events are delayed until the client comes back online. Conflicts might happen and must be resolved, for instance when users attempt to connect from multiple devices.

The concept of PXC was initially described by [Braden MacDonald](https://github.com/bradenmacdonald
) in the "XBlock Next Proposals" from [2020](https://docs.google.com/document/d/1btnE7pIZccvlrDrJ8gHjEPfOrkMCZgSkgw8D9jXaLx4/edit?tab=t.0), [2024](https://docs.google.com/document/d/1xelb7iGvWRtT71F2c_dBkQEB47Cj0etT5VGgbVdAPKY/edit?tab=t.0), [2026](https://docs.google.com/document/d/1p-xvUKgG5HaUE-bzew98KPfrGCEZ6wsb6cUJG_gET28/edit?tab=t.0).

## Sub-projects

| Directory | Description |
|-----------|-------------|
| [src/pxc/lib/](./src/pxc/lib/) | **Core library.** Platform-agnostic runtime for loading activities, validating manifests, executing WebAssembly sandboxes, and managing field storage. Also contains the full [Activity API](./src/pxc/lib/README.md#activity-api-reference) and [Platform API](./src/pxc/lib/README.md#platform-api-reference) reference documentation. Refer to this project for more information about the standard. |
| [src/pxc/lib/sandbox/](./src/pxc/lib/sandbox/) | **Sandbox definition.** WIT interface and JSON Schema for the WASM Component Model sandbox. |
| [src/pxc/lib/static/](./src/pxc/lib/static/) | **Shared static files.** The `PXC` base class ([pxc.js](./src/pxc/lib/static/js/pxc.js)) that powers the `<pxc-activity>` web component. |
| [src/pxc/demo/](./src/pxc/demo/) | **Demo server.** Minimal FastAPI app that serves sample activities with a toolbar for switching users, permissions, and embedding modes. Useful for development and testing. |
| [src/pxc/notebook/](./src/pxc/notebook/) | **Notebook application.** Full courseware management app (FastAPI + Next.js) for organizing courses, pages, and activities with drag-and-drop, real-time execution, and SQLite persistence. |
| [src/pxc/lti/](./src/pxc/lti/) | **LTI 1.3 tool provider.** FastAPI app that exposes PXC activities as LTI 1.3 tools for embedding in Open edX, Canvas, or any LMS. Includes platform registration admin and deep linking support. |
| [samples/](./samples/) | **Sample activities.** Reference PXC activities (MCQ, quiz, video, chat, etc.) that demonstrate the standard. |

Each sub-project under `src/pxc/` has its own `pyproject.toml` and can be installed independently (`pip install -e src/pxc/lib`, etc.). The apps (`pxc-demo`, `pxc-notebook`, `pxc-lti`) all depend on `pxc-lib`. The root `pyproject.toml` is a dev-only umbrella that holds the shared linting/typing/formatting configuration.

## Installation

Make sure to install the following requirements:

- Python 3.11+
- [jco](https://bytecodealliance.github.io/jco/): for building JS plugins to WebAssembly components. See the official documentation for installation instructions.

Then install the project with:

    npm install
    make install-dev

## Quick Start

Build all sample activities with sandboxes:

    make samples

Launch the demo server:

    make demo-server

Then open http://127.0.0.1:9752 in a browser.

Launch the LTI server:

    make lti-server

Then configure your LMS to connect to http://127.0.0.1:9754.

## Development

Install all sub-projects in editable mode plus dev dependencies:

    make install-dev

Run tests:

    make test

## License

PXC is licensed under the Apache License 2.0 — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
