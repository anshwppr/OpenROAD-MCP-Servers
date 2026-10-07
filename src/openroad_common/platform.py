"""Technology platforms: everything technology-specific the stage servers need.

The canonical keys are OpenROAD's ``.vars`` variable names (``site``, ``tapcell_args``,
``wire_rc_layer``, ``cts_buffer``, ...), so any OpenROAD/ORFS-style platform maps 1:1.
A platform is merged from, in order (later wins):

1. ``GENERIC_DEFAULTS`` - no technology content at all;
2. an OpenROAD ``.vars`` file, evaluated by a safe Tcl interpreter in the openroad session;
3. a JSON file - standalone (every key, for any PDK) or extending a ``.vars`` file.

Sources: built-in presets (``platforms/*.json`` next to this module), every ``*/*.vars``
under ``$OPENROAD_PLATFORMS_DIR`` and user JSON files in ``$OPENROAD_USER_PLATFORMS_DIR``.
No tool code may hard-code technology names; it asks the platform instead.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from openroad_common.tcl import tcl_quote, to_wsl_path

BUILTIN_DIR = Path(__file__).parent / "platforms"

GENERIC_DEFAULTS: dict[str, Any] = {
    "power_net": "VDD",
    "ground_net": "VSS",
    "global_place_density": 0.6,
    "global_place_pad": 0.0,
    "detail_place_pad": 0.0,
    "tie_separation": 5.0,
    "macro_extension": 2.0,  # global-routing gcells around macros (flow.tcl)
}

FILE_KEYS = {"tech_lef", "std_cell_lef", "liberty_file", "pdn_cfg", "tracks_file", "layer_rc_file", "rcx_rules_file",
             "fill_rules"}
FILE_LIST_KEYS = {"extra_lef", "extra_liberty"}
LIST_KEYS = {"dont_use", "macro_place_halo"}
FLOAT_KEYS = {"global_place_density", "global_place_density_penalty", "global_place_pad", "detail_place_pad",
              "tie_separation", "cts_cluster_diameter", "supply_voltage", "macro_extension"}
# Tcl bookkeeping variables a .vars evaluation also leaves behind.
IGNORED_KEYS = {"platform"}


def platforms_root() -> str:
    """WSL directory scanned for ``*/*.vars`` platform files."""
    return os.environ.get("OPENROAD_PLATFORMS_DIR", "/home/ansh/OpenROAD/test")


def user_platforms_dir() -> Path:
    return Path(os.environ.get("OPENROAD_USER_PLATFORMS_DIR", Path.home() / ".openroad_mcp" / "platforms"))


def default_platform_name() -> str:
    """``OPENROAD_PLATFORM``, else the preset/user JSON marked ``"default": true``."""
    if os.environ.get("OPENROAD_PLATFORM"):
        return os.environ["OPENROAD_PLATFORM"]
    for _kind, path in _json_files():
        data = _load_json(path)
        if data.get("default"):
            return str(data.get("name", path.stem)).lower()
    raise ToolError("No platform given: pass platform=..., set OPENROAD_PLATFORM, or mark a platform JSON "
                    'with "default": true.')


# ---------------------------------------------------------------------------
# Tcl value parsing and normalization
# ---------------------------------------------------------------------------


def tcl_split(text: str) -> list[str]:
    """Split a Tcl list into its elements (braces, quotes and backslashes honoured)."""
    items: list[str] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text[i] == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if text[j] == "\\":
                    j += 2
                    continue
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            if depth:
                raise ValueError(f"unbalanced braces in Tcl list: {text!r}")
            items.append(text[i + 1 : j - 1])
            i = j
        elif text[i] == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            items.append("".join(buf))
            i = j + 1
        else:
            j, buf = i, []
            while j < n and not text[j].isspace():
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            items.append("".join(buf))
            i = j
    return items


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def layer_adjustments(value: Any) -> list[list[Any]]:
    """Normalize every ``.vars`` spelling to ``[[layers, adjustment], ...]``.

    ``{{layerA-layerB} 0.5}``, ``{layerA 0.4} {layerB 0.4}`` and ``{{{layerA-layerB} 0.25}}`` all work.
    """
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[0], str) and _is_number(str(value[1])) and not _is_number(value[0]):
            return [[value[0], float(value[1])]]
        return [pair for item in value for pair in layer_adjustments(item)]
    text = str(value).strip()
    items = tcl_split(text)
    if len(items) == 2 and _is_number(items[1]) and not _is_number(items[0]):
        return [[items[0].strip(), float(items[1])]]
    if len(items) == 1:
        # One element: either an extra level of braces to peel off, or a lone word (not a pair).
        return [] if items[0].strip() == text else layer_adjustments(items[0])
    return [pair for item in items for pair in layer_adjustments(item)]


def parse_tapcell_args(args: str | None) -> dict[str, Any]:
    tokens = tcl_split(args or "")
    out: dict[str, Any] = {}
    for flag, key in (("-distance", "distance"), ("-tapcell_master", "tapcell_master"),
                      ("-endcap_master", "endcap_master")):
        if flag in tokens and tokens.index(flag) + 1 < len(tokens):
            value = tokens[tokens.index(flag) + 1]
            out[key] = float(value) if key == "distance" and _is_number(value) else value
    return out


def _to_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return tcl_split(str(value))


def resolve_path(path: str, base_dir: str | None) -> str:
    """Absolute WSL path for a platform file (relative paths are joined to ``base_dir``)."""
    p = str(path).strip()
    if not p:
        return p
    if p.startswith("/") or p.startswith("~") or re.match(r"^[A-Za-z]:[\\/]", p) or p.startswith("\\\\"):
        return to_wsl_path(p)
    return posixpath.normpath(posixpath.join(base_dir or ".", p.replace("\\", "/")))


def normalize(raw: dict[str, Any], base_dir: str | None) -> dict[str, Any]:
    """Convert raw (Tcl string or JSON) values into typed values with absolute file paths."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in IGNORED_KEYS:
            continue
        if key in FILE_KEYS:
            out[key] = resolve_path(value, base_dir) if value not in (None, "") else None
        elif key in FILE_LIST_KEYS:
            out[key] = [resolve_path(v, base_dir) for v in _to_list(value)]
        elif key == "liberty_files":
            if isinstance(value, dict):
                pairs = list(value.items())
            else:
                items = _to_list(value)
                pairs = list(zip(items[0::2], items[1::2]))
            out[key] = {corner: resolve_path(path, base_dir) for corner, path in pairs}
        elif key in LIST_KEYS:
            out[key] = _to_list(value)
        elif key in FLOAT_KEYS:
            out[key] = float(value) if value not in (None, "") and _is_number(str(value)) else None
        elif key == "global_routing_layer_adjustments":
            out[key] = layer_adjustments(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------


@dataclass
class Platform:
    name: str
    source: str
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.values.get(key)
        return default if value in (None, "", []) else value

    def require(self, key: str, hint: str = "") -> Any:
        value = self.get(key)
        if value is None:
            raise ToolError(
                f"Platform '{self.name}' does not define '{key}'."
                + (f" {hint}" if hint else f" Pass it as an argument, or add '{key}' to the platform.")
            )
        return value

    def lef_files(self) -> list[str]:
        files = [self.get("tech_lef"), self.get("std_cell_lef"), *self.get("extra_lef", [])]
        return [f for f in files if f]

    def liberty_files(self, corner: str | None = None) -> list[str]:
        if corner:
            corners = self.get("liberty_files", {})
            if corner not in corners:
                raise ToolError(
                    f"Platform '{self.name}' has no liberty corner '{corner}'. Available: {sorted(corners) or 'none'}."
                )
            return [corners[corner], *self.get("extra_liberty", [])]
        files = [self.get("liberty_file"), *self.get("extra_liberty", [])]
        return [f for f in files if f]

    def tapcell(self) -> dict[str, Any]:
        return parse_tapcell_args(self.get("tapcell_args"))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "values": self.values}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Platform:
        return cls(name=data["name"], source=data.get("source", "checkpoint"), values=dict(data.get("values", {})))

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, **self.values, "tapcell": self.tapcell()}


def build_platform(name: str, source: str, layers: list[tuple[dict[str, Any], str | None]]) -> Platform:
    """Merge ``(raw values, base_dir)`` layers over the generic defaults."""
    values = dict(GENERIC_DEFAULTS)
    for raw, base_dir in layers:
        values.update(normalize(raw, base_dir))
    return Platform(name=name, source=source, values=values)


def vars_base_dir(vars_path: str) -> str:
    """OpenROAD's ``.vars`` paths are relative to the directory above the platform directory."""
    return posixpath.dirname(posixpath.dirname(vars_path))


def vars_platform_name(vars_path: str) -> str:
    return posixpath.splitext(posixpath.basename(vars_path))[0].lower()


# ---------------------------------------------------------------------------
# Tcl side (lives in the stage driver)
# ---------------------------------------------------------------------------

PLATFORM_DRIVER_TCL = r"""
# Evaluate an OpenROAD .vars file in a safe interpreter and emit its scalar variables.
# Commands other than plain Tcl (e.g. suppress_message) are ignored via a no-op unknown.
proc __plat_vars {path} {
  set f [open $path]
  set text [read $f]
  close $f
  set i [interp create -safe]
  $i eval {proc unknown args {}}
  catch {$i eval $text}
  set skip {errorCode errorInfo tcl_interactive tcl_patchLevel tcl_version tcl_platform auto_path auto_index env argv argv0 argc}
  foreach v [lsort [$i eval {info vars}]] {
    if {$v in $skip} { continue }
    if {[$i eval [list array exists $v]]} { continue }
    __mcp_rec var $v [$i eval [list set $v]]
  }
  interp delete $i
}

proc __plat_discover {root} {
  foreach f [lsort [glob -nocomplain -directory $root -- */*.vars]] {
    __mcp_rec vars $f
  }
}

