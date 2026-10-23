#!/usr/bin/env python3
"""tldr: keep a mirror tree of .tldr summaries in sync with a repository.

The tool is deterministic and knows no programming language. It hashes
files, keeps the tldr/ tree in shape, reports what is missing or stale,
writes the header of each .tldr and answers call-graph queries by reading
the .tldr text. It never calls an LLM: the agent writes the summaries.

Python 3.11 or newer, standard library only.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("tldr: Python 3.11 or newer is required")

import argparse
import hashlib
import os
import posixpath
import re
import subprocess
import tomllib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

GENERATOR = "tldr/1.0"
CONFIG_FILE = ".tldr.toml"
CACHE_DIR = ".tldr-cache"
DEFAULT_OUTPUT = "tldr"
DEFAULT_TESTS = ["tests", "test_*", "*_test.*"]
TEXT_SNIFF_BYTES = 8000  # like git: a NUL byte in this prefix means non-text
NON_TEXT_MAX_LINES = 5
MIN_SUMMARY_WORDS = 6
EDGE_TAGS = ("virtual", "ref", "~")
SUMMARY_WIDTH = 120  # query output cuts summaries to this many characters
MAX_PATHS = 20
WORKERS = min(32, (os.cpu_count() or 4) + 4)
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7})(?: |$)|^={7}$", re.MULTILINE)


class TldrError(Exception):
    """A user-facing error, printed without a traceback."""


# ------------------------------------------------------------------ files

def read_text(path):
    # surrogateescape keeps undecodable bytes, so file names that are not
    # UTF-8 survive a round trip through a header.
    try:
        with open(path, encoding="utf-8-sig", errors="surrogateescape") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
        f.write(text)


def sha256_of(path):
    """SHA-256 of a file's raw bytes, or None if it does not exist."""
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


