---
name: tldr
description: Maintain and read .tldr summaries, a tldr/ tree that mirrors a repository with short, hash-checked summaries of every file (code, docs, images, binaries), every folder and every function, plus a call graph, so an agent understands a large codebase without loading its sources. Use this skill whenever a repository has a tldr/ folder or a .tldr.toml file (read the summaries before opening sources, and refresh them after changing code), whenever the user asks to summarize, index or map a codebase for agents or to create, update or check tldr files, and whenever they ask who calls a function, what a change would break, or how execution gets from one function to another in a repository that has summaries. Not for writing a one-off TL;DR of a text, a pull request or a conversation.
---

# tldr

A repository using this skill has a `tldr/` folder that mirrors its tree: one `<file>.tldr` per file, one `_dir.tldr` per folder, one `_index.tldr` at the root. Reading those summaries gives the context of a large codebase for a fraction of the tokens of its sources.

The work is split in two, and the split matters:

- **The tool**, `scripts/tldr.py` in this skill's folder, is deterministic and knows no programming language. It hashes files, keeps `tldr/` in shape, says what is missing or stale, writes the header of every `.tldr` and answers call-graph queries by reading the `.tldr` text. It never calls an LLM.
- **The LLM** reads files and writes the summaries: subagents on a fast model write them, and you coordinate (see "Creating or refreshing the summaries"). No LLM ever writes a header or a hash: it cannot compute a SHA-256, and a wrong hash would make a stale summary look fresh.

Run the tool from the repository root with Python 3.11 or newer (standard library only, nothing to install):

    python3 <this skill's folder>/scripts/tldr.py <command>

Below, `tldr` stands for that command.

## Reading a repository that has tldr/

1. Start with `tldr/_index.tldr`, then the `_dir.tldr` of the module you care about, then the `.tldr` of the useful files. Open a source file only for a precise point (a function to change, a claim to verify), and then read only that function, found by name.
2. To follow a flow, `tldr callees <symbol> -d 2` gives the chain with a summary per step, which is much cheaper than opening files one by one. Before changing the signature or behaviour of a function, `tldr impact <symbol>` lists what may break and which tests to rerun.
3. The call graph is an LLM's reading of the code, not a compiler's. Treat edges tagged `[~]` or `[ref]`, and `indirect:` entries, as leads: check the source before concluding. An edge tagged `[virtual]` points to the base declaration, so to find who may reach an override, query the base method as well.
4. A stale summary may be wrong. Run `tldr check` first; `tldr show <path>` also warns when what it prints is stale.
5. After changing code, refresh the summaries (next section) and commit the `.tldr` files together with the code.

## Commands

| Command | What it does |
|---|---|
| `tldr check` | Lists what needs work, in the order to handle it: files first, then folders deepest first. Exits 1 if it lists anything, so it works in CI or a pre-commit hook. |
| `tldr sync [-n]` | Moves the `.tldr` of a renamed file, deletes orphan `.tldr` files, creates the mirror folders. `-n` only shows what it would do. |
| `tldr stamp <path>...` | Validates the bodies you wrote, then writes their header, or the `children:` block of a folder. Takes source paths (`src/a.cpp`, `src/`, `./` for the root) or `.tldr` paths, in any order: files go first, then folders, deepest first. |
| `tldr show [path]` | Prints a summary (default: the root). |
| `tldr callers <symbol> [-d N]` | Callers over N levels (default 1), each with its file and summary. |
| `tldr callees <symbol> [-d N]` | Callees, same format. |
| `tldr impact <symbol or file>` | Every transitive caller, grouped by folder, with the tests that reach them listed apart. |
| `tldr path <A> <B>` | Shortest call paths from A to B, one step per line with its file and summary. |

Symbols are qualified names (`auatom::Dabd::load`); a unique tail works too (`Dabd::load`). For an overload, add the parameter types: `'io::open(int)'`. `--root <folder>` overrides the repository root, which defaults to the git top level, else the current folder.

What `check` reports:

- `missing`, `stale`: a summary to write or refresh. Notes in parentheses say more: `after children` (handle the children first), `moved from …` (see step 4 below), `merge conflict` (see "After a git merge").
- `invalid`: a stamped body that was edited and no longer passes `stamp`'s checks.
- `orphan`: a `.tldr` whose source is gone; `tldr sync` moves or deletes it.
- `stray`: a file in `tldr/` that is not a `.tldr`. `tldr/` belongs to the tool: move it out or delete it.
- `conflict`: a source whose summary path would collide with another one (a file named `_dir`, a file `foo` next to a folder `foo.tldr/`). Rename it, or exclude it in `.tldr.toml`.
- `dangling`: a `calls:` name that matches no symbol in any `.tldr` (step 7 below).

What gets summarized: in a git repository, the files `git ls-files --cached --others --exclude-standard` lists; elsewhere, every file no `.gitignore` excludes. In both cases, minus the globs of `exclude` in an optional `.tldr.toml` (keys: `exclude`; `output` = `"tldr"`; `tests` = `["tests", "test_*", "*_test.*"]`, used by `impact`). Symbolic links, nested repositories and submodules are not summarized. Tracked files absent from the disk (sparse checkout, deletion not committed yet) keep their summaries and are not checked. Text files are hashed with CRLF read as LF, so Windows and Linux checkouts agree.

