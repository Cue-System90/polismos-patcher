#!/usr/bin/env python3
"""uiscale_tool – Gleitkomma-Konstanten in Unity-Mono-Assemblies finden und patchen.

Viele Unity-Spiele setzen ihre UI-Skalierung über CanvasScaler.scaleFactor aus
einer Handvoll fester Werte. Dieses Werkzeug findet die Methoden, die den
CanvasScaler ansteuern, listet deren Konstanten (ldc.r4) auf und ersetzt sie
gezielt. Beschriftungen im Menü (UTF-16-Strings gleicher Länge) lassen sich
mit umbenennen.

    uiscale_tool.py scan  Assembly-CSharp.dll
    uiscale_tool.py scan  Assembly-CSharp.dll --method SettingsScript::apply
    uiscale_tool.py patch Assembly-CSharp.dll --method SettingsScript::apply --near-scaler \\
        --replace 1.2=1.6 --replace 1.3=2.0 --label 1.20=1.60 --label 1.30=2.00

Benötigt dnfile und dncil (pip install -r requirements.txt). Funktioniert nur
bei Mono-Builds (Ordner Managed/ mit Assembly-CSharp.dll), nicht bei IL2CPP.
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import argparse_de
except ImportError:  # Skript wurde einzeln kopiert
    argparse_de = None

import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.cil.error import MethodBodyFormatError
from dncil.clr.token import StringToken, Token

CANVAS_SCALER = "CanvasScaler"
SCALER_SETTERS = (
    "set_scaleFactor",
    "set_referenceResolution",
    "set_matchWidthOrHeight",
    "set_dynamicPixelsPerUnit",
    "set_referencePixelsPerUnit",
)


def f32(x: float) -> float:
    """Rundet wie der Compiler auf einfache Genauigkeit (ldc.r4)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


class _FileOffsetReader(CilMethodBodyReaderBase):
    """Liest Methodenkörper so, dass Instruktions-Offsets Dateioffsets sind."""

    def __init__(self, pe, rva):
        self.pe = pe
        self.offset = pe.get_offset_from_rva(rva)

    def read(self, n):
        data = self.pe.get_data(self.pe.get_rva_from_offset(self.offset), n)
        self.offset += n
        return data

    def tell(self):
        return self.offset

    def seek(self, offset):
        self.offset = offset
        return offset


@dataclass
class Insn:
    offset: int        # Dateioffset des Opcodes
    opcode: str
    operand: object
    text: str          # aufgelöster Operand (Methode, Feld, String …)

    def local_index(self) -> int | None:
        """Nummer der lokalen Variable bei ldloc*/stloc*, sonst None."""
        base, _, suffix = self.opcode.partition(".")
        if base not in ("ldloc", "stloc"):
            return None
        if suffix.isdigit():
            return int(suffix)
        return int(self.operand) if self.operand is not None else None


@dataclass
class Method:
    rid: int
    name: str          # "Typ::Methode"
    row: object

    def __str__(self):
        return self.name


