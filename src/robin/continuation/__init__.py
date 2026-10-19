"""Continuing a session in another runtime: Claude Code, Codex and Pi.

A source transcript is read into one canonical message list, written in the target runtime's own session
format into its store, and resumed there natively. Tool calls, tool results and reasoning cross as tagged
text, the shape Codex's own Claude importer uses, so a target never replays calls to tools it doesn't have.
Formats and evidence: docs/cross-runtime-resume.md.

The approach and the format knowledge follow bodysnatcher (https://github.com/alfredosdpiii/bodysnatcher):

    MIT License. Copyright (c) 2026 alfredosdpiii

    Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
    associated documentation files (the "Software"), to deal in the Software without restriction,
    including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
    subject to the following conditions: The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
    LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
    NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
    WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

RUNTIMES = ("claude", "codex", "pi")
CONVERTER_VERSION = "1"  # part of every preview hash: a converter change invalidates open previews
