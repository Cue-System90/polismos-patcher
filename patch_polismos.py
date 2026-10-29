#!/usr/bin/env python3
"""Polismos-Patcher: größere UI-Skalierung und Depth of Field aus.

    patch_polismos.py                       Spielordner in den Steam-Bibliotheken suchen,
                                            Stufe 3 -> 1.60, Stufe 4 -> 2.00, DoF aus
    patch_polismos.py --game-dir PFAD
    patch_polismos.py --scale3 1.5 --scale4 1.8
    patch_polismos.py --keep-dof            nur UI-Skalierung
    patch_polismos.py --no-ui               nur Depth of Field
    patch_polismos.py --restore             Originaldateien zurückspielen

Beim ersten Lauf werden die Originale als *.orig neben die Spieldateien gelegt.
Jeder weitere Lauf patcht wieder von diesen Originalen aus, ist also beliebig
wiederholbar. Ein Steam-Update oder eine Integritätsprüfung überschreibt die
Patches; danach einfach erneut ausführen.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uiscale_tool import Assembly  # noqa: E402

APP_ID = "2745040"
DLL_REL = os.path.join("Polismos_Data", "Managed", "Assembly-CSharp.dll")
ASSETS_REL = os.path.join("Polismos_Data", "sharedassets1.assets")
GGM_REL = os.path.join("Polismos_Data", "globalgamemanagers.assets")
# Methoden, die die Regler-Stufe auf CanvasScaler.scaleFactor abbilden: das
# Einstellungsmenü und der Ladebildschirm halten je eine Kopie der Tabelle.
UI_METHODS = ("SettingsScript::apply", "LoadSceneScript::Start")
ORIG_TABLE = [0.67, 1.0, 1.1, 1.2, 1.3]  # Stufe 0..4 im Original
ORIG_SLOTS = {3: 1.2, 4: 1.3}            # die Stufen, die der Patcher ändert
ORIG_LABELS = {3: "1.20", 4: "1.30"}     # Beschriftung am Regler
DOF_ACTIVE_OFF, DOF_MODE_OFF = 44, 52    # Offsets im serialisierten DepthOfField-Objekt


# ---- Hilfen -----------------------------------------------------------------
def steam_libraries() -> list[str]:
    roots = ("~/.steam/steam", "~/.local/share/Steam",
             "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")
    cands: list[str] = []
    for root in roots:
        vdf = os.path.expanduser(os.path.join(root, "steamapps", "libraryfolders.vdf"))
        if os.path.isfile(vdf):
            cands.append(os.path.expanduser(root))
            txt = open(vdf, encoding="utf-8", errors="replace").read()
            cands += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', txt)]
    out: list[str] = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def find_game_dir() -> str | None:
    for lib in steam_libraries():
        d = os.path.join(lib, "steamapps", "common", "Polismos")
        if os.path.isfile(os.path.join(d, DLL_REL)):
            return d
    return None


def ensure_backup(path: str) -> str:
    orig = path + ".orig"
    if not os.path.exists(orig):
        shutil.copy2(path, orig)
        print(f"  Backup angelegt: {orig}")
    return orig


def atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, os.stat(path).st_mode)
    os.replace(tmp, path)


def label_for(scale: float) -> str:
    s = f"{scale:.2f}"
    if len(s) != 4:
        raise SystemExit(f"Faktor {scale:g} ergibt die Beschriftung '{s}'; erlaubt sind 4 Zeichen (0.50 … 9.99)")
    return s


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


# ---- UI-Skalierung ------------------------------------------------------------
def patch_ui(game: str, scales: dict[int, float], dry: bool) -> None:
    dll = os.path.join(game, DLL_REL)
    asm = Assembly(ensure_backup(dll))
    for name in UI_METHODS:
        m = asm.find_method(name)
        table = [v for _, v in asm.scaler_block(m)]
        if table != [f32(v) for v in ORIG_TABLE]:
            raise SystemExit(f"{m}: unerwartete Wertetabelle {table}; andere Spielversion?")
        for slot, new in scales.items():
            sites = asm.replace_float(m, ORIG_SLOTS[slot], new, near_scaler=True)
            print(f"  {m}: Stufe {slot}  {ORIG_SLOTS[slot]:.2f} -> {new:.2f}   (Offset {sites[0]:#x})")
    asm.replace_labels({ORIG_LABELS[s]: label_for(v) for s, v in scales.items()})
    if not dry:
        asm.save(dll)


def read_slots(game: str) -> dict[str, list[float]]:
    """Wertetabelle (Stufe 0..4) je Methode, wie sie jetzt in der DLL steht."""
    asm = Assembly(os.path.join(game, DLL_REL))
    return {name: [v for _, v in asm.scaler_block(asm.find_method(name))] for name in UI_METHODS}


# ---- Depth of Field -------------------------------------------------------------
def dof_object(env, assets_basename: str):
    """Findet die DepthOfField-Komponente über den m_Script-Zeiger auf das MonoScript."""
    scripts = {}
    for obj in env.objects:
        if obj.type.name == "MonoScript":
            d = obj.read()
            scripts[(os.path.basename(obj.assets_file.name), obj.path_id)] = d.m_ClassName
    hits = []
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour" or os.path.basename(obj.assets_file.name) != assets_basename:
            continue
        raw = obj.get_raw_data()
        if len(raw) < DOF_MODE_OFF + 4:
            continue
        fid, pid = struct.unpack_from("<iq", raw, 16)          # m_Script PPtr
        fname = assets_basename if fid == 0 else os.path.basename(obj.assets_file.externals[fid - 1].path)
        if scripts.get((fname, pid)) == "DepthOfField" and raw[28:44] == struct.pack("<i", 12) + b"DepthOfField":
            hits.append((obj, raw))
    if len(hits) != 1:
        raise SystemExit(f"Erwartet genau eine DepthOfField-Komponente, gefunden: {len(hits)}")
    return hits[0]


def patch_dof(game: str, dry: bool) -> None:
    import UnityPy
    assets = os.path.join(game, ASSETS_REL)
    orig = ensure_backup(assets)
    env = UnityPy.load(os.path.join(game, GGM_REL), orig)
    obj, raw = dof_object(env, os.path.basename(orig))
    active, override, mode = struct.unpack_from("<iii", raw, DOF_ACTIVE_OFF)
    if override != 1 or active not in (0, 1):
        raise SystemExit(f"Unerwartetes DepthOfField-Layout: {raw[DOF_ACTIVE_OFF:DOF_MODE_OFF + 4].hex()}")
    data = bytearray(open(orig, "rb").read())
    start = obj.byte_start
    if data[start:start + obj.byte_size] != raw:
        raise SystemExit("Objektposition passt nicht zur Datei")
    data[start + DOF_ACTIVE_OFF] = 0   # VolumeComponent.active = false
    data[start + DOF_MODE_OFF] = 0     # mode = DepthOfFieldMode.Off
    print(f"  DepthOfField (Objekt {obj.path_id}): active {active} -> 0, mode {mode} -> 0   (Offset {start:#x})")
    if not dry:
        atomic_write(assets, bytes(data))


def read_dof(game: str) -> tuple[int, int]:
    import UnityPy
    env = UnityPy.load(os.path.join(game, GGM_REL), os.path.join(game, ASSETS_REL))
    _, raw = dof_object(env, os.path.basename(ASSETS_REL))
    active, _, mode = struct.unpack_from("<iii", raw, DOF_ACTIVE_OFF)
    return active, mode


# ---- Ablauf ------------------------------------------------------------------------
def restore(game: str) -> None:
    for rel in (DLL_REL, ASSETS_REL):
        live = os.path.join(game, rel)
        orig = live + ".orig"
        if os.path.exists(orig):
            shutil.copy2(orig, live)
            print(f"  wiederhergestellt: {live}")
        else:
            print(f"  kein Backup vorhanden: {orig}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game-dir", help="Polismos-Ordner (Standard: Steam-Bibliotheken durchsuchen)")
    ap.add_argument("--scale3", type=float, default=1.6, help="Faktor für Regler-Stufe 3, Original 1.20 (Standard 1.60)")
    ap.add_argument("--scale4", type=float, default=2.0, help="Faktor für Regler-Stufe 4, Original 1.30 (Standard 2.00)")
    ap.add_argument("--keep-dof", action="store_true", help="Depth of Field eingeschaltet lassen")
    ap.add_argument("--no-ui", action="store_true", help="UI-Skalierung nicht anfassen")
    ap.add_argument("--restore", action="store_true", help="Originaldateien zurückspielen")
    ap.add_argument("--dry-run", action="store_true", help="nur prüfen, nichts schreiben")
    args = ap.parse_args()

    game = args.game_dir or find_game_dir()
    if not game or not os.path.isfile(os.path.join(game, DLL_REL)):
        raise SystemExit("Spielordner nicht gefunden, bitte --game-dir angeben")
    print(f"Spielordner: {game}")

    if args.restore:
        restore(game)
        return
    if not args.no_ui:
        print("UI-Skalierung:")
        patch_ui(game, {3: args.scale3, 4: args.scale4}, args.dry_run)
    if not args.keep_dof:
        print("Depth of Field:")
        patch_dof(game, args.dry_run)
    if args.dry_run:
        print("Probelauf, nichts geschrieben.")
        return

    print("Prüfung:")
    for name, table in read_slots(game).items():
        print(f"  {name}: Stufen 0..4 = " + "  ".join(f"{v:.2f}" for v in table))
    if not args.keep_dof:
        active, mode = read_dof(game)
        print(f"  DepthOfField: active={active} mode={mode} ({'aus' if active == 0 and mode == 0 else 'AN'})")
    print("Hinweis: Die gewählte Stufe (uiScale, 0..4) steht in den PlayerPrefs des Spiels:")
    print(f"  Proton:  <Bibliothek>/steamapps/compatdata/{APP_ID}/pfx/user.reg  [Software\\\\Composition GS\\\\Polismos]")
    print("  Windows: HKEY_CURRENT_USER\\Software\\Composition GS\\Polismos")


if __name__ == "__main__":
    main()