class Assembly:
    def __init__(self, path: str):
        self.path = path
        self.data = bytearray(open(path, "rb").read())
        self.pe = dnfile.dnPE(data=bytes(self.data))
        self._methods: list[Method] | None = None

    # ---- Metadaten -----------------------------------------------------
    def methods(self) -> list[Method]:
        if self._methods is None:
            md = self.pe.net.mdtables.MethodDef
            td = self.pe.net.mdtables.TypeDef
            starts = []
            for t in td.rows:
                try:
                    starts.append(t.MethodList[0].row_index if t.MethodList else None)
                except Exception:
                    starts.append(None)
            owner = {}
            n = len(md.rows)
            for i, t in enumerate(td.rows):
                s = starts[i]
                if s is None:
                    continue
                e = next((x for x in starts[i + 1:] if x is not None), n + 1)
                for r in range(s, e):
                    owner[r] = t.TypeName
            self._methods = [
                Method(i, f"{owner.get(i, '?')}::{row.Name}", row)
                for i, row in enumerate(md.rows, start=1)
            ]
        return self._methods

    def find_method(self, name: str) -> Method:
        exact = [m for m in self.methods() if m.name == name]
        if len(exact) == 1:
            return exact[0]
        loose = [m for m in self.methods() if m.name.endswith("::" + name) or m.name.endswith(name)]
        if len(loose) == 1:
            return loose[0]
        if not loose:
            raise LookupError(f"Methode nicht gefunden: {name}")
        raise LookupError("Mehrdeutig: " + ", ".join(m.name for m in loose))

    def _resolve(self, tok: Token) -> str:
        if isinstance(tok, StringToken):
            try:
                us = self.pe.net.user_strings.get(tok.rid)
                return f'"{us.value}"' if us else "<str?>"
            except Exception:
                return "<str?>"
        table = self.pe.net.mdtables.tables.get(tok.table)
        if table is None:
            return f"<{tok.value:08x}>"
        try:
            row = table.rows[tok.rid - 1]
        except IndexError:
            return f"<{tok.value:08x}>"
        cls = type(row).__name__
        try:
            if cls == "MethodDefRow":
                return str(row.Name)
            if cls == "MemberRefRow":
                parent = row.Class.row if row.Class else None
                pn = getattr(parent, "TypeName", None) or getattr(parent, "Name", None) or "?"
                return f"{pn}::{row.Name}"
            if cls == "TypeRefRow":
                return f"{row.TypeNamespace}.{row.TypeName}"
            if cls == "TypeDefRow":
                return str(row.TypeName)
            if cls == "FieldRow":
                return str(row.Name)
            if cls == "MethodSpecRow":
                return f"{getattr(row.Method.row, 'Name', '?')}<>"
        except Exception:
            pass
        return cls

    def disasm(self, m: Method) -> list[Insn]:
        if not m.row.Rva:
            return []
        try:
            body = CilMethodBody(_FileOffsetReader(self.pe, m.row.Rva))
        except MethodBodyFormatError:
            return []
        out = []
        for ins in body.instructions:
            op = ins.operand
            text = self._resolve(op) if isinstance(op, Token) else ("" if op is None else repr(op))
            out.append(Insn(ins.offset, ins.opcode.name, op, text))
        return out

    def float_consts(self, m: Method) -> list[tuple[int, float]]:
        """(Dateioffset des Werts, Wert) aller ldc.r4 in der Methode."""
        return [(i.offset + 1, float(i.operand)) for i in self.disasm(m) if i.opcode == "ldc.r4"]

    def scaler_calls(self, m: Method) -> list[Insn]:
        return [
            i for i in self.disasm(m)
            if i.opcode in ("call", "callvirt")
            and i.text.startswith(CANVAS_SCALER + "::")
            and i.text.split("::", 1)[1] in SCALER_SETTERS
        ]

    def scaler_block(self, m: Method) -> list[tuple[int, float]]:
        """Die Konstanten, die tatsächlich im CanvasScaler landen.

        Steht direkt vor dem Setter ein ldc.r4, ist es dieser Wert. Steht dort
        ein ldloc, werden alle ldc.r4 gesammelt, die per stloc in dieselbe
        Variable geschrieben werden: das ist die Wertetabelle, aus der das Spiel
        per Regler-Stufe auswählt. Andere Konstanten der Methode bleiben außen vor.
        """
        ins = self.disasm(m)
        pos = {i.offset: k for k, i in enumerate(ins)}
        out: list[tuple[int, float]] = []
        for c in self.scaler_calls(m):
            k = pos[c.offset]
            if k == 0:
                continue
            prev = ins[k - 1]
            if prev.opcode == "ldc.r4":
                out.append((prev.offset + 1, float(prev.operand)))
            elif prev.local_index() is not None:
                n = prev.local_index()
                for a, b in zip(ins, ins[1:]):
                    if a.opcode == "ldc.r4" and b.opcode.startswith("stloc") and b.local_index() == n:
                        out.append((a.offset + 1, float(a.operand)))
        seen: set[int] = set()
        return [x for x in out if not (x[0] in seen or seen.add(x[0]))]

    # ---- Patchen -------------------------------------------------------
    def replace_float(self, m: Method, old: float, new: float,
                      allow_multiple: bool = False, near_scaler: bool = False) -> list[int]:
        consts = self.scaler_block(m) if near_scaler else self.float_consts(m)
        sites = [off for off, v in consts if v == f32(old)]
        if not sites:
            where = " in der Wertetabelle des CanvasScalers" if near_scaler else ""
            raise ValueError(f"{m}: keine ldc.r4-Konstante {old:g}{where}")
        if len(sites) > 1 and not allow_multiple:
            where = ", ".join(hex(s) for s in sites)
            hint = "--all-occurrences" if near_scaler else "--near-scaler oder --all-occurrences"
            raise ValueError(f"{m}: Konstante {old:g} kommt {len(sites)}x vor ({where}); {hint} setzen")
        for off in sites:
            struct.pack_into("<f", self.data, off, new)
        return sites

    def label_offset(self, text: str) -> int:
        needle = text.encode("utf-16-le")
        n = self.data.count(needle)
        if n != 1:
            raise ValueError(f'Beschriftung "{text}" kommt {n}x vor, muss genau 1x vorkommen')
        return self.data.find(needle)

    def replace_labels(self, mapping: dict[str, str]) -> list[int]:
        """Benennt UTF-16-Strings gleicher Länge um. Erst alle Offsets bestimmen,
        dann schreiben, damit sich Umbenennungen nicht gegenseitig stören."""
        plan = []
        for old, new in mapping.items():
            if len(old) != len(new):
                raise ValueError(f'"{old}" -> "{new}": Beschriftungen müssen gleich lang sein')
            plan.append((self.label_offset(old), new.encode("utf-16-le")))
        for off, enc in plan:
            self.data[off:off + len(enc)] = enc
        return [off for off, _ in plan]

    def save(self, out: str | None = None) -> str:
        out = out or self.path
        tmp = out + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self.data)
        if os.path.exists(out):
            os.chmod(tmp, os.stat(out).st_mode)
        os.replace(tmp, out)
        return out