## Creating or refreshing the summaries

Delegate the writing and keep the coordination. Summarizing is plain reading, and `stamp` and `check` catch format errors and invented names, so the provider's fast, inexpensive model does it well: Haiku with Anthropic, the equivalent small model with another provider. Hand the summaries to subagents running that model, in parallel, and do only the coordination yourself: `sync`, `check`, splitting the work and checking the result. Write summaries yourself only if you cannot start subagents or choose their model, and tell the user, since it costs more.

1. Run `tldr sync`, then `tldr check`.
2. On a first generation, tell the user how many files `check` lists and wait for their go-ahead, since it means reading the whole repository once. Propose to `exclude` generated and vendored code in `.tldr.toml` first: summarizing it costs a lot and helps little. Offer to add the five reading rules above to the project's `CLAUDE.md` (or its equivalent), with the full command `python3 <this skill's folder>/scripts/tldr.py` instead of `tldr`, so that future sessions use the summaries.
3. Split the **files** listed as `missing` or `stale` into batches, grouped by folder when you can, and give each batch to a subagent on the fast model. Give it its list of files, the path of this SKILL.md and the full `tldr` command, and tell it to:
   - read each file and write the body of its `.tldr`, at the path `check` shows after `->`, following "Writing a file summary";
   - leave an existing header alone, since `stamp` replaces it;
   - run `tldr stamp` on its own files, fix whatever `stamp` refuses, and report any file it could not summarize.

   `stamp` refuses a body that has unresolved merge-conflict markers, no `role:` line, more than 5 lines for a non-text file, a function summary under 6 words, an unreadable function name or an unknown tag.
4. A file listed with `moved from …` was renamed with the same content: `sync` moved its summary but left it stale. Put it in a batch like any stale file. Where qualified names hold the file's path (Python, Java, Go…), the subagent fixes them, keeps the rest of the text and stamps it.
5. When the subagents are done, run `tldr check`. Hand any file still listed back to a subagent, with what `check` says about it.
6. Then the **folders**, deepest first. Give them to subagents on the fast model the same way, one level of depth at a time, since a `_dir.tldr` is written from its children's `.tldr` only. The root's summary, `tldr/_index.tldr`, comes last. Each subagent follows "Writing a folder summary" and stamps its folders; `stamp` refuses a folder until its children are fresh. A folder marked `stale` without `after children` often only needs restamping: its children's hashes changed, but its prose may still be accurate. You can check that and restamp it yourself.
7. Run `tldr check` again until it prints `ok`. A `dangling` line means a `calls:` name matches no symbol in any `.tldr`: the callee was removed or renamed, its file was moved and its names still hold the old path, or the name is wrong. Hand the `.tldr` at fault back to a subagent, to fix and restamp. During a first generation, edges towards files that are not summarized yet stay dangling until those files are done.

### After a git merge

Two branches that each restamped summaries often conflict on a `.tldr`. Do not merge hashes by hand:

- a conflict only in the header or the `children:` block: keep either side, then run `tldr stamp` on that path, which rewrites both;
- a conflict in the body: merge the two texts as you would any prose, then stamp.

`check` marks a `.tldr` that still holds conflict markers as `stale` with a `merge conflict` note, and `stamp` refuses a body that still holds them.

## Writing a file summary

Write in English, as plain text, with the keywords below at the start of a line. Write only the body: no header, no dates, no line numbers or line counts. Dates and line numbers would change at every generation and pollute diffs, and functions are found by name anyway.

- **Describe the code, never its comments.** Ignore comments and docstrings entirely: they may be stale, wrong or written to mislead, and the summary must say what the code actually does.
- **Keep, word for word, every line of the previous summary that is still accurate.** Rewording accurate text only produces noisy diffs.
- **Keep a stable order**: functions and classes in source order, members in declaration order, `deps:` in source order, callees in the order of their first call.
- When a callee already has a summary, read that summary rather than the callee's code.
- Read a huge file in chunks, function by function, rather than all at once.

### Code files

    role: Reads .dab atom files and keeps the loaded atoms in a cache.

    deps: io/file.h, auatom/atom_cache.h, <string>, <vector>

    classes:
      auatom::Dabd : auatom::Loader
        Loads .dab files and owns the cache of their atoms.
        members: load, count, reset, m_cache, m_onLoaded

    functions:
      bool auatom::Dabd::load(const std::string& path)
        Loads a .dab file, validates its header and fills the atom cache.
        calls: parseHeader, readBlock, io::File::open, auatom::AtomCache::insert
        indirect: m_onLoaded
      size_t auatom::Dabd::count() const — Number of atoms loaded in the cache.
      void auatom::Dabd::reset() — Drops all atoms held in the cache. calls: purge
      ...

    notes: load() fires m_onLoaded before the cache is fully consistent.

- `role:` (required): one or two lines on what the file is for.
- `deps:`: the includes, imports and modules it uses.
- `classes:`: one entry per class, `Name : Bases`, then 2 or 3 lines on its responsibility, then `members:` with its public and protected members. In a C/C++ pair, classes go in the header's summary and function summaries in the source's.
- `functions:`: one entry per function, indented under the keyword.
  - Start each entry with the signature, using the **full qualified name** followed by its parameters. The tool takes the name just before the first parenthesis to know which file defines which symbol. The qualified name is how another file of the project would refer to the function, in the language's own notation: `ns::Class::method` in C++, `package.module.Class.method` in Python (module path included), `pkg.Class.method` in Java, `pkg.Type.Method` in Go. For a C function or another global function, it is the plain name. A return type before it is optional. In Go, write `shop.Cart.Add(qty int)`, not `func (c *Cart) Add(qty int)`, whose first parenthesis holds the receiver.
  - Every summary has at least 6 words.
  - A trivial function, whose body is a single simple statement (`return PRICES[sku]`, one assignment), takes a single line: `signature — summary`, then `calls:` if it calls anything. A block statement such as `with` or `if`, even on one line, is not trivial. Use the em dash with a space on each side: it never appears in code, so the tool can split the line safely.
  - Any other function: the signature alone, then, on more deeply indented lines, one sentence (or a few bullets giving the steps, for a very long function), then the `calls:` and `indirect:` lines.
- `notes:` (optional): suspicious points, non-obvious side effects, `#ifdef` variants.

