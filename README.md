# Polismos — UI-Skalierung bis 2.00 und Depth of Field aus

Ein Patcher für **Polismos** (Steam, Unity 2021.3, Mono). Das Spiel erlaubt am Regler
höchstens eine UI-Skalierung von 1.30, und die Tiefenunschärfe lässt sich im Menü nicht
abschalten. Beide Grenzen sind fest einprogrammiert; dieser Patcher hebt sie auf.

## Was er tut

| Patch | Wirkung |
|---|---|
| **UI-Skalierung** | Die obersten beiden Regler-Stufen liefern 1.60 und 2.00 statt 1.20 und 1.30. Die Beschriftung am Regler wird mit angepasst. Faktoren sind per Option frei wählbar. |
| **Depth of Field** | Die DepthOfField-Komponente im URP-Volume-Profil wird deaktiviert (`active = false`, Modus `Off`). Das Bild bleibt in jeder Zoomstufe scharf. |

Beide Patches sind einzeln abschaltbar, `--restore` holt die Originale zurück.

Regler-Stufen nach dem Patch (Standardwerte):

| Stufe | Original | gepatcht |
|---|---|---|
| 0 | 0.67 | 0.67 |
| 1 | 1.00 | 1.00 |
| 2 | 1.10 | 1.10 |
| 3 | 1.20 | **1.60** |
| 4 | 1.30 | **2.00** |

## Benutzung

Voraussetzung ist Python 3.10 oder neuer. Das Spiel darf während des Patchens nicht laufen.

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python patch_polismos.py
```

Ohne Angaben sucht der Patcher Polismos in den Steam-Bibliotheken aus `libraryfolders.vdf`.
Weitere Möglichkeiten:

```bash
venv/bin/python patch_polismos.py --game-dir "/pfad/zu/steamapps/common/Polismos"
venv/bin/python patch_polismos.py --scale3 1.5 --scale4 1.8   # eigene Faktoren
venv/bin/python patch_polismos.py --keep-dof                  # nur UI-Skalierung
venv/bin/python patch_polismos.py --no-ui                     # nur Depth of Field
venv/bin/python patch_polismos.py --dry-run                   # nur prüfen
venv/bin/python patch_polismos.py --restore                   # Originale zurück
```

Beim ersten Lauf werden `Assembly-CSharp.dll.orig` und `sharedassets1.assets.orig` neben die
Spieldateien gelegt. Jeder weitere Lauf geht von diesen Originalen aus und ist daher beliebig
wiederholbar. Ein Steam-Update oder „Spieldateien überprüfen“ setzt die Patches zurück, danach
einfach erneut ausführen.

Die gewählte Stufe speichert das Spiel als Zahl 0 bis 4 unter dem Schlüssel `uiScale` in den
PlayerPrefs: unter Proton in `steamapps/compatdata/2745040/pfx/user.reg`, Abschnitt
`[Software\Composition GS\Polismos]`, unter Windows in `HKEY_CURRENT_USER\Software\Composition GS\Polismos`.
Wer bereits auf Stufe 4 steht, bekommt nach dem Patch sofort 2.00; eine Stufe tiefer ist 1.60.

## Wie der Patch gefunden wurde

- `SettingsScript::apply` in `Assembly-CSharp.dll` bildet die Regler-Stufe über eine Kette
  von Vergleichen auf feste `ldc.r4`-Konstanten ab und schreibt den Wert in vier
  `CanvasScaler.scaleFactor`. Der Ladebildschirm (`LoadSceneScript::Start`) hält eine zweite
  Kopie derselben Tabelle. Der Patcher sucht beide Methoden über die Metadaten, nimmt nur die
  Konstanten, die in die an den `CanvasScaler` übergebene Variable fließen (in `apply` steht
  eine weitere 1.3 für die Kamerageschwindigkeit), und ersetzt 1.2 und 1.3 an Ort und Stelle.
  Die Menü-Beschriftungen „1.20“ und „1.30“ sind UTF-16-Strings gleicher Länge im `#US`-Heap.
- Depth of Field hängt an keinem Setting. `CameraScript` holt sich die Komponente mit
  `volume.profile.TryGet<DepthOfField>()` aus dem Profil „SampleSceneProfile“ in
  `sharedassets1.assets` und überschreibt nur die Fokusdistanz. Der Patcher findet das
  Objekt über seinen `m_Script`-Zeiger auf das MonoScript `DepthOfField` und setzt in den
  serialisierten Daten `active` und `mode` auf 0.

Dieses Repository enthält keine Spieldateien. Die Patches werden auf die eigene Installation
angewendet.

## Allgemeiner Ansatz für andere Unity-Spiele

`uiscale_tool.py` ist der spielunabhängige Teil: Es listet alle Methoden einer
Mono-Assembly auf, die den `CanvasScaler` ansteuern, zeigt deren Gleitkomma-Konstanten,
markiert mit `*` die Werte, die tatsächlich im `CanvasScaler` landen, und ersetzt sie auf
Wunsch samt Beschriftung.

```bash
venv/bin/python uiscale_tool.py scan  Assembly-CSharp.dll
venv/bin/python uiscale_tool.py patch Assembly-CSharp.dll --method SettingsScript::apply --near-scaler \
    --replace 1.2=1.6 --replace 1.3=2.0 --label 1.20=1.60 --label 1.30=2.00
```

Wo die Grenzen dieses Ansatzes liegen und wie ein wirklich allgemeiner UI-Scale-Patch für
Unity-Spiele aussehen könnte, steht in [docs/unity-ui-scale-allgemein.md](docs/unity-ui-scale-allgemein.md).