# ---- Kommandozeile -------------------------------------------------------
def cmd_scan(args):
    asm = Assembly(args.dll)
    found = 0
    for m in asm.methods():
        if args.method and not (m.name == args.method or m.name.endswith(args.method)):
            continue
        calls = asm.scaler_calls(m)
        if not calls and not args.method:
            continue
        block = {off for off, _ in asm.scaler_block(m)}
        found += 1
        print(f"== {m.name}")
        for c in calls:
            print(f"   {c.offset:#08x}   {c.opcode:8s} {c.text}")
        for off, v in asm.float_consts(m):
            mark = "*" if off in block else " "
            print(f"   {off:#08x} {mark} ldc.r4   {v:g}")
    if found:
        print("* = Wert, der in den CanvasScaler geschrieben wird (Wertetabelle der Skalierung)")
    else:
        print("Keine Methode gefunden, die den CanvasScaler ansteuert.")
        print("Mögliche Gründe: IL2CPP-Build, Skalierung nur in Szenen-Assets, UI Toolkit statt uGUI.")


def cmd_patch(args):
    asm = Assembly(args.dll)
    m = asm.find_method(args.method)
    for spec in args.replace or []:
        old, new = (float(x) for x in spec.split("=", 1))
        sites = asm.replace_float(m, old, new, args.all_occurrences, args.near_scaler)
        print(f"{m}: {old:g} -> {new:g} @ " + ", ".join(hex(s) for s in sites))
    if args.label:
        mapping = dict(spec.split("=", 1) for spec in args.label)
        for (old, new), off in zip(mapping.items(), asm.replace_labels(mapping)):
            print(f'Beschriftung "{old}" -> "{new}" @ {off:#x}')
    out = args.out or args.dll
    if not args.out and not args.no_backup:
        orig = args.dll + ".orig"
        if not os.path.exists(orig):
            shutil.copy2(args.dll, orig)
            print(f"Backup: {orig}")
    asm.save(out)
    print(f"Geschrieben: {out}")


def main():
    if argparse_de:
        argparse_de.install()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, title="Befehle", metavar="BEFEHL")
    s = sub.add_parser("scan", help="Methoden mit CanvasScaler-Zugriff und ihre Konstanten auflisten")
    s.add_argument("dll")
    s.add_argument("--method", help="nur diese Methode zeigen (Typ::Name oder Name)")
    s.set_defaults(func=cmd_scan)
    p = sub.add_parser("patch", help="Konstanten einer Methode ersetzen")
    p.add_argument("dll")
    p.add_argument("--method", required=True, help="Typ::Name der Methode")
    p.add_argument("--replace", action="append", metavar="ALT=NEU", help="ldc.r4-Konstante ersetzen (mehrfach möglich)")
    p.add_argument("--label", action="append", metavar="ALT=NEU", help="UTF-16-Beschriftung gleicher Länge umbenennen")
    p.add_argument("--near-scaler", action="store_true", help="nur die Wertetabelle des CanvasScalers betrachten (* im scan)")
    p.add_argument("--all-occurrences", action="store_true", help="Konstante auch ersetzen, wenn sie mehrfach vorkommt")
    p.add_argument("--out", help="Ergebnis in diese Datei schreiben statt die DLL zu überschreiben")
    p.add_argument("--no-backup", action="store_true", help="kein *.orig anlegen")
    p.set_defaults(func=cmd_patch)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