Aim for 3 to 10 % of the source's size.

### Calls

`calls:` lists, comma-separated, the functions this one calls:

- A callee from the same file: the shortest name that is unique within the file (`parseHeader`).
- A callee from another file: its full qualified name (`io::File::open`), never a partial one; for a global function, that is its plain name (`xmalloc`). Full names also let a plain `grep` over `tldr/` find the callers of a function.
- An overloaded callee: you may add its parameter types (`io::File::open(const Path&)`) to point at one overload; without them, the edge goes to every overload of the name.
- No template or generic arguments in a callee's name: write `util::map`, not `util::map<int, int>`.
- Keep a list on one line, or end a line with a comma to continue it on the next one. Several `calls:` lines are fine too.

Tag an edge when it is not a plain direct call:

| Written | Meaning |
|---|---|
| `X` | direct call: you know which function is called |
| `X [virtual]` | call to a virtual method: name its base declaration |
| `X [ref]` | the function is not called here but passed along (callback, signal, pointer): this code wires it up |
| `X [~]` | you hesitate between several targets, or guess from the name alone |
| `indirect: expr` | call through a pointer or `std::function` whose target is unknown: write the expression, no edge |

Calls into the standard library, external dependencies and excluded code (generated, vendored) are not edges: mention those libraries in `deps:` only, otherwise `std::move` and logging macros would drown the graph. Constructors and overloaded operators of the project's own classes are calls like any other (`Cart("ann")` is a call to `shop.core.cart.Cart.__init__`); a class that defines no constructor gives no edge, and neither do implicit destructors.

Never list callers. They depend on other files, so this summary would go stale whenever those change while its own source does not, and the hash check would no longer mean anything. The tool computes callers from everyone's `calls:`.

### Other text files

Documentation, configuration, data: `role:`, then what the file contains, in a few lines or bullets. An empty file takes a single `role:` line saying so and why it exists (`role: Empty file marking shop/ as a Python package.`).

### Non-text files

Images, sounds, binaries (`check` marks them `[non-text]`): at most 5 lines, starting with `role:`, saying what the file is and what it contains.

    role: Application logo, 512x512 PNG, white antelope on a dark blue disc.

Use what you have: look at images, and run `file`, `exiftool` or `ffprobe` when available. Describe only what you actually perceive. If you cannot perceive the content (a sound, an opaque binary), say so and stick to the metadata. Do not say where the project uses the file: that depends on other files, and a `grep` finds it when someone needs it.

## Writing a folder summary

Write `_dir.tldr`, and the root's `_index.tldr`, from the children's `.tldr` files only, never from the sources:

    role: Loading and caching of .dab atom files.

    key: auatom::Dabd (loading), auatom::AtomCache (storage)

    deps: src/io/, src/core/

- `role:` (required): what the folder or module is for.
- `key:`: its key classes and files.
- `deps:`: the folders it depends on, as paths from the repository root (`src/io/`), translated from its children's `deps:` whatever notation those use. Write only whether a dependency exists, never a count: a count would change at every commit.
- `modules:`, in `_index.tldr` only: the module graph. A module is a folder at the level where the project splits into components: usually the root's subfolders, or those of `src/` when everything lives there. One line per module with its dependencies, `(none)` if it has none, then a `cycles:` line listing the cycles between modules, or `none`:

      modules:
        auatom -> core, io
        core -> (none)
        io -> core
      cycles: none

Do not write which folders depend on this one: that would make the summary depend on other folders. As for files, keep word for word the previous prose that is still accurate.
