# Tooling conventions

This project is managed with [uv](https://docs.astral.sh/uv/). Always use `uv`
to run Python and project tools so the project's virtual environment and locked
dependencies are used.

## Shell

- The default shell is PowerShell (`pwsh`). Use PowerShell syntax, not cmd or bash.
  - Chain commands with `;`, not `&&`.
  - Environment variables: `$env:TEMP`, `$env:USERPROFILE`. Never `%TEMP%`.
  - Set a variable for the session: `$env:AWS_PROFILE = "aws001"`.

## Running Python

- Run Python programs with `uv run`, never with a bare `python`/`python3`.
  - Run a script: `uv run main.py`
  - Run a module: `uv run python -m <module>`

## Running tests

- Run the test suite with `uv run python -m pytest`, not a bare `pytest`.
  - Full suite: `uv run python -m pytest`
  - A single file: `uv run python -m pytest tests/test_routing.py`

## Running ruff

- Run ruff with `uv run ruff`, never with a bare `ruff`.
  - Lint: `uv run ruff check .`
  - Organize imports: `uv run ruff check --select I --fix .`
  - Format: `uv run ruff format .`

## Running ty

- Run ty with `uv run ty`, never with a bare `ty`.
  - Lint: `uv run ty check .`

## Running checkov

- Run checkov with `uv run checkov`, never with a bare `checkov`.
- Run it from `infra/` and skip the provider cache and build output:
  `uv run checkov -d . --framework terraform --compact --quiet --skip-path .terraform --skip-path build`
- Write the results to a file and read the file afterwards, because console
  output often does not reach the assistant:
  `--output cli --output-file-path $env:TEMP\checkov_rve` (creates
  `$env:TEMP\checkov_rve\results_cli.txt`).
- After every `tofu plan -out=tfplan`, validate the plan with checkov. Run from
  `infra/`, and write the plan JSON to the temp folder, not into the repo:
  `tofu show -json tfplan > $env:TEMP\tfplan.json`, then
  `uv run checkov -f $env:TEMP\tfplan.json --framework terraform_plan --compact --quiet --output cli --output-file-path $env:TEMP\checkov_rve_plan`
  (creates `$env:TEMP\checkov_rve_plan\results_cli.txt`). Read that file.
- If checkov reports any failed check, do not fix or suppress it on your own.
  Show the findings (check ID, resource, short explanation) and ask the user
  how to handle each one: fix it, suppress it with a reason, or accept it.
  Wait for the answer before continuing, and never run `tofu apply` before then.
- Suppress accepted findings inline in the resource block, with a reason:
  `#checkov:skip=<CHECK_ID>:<reason>`.

## Slow commands and missing output

- `tofu` and `uv run checkov` can take a minute or more before any output
  appears. Wait long enough before reading the output, or treat empty output
  as "not finished yet", not as success or failure.
- If the output still does not appear, ask the user to run the command and
  share the result, instead of retrying the same command repeatedly.

## Creating Diagrams

- A MCP Server drawio-app exists, to support the diagram creation.

## Writing Markdown

- Write Markdown tables in the compact (non-padded) GitHub Flavored Markdown
  style: each cell is only as wide as its text, and the delimiter row is a
  minimal `| --- | --- |`. Do **not** pad cells with spaces to align columns,
  because the extra padding trips up the Markdown linter. Both styles render
  identically, so always prefer the compact one.
- Example:

  ```markdown
  | Argument | Description |
  | --- | --- |
  | `--file` | Input JSON file. Must exist. |
  | `--sort` | Optional flag. Off by default. |
  ```

- Follow common `markdownlint` rules so generated Markdown stays lint-clean.
  Before finishing any work that creates or edits a Markdown file, review the
  file and fix lint issues automatically rather than leaving them for the user.
  The most common ones to check:
  - **MD032 (blanks-around-lists):** surround every list with a blank line
    before the first item and after the last item.
  - **MD031 (blanks-around-fences):** surround fenced code blocks with blank
    lines.
  - **MD022 (blanks-around-headings):** put a blank line above and below every
    heading.
  - **MD040 (fenced-code-language):** give every fenced code block a language
    (for example ```` ```text ````).
  - **MD047 (single-trailing-newline):** end the file with exactly one newline.
  - **MD009 (no-trailing-spaces):** do not leave trailing whitespace on a line.
- `npx` is installed, so run the linter instead of only inspecting by hand:
  `npx markdownlint-cli2 <file>`. Run it on every Markdown file you create or
  edit and fix all findings. Only fall back to manual inspection if the command
  fails to run, and state that in the verification note.
