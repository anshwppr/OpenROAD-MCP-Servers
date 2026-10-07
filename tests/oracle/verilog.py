"""Parser for the flat structural Verilog used by the contest netlists.

Handles what the input netlists and OpenROAD's write_verilog produce: one module, port /
input / output / wire declarations with optional [msb:lsb] ranges, cell instances with named
pin connections (single- or multi-line), 1'b0 / 1'b1 constants, escaped identifiers and
simple ``assign a = b;`` aliases. Independent of OpenROAD and of the server code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CONST0 = "1'b0"
CONST1 = "1'b1"
CONSTANTS = (CONST0, CONST1)

_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_ESCAPED_RE = re.compile(r"\\(\S+)\s")
_DECL_RE = re.compile(r"^(input|output|inout|wire)\s*(\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s*(.*)$", re.DOTALL)
_INST_RE = re.compile(r"^([A-Za-z_][\w$]*)\s+([^\s(]+)\s*\((.*)\)$", re.DOTALL)
_CONN_RE = re.compile(r"\.\s*([\w$]+)\s*\(\s*([^()]*?)\s*\)")
_CONST_RE = re.compile(r"^1'[bB]([01])$")


@dataclass
class Instance:
    name: str
    cell: str
    pins: dict[str, str | None]  # pin -> net bit name, CONST0/CONST1, or None (unconnected)


@dataclass
class Netlist:
    module: str
    port_order: list[str] = field(default_factory=list)
    # bit name -> "input" / "output"
    port_bits: dict[str, str] = field(default_factory=dict)
    # port base name -> (direction, [bit names msb..lsb] or [name] for scalars)
    ports: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    wires: set[str] = field(default_factory=set)
    instances: dict[str, Instance] = field(default_factory=dict)
    assigns: list[tuple[str, str]] = field(default_factory=list)


def _bits(name: str, rng: tuple[int, int] | None) -> list[str]:
    if rng is None:
        return [name]
    msb, lsb = rng
    step = -1 if msb >= lsb else 1
    return [f"{name}[{i}]" for i in range(msb, lsb + step, step)]


def _expr(text: str, const_nets: dict[str, str]) -> str | None:
    text = text.strip()
    if not text:
        return None
    m = _CONST_RE.match(text)
    if m:
        return CONST1 if m.group(1) == "1" else CONST0
    text = re.sub(r"\s+", "", text)
    return const_nets.get(text, text)


def parse_text(text: str, undeclared_const_nets: bool = False) -> Netlist:
    """Parse one module. With ``undeclared_const_nets``, undeclared nets named one_ / zero_
    (how OpenROAD writes constants) are read as 1'b1 / 1'b0."""
    text = _COMMENT_RE.sub(" ", text)
    text = _ESCAPED_RE.sub(lambda m: m.group(1) + " ", text)
    start = re.search(r"\bmodule\s+([\w$]+)\s*(\((.*?)\))?\s*;", text, re.DOTALL)
    if not start:
        raise ValueError("no module found")
    nl = Netlist(module=start.group(1))
    if start.group(3):
        nl.port_order = [p.strip() for p in start.group(3).split(",") if p.strip()]
    body = text[start.end() : text.find("endmodule", start.end())]
    statements = [s.strip() for s in body.split(";") if s.strip()]

    # Declarations first, so constants and widths are known before instances.
    instances: list[str] = []
    for stmt in statements:
        m = _DECL_RE.match(stmt)
        if m:
            kind, rng = m.group(1), (int(m.group(3)), int(m.group(4))) if m.group(2) else None
            for name in (n.strip() for n in m.group(5).split(",")):
                if not name:
                    continue
                bits = _bits(name, rng)
                if kind == "wire":
                    nl.wires.update(bits)
                else:
                    direction = "input" if kind == "input" else "output"
                    nl.ports[name] = (direction, bits)
                    for b in bits:
                        nl.port_bits[b] = direction
        elif stmt.startswith("assign"):
            lhs, _, rhs = stmt[len("assign") :].partition("=")
            nl.assigns.append((lhs.strip(), rhs.strip()))
        else:
            instances.append(stmt)

    declared = nl.wires | set(nl.port_bits)
    const_nets: dict[str, str] = {}
    if undeclared_const_nets:
        for name, value in (("one_", CONST1), ("zero_", CONST0)):
            if name not in declared:
                const_nets[name] = value

    for stmt in instances:
        m = _INST_RE.match(stmt)
        if not m:
            raise ValueError(f"cannot parse statement: {stmt[:80]!r}")
        cell, name, conns = m.groups()
        pins = {p: _expr(e, const_nets) for p, e in _CONN_RE.findall(conns)}
        if name in nl.instances:
            raise ValueError(f"duplicate instance {name}")
        nl.instances[name] = Instance(name, cell, pins)
    nl.assigns = [(_expr(a, const_nets) or a, _expr(b, const_nets) or b) for a, b in nl.assigns]
    return nl


def parse_file(path: str | Path, undeclared_const_nets: bool = False) -> Netlist:
    return parse_text(Path(path).read_text(encoding="utf-8"), undeclared_const_nets)