def hash_source(path):
    """(sha256, is_text) of a source file, read once.

    A text file is hashed with CRLF read as LF, so that a Windows checkout
    and a Linux checkout of the same commit get the same hash.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(TEXT_SNIFF_BYTES)
            is_text = b"\0" not in head
            digest, chunk, carry = hashlib.sha256(), head, b""
            while chunk:
                if is_text:
                    chunk = carry + chunk
                    carry = b"\r" if chunk.endswith(b"\r") else b""  # may pair with a later \n
                    chunk = (chunk[:-1] if carry else chunk).replace(b"\r\n", b"\n")
                digest.update(chunk)
                chunk = f.read(1 << 20)
            digest.update(carry)
    except OSError as e:
        raise TldrError(f"cannot read {path}: {e.strerror}") from e
    return digest.hexdigest(), is_text


def posix_rel(path, start):
    rel = os.path.relpath(path, start).replace(os.sep, "/")
    return "" if rel == "." else rel


def join_rel(parent, name):
    return f"{parent}/{name}" if parent else name


def depth(rel):
    return rel.count("/") + 1 if rel else 0


def display(rel, is_dir):
    """How a source is named in output: a/b.cpp, a/b/, or ./ for the root."""
    if not is_dir:
        return rel
    return f"{rel}/" if rel else "./"


def classify(inner):
    """What a path inside the output folder mirrors: ('dir', d), ('file', f) or None."""
    if inner == "_index.tldr":
        return "dir", ""
    parent, _, name = inner.rpartition("/")
    if name == "_dir.tldr" and parent:
        return "dir", parent
    if name.endswith(".tldr"):
        return "file", inner[:-len(".tldr")]
    return None


# ----------------------------------------------------- gitignore patterns

@dataclass
class Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool


def glob_to_regex(glob):
    out, i, n = [], 0, len(glob)
    while i < n:
        between_slashes = (i == 0 or glob[i - 1] == "/") and (i + 2 == n or glob[i + 2:i + 3] == "/")
        if glob.startswith("**", i) and between_slashes:
            if i + 2 == n:
                out.append(".*")
                i += 2
            else:
                out.append("(?:.*/)?")
                i += 3
        elif glob[i] == "*":  # also "**" glued to other characters: a plain star
            out.append("[^/]*")
            i += 2 if glob.startswith("**", i) else 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        elif glob[i] == "[" and (j := glob.find("]", i + 2)) != -1 and glob[i + 1:j] not in ("!", "^"):
            members = glob[i + 1:j]
            negate = members[0] in "!^"
            members = members[1:] if negate else members
            out.append("[" + ("^" if negate else "") + members.replace("\\", "\\\\") + "]")
            i = j + 1
        elif glob[i] == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def compile_rule(line):
    """Compile one gitignore-style pattern; None for blanks and comments."""
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += " "  # "\ " keeps a trailing space
    line = stripped
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    prefix = "" if "/" in line else "(?:.*/)?"  # a slash anchors the pattern
    line = line.lstrip("/")
    try:
        regex = re.compile(prefix + glob_to_regex(line))
    except re.error:  # a pattern git reads differently: match it literally
        regex = re.compile(prefix + re.escape(line))
    return Rule(regex, negate, dir_only)


class Matcher:
    """Gitignore-style rules, relative to a base folder of the repository."""

    def __init__(self, patterns, base=""):
        self.rules = [r for r in map(compile_rule, patterns) if r]
        self.base = base

    def verdict(self, rel, is_dir):
        """True (excluded), False (re-included) or None (no rule matches)."""
        if self.base:
            if not rel.startswith(self.base + "/"):
                return None
            rel = rel[len(self.base) + 1:]
        result = None
        for rule in self.rules:
            if (is_dir or not rule.dir_only) and rule.regex.fullmatch(rel):
                result = not rule.negate
        return result

    def covers(self, rel):
        """True if rel, or one of its parent folders, is excluded."""
        parts = rel.split("/")
        for k in range(1, len(parts) + 1):
            if self.verdict("/".join(parts[:k]), k < len(parts)):
                return True
        return False


# ---------------------------------------------------------- listing files

def git_toplevel(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except FileNotFoundError:  # git is not installed
        return None
    return os.path.realpath(out.stdout.strip()) if out.returncode == 0 else None


def git_files(root, *flags):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", *flags],
                             capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        raise TldrError(f"git ls-files failed: {e.stderr.decode(errors='replace').strip()}") from e
    return {p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def list_git(root):
    """(files to consider, tracked files): the .gitignore files apply."""
    return git_files(root, "--cached", "--others", "--exclude-standard"), git_files(root, "--cached")


def list_walk(root):
    """Files outside git, applying every .gitignore found on the way down."""
    matchers, found = {}, set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = posix_rel(dirpath, root)
        rules = read_text(os.path.join(dirpath, ".gitignore"))
        if rules is not None:
            matchers[rel_dir] = Matcher(rules.splitlines(), rel_dir)
        parts = rel_dir.split("/") if rel_dir else []
        chain = [matchers[key] for k in range(len(parts) + 1)
                 if (key := "/".join(parts[:k])) in matchers]

        def ignored(rel, is_dir):
            # Deeper .gitignore files come last, so their verdict wins.
            result = False
            for matcher in chain:
                verdict = matcher.verdict(rel, is_dir)
                if verdict is not None:
                    result = verdict
            return result

        kept = []
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            if name == ".git" or os.path.islink(path) or os.path.lexists(os.path.join(path, ".git")):
                continue  # like git, skip nested repositories
            if not ignored(join_rel(rel_dir, name), True):
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = join_rel(rel_dir, name)
            if not ignored(rel, False):
                found.add(rel)
    return found


def name_conflict(rel, dirs):
    """Why a source file cannot get a summary path of its own, or None."""
    parts = rel.split("/")
    if "\n" in rel or "\r" in rel:
        return "its name contains a line break"
    if len(parts) > 1 and parts[-1] == "_dir":
        return "its summary would be the folder summary _dir.tldr"
    if rel == "_index":
        return "its summary would be the root summary _index.tldr"
    if "_dir.tldr" in parts[:-1] or (len(parts) > 1 and parts[0] == "_index.tldr"):
        return "one of its folders is named like a folder summary"
    if rel + ".tldr" in dirs:
        return f"the folder {rel}.tldr/ would have the same summary path"
    return None


# ----------------------------------------------------------------- config

@dataclass
class Config:
    exclude: list
    output: str
    tests: list


def load_config(root):
    """Read .tldr.toml if present; every key has a default."""
    data = {}
    path = os.path.join(root, CONFIG_FILE)
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise TldrError(f"{CONFIG_FILE}: {e}") from e
    unknown = sorted(set(data) - {"exclude", "output", "tests"})
    if unknown:
        raise TldrError(f"{CONFIG_FILE}: unknown key(s): {', '.join(unknown)}")

    def globs(key, default):
        value = data.get(key, default)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise TldrError(f"{CONFIG_FILE}: '{key}' must be a list of strings")
        return value

    output = data.get("output", DEFAULT_OUTPUT)
    output = posixpath.normpath(output.strip()) if isinstance(output, str) and output.strip() else ""
    if output in ("", ".") or output.startswith(("../", "/")) or output == "..":
        raise TldrError(f"{CONFIG_FILE}: 'output' must be a folder inside the repository")
    return Config(globs("exclude", []), output, globs("tests", DEFAULT_TESTS))


# ------------------------------------------------------------ .tldr files

@dataclass
class Tldr:
    header: dict
    children: list  # [(sha256, label)] for a folder summary
    body: str
    has_header: bool


def parse_tldr(text):
    """Split a .tldr into its header (written by the tool) and its body."""
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if not lines or not lines[0].startswith("source:"):
        return Tldr({}, [], text.strip("\n").rstrip(), False)
    header, children, i, in_children = {}, [], 0, False
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        if in_children and line[:1].isspace():
            content = line[2:] if line.startswith("  ") else line.lstrip()
            digest, sep, label = content.partition("  ")
            if not sep:
                digest, _, label = content.partition(" ")
            children.append((digest, label))
        else:
            key, _, value = line.partition(":")
            in_children = key.strip() == "children"
            if not in_children:
                # Only the space after the colon goes: a name may end with spaces.
                header[key.strip()] = value[1:] if value.startswith(" ") else value
        i += 1
    return Tldr(header, children, "\n".join(lines[i:]).strip("\n").rstrip(), True)


def render_file_tldr(rel, digest, body, generator=GENERATOR, moved_from=None):
    moved = f"moved-from: {moved_from}\n" if moved_from is not None else ""
    return f"source: {rel}\nsha256: {digest}\ngenerator: {generator}\n{moved}\n{body}\n"


def render_dir_tldr(d, children, body):
    # One line per child, hash first: no padding that shifts when a child is
    # added, so branches touching different children change different lines.
    rows = "".join(f"  {digest}  {label}\n" for digest, label in children)
    return f"source: {display(d, True)}\ngenerator: {GENERATOR}\nchildren:\n{rows}\n{body}\n"


# ------------------------------------------------------------ body format

SECTION_RE = re.compile(r"([a-z_]+):(.*)")
CALL_TAG_RE = re.compile(r"(.*?\S)\s+\[([^\]]*)\]")
INLINE_KEY_RE = re.compile(r"(?:^|\s+)(calls|indirect):\s*")
MEMBERS_RE = re.compile(r"members:\s*(.*)")
SEPARATOR_RE = re.compile(r"(::|\.)")


@dataclass
class Call:
    name: str
    tag: str = ""


@dataclass
class Function:
    signature: str
    name: str
    params: str | None = None
    summary: str = ""
    calls: list = field(default_factory=list)
    indirect: list = field(default_factory=list)


@dataclass
class Body:
    role: str = ""
    functions: list = field(default_factory=list)
    symbols: set = field(default_factory=set)  # functions, classes, members


def split_top(text):
    """Split a comma-separated list, ignoring commas inside brackets.

    Angle brackets do not count: operator< and operator<< would unbalance them.
    """
    parts, current, level = [], [], 0
    for ch in text:
        if ch in "([{":
            level += 1
        elif ch in ")]}":
            level = max(0, level - 1)
        if ch == "," and level == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def strip_generics(head):
    """Drop a trailing <...> block: "fn parse<T: FromStr>" -> "fn parse"."""
    if not head.endswith(">"):
        return head
    level = 0
    for i in range(len(head) - 1, -1, -1):
        level += {">": 1, "<": -1}.get(head[i], 0)
        if level == 0:
            return head if head[:i].endswith("operator") else head[:i]
    return head  # operator>, operator->, operator>>


def normalize_params(text):
    """Parameter types only, so "int fd, const Path &p" and "int, const Path&" agree."""
    types = []
    for param in split_top(text):
        param = re.sub(r"\s+", " ", param.split("=")[0]).strip()
        m = re.fullmatch(r"(.*?[\s*&])[A-Za-z_]\w*", param)
        if m and m.group(1).strip():  # "type name": drop the name
            param = m.group(1)
        types.append(re.sub(r"\s*([&*,<>])\s*", r"\1", param).strip())
    return ",".join(types)


def split_signature(signature):
    """(qualified name, parameter types or None) of a signature.

    The name is the last word before the first parenthesis, generic
    parameters left out: this holds for "bool ns::Cls::load(...)",
    "pkg.mod.func(...)", "fn parse<T>(...)" or "Cls::operator()(...)".
    A leading "::" (C++ global namespace) is dropped.
    """
    k = signature.find("(")
    head = signature if k == -1 else signature[:k]
    if k != -1 and head.endswith("operator") and signature.startswith("()", k):
        head += "()"
        k = signature.find("(", k + 2)
    params = None
    if k != -1:
        level = 0
        for i in range(k, len(signature)):
            level += {"(": 1, ")": -1}.get(signature[i], 0)
            if level == 0:
                params = normalize_params(signature[k + 1:i])
                break
    tokens = strip_generics(head.rstrip()).split()
    name = tokens[-1].lstrip("*&") if tokens else ""
    return (name[2:] if name.startswith("::") else name), params


def suffixes(symbol):
    """Every tail of a qualified name: a::b::c, b::c and c."""
    parts = SEPARATOR_RE.split(symbol)
    return ["".join(parts[i:]) for i in range(0, len(parts), 2)]


def is_short(name):
    return "::" not in name and "." not in name


def count_words(text):
    return sum(1 for word in text.split() if re.search(r"\w", word))


def entries(lines):
    """Group the lines of a section into (entry, continuation lines)."""
    result, base = [], None
    for raw in lines:
        text = raw.strip()
        if not text or text == "...":
            continue
        indent = len(raw) - len(raw.lstrip())
        if base is None:
            base = indent
        if indent <= base or not result:
            result.append((text, []))
        else:
            result[-1][1].append(text)
    return result


def add_items(fn, key, value):
    for item in split_top(value):
        if key == "indirect":
            fn.indirect.append(item)
        else:
            m = CALL_TAG_RE.fullmatch(item)
            fn.calls.append(Call(m.group(1), m.group(2).strip()) if m else Call(item))


def parse_functions(lines):
    functions = []
    for first, rest in entries(lines):
        signature, dash, tail = first.partition(" — ")
        name, params = split_signature(signature)
        fn = Function(signature.strip(), name, params)
        list_key = None  # a calls:/indirect: list ending with a comma goes on
        for text in ([tail] if dash else []) + rest:
            if list_key:
                add_items(fn, list_key, text)
                list_key = list_key if text.rstrip().endswith(",") else None
                continue
            parts = INLINE_KEY_RE.split(text)
            words = parts[0].strip().lstrip("-*• ").strip()
            if words:
                fn.summary = f"{fn.summary} {words}".strip()
            for key, value in zip(parts[1::2], parts[2::2]):
                add_items(fn, key, value)
                list_key = key if value.rstrip().endswith(",") else None
        functions.append(fn)
    return functions


def parse_classes(lines, symbols):
    for first, rest in entries(lines):
        tokens = first.split(" : ")[0].split("(")[0].split()
        if not tokens:
            continue
        cls = tokens[-1]
        symbols.add(cls)
        for text in rest:
            m = MEMBERS_RE.fullmatch(text)
            for member in split_top(m.group(1)) if m else []:
                name = split_signature(member)[0]
                # The separator follows the class name; an unqualified class
                # could be either style, so both forms are registered.
                if "::" in cls or "." not in cls:
                    symbols.add(f"{cls}::{name}")
                if "." in cls or "::" not in cls:
                    symbols.add(f"{cls}.{name}")


def parse_body(text):
    sections, current = {}, None
    for line in text.splitlines():
        m = SECTION_RE.fullmatch(line.rstrip()) if line[:1] not in ("", " ", "\t") else None
        if m:
            current = m.group(1)
            sections.setdefault(current, []).append(m.group(2).strip())
        elif current is not None:
            sections[current].append(line)
    body = Body()
    body.role = " ".join(line.strip() for line in sections.get("role", []) if line.strip())
    body.functions = parse_functions(sections.get("functions", [])[1:])
    body.symbols = {fn.name for fn in body.functions if fn.name}
    parse_classes(sections.get("classes", [])[1:], body.symbols)
    return body


def validate_file_body(text, is_text):
    """Problems that make stamp refuse a file summary."""
    if CONFLICT_MARKER_RE.search(text):
        return ["unresolved merge conflict markers in the body"]
    problems = []
    body = parse_body(text)
    if not body.role:
        problems.append("missing 'role:' line")
    if not is_text:
        count = sum(1 for line in text.splitlines() if line.strip())
        if count > NON_TEXT_MAX_LINES:
            problems.append(f"non-text file: {count} lines, at most {NON_TEXT_MAX_LINES}")
    for fn in body.functions:
        label = fn.name or fn.signature
        if not fn.name:
            problems.append(f"cannot read a function name in '{fn.signature}'")
        words = count_words(fn.summary)
        if words < MIN_SUMMARY_WORDS:
            problems.append(f"{label}: summary has {words} word(s), needs at least "
                            f"{MIN_SUMMARY_WORDS} (one-line entries are 'signature — summary')")
        for call in fn.calls:
            if call.tag and call.tag not in EDGE_TAGS:
                problems.append(f"{label}: unknown tag [{call.tag}] on {call.name} "
                                f"(use {', '.join(EDGE_TAGS)})")
    return problems


def validate_dir_body(text):
    if CONFLICT_MARKER_RE.search(text):
        return ["unresolved merge conflict markers in the body"]
    return [] if parse_body(text).role else ["missing 'role:' line"]


# ------------------------------------------------------------- repository

class Repo:
    """The files to summarize, and their mirror under the output folder."""

    def __init__(self, root, config):
        self.root = root
        self.config = config
        listed, tracked = list_git(root) if git_toplevel(root) else (list_walk(root), set())
        excluded = Matcher(config.exclude)
        reserved = (config.output, CACHE_DIR)
        present, absent = [], []
        for rel in sorted(listed):
            if any(rel == r or rel.startswith(r + "/") for r in reserved) or excluded.covers(rel):
                continue
            path = self.abspath(rel)
            if os.path.islink(path):
                continue  # a link is not summarized; its target is, if included
            if os.path.isfile(path):
                present.append(rel)
            elif rel in tracked and not os.path.lexists(path):
                absent.append(rel)  # sparse checkout, or a deletion not committed yet
        dirs = {rel.rpartition("/")[0] for rel in present + absent}
        dirs |= {d.rsplit("/", k)[0] for d in list(dirs) for k in range(1, depth(d))}
        self.conflicts = [(rel, why) for rel in present if (why := name_conflict(rel, dirs))]
        conflicting = {rel for rel, _ in self.conflicts}
        self.files = [rel for rel in present if rel not in conflicting]
        self.file_set = set(self.files)
        self.absent = set(absent)  # kept in the tree, but never checked
        if not self.files and not self.absent:
            raise TldrError(f"nothing to summarize in {root}: every file is ignored or excluded")

        self.dirs = {""}
        self.child_files, self.child_dirs = {}, {}
        for rel in sorted(self.file_set | self.absent):
            parent, _, name = rel.rpartition("/")
            self.child_files.setdefault(parent, []).append(name)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                grand, _, name = parent.rpartition("/")
                self.child_dirs.setdefault(grand, []).append(name)
                parent = grand
        self._hashes = {}
        self._dir_status = {}

    def abspath(self, rel):
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def tldr_rel(self, rel, is_dir):
        out = self.config.output
        if not is_dir:
            return f"{out}/{rel}.tldr"
        return f"{out}/{rel}/_dir.tldr" if rel else f"{out}/_index.tldr"

    def files_under(self, d):
        return self.files if not d else [f for f in self.files if f.startswith(d + "/")]

    def hash_all(self, rels=None):
        todo = [r for r in (self.files if rels is None else rels) if r not in self._hashes]
        if len(todo) <= 1:
            for rel in todo:
                self._hashes[rel] = hash_source(self.abspath(rel))
            return
        with ThreadPoolExecutor(WORKERS) as pool:
            results = pool.map(lambda rel: hash_source(self.abspath(rel)), todo)
            for rel, result in zip(todo, results):
                self._hashes[rel] = result

    def digest(self, rel):
        self.hash_all([rel])
        return self._hashes[rel][0]

    def is_text(self, rel):
        self.hash_all([rel])
        return self._hashes[rel][1]

    def file_status(self, rel):
        """(status, note) of a file summary; status is None when it is fresh."""
        if rel in self.absent:
            return None, ""
        text = read_text(self.abspath(self.tldr_rel(rel, False)))
        if text is None:
            return "missing", ""
        if CONFLICT_MARKER_RE.search(text):
            return "stale", "merge conflict: resolve the body if needed, then stamp"
        t = parse_tldr(text)
        if "moved-from" in t.header:
            return "stale", f"moved from {t.header['moved-from']}: fix its names if they hold the path, then stamp"
        fresh = (t.has_header and t.body
                 and t.header.get("source") == rel
                 and t.header.get("sha256") == self.digest(rel)
                 and t.header.get("generator") == GENERATOR)
        if not fresh:
            return "stale", ""
        problems = validate_file_body(t.body, self.is_text(rel))
        return ("invalid", problems[0]) if problems else (None, "")

    def children(self, d):
        """[(label, rel, is_dir)] of a folder, sorted by label."""
        rows = [(f"{n}.tldr", join_rel(d, n), False) for n in self.child_files.get(d, [])]
        rows += [(f"{n}/_dir.tldr", join_rel(d, n), True) for n in self.child_dirs.get(d, [])]
        return sorted(rows)

    def expected_children(self, d):
        return [(sha256_of(self.abspath(self.tldr_rel(rel, is_dir))) or "-", label)
                for label, rel, is_dir in self.children(d)]

    def status(self, rel, is_dir):
        return self.dir_status(rel) if is_dir else self.file_status(rel)

    def dir_status(self, d):
        """(status, note) of a folder summary; status is None when it is fresh."""
        if d not in self._dir_status:
            pending = any(self.status(rel, is_dir)[0] for _, rel, is_dir in self.children(d))
            note = "after children" if pending else ""
            text = read_text(self.abspath(self.tldr_rel(d, True)))
            if text is None:
                result = ("missing", note)
            elif CONFLICT_MARKER_RE.search(text):
                result = ("stale", "merge conflict: resolve the body if needed, then stamp")
            else:
                t = parse_tldr(text)
                fresh = (not pending and t.has_header and t.body
                         and t.header.get("source") == display(d, True)
                         and t.header.get("generator") == GENERATOR
                         and t.children == self.expected_children(d))
                problems = validate_dir_body(t.body) if fresh else []
                result = ("stale", note) if not fresh else (("invalid", problems[0]) if problems else (None, ""))
            self._dir_status[d] = result
        return self._dir_status[d]

    def orphans(self):
        """[(path, kind)] of files under the output folder that match nothing."""
        out = self.config.output
        out_abs = self.abspath(out)
        result = []
        for dirpath, dirnames, filenames in os.walk(out_abs):
            dirnames.sort()
            for name in sorted(filenames):
                inner = posix_rel(os.path.join(dirpath, name), out_abs)
                kind = classify(inner)
                if kind is None:
                    result.append((f"{out}/{inner}", "stray"))
                elif kind[0] == "dir" and kind[1] not in self.dirs:
                    result.append((f"{out}/{inner}", "orphan"))
                elif kind[0] == "file" and kind[1] not in self.file_set and kind[1] not in self.absent:
                    result.append((f"{out}/{inner}", "orphan"))
        return result

    def resolve_target(self, arg):
        """(rel, is_dir) of a source path, or of the .tldr path that mirrors it."""
        absolute = os.path.abspath(arg)
        if os.path.isdir(absolute):
            absolute = os.path.realpath(absolute)
        else:  # resolve the folders, not a link the file itself may be
            absolute = os.path.join(os.path.realpath(os.path.dirname(absolute)),
                                    os.path.basename(absolute))
        rel = posix_rel(absolute, self.root)
        if rel == ".." or rel.startswith("../"):
            raise TldrError(f"{arg} is outside the repository")
        out = self.config.output
        if rel.startswith(out + "/"):
            kind = classify(rel[len(out) + 1:])
            if kind:
                rel = kind[1]
        if rel in self.file_set:
            return rel, False
        if rel in self.dirs:
            return rel, True
        raise TldrError(f"{arg} is not a file or folder tldr summarizes "
                        f"(missing, or excluded by .gitignore or {CONFIG_FILE})")


# -------------------------------------------------------------- call graph

class Graph:
    """The call graph, rebuilt from the calls: lines of every file summary.

    A node is (qualified name, file, parameters): two static functions with
    the same name in two files, or two overloads, stay apart.
    """

    def __init__(self, root, config):
        out_dir = os.path.join(root, config.output)
        paths = []
        for dirpath, dirnames, filenames in os.walk(out_dir):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                kind = classify(posix_rel(path, out_dir))
                if kind and kind[0] == "file":
                    paths.append((kind[1], path))
        with ThreadPoolExecutor(WORKERS) as pool:
            texts = list(pool.map(read_text, [path for _, path in paths]))

        self.nodes = {}       # node -> (summary, is_function)
        self.by_name = {}     # name -> [nodes]
        self.file_nodes = {}  # file -> [nodes]
        self.out = {}         # node -> {(callee node or None, written name, tag): None}
        self.inc = {}         # node -> {(caller node, tag): None}
        self.indirect = {}    # node -> [expressions]
        self.dangling = []    # (file, caller label, written name)
        parsed = []
        for (file, _), text in zip(paths, texts):
            body = parse_body(parse_tldr(text or "").body)
            parsed.append((file, body))
            names = set()
            for fn in body.functions:
                if fn.name:
                    names.add(fn.name)
                    self._add((fn.name, file, fn.params or ""), fn.summary, True)
            for symbol in sorted(body.symbols - names):  # classes and members
                self._add((symbol, file, ""), "", False)
        self.by_suffix = {}
        for name in self.by_name:
            for tail in suffixes(name):
                self.by_suffix.setdefault(tail, set()).add(name)

        for file, body in parsed:
            for fn in body.functions:
                if not fn.name:
                    continue
                caller = (fn.name, file, fn.params or "")
                edges = self.out.setdefault(caller, {})
                for call in fn.calls:
                    targets = self.resolve(call.name, file)
                    if not targets:
                        self.dangling.append((file, self.label(caller), call.name))
                    for target in targets or [None]:
                        edges[(target, call.name, call.tag)] = None
                        if target:
                            self.inc.setdefault(target, {})[(caller, call.tag)] = None
                if fn.indirect:
                    self.indirect.setdefault(caller, []).extend(fn.indirect)

    def _add(self, node, summary, is_function):
        if node not in self.nodes:
            self.nodes[node] = (summary, is_function)
            self.by_name.setdefault(node[0], []).append(node)
            self.file_nodes.setdefault(node[1], []).append(node)

    def _pick(self, nodes, params):
        """Prefer definitions over declarations, then matching parameters."""
        nodes = [n for n in nodes if self.nodes[n][1]] or list(nodes)
        if params is not None:
            nodes = [n for n in nodes if n[2] == params] or nodes
        return sorted(nodes)

    def resolve(self, written, file):
        """Nodes a calls: entry points to.

        Look in the caller's own file first, then for the exact name anywhere
        (a C function's full name is its plain name), then for a unique tail
        of a qualified name. A plain name never matches by tail: "load" must
        not silently bind to some other file's ns::Cls::load.
        """
        name, params = split_signature(written)
        names = self.by_suffix.get(name, set())
        local = [n for candidate in names for n in self.by_name[candidate] if n[1] == file]
        if local:
            return self._pick(local, params)
        if name in self.by_name:
            return self._pick(self.by_name[name], params)
        if not is_short(name):
            return self._pick([n for candidate in names for n in self.by_name[candidate]], params)
        return []

    def lookup(self, symbol):
        name, params = split_signature(symbol)
        names = [name] if name in self.by_name else sorted(self.by_suffix.get(name, ()))
        nodes = [n for candidate in names for n in self.by_name[candidate]]
        if not nodes:
            raise TldrError(f"unknown symbol: {symbol} (defined in no .tldr)")
        return self._pick(nodes, params)

    def label(self, node):
        """The name, with its parameters when the name is overloaded."""
        name, _, params = node
        overloads = {n[2] for n in self.by_name.get(name, []) if self.nodes[n][1]}
        return f"{name}({params})" if len(overloads) > 1 else name

    def info(self, node):
        return node[1], self.nodes[node][0]


def shorten(text, width=SUMMARY_WIDTH):
    return text if len(text) <= width else text[:width - 3].rstrip() + "..."


def print_rows(rows):
    """Print (level, label, file, summary) rows in aligned columns."""
    labels = ["  " * level + label for level, label, _, _ in rows]
    label_width = max(map(len, labels))
    file_width = max(len(file) for _, _, file, _ in rows)
    for label, (_, _, file, summary) in zip(labels, rows):
        print(f"{label:<{label_width}}  {file:<{file_width}}  {shorten(summary)}".rstrip())


def call_tree(graph, root, max_depth, forward):
    rows, expanded = [], {root}

    def walk(node, level, path):
        if forward:
            edges = list(graph.out.get(node, {}))
        else:
            edges = [(caller, None, tag) for caller, tag in graph.inc.get(node, {})]
        for target, written, tag in edges:
            mark = f" [{tag}]" if tag else ""
            if target is None:
                rows.append((level, written + mark, "?", "defined in no .tldr"))
                continue
            label = graph.label(target) + mark
            file, summary = graph.info(target)
            if target in path:
                rows.append((level, label + " (cycle)", file, summary))
            elif level + 1 < max_depth and target in expanded:
                rows.append((level, label + " (see above)", file, summary))
            else:
                rows.append((level, label, file, summary))
                if level + 1 < max_depth:
                    expanded.add(target)
                    walk(target, level + 1, path | {target})
        if forward:
            for expr in graph.indirect.get(node, []):
                rows.append((level, f"indirect: {expr}", "", ""))

    walk(root, 0, {root})
    return rows


# ---------------------------------------------------------------- commands

def locate(args):
    if args.root:
        root = os.path.realpath(args.root)
    else:
        root = git_toplevel(os.getcwd()) or os.path.realpath(os.getcwd())
    if not os.path.isdir(root):
        raise TldrError(f"not a folder: {root}")
    return root, load_config(root)


def todo_line(status, label, tldr_path, notes):
    notes = "".join(f"  ({note})" if not note.startswith("[") else f"  {note}"
                    for note in notes if note)
    return f"{status:<9}{label}  -> {tldr_path}{notes}"


def cmd_check(args):
    repo = Repo(*locate(args))
    repo.hash_all()
    lines = [f"conflict {rel}  ({why}: rename it or exclude it)" for rel, why in repo.conflicts]
    orphans = repo.orphans()
    lines += [f"{kind:<9}{path}" for path, kind in orphans]
    for rel in repo.files:
        status, note = repo.file_status(rel)
        if status:
            marker = "" if repo.is_text(rel) else "[non-text]"
            lines.append(todo_line(status, rel, repo.tldr_rel(rel, False), [marker, note]))
    for d in sorted(repo.dirs, key=lambda d: (-depth(d), d)):  # deepest first, root last
        status, note = repo.dir_status(d)
        if status:
            lines.append(todo_line(status, display(d, True), repo.tldr_rel(d, True), [note]))
    lines += [f"dangling {file}  {caller} -> {callee}"
              for file, caller, callee in Graph(repo.root, repo.config).dangling]
    if repo.absent:
        print(f"tldr: {len(repo.absent)} tracked file(s) absent from disk (sparse checkout or "
              "uncommitted deletion) were not checked", file=sys.stderr)
    if not lines:
        print("ok")
        return 0
    print("\n".join(lines))
    if orphans:
        print("\nrun 'tldr sync' to move or remove the orphans")
    return 1


def cmd_sync(args):
    repo = Repo(*locate(args))
    dry = args.dry_run
    missing = [rel for rel in repo.files
               if not os.path.exists(repo.abspath(repo.tldr_rel(rel, False)))]
    repo.hash_all(missing)
    by_hash = {}
    for rel in missing:
        by_hash.setdefault(repo.digest(rel), []).append(rel)
    orphans = repo.orphans()
    headers = {path: parse_tldr(read_text(repo.abspath(path)) or "").header
               for path, kind in orphans
               if kind == "orphan" and classify(path[len(repo.config.output) + 1:])[0] == "file"}
    orphan_hashes = {}
    for header in headers.values():
        orphan_hashes[header.get("sha256")] = orphan_hashes.get(header.get("sha256"), 0) + 1

    changes = 0
    for path, kind in orphans:
        if kind == "stray":
            print(f"left     {path}  (not a .tldr: move or delete it yourself)")
            continue
        header = headers.get(path, {})
        digest = header.get("sha256")
        # Only a match that cannot be a coincidence counts as a rename: one
        # orphan and one new file with that content, and not an empty file.
        renamed = (digest and digest != EMPTY_DIGEST and orphan_hashes.get(digest) == 1
                   and len(by_hash.get(digest, [])) == 1)
        if renamed:
            target = by_hash[digest][0]
            new_path = repo.tldr_rel(target, False)
            print(f"moved    {path} -> {new_path}  (stale until reviewed and stamped)")
            if not dry:
                # The text is kept but left stale: in Python, Java or Go the
                # qualified names hold the path, so the agent must check them.
                t = parse_tldr(read_text(repo.abspath(path)))
                write_text(repo.abspath(new_path),
                           render_file_tldr(target, "-", t.body, header.get("generator", GENERATOR),
                                            moved_from=header.get("source", "?")))
                os.remove(repo.abspath(path))
        else:
            print(f"removed  {path}")
            if not dry:
                os.remove(repo.abspath(path))
        changes += 1

    created = 0
    if not dry:
        out_abs = repo.abspath(repo.config.output)
        for dirpath, _, _ in os.walk(out_abs, topdown=False):
            if dirpath != out_abs and not os.listdir(dirpath):
                os.rmdir(dirpath)
        for d in repo.dirs:
            folder = os.path.dirname(repo.abspath(repo.tldr_rel(d, True)))
            if not os.path.isdir(folder):
                os.makedirs(folder)
                created += 1
    if created:
        print(f"created  {created} mirror folder(s)")
    if not changes and not created:
        print("nothing to move, remove or create")
    return 0


def cmd_stamp(args):
    repo = Repo(*locate(args))
    targets = sorted({repo.resolve_target(arg) for arg in args.paths},
                     key=lambda t: (t[1], -depth(t[0]), t[0]))  # files, then deepest folders
    repo.hash_all(sorted({f for rel, is_dir in targets
                          for f in (repo.files_under(rel) if is_dir else [rel])}))
    failed = False
    for rel, is_dir in targets:
        label = display(rel, is_dir)
        tldr_rel = repo.tldr_rel(rel, is_dir)
        text = read_text(repo.abspath(tldr_rel))
        if text is None:
            print(f"refused  {label}\n  - write its body first, in {tldr_rel}")
            failed = True
            continue
        body = parse_tldr(text).body
        if is_dir:
            problems = validate_dir_body(body)
            pending = [display(r, d) for _, r, d in repo.children(rel) if repo.status(r, d)[0]]
            if pending:
                problems.append("stamp its children first: " + ", ".join(pending))
            content = render_dir_tldr(rel, repo.expected_children(rel), body)
        else:
            problems = validate_file_body(body, repo.is_text(rel))
            content = render_file_tldr(rel, repo.digest(rel), body)
        if problems:
            print(f"refused  {label}")
            print("\n".join(f"  - {p}" for p in problems))
            failed = True
            continue
        write_text(repo.abspath(tldr_rel), content)
        repo._dir_status.clear()  # parents now see the new child hash
        print(f"stamped  {label}")
    return 1 if failed else 0


def cmd_show(args):
    repo = Repo(*locate(args))
    rel, is_dir = repo.resolve_target(args.path)
    text = read_text(repo.abspath(repo.tldr_rel(rel, is_dir)))
    if text is None:
        raise TldrError(f"no summary yet for {display(rel, is_dir)}: run tldr check")
    repo.hash_all(repo.files_under(rel) if is_dir else [rel])
    status, _ = repo.status(rel, is_dir)
    if status:
        print(f"tldr: warning: the summary of {display(rel, is_dir)} is {status}; "
              "run tldr check", file=sys.stderr)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_calls(args, forward):
    graph = Graph(*locate(args))
    targets = graph.lookup(args.symbol)
    for i, target in enumerate(targets):
        if len(targets) > 1:
            print(("\n" if i else "") + f"{graph.label(target)}  ({target[1]}):")
        rows = call_tree(graph, target, max(1, args.depth), forward)
        if rows:
            print_rows(rows)
        else:
            print(f"{graph.label(target)} has no known {'callees' if forward else 'callers'}")
    return 0


def cmd_impact(args):
    root, config = locate(args)
    graph = Graph(root, config)
    path = os.path.abspath(args.target)
    if os.path.isfile(path):
        file = posix_rel(path, root)
        if file not in graph.file_nodes:
            raise TldrError(f"no summary for {file}: run tldr check")
        starts = set(graph.file_nodes[file])
        for node in graph.file_nodes[file]:
            if not graph.nodes[node][1]:  # a declaration: its definitions count too
                starts.update(n for n in graph.by_name[node[0]] if graph.nodes[n][1])
    else:
        starts = set(graph.lookup(args.target))

    reached, queue = set(), deque(starts)
    while queue:
        for caller, _ in graph.inc.get(queue.popleft(), {}):
            if caller not in reached and caller not in starts:
                reached.add(caller)
                queue.append(caller)

    files = {node[1] for node in reached}
    print(f"impact of {args.target}: {len(reached)} function(s) in {len(files)} file(s)")
    tests = Matcher(config.tests)
    groups = {}
    for node in sorted(reached, key=lambda n: (n[1], n[0])):
        folder = "tests to rerun:" if tests.covers(node[1]) else display(node[1].rpartition("/")[0], True)
        groups.setdefault(folder, []).append(node)
    sections = sorted(groups, key=lambda g: (g == "tests to rerun:", g))
    if "tests to rerun:" not in groups:
        sections.append("tests to rerun:")
    for section in sections:
        print(f"\n{section}")
        if section not in groups:
            print("  none found among the callers")
            continue
        print_rows([(1, graph.label(n), n[1], graph.nodes[n][0]) for n in groups[section]])
    return 0


def cmd_path(args):
    graph = Graph(*locate(args))
    starts = graph.lookup(args.source)
    goals = set(graph.lookup(args.target))
    dist = {s: 0 for s in starts}
    parents = {s: [] for s in starts}
    queue = deque(starts)
    while queue:
        node = queue.popleft()
        for target, _, _ in graph.out.get(node, {}):
            if target is None:
                continue
            if target not in dist:
                dist[target] = dist[node] + 1
                parents[target] = [node]
                queue.append(target)
            elif dist[target] == dist[node] + 1 and node not in parents[target]:
                parents[target].append(node)

    reached = sorted(g for g in goals if dist.get(g, 0) > 0)
    if not reached:
        print(f"no call path from {args.source} to {args.target}")
        return 1
    best = min(dist[g] for g in reached)
    paths = []

    def back(node, tail):
        if len(paths) >= MAX_PATHS:
            return
        if not parents[node]:
            paths.append([node] + tail)
            return
        for parent in parents[node]:
            back(parent, [node] + tail)

    for goal in reached:
        if dist[goal] == best:
            back(goal, [])
    for i, path in enumerate(paths):
        if i:
            print()
        print_rows([(0, ("-> " if k else "") + graph.label(n), n[1], graph.nodes[n][0])
                    for k, n in enumerate(path)])
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")  # file names need not be UTF-8
    parser = argparse.ArgumentParser(
        prog="tldr",
        description="Keep the tldr/ summaries of a repository fresh, and query their call graph.")
    parser.add_argument("--root", help="repository root (default: the git top level, "
                                       "else the current folder)")
    parser.add_argument("-V", "--version", action="version", version=GENERATOR)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="list missing, stale and orphan summaries, and dangling "
                                 "call edges; exit 1 if there are any")
    p = sub.add_parser("sync", help="move the summaries of renamed files, delete orphans, "
                                    "create mirror folders")
    p.add_argument("-n", "--dry-run", action="store_true", help="only show what would change")
    p = sub.add_parser("stamp", help="validate summary bodies and write their header")
    p.add_argument("paths", nargs="+", help="source files or folders (or their .tldr paths)")
    p = sub.add_parser("show", help="print a summary")
    p.add_argument("path", nargs="?", default=".", help="file or folder (default: the root)")
    for name in ("callers", "callees"):
        p = sub.add_parser(name, help=f"{name} of a function, with their summaries")
        p.add_argument("symbol")
        p.add_argument("-d", "--depth", type=int, default=1, help="levels to follow (default 1)")
    p = sub.add_parser("impact", help="transitive callers of a function or file, and the "
                                      "tests that reach it")
    p.add_argument("target", help="a symbol or a source file")
    p = sub.add_parser("path", help="shortest call paths from one function to another")
    p.add_argument("source")
    p.add_argument("target")

    args = parser.parse_args(argv)
    commands = {
        "check": cmd_check,
        "sync": cmd_sync,
        "stamp": cmd_stamp,
        "show": cmd_show,
        "callers": lambda a: cmd_calls(a, forward=False),
        "callees": lambda a: cmd_calls(a, forward=True),
        "impact": cmd_impact,
        "path": cmd_path,
    }
    try:
        return commands[args.command](args)
    except TldrError as e:
        print(f"tldr: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head & co
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