proc __plat_read_text {path} {
  set f [open $path]
  set text [read $f]
  close $f
  __mcp_rec text $text
}
"""


async def _run_records(session, script: str) -> list[list[str]]:
    from openroad_common.records import query  # local import: records imports session

    records, _ = await query(session, script, require_design=False)
    return records


async def read_vars(session, vars_path: str) -> dict[str, str]:
    records = await _run_records(session, f"__plat_vars {tcl_quote(vars_path)}")
    return {r[1]: (r[2] if len(r) > 2 else "") for r in records if r and r[0] == "var"}


async def discover_vars(session) -> list[str]:
    records = await _run_records(session, f"__plat_discover {tcl_quote(platforms_root())}")
    return [r[1] for r in records if r and r[0] == "vars"]


def _json_files() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for kind, folder in (("builtin", BUILTIN_DIR), ("user", user_platforms_dir())):
        if folder.is_dir():
            found += [(kind, p) for p in sorted(folder.glob("*.json"))]
    return found


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"Cannot read platform JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"Platform JSON {path} must contain an object.")
    return data


async def list_platforms(session) -> list[dict[str, Any]]:
    """Every platform the servers can use: JSON presets/user files first, then discovered ``.vars``."""
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    for kind, path in _json_files():
        data = _load_json(path)
        name = str(data.get("name", path.stem)).lower()
        if name not in names:
            names.add(name)
            out.append({"name": name, "kind": kind, "source": str(path), "description": data.get("description", "")})
    for vars_path in await discover_vars(session):
        name = vars_platform_name(vars_path)
        if name not in names:
            names.add(name)
            out.append({"name": name, "kind": "vars", "source": vars_path, "description": "OpenROAD .vars file"})
    return out


async def _platform_from_json(session, data: dict[str, Any], source: str, json_dir: str | None) -> Platform:
    name = str(data.get("name") or posixpath.splitext(posixpath.basename(source))[0]).lower()
    layers: list[tuple[dict[str, Any], str | None]] = []
    vars_ref = data.get("vars")
    if vars_ref:
        vars_path = resolve_path(vars_ref, platforms_root())
        layers.append((await read_vars(session, vars_path), data.get("base_dir") or vars_base_dir(vars_path)))
    overrides = dict(data.get("overrides", {}))
    meta = {"name", "description", "vars", "base_dir", "overrides", "default"}
    overrides.update({k: v for k, v in data.items() if k not in meta})
    base_dir = resolve_path(data["base_dir"], None) if data.get("base_dir") else json_dir
    layers.append((overrides, base_dir))
    return build_platform(name, source, layers)


async def resolve_platform(session, name_or_path: str | None = None) -> Platform:
    """Resolve a platform by name (preset, user JSON or discovered .vars) or by file path."""
    target = (name_or_path or default_platform_name()).strip()
    lowered = target.lower()

    if lowered.endswith(".vars"):
        vars_path = to_wsl_path(target)
        return build_platform(vars_platform_name(vars_path), vars_path,
                              [(await read_vars(session, vars_path), vars_base_dir(vars_path))])
    if lowered.endswith(".json"):
        local = Path(target)
        if local.exists():
            return await _platform_from_json(session, _load_json(local), str(local), to_wsl_path(str(local.parent)))
        wsl = to_wsl_path(target)
        records = await _run_records(session, f"__plat_read_text {tcl_quote(wsl)}")
        text = next((r[1] for r in records if r and r[0] == "text"), "")
        return await _platform_from_json(session, json.loads(text), wsl, posixpath.dirname(wsl))

    for _kind, path in _json_files():
        data = _load_json(path)
        if str(data.get("name", path.stem)).lower() == lowered:
            return await _platform_from_json(session, data, str(path), to_wsl_path(str(path.parent)))
    for vars_path in await discover_vars(session):
        if vars_platform_name(vars_path) == lowered:
            return build_platform(lowered, vars_path, [(await read_vars(session, vars_path), vars_base_dir(vars_path))])
    known = ", ".join(p["name"] for p in await list_platforms(session)) or "none"
    raise ToolError(f"Unknown platform '{target}'. Known platforms: {known}. "
                    "Pass a .vars or .json path, or add a JSON file to the user platforms directory.")
