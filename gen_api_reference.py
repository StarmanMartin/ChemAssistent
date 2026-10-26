"""Build CHEMOTION_API.md for the AI agent from the chemotionapi docs.

The Sphinx docs of the chemotionapi repository (tag v312.0.1) are mostly
autodoc directives, so this script walks docs/index.rst in toctree order,
keeps the prose and code blocks, and replaces every `autoclass`,
`automodule` and `autoenum` directive with the signatures and docstrings of
the installed chemotion_api package (the same thing Sphinx would render).

Usage: python gen_api_reference.py <chemotionapi checkout> <output.md>
"""
from __future__ import annotations

import enum
import importlib
import inspect
import re
import sys
from pathlib import Path


DIRECTIVE = re.compile(r"^\.\.\s+(autoclass|automodule|autoenum)::\s*(\S+)\s*$")
CODE_BLOCK = re.compile(r"^\.\.\s+code-block::\s*(\S*)\s*$")
OTHER_DIRECTIVE = re.compile(r"^\.\.\s+[\w-]+::")
ROLE = re.compile(r":(?:meth|mod|class|func|attr|ref|warning):`~?([^`]+)`")
HEADING_CHARS = {"=": "##", "*": "###", "-": "###", "_": "####"}
# Sections about installing the package or running the docker CLI: the agent
# only uses the Python API against an existing server.
SKIP_SECTIONS = ("Installation", "Instance CLI")


def resolve(dotted: str):
    """Import `pkg.module.Name` (or a module) by trying the longest module prefix."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for name in parts[cut:]:
            obj = getattr(obj, name)
        return obj
    raise ImportError(dotted)


def signature(obj) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "(...)"


def doc(obj) -> str:
    text = inspect.getdoc(obj) or ""
    return ROLE.sub(r"`\1`", text).strip()


def indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def class_members(cls, inherited: bool) -> list[tuple[str, object, str]]:
    """Public methods and properties as (name, object, kind), in source order."""
    owners = [klass for klass in cls.__mro__ if klass is not object] if inherited else [cls]
    seen: set[str] = set()
    members = []
    for owner in owners:
        if not owner.__module__.startswith("chemotion_api"):
            continue
        for name, value in vars(owner).items():
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            if isinstance(value, property):
                members.append((name, value, "property"))
            elif isinstance(value, (staticmethod, classmethod)):
                members.append((name, value.__func__, "method"))
            elif inspect.isfunction(value):
                members.append((name, value, "method"))
    return members


def render_class(cls, inherited: bool) -> str:
    out = [f"#### class `{cls.__module__}.{cls.__qualname__}{signature(cls)}`"]
    bases = [base.__name__ for base in cls.__bases__ if base is not object]
    if bases:
        out.append(f"Bases: {', '.join(bases)}")
    if doc(cls):
        out.append(doc(cls))
    for name, member, kind in class_members(cls, inherited):
        if kind == "property":
            head = f"- property `{name}`"
            text = doc(member)
        else:
            head = f"- `{name}{signature(member)}`"
            text = doc(member)
        out.append(head + ("\n" + indent(text, "    ") if text else ""))
    return "\n\n".join(out)


def render_enum(cls) -> str:
    out = [f"#### enum `{cls.__module__}.{cls.__qualname__}`"]
    if doc(cls) and doc(cls) != doc(enum.Enum):
        out.append(doc(cls))
    out.append("\n".join(f"- `{member.name}` = `{member.value!r}`" for member in cls))
    return "\n\n".join(out)


def render_module(module) -> str:
    out = [f"#### module `{module.__name__}`"]
    if doc(module):
        out.append(doc(module))
    for name, value in vars(module).items():
        if name.startswith("_") or getattr(value, "__module__", None) != module.__name__:
            continue
        if inspect.isclass(value):
            out.append(render_enum(value) if issubclass(value, enum.Enum) else render_class(value, False))
        elif inspect.isfunction(value):
            out.append(f"- `{name}{signature(value)}`\n" + indent(doc(value), "    "))
    return "\n\n".join(out)


def render_directive(kind: str, target: str, options: list[str]) -> str:
    try:
        obj = resolve(target)
    except (ImportError, AttributeError) as exc:
        return f"_(could not document {target}: {exc})_"
    if kind == "automodule":
        return render_module(obj)
    if kind == "autoenum" or (inspect.isclass(obj) and issubclass(obj, enum.Enum)):
        return render_enum(obj)
    return render_class(obj, ":inherited-members:" in options)


def convert_rst(path: Path) -> str:
    """A light reST -> Markdown conversion that expands the autodoc directives."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    skip_level = None
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        # Heading: text underlined with a repeated punctuation character.
        if line.strip() and nxt and len(set(nxt.strip())) == 1 and nxt.strip()[0] in HEADING_CHARS \
                and len(nxt.strip()) >= 3:
            level = HEADING_CHARS[nxt.strip()[0]]
            i += 2
            if skip_level is not None and len(level) > len(skip_level):
                continue
            skip_level = level if any(word in line for word in SKIP_SECTIONS) else None
            if skip_level is None:
                out.append(f"{level} {line.strip()}")
            continue
        if skip_level is not None:
            i += 1
            continue
        match = DIRECTIVE.match(line)
        if match:
            i += 1
            options = []
            while i < len(lines) and lines[i].strip().startswith(":"):
                options.append(lines[i].strip())
                i += 1
            out.append(render_directive(match.group(1), match.group(2), options))
            continue
        match = CODE_BLOCK.match(line)
        if match:
            i += 1
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                block.append(lines[i])
                i += 1
            code = inspect.cleandoc("\n".join(block))
            out.append(f"```{match.group(1)}\n{code}\n```")
            continue
        if OTHER_DIRECTIVE.match(line):
            # raw html, toctree, role definitions: skip the directive body.
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                i += 1
            continue
        out.append(ROLE.sub(r"`\1`", line))
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def toctree(index: Path) -> list[str]:
    names, inside = [], False
    for line in index.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(".. toctree::"):
            inside = True
            continue
        if inside:
            if line.strip().startswith(":") or not line.strip():
                if names and not line.strip():
                    break
                continue
            if not line.startswith((" ", "\t")):
                break
            names.append(line.strip())
    return names


def build(repo: Path) -> str:
    docs = repo / "docs"
    parts = [
        "# chemotion_api reference (tag v312.0.1)",
        "Generated from the docs of https://github.com/Chemotion/chemotionapi (tag v312.0.1) "
        "and the docstrings of the installed package. The full source is in ./chemotionapi/.",
    ]
    for name in toctree(docs / "index.rst"):
        rst = docs / f"{name}.rst"
        if rst.is_file():
            parts.append(convert_rst(rst))
    return "\n\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    Path(argv[2]).write_text(build(Path(argv[1])), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
